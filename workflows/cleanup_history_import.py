"""cleanup_history_import — 旧问题商品三笔历史 → 新库(一次性)。

用法:
  python cli.py cleanup_history_import                       # 预览:体量/列探测/类别分布
  python cli.py cleanup_history_import -p seen=/path/seen_sku_categories.json \
                                       -p brand=/path/brand_cache.json
  python cli.py cleanup_history_import -p apply=1 [-p seen=… -p brand=…]   # 真正入库

三笔账(所有者定稿 2026-08-11,建模走时间线):
  ① walmart_cleanup.error_items(41.7 万行 per-run 快照)
     → catalog.product_events(event=problem_categorized,source=本工作流,
       occurred_at=旧 run_ts)。**只保留类别变化的行**(时间线拼接,见
       services/cleanup_history.timeline)——41.7 万行进来,落库的是每个
       (store, sku) 的类别变迁序列,不膨胀。
     产品库缺行的 ASIN 照样入账:product_events 不依赖 products 行,
     挂接在查询时 LEFT JOIN(要不要补 stub 产品行是另一个待定决策,不阻塞)。
  ② seen_sku_categories.json(20.1 万对)→ ops.cleanup_seen_categories。
     错误统计报表(Step 3/4/5)的累计数唯一真值,报表迁移前必须先有它,
     否则累计口径当场跳变。
  ③ brand_cache.json → processed_asins 进 ops.dedupe('cleanup:brand_asin')
     ——品牌采集(Step 6)开跑前必须先有,否则 2544 个已处理 ASIN 会被
     重新采一遍、往飞书重复追加;pending_batches 原样存档
     ('cleanup:brand_pending'),Step 6 接手时人工核一眼再销。

幂等语义(与 kpi_history_import 的 DO NOTHING 不同,这里是**擦净重灌**):
  ① 每次 apply 先删本工作流 source 的全部事件再重导——时间线折叠有跨行状态,
    断点续传要重建状态得不偿失;擦净重灌简单且结果确定。删的只是自己导入的
    行,实时链路写的 problem_categorized(source=problem_product_cleanup)
    一根手指都不碰。这是"账本只追加"的唯一例外,理由是:历史导入的产物
    不是观测,是**旧账的投影**,投影可以重投。
  ② ③ ON CONFLICT DO NOTHING,可重复执行。

数据源(全在生产 Mac,克隆里没有):
  旧库连接走 registry.db.legacy_cleanup_conn(只读事务;两库不同实例时
  LEGACY_CLEANUP_DSN 覆盖)。两个 JSON 用 -p 传绝对路径。
  列名/JSON 形态按 legacy_survey 的调查预设,**预览模式先探测再报告**——
  对不上会把真实列名打出来,先校准映射再 apply。
"""

import json
import logging
from pathlib import Path

from registry import db
from services import cleanup_history as ch
from services import product_events

DANGEROUS = False

logger = logging.getLogger("workflows.cleanup_history_import")

SOURCE = "cleanup_history_import"

# 预设的旧表列(legacy_survey C11:唯一键 (run_ts, store, sku);
# 2026-05-14 前 reason/category 为空)。探测对不上就报真实列名。
_NEED_COLS = {"run_ts", "store", "sku", "category", "reason"}

