"""audit_import — 旧审核库(walmart_audit)→ 中心库 audit schema 一次性搬迁(批次 A)。

用法:
  python cli.py audit_import                    # dry-run:逐表体检报告,不写一行
  python cli.py audit_import --execute          # 真搬(整轮单事务,失败全回滚)
  python cli.py audit_import -p table=audit_runs --execute   # 只搬指定表(外键对成对)
  python cli.py audit_import -p replace=yes --execute        # 目标非空时清掉重灌

dry-run 报告逐表给出:源行数 / 目标行数 / 列对照 / **将要执行的动作**——
含 replace=yes 时的 TRUNCATE 预告(dry-run 必须把最危险的动作说出来,
安全铁律)。列对照走 pg_attribute + format_type(含长度/精度)口径:
  源独有列   = 致命(照搬会静默丢数据);
  类型不一致 = 致命(先改 refdata/schema.sql 再来);
  目标独有列 = 警告(导入吃默认值);
  清单里的表在旧库不存在 = 致命(清单与旧库对不上,绝不静默少搬)。
有致命项时 --execute 拒绝执行;execute 摘要含完整体检行,不吞警告。

三张"反推表"(pt_meta/pt_spec/prohibited_policy)旧仓无权威 DDL,
**生产实表(pg_dump -s walmart_audit)才是最终基准**——执行前先核对一遍,
dry-run 是最后一道网,不是设计手段(评审教训 2026-08-13)。

前置纪律:搬迁窗口内旧审核系统的写入方必须先停(黑名单双调度 + worker,
清单见 audit_migration_plan.md 批次 D)。源连接用 REPEATABLE READ 单快照
(体检/计数/COPY 读同一份数据,自我一致),但快照外的新写入不会进来,
停写才能保证"搬完即全量";行数不符时报错也会提示这个方向。

防重:目标表非空默认跳过该表;-p replace=yes(也认 true/1,其余值报错)
才 TRUNCATE 重灌(audit schema 是我们的副本,清掉无损旧库;audit_runs 用
CASCADE 连清 audit_hits 后两表都重灌)。标识列(run_id/hit_id/id)按原值
搬入,导入后 setval 续接(找不到序列即报错,不静默)。每表拷贝后源/目标
行数必须相等,不等即抛错整轮回滚。旧库全程只读连接(registry.db)。

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
_TRUTHY = {"yes", "true", "1"}


def diff_columns(src: list[tuple[str, str]],
                 tgt: list[tuple[str, str]]) -> tuple[list[str], list[str], list[str]]:
    """输入:源/目标 (列名, 完整类型) 列表 → 输出:(致命问题, 警告, 可拷贝列序)。

    纯函数(便于测试)。类型串来自 format_type,含长度/精度
    (numeric(5,2) / character(1) 等),精度不符同样判致命。
      源独有列   → 致命(照搬会静默丢这列数据)
      类型不一致 → 致命(COPY 文本装载可能坏值,先改 DDL 再来)
      目标独有列 → 警告(导入后吃默认值/NULL,人眼确认即可)
    可拷贝列序 = 源列顺序中双方同名同型的列。
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


def _exists(conn, qualified: str) -> bool:
    """to_regclass 判表存在——列查询返回空分不清"无表"与"无权限",这里分得清。"""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,))
        return bool(cur.fetchone()[0])


