"""audit_import — 旧审核库(walmart_audit)→ 中心库 audit schema 一次性搬迁(批次 A)。

用法:
  python cli.py audit_import                    # dry-run:逐表体检报告,不写一行
  python cli.py audit_import --execute          # 真搬(整轮单事务,失败全回滚)
  python cli.py audit_import -p table=audit_runs --execute   # 只搬指定表
  python cli.py audit_import -p replace=yes --execute        # 目标非空时清掉重灌

dry-run 报告逐表给出:源行数 / 目标行数 / 列对照(源独有列=致命——照搬会丢数据;
目标独有列=吃默认值,警告;同名列类型不一致=致命)。三张"反推表"
(pt_meta/pt_spec/prohibited_policy)旧仓无 DDL,本仓 DDL 是按 sync 脚本推定的,
**必须先看 dry-run 的列对照,全绿才 --execute**;有致命项时 --execute 拒绝执行。

防重:目标表非空默认跳过该表;-p replace=yes 才 TRUNCATE 重灌(audit schema
是我们的副本,清掉无损旧库;audit_runs 与 audit_hits 有外键,成对处理,
TRUNCATE runs 用 CASCADE 并在报告中注明)。标识列(run_id/hit_id/id)按原值
搬入,导入后 setval 续接自增。每表拷贝后源/目标行数必须相等,不等即抛错
(整轮事务回滚)。旧库全程只读连接(registry.db.legacy_audit_conn)。

不搬清单(docstring 即契约):products(catalog.products 取代)、
llm_cache(catalog.llm_cache 已有)、sync_runs(ops.runs 取代)、
llm_usage / llm_route_events(批次 C 可选重建)。
"""

import logging
import re

from registry import db

DANGEROUS = True

logger = logging.getLogger("workflows.audit_import")

# 搬迁清单(顺序即契约:audit_runs 必须先于 audit_hits——外键)
TABLES = (
    "blacklist_brands",
    "walmart_category_map",
    "phase0_blacklist_sellers",
    "phase0_blacklist_asins",
    "phase0_blacklist_amazon_cats",
    "blacklist_brand_ip_stats",
    "violation_groundtruth",
    "walmart_error_records",
    "walmart_pt_meta",
    "walmart_pt_spec",
    "walmart_prohibited_policy",
    "audit_runs",
    "audit_hits",
)

# 外键成对:选了其中一个就必须两个都处理(runs 在前)
_FK_PAIR = ("audit_runs", "audit_hits")

# 带自增标识列的表:导入后 setval 续接,否则后续 INSERT 撞主键
_IDENTITY = {"audit_runs": "run_id", "audit_hits": "hit_id",
             "walmart_error_records": "id"}

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def diff_columns(src: list[tuple[str, str]],
                 tgt: list[tuple[str, str]]) -> tuple[list[str], list[str], list[str]]:
    """输入:源/目标 (列名, 类型) 列表 → 输出:(致命问题, 警告, 可拷贝列序)。

    纯函数(便于测试)。判定口径:
      源独有列   → 致命(照搬会静默丢这列数据)
      类型不一致 → 致命(COPY 文本装载可能坏值,先改 DDL 再来)
      目标独有列 → 警告(导入后吃默认值/NULL,人眼确认即可)
    可拷贝列序 = 源列顺序中双方共有的列。
    """
    src_map, tgt_map = dict(src), dict(tgt)
    fatal, warn = [], []
    for name, typ in src:
        if name not in tgt_map:
            fatal.append(f"源独有列 {name}({typ})——目标缺列,照搬丢数据")
        elif tgt_map[name] != typ:
            fatal.append(f"列 {name} 类型不一致:源 {typ} / 目标 {tgt_map[name]}")
    for name, typ in tgt:
        if name not in src_map:
            warn.append(f"目标独有列 {name}({typ})——导入后吃默认值")
    common = [n for n, _ in src if n in tgt_map and tgt_map[n] == src_map[n]]
    return fatal, warn, common


def _columns(conn, schema: str, table: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table))
        return [(r[0], r[1]) for r in cur.fetchall()]