_PROBE_SQL = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'error_items'
"""

_WIPE_SQL = "DELETE FROM catalog.product_events WHERE source = %s"

_SEEN_SQL = """
INSERT INTO ops.cleanup_seen_categories (sku, category)
VALUES (%s, %s) ON CONFLICT DO NOTHING
"""

_DEDUPE_SQL = """
INSERT INTO ops.dedupe (scope, key, meta)
VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING
"""

_BATCH = 5000


def _probe(legacy) -> tuple[set, int]:
    """输入:旧库连接 → 输出:(实际列集合, 总行数)。表不存在直接抛错。"""
    with legacy.cursor() as cur:
        cur.execute(_PROBE_SQL)
        cols = {r[0] for r in cur.fetchall()}
        if not cols:
            raise RuntimeError("旧库里没有 error_items 表——确认 DSN 指向 "
                               "walmart_cleanup(LEGACY_CLEANUP_DSN 可覆盖)")
        cur.execute("SELECT count(*) FROM error_items")
        total = cur.fetchone()[0]
    return cols, total


def _import_events(legacy) -> dict:
    """输入:旧库连接 → 输出:导入统计。流式读全表(按 run_ts 升序),
    时间线折叠后分批写 product_events。"""
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_WIPE_SQL, (SOURCE,))
            wiped = cur.rowcount or 0
        # 服务端游标流式读:41.7 万行别一口气进内存
        with legacy.cursor(name="hist") as src:
            src.itersize = _BATCH
            src.execute("SELECT run_ts, store, sku, category, reason "
                        "FROM error_items ORDER BY run_ts, store, sku")
            cols = ("run_ts", "store", "sku", "category", "reason")
            folder = ch.Folder()          # 折叠逻辑只有 services 这一份
            batch: list[dict] = []
            for row in src:
                ev = folder.push(dict(zip(cols, row)))
                if ev is None:
                    continue
                ev["event"] = product_events.PROBLEM_CATEGORIZED
                ev["source"] = SOURCE
                batch.append(ev)
                if len(batch) >= _BATCH:
                    product_events.record_many(conn, batch)
                    batch = []
            if batch:
                product_events.record_many(conn, batch)
    return {**folder.stats, "wiped": wiped}


def _import_seen(pairs: list) -> str:
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_SEEN_SQL, pairs)
    return f"seen 对 {len(pairs)} 条入 ops.cleanup_seen_categories(已存在跳过)"

def _import_brand(parsed: tuple) -> str:
    processed, pending = parsed
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_DEDUPE_SQL,
                            [("cleanup:brand_asin", a, None) for a in processed])
            cur.executemany(_DEDUPE_SQL,
                            [("cleanup:brand_pending", str(i),
                              json.dumps(b, ensure_ascii=False, default=str))
                             for i, b in enumerate(pending)])
    return (f"品牌防重 {len(processed)} 个 ASIN 入 ops.dedupe,"
            f"pending 批次存档 {len(pending)} 条")


def run(params: dict) -> str:
    """输入:params(seen/brand/apply)→ 输出:导入或预览摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    seen_path = str(params.get("seen", "")).strip()
    brand_path = str(params.get("brand", "")).strip()
    lines = []

    # 文件检查放在一切重活之前(2026-08-11 实操教训:路径打错时事件导入
    # 已经跑完才炸在 JSON 上,摘要全丢,人对着 traceback 猜发生了什么)。
    # 顺带在这里就解析——形状指纹校验(seen/brand 传反)也要趁早炸。
    payloads: dict = {}
    for label, path in (("seen", seen_path), ("brand", brand_path)):
        if not path:
            continue
        f = Path(path)
        if not f.is_file():
            return (f"⛔ {label} 文件不存在:{path}"
                    f"(路径要从 /Users/… 起,别带示例里的 /path 前缀)")
        obj = json.loads(f.read_text())
        payloads[label] = (ch.parse_seen(obj) if label == "seen"
                           else ch.parse_brand_cache(obj))

    with db.legacy_cleanup_conn() as legacy:
        cols, total = _probe(legacy)
        missing = _NEED_COLS - cols
        if missing:
            # 预设列名是调查推断,生产机上第一跑就是来校准它的
            return (f"⛔ error_items 缺预期列 {sorted(missing)},"
                    f"实际列:{sorted(cols)}——先校准 _NEED_COLS/SELECT 再跑")
        lines.append(f"error_items {total} 行,列探测通过")

        if not apply:
            with legacy.cursor() as cur:
                cur.execute("SELECT coalesce(nullif(btrim(category), ''), '(空)'),"
                            " count(*) FROM error_items GROUP BY 1"
                            " ORDER BY 2 DESC LIMIT 20")
                dist = cur.fetchall()
            lines.append("类别分布(前 20):" + ", ".join(
                f"{c}×{n}" for c, n in dist))
            if "seen" in payloads:
                lines.append(f"seen 文件可解析:{len(payloads['seen'])} 对")
            if "brand" in payloads:
                lines.append(f"brand 文件可解析:{len(payloads['brand'][0])} 个 ASIN")
            lines.append("预览完毕;-p apply=1 真正入库(事件侧擦净重灌,"
                         "seen/brand 侧 DO NOTHING)")
            return ";".join(lines)

        st = _import_events(legacy)
        lines.append(f"时间线导入:读 {st['rows']} 行 → 变迁事件 {st['kept']} 条"
                     f"(擦净旧导入 {st['wiped']} 条"
                     + (f",类别未识别 {st['unknown_category']} 行(原文已随行入库)"
                        if st['unknown_category'] else "") + ")")
    if "seen" in payloads:
        lines.append(_import_seen(payloads["seen"]))
    if "brand" in payloads:
        lines.append(_import_brand(payloads["brand"]))
    if not seen_path or not brand_path:
        lines.append("⚠ seen/brand 未全给:报表累计数与品牌防重各缺一笔,"
                     "补齐路径重跑即可(DO NOTHING 幂等)")
    return ";".join(lines)