def _columns(conn, qualified: str) -> list[tuple[str, str]]:
    """输入:连接 + schema.表名 → 输出:[(列名, format_type 完整类型)]。

    走 pg_catalog 而非 information_schema:format_type 渲染含精度/长度
    (numeric(5,2)、character(1)),且 pg_attribute 对所有角色可读,
    不会因缺 SELECT 权限静默返回空列表。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "FROM pg_catalog.pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 "
            "  AND NOT a.attisdropped ORDER BY a.attnum",
            (qualified,))
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


def _parse_replace(params: dict) -> bool:
    raw = str(params.get("replace", "")).strip().lower()
    if not raw:
        return False
    if raw not in _TRUTHY:
        raise ValueError(f"replace={raw!r} 无法识别(认 yes/true/1)——"
                         f"宁可报错,不静默当没说")
    return True


def run(params: dict) -> str:
    """输入:params(execute/table/replace)→ 输出:逐表体检 + 搬迁摘要。"""
    execute = bool(params.get("execute"))
    replace = _parse_replace(params)
    tables = _pick_tables(params)

    lines, plans, fatal_any = [], [], False
    with db.legacy_audit_conn() as src, db.pg_conn() as tgt:
        # 单快照:体检/计数/COPY 全程读同一份数据(防旧系统并发写导致
        # "体检行数"与"实拷行数"错位;彻底的解法是先停旧写入方,见 docstring)
        try:
            from psycopg import IsolationLevel
            src.isolation_level = IsolationLevel.REPEATABLE_READ
        except ImportError:     # 测试假连接环境无 psycopg,隔离级别无语义
            pass
        for t in tables:
            q_src, q_tgt = f"public.{t}", f"audit.{t}"
            if not _exists(src, q_src):
                lines.append(f"✗ {t}: 旧库无此表——清单与旧库对不上,绝不静默少搬")
                fatal_any = True
                continue
            if not _exists(tgt, q_tgt):
                lines.append(f"✗ {t}: 目标 audit.{t} 不存在——先跑 db_init")
                fatal_any = True
                continue
            fatal, warn, common = diff_columns(
                _columns(src, q_src), _columns(tgt, q_tgt))
            n_src, n_tgt = _count(src, q_src), _count(tgt, q_tgt)
            note = []
            if fatal:
                fatal_any = True
                note += [f"致命:{x}" for x in fatal]
            note += [f"警告:{x}" for x in warn]
            if n_tgt and replace:
                casc = "(CASCADE 连清 audit_hits 后两表重灌)" \
                    if t == "audit_runs" else ""
                note.append(f"动作:TRUNCATE {n_tgt} 行{casc}后重灌 {n_src} 行")
            elif n_tgt:
                note.append(f"目标已有 {n_tgt} 行——将跳过(要重灌加 -p replace=yes)")
            else:
                note.append(f"动作:导入 {n_src} 行")
            mark = "✗" if fatal else "✓"
            lines.append(f"{mark} {t}: 源 {n_src} 行 / 目标 {n_tgt} 行;"
                         + ";".join(note))
            plans.append((t, common, n_src, n_tgt))

        if not execute:
            return "\n".join(
                ["audit_import dry-run 体检(不写一行;全绿后 --execute):", *lines])
        if fatal_any:
            raise RuntimeError("存在致命项,拒绝搬迁——先按体检报告处置"
                               "(改 refdata/schema.sql 或核对旧库):\n"
                               + "\n".join(lines))

        done = []
        for t, common, n_src, n_tgt in plans:
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
                raise RuntimeError(
                    f"{t} 行数不符:源 {n_src} / 导入后 {n_new}——源库仍在被写入?"
                    f"(前置纪律:先停旧调度与 worker)整轮回滚")
            if t in _IDENTITY:
                col = _IDENTITY[t]
                cur = tgt.execute(
                    f"SELECT setval(pg_get_serial_sequence('audit.{t}', '{col}'), "
                    f"coalesce((SELECT max({col}) FROM audit.{t}), 0) + 1, false)")
                if cur.fetchone()[0] is None:
                    raise RuntimeError(
                        f"{t}.{col} 找不到自增序列——identity 定义丢失,整轮回滚")
            done.append(f"{t}: {n_src} 行 ✓")
        # 体检行随摘要一并返回:警告(目标独有列等)不因 execute 而被吞掉
        return "\n".join(["audit_import 完成(单事务)。体检:", *lines,
                          "── 执行:", *done])