def _count(conn, qualified: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {qualified}")
        return cur.fetchone()[0]


def _copy_table(src, tgt, table: str, cols: list[str]) -> None:
    for ident in [table, *cols]:
        if not _IDENT_RE.match(ident):
            raise ValueError(f"非法标识符:{ident}")
    col_list = ", ".join(cols)
    with src.cursor() as sc, tgt.cursor() as tc:
        with sc.copy(f"COPY (SELECT {col_list} FROM public.{table}) TO STDOUT") as out, \
             tc.copy(f"COPY audit.{table} ({col_list}) FROM STDIN") as into:
            for chunk in out:
                into.write(chunk)


def _pick_tables(params: dict) -> list[str]:
    want = (params.get("table") or "").strip()
    if not want:
        return list(TABLES)
    if want not in TABLES:
        raise ValueError(f"未知表 {want}(可选:{', '.join(TABLES)})")
    if want in _FK_PAIR:
        return list(_FK_PAIR)       # 成对,runs 在前
    return [want]


def run(params: dict) -> str:
    """输入:params(execute/table/replace)→ 输出:逐表体检或搬迁摘要。"""
    execute = bool(params.get("execute"))
    replace = str(params.get("replace", "")).lower() == "yes"
    tables = _pick_tables(params)

    lines, fatal_any = [], False
    with db.legacy_audit_conn() as src, db.pg_conn() as tgt:
        plans = []
        for t in tables:
            src_cols = _columns(src, "public", t)
            tgt_cols = _columns(tgt, "audit", t)
            if not src_cols:
                lines.append(f"✗ {t}: 旧库无此表(源列为空)——跳过")
                continue
            if not tgt_cols:
                lines.append(f"✗ {t}: 目标 audit.{t} 不存在——先跑 db_init")
                fatal_any = True
                continue
            fatal, warn, common = diff_columns(src_cols, tgt_cols)
            n_src = _count(src, f"public.{t}")
            n_tgt = _count(tgt, f"audit.{t}")
            mark = "✗" if fatal else "✓"
            note = []
            if fatal:
                fatal_any = True
                note += [f"致命:{x}" for x in fatal]
            note += [f"警告:{x}" for x in warn]
            if n_tgt and not replace:
                note.append(f"目标已有 {n_tgt} 行——将跳过(要重灌加 -p replace=yes)")
            lines.append(f"{mark} {t}: 源 {n_src} 行 / 目标 {n_tgt} 行"
                         + (";" + ";".join(note) if note else ";列全对齐"))
            plans.append((t, common, n_src, n_tgt, bool(fatal)))

        if not execute:
            head = "audit_import dry-run 体检(不写一行;全绿后 --execute):"
            return "\n".join([head, *lines])
        if fatal_any:
            raise RuntimeError("存在致命列差异,拒绝搬迁——先按 dry-run 报告修 "
                               "refdata/schema.sql 的 audit DDL:\n" + "\n".join(lines))

        done = []
        for t, common, n_src, n_tgt, _ in plans:
            if n_tgt and not replace:
                done.append(f"跳过 {t}(目标非空)")
                continue
            if n_tgt and replace:
                cascade = " CASCADE" if t == "audit_runs" else ""
                tgt.execute(f"TRUNCATE audit.{t}{cascade}")
                if cascade:
                    done.append("TRUNCATE audit_runs CASCADE(hits 一并清,随后重灌)")
            _copy_table(src, tgt, t, common)
            n_new = _count(tgt, f"audit.{t}")
            if n_new != n_src:
                raise RuntimeError(f"{t} 行数不符:源 {n_src} / 导入后 {n_new},整轮回滚")
            if t in _IDENTITY:
                col = _IDENTITY[t]
                tgt.execute(
                    f"SELECT setval(pg_get_serial_sequence('audit.{t}', '{col}'), "
                    f"coalesce((SELECT max({col}) FROM audit.{t}), 0) + 1, false)")
            done.append(f"{t}: {n_src} 行 ✓")
        return "audit_import 完成(单事务):\n" + "\n".join(done)
