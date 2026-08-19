"""采集记录 → 产品中心两层落库积木(增量导出契约 v1)。

一条 record = 一次采集的完整结果,拆成两层(brief 第一节):
  身份层 catalog.products   —— 慢变字段 upsert(一个 (marketplace,asin) 一行)
  观测层 catalog.snapshots  —— 快变字段 append(永不去重,source_id 幂等)

三条硬规则(契约 §5.1,违反会静默毁数据,别优化):
  1. **outcome != "ok" 只进 snapshots,绝不 upsert products**——失败/降级采集
     也是合法观测,但不能拿它刷新产品身份;
  2. **null/[] 一律是「本次没取到」,不是「该商品没有这个属性」**——软降级页
     会整块剥掉面包屑/详情表,拿空值覆盖已有值 = 静默毁档案。所有慢变字段
     一律 COALESCE(新值, 旧值);
  3. **slow_hash 是不透明值**,原样存、原样比,绝不按 slow 自行重算
     (两侧算法不同,重算必然不等)。它变化 = 审核重审信号。
"""

import json
import logging

from services import product_events

logger = logging.getLogger("services.product_ingest")

OUTCOME_OK = "ok"

_SNAPSHOT_SQL = """
INSERT INTO catalog.snapshots (
    marketplace, asin, scrape_params, price, stock_state, stock_count,
    delivery_days, shipping, shipping_raw, buybox, raw, scraped_at, source_id,
    outcome, completeness_ok)
VALUES (%(marketplace)s, %(asin)s, %(scrape_params)s::jsonb, %(price)s,
        %(stock_state)s, %(stock_count)s, %(delivery_days)s,
        %(shipping)s, %(shipping_raw)s,
        %(buybox)s::jsonb, %(raw)s::jsonb, %(scraped_at)s, %(source_id)s,
        %(outcome)s, %(completeness_ok)s)
ON CONFLICT (source_id) DO NOTHING
"""

# 慢变字段一律 COALESCE(新值, 旧值):空值绝不覆盖(规则 2)。
# slow_hash 同样只在非空时更新——它是审核重审的触发信号,不能被空值抹掉。
_PRODUCT_SQL = """
INSERT INTO catalog.products (
    marketplace, asin, title, brand, amazon_category, image_url, slow_hash,
    slow, browse_node_chain, browse_node_id, updated_at)
VALUES (%(marketplace)s, %(asin)s, %(title)s, %(brand)s, %(amazon_category)s,
        %(image_url)s, %(slow_hash)s, %(slow)s::jsonb,
        %(browse_node_chain)s, %(browse_node_id)s, now())
ON CONFLICT (marketplace, asin) DO UPDATE SET
    title = COALESCE(EXCLUDED.title, catalog.products.title),
    brand = COALESCE(EXCLUDED.brand, catalog.products.brand),
    amazon_category = COALESCE(EXCLUDED.amazon_category,
                               catalog.products.amazon_category),
    image_url = COALESCE(EXCLUDED.image_url, catalog.products.image_url),
    slow_hash = COALESCE(EXCLUDED.slow_hash, catalog.products.slow_hash),
    slow = COALESCE(EXCLUDED.slow, catalog.products.slow),
    -- 类目 ID 链(契约 v1 追加):名称会漂 ID 不会,是 L1 最精确的类目锚
    browse_node_chain = COALESCE(EXCLUDED.browse_node_chain,
                                 catalog.products.browse_node_chain),
    browse_node_id = COALESCE(EXCLUDED.browse_node_id,
                              catalog.products.browse_node_id),
    -- 审核重审触发(批次 B1,批复 #9):慢变字段真变了才把 approved 翻回
    -- pending;rejected 永不自动重审(force_rerun 手动通道在 product_audit)。
    -- EXCLUDED.slow_hash 为 NULL(本次没采到 hash)不触发——上面 COALESCE
    -- 保旧值,身份未变。不能用 updated_at 当触发(它无条件刷新)。
    audit_status = CASE
        WHEN EXCLUDED.slow_hash IS NOT NULL
             AND catalog.products.slow_hash IS DISTINCT FROM EXCLUDED.slow_hash
             AND catalog.products.audit_status = 'approved'
        THEN 'pending'
        ELSE catalog.products.audit_status END,
    updated_at = now()
RETURNING (xmax = 0) AS inserted
"""


def _blank_to_none(v):
    """输入:任意值 → 输出:空串/空列表/空字典一律 None(规则 2 的统一入口)。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (list, dict, tuple)):
        return v or None
    return v


def _category(slow: dict) -> str | None:
    """输入:slow → 输出:类目路径拼串(' > ' 连接),空列表按未取到处理。"""
    path = _blank_to_none(slow.get("category_path"))
    if not path:
        return None
    if isinstance(path, str):
        return path
    return " > ".join(str(p) for p in path if p)


_NODE_SENTINELS = {"", "n/a", "none", "null", "-"}   # 采集侧空值哨兵(root_category_id="N/A")


def category_nodes(slow: dict, raw: dict | None = None) -> tuple[str | None, str | None]:
    """输入:slow(+可选 raw)→ 输出:(browse_node 链原文, 叶子 node_id)。

    所有者定稿 2026-08-14:**类目名会漂,browse_node_id 不会**——Amazon 的
    URL slug / 面包屑 / Best Sellers 导航三套名称不一致,按路径字符串匹配
    会把同一类目误判成缺口;ID 链的**最后一段 = 当前最细类目**,拿它直查
    映射表的 browse_node_id 列即可精确命中(该表 ID 覆盖率实测 100%)。

    取值口径(采集器源码核实 2026-08-14):采集侧解析面包屑时 ID 与名字树
    **同一处产出**(worker/parser.py:2532-2545 正则 `node=(\\d+)`),落库三列
    root_category_id / category_ids / category_tree;但增量导出的 slow 段
    只挑了名字树(server/api/export_incremental.py:235)——**ID 未进 slow,
    却在 raw 里幸存**(不在 _RAW_DROP 剔除清单内)。故取值顺序:
      1. raw.category_ids —— 现役真实键名(逗号串,root→leaf 有序);
      2. slow.category_id_chain —— 若采集侧后续把它提进 slow(契约追加)。
    两处都没有 → (None, None),行为退回字符串路径匹配。

    ⚠ 已知口径(采集侧 common/slowhash.py:136-137):category_ids **被有意
    排除在 slow_hash 之外**,故"只有 ID 链变了"不会推动 slow_hash、不会触发
    重审——ID 是补锚用的,不当变更信号。
    """
    chain = None
    if raw:
        chain = _blank_to_none(raw.get("category_ids"))
    if not chain:
        chain = _blank_to_none(slow.get("category_id_chain"))
    if not chain:
        return None, None
    if isinstance(chain, (list, tuple)):
        ids = [str(x).strip() for x in chain]
    else:
        ids = [x.strip() for x in str(chain).split(",")]
    ids = [i for i in ids if i and i.lower() not in _NODE_SENTINELS]
    if not ids:
        return None, None
    return ",".join(ids), ids[-1]


def _main_image(slow: dict) -> str | None:
    """输入:slow → 输出:主图(images 首图,契约:首图=主图)。"""
    imgs = _blank_to_none(slow.get("images"))
    if not imgs:
        return None
    return str(imgs[0]) if isinstance(imgs, (list, tuple)) else str(imgs)


def _opt_int(v):
    """输入:契约里的 int|null 字段 → 输出:int 或 None。

    ⚠ **None 与 0 是两回事**(契约 3b):None = 本次没采到,0 = 采到了确实是 0。
    这里绝不把 None 折成 0——折了下游就再也分不出"缺货"和"不知道"。
    """
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        logger.warning("数值字段无法解析(按未采到处理): %r", v)
        return None


def _opt_float(v):
    """输入:契约里的 float|null 字段 → 输出:float 或 None(**0.0 不折成 None**)。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        logger.warning("数值字段无法解析(按未采到处理): %r", v)
        return None


def snapshot_params(rec: dict) -> dict:
    """输入:record → 输出:snapshots 行参数。"""
    fast = rec.get("fast") or {}
    buybox = {k: fast.get(k) for k in
              ("buybox_price", "buybox_seller", "buybox_seller_id")
              if fast.get(k) is not None}
    return {
        "marketplace": rec.get("marketplace") or "US",
        "asin": rec.get("asin"),
        "scrape_params": json.dumps(rec.get("scrape_params") or {},
                                    sort_keys=True, ensure_ascii=False),
        "price": fast.get("price"),
        "stock_state": _blank_to_none(fast.get("stock_state")),
        "stock_count": _opt_int(fast.get("stock_count")),
        "delivery_days": _opt_int(fast.get("delivery_days")),
        # 运费(采集侧 2026-08-10 纯追加,contract_version 仍是 1):
        # FREE→0.0(确认免运费)/ N/A→None(这次没采到,落地价算不出来)/ $5.99→5.99。
        # 与 stock_count 同一条不变量:**null ≠ 0,下游禁止 or 0**——把没采到
        # 当免运费,落地价照样算得出来、看着正常,只是偏小,两侧都不报错。
        # shipping_raw 原样留存:出现新形态(如满额免邮门槛)时不必等契约改版。
        "shipping": _opt_float(fast.get("shipping")),
        "shipping_raw": _blank_to_none(fast.get("shipping_raw")),
        "buybox": json.dumps(buybox, ensure_ascii=False) if buybox else None,
        "raw": json.dumps(rec.get("raw"), ensure_ascii=False)
               if rec.get("raw") is not None else None,
        "scraped_at": rec.get("scraped_at"),
        "source_id": rec.get("source_id"),
        # 采集结局随观测一起落库(2026-08-09 补):此前只在摄取时计数,
        # 事后没人答得上"这个 ASIN 为什么没有新数据"。ok 也存,不然
        # "从来没采过"和"采过但被降级"在库里长得一模一样。
        "outcome": _blank_to_none(rec.get("outcome")) or OUTCOME_OK,
        "completeness_ok": rec.get("completeness_ok"),
    }


def product_params(rec: dict) -> dict:
    """输入:record → 输出:products 行参数(慢变字段,空值已归一为 None)。"""
    slow = rec.get("slow") or {}
    # raw 也要看:类目 ID 链现役只在 raw 里(见 category_nodes 取值口径)
    chain, node_id = category_nodes(slow, rec.get("raw") or {})
    return {
        "marketplace": rec.get("marketplace") or "US",
        "asin": rec.get("asin"),
        "title": _blank_to_none(slow.get("title")),
        "brand": _blank_to_none(slow.get("brand")),
        "amazon_category": _category(slow),
        "browse_node_chain": chain,
        "browse_node_id": node_id,
        "image_url": _main_image(slow),
        "slow_hash": _blank_to_none(rec.get("slow_hash")),
        # slow 段全量留存:卖点/描述/重量/尺寸/变体都在这里,契约的 raw 已裁剪
        "slow": json.dumps(slow, ensure_ascii=False) if slow else None,
    }


def ingest_batch(conn, records: list[dict]) -> dict:
    """输入:连接 + record 列表 → 输出:计数 dict。

    计数项:snapshots 新增/重复(source_id 已在库)、products 更新、
    非 ok 跳过身份层、completeness_ok=false(只告警不拦)、缺 asin/source_id 丢弃,
    外加每种非 ok 结局一个 `outcome_<结局>` 键(摘要里给分布,库里查明细看
    snapshots.outcome)。
    """
    counts = {"snapshots": 0, "dup": 0, "products": 0, "skipped_outcome": 0,
              "incomplete": 0, "invalid": 0}
    outcomes: dict[str, int] = {}
    new_events: list[dict] = []
    with conn.cursor() as cur:
        for rec in records:
            if not rec.get("asin") or not rec.get("source_id"):
                counts["invalid"] += 1
                continue
            cur.execute(_SNAPSHOT_SQL, snapshot_params(rec))
            if cur.rowcount:
                counts["snapshots"] += 1
            else:
                counts["dup"] += 1

            outcome = str(rec.get("outcome") or OUTCOME_OK)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome != OUTCOME_OK:
                # 规则 1:失败/降级采集是合法观测,但不刷新产品身份
                counts["skipped_outcome"] += 1
                key = f"outcome_{outcome}"
                counts[key] = counts.get(key, 0) + 1
                continue
            if rec.get("completeness_ok") is False:
                counts["incomplete"] += 1   # COALESCE 已防覆盖,这里只计数
            cur.execute(_PRODUCT_SQL, product_params(rec))
            counts["products"] += 1
            # xmax=0 = 本条是全新插入(非 upsert 更新)→ 入库事件(批次 B1)
            row = cur.fetchone()
            if row and row[0]:
                counts["new_products"] = counts.get("new_products", 0) + 1
                new_events.append({
                    "sku": rec["asin"], "event": product_events.PRODUCT_INGESTED,
                    "source": "product_ingest",
                    "detail": {"source_id": rec.get("source_id"),
                               "slow_hash": rec.get("slow_hash")}})
    if new_events:
        product_events.record_many(conn, new_events)
    if counts["skipped_outcome"]:
        detail = ",".join(f"{k}:{v}" for k, v in sorted(outcomes.items())
                          if k != OUTCOME_OK)
        logger.info("非 ok 采集 %d 条只落观测层(%s)",
                    counts["skipped_outcome"], detail)
    return counts


# ══════════════════════════════════════════════════════════════════════════════
#  两台泵:全局增量泵 pump(全局游标,独占,要锁)
#        与按批泵 pump_batch(批内游标,幂等,无锁)
# ══════════════════════════════════════════════════════════════════════════════
#
# 放 services 是因为多个工作流要用而工作流之间不准互相 import(铁律 1)。
# pump 的游标推进规则(空页不推进、只认 next_cursor、409 停在原地)抄漏
# 一条就是**静默丢一段数据**,两侧都不报错——所以全项目只有这一份。
# pump_batch(2026-08-19)给 order_audit / product_audit / list_new 的
# 同轮闭环用:批内游标每次从 0 拉到底,不落库不占锁,重复摄取靠
# snapshots.source_id 幂等吸收。

CURSOR_NAME = "product_ingest"

# 「摄取跑到哪儿了」的水位线。**和游标是两件事**:游标只在有新数据时前进,
# 水位线每次跑完都刷。历史职责是给 order_audit 当"我有没有资格说'这条数据
# 没到'"的凭据(2026-08-10 的 127 条冤案:批次 completed ≠ 数据落库);
# 2026-08-19 起 order_audit 改用 pump_batch 把批次自己的事件流拉到底,
# 逐批凭据更强,水位线**不再参与认账**,保留作运维观测
# ("全局泵最后一次追平是什么时候"排障时仍要看)。
WATERMARK_NAME = "product_ingest:last_run"


def load_cursor(conn) -> int:
    """输入:连接 → 输出:当前游标(没有记录则 0)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ops.cursors WHERE name = %s",
                    (CURSOR_NAME,))
        row = cur.fetchone()
    return int((row[0] or {}).get("cursor", 0)) if row else 0


def save_cursor(conn, value: int) -> None:
    """输入:连接 + 游标 → 输出:无(写 ops.cursors)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.cursors (name, value, updated_at)"
            " VALUES (%s, jsonb_build_object('cursor', %s::bigint), now())"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = now()", (CURSOR_NAME, value))
    conn.commit()


def pump(scraper, db, *, limit: int = 500, max_pages=None, cursor=None) -> dict:
    """输入:api.scraper + registry.db(+分页参数)→ 输出:本轮摄取结果 dict。

    返回 {ok, cursor_from, cursor_to, pages, fetched, totals, error}:
    - `ok=False` 时 `error` 是给人看的一句话,**游标一定没推进**;
    - `totals` 是 ingest_batch 计数的累加(含动态的 outcome_* 键)。

    游标纪律(契约边界语义,三条都别改):
    - 只认响应返回的 `next_cursor`,不自己算 max(seq) 或 cursor+count;
    - **空页不推进**——这是唯一不丢数据的方向;
    - 409 保留期缺口:停在原地并要求人工全量对账后 `-p cursor=N` 重置,
      **绝不当普通错误重试**(那会一路跳过被裁掉的区间,静默丢数据)。

    ⚠ 调用方必须先拿到 `product_ingest` 的 flock(见 services/runlock):
    两个进程同时推游标,后写的会盖掉先写的,中间那段记录永远不会再被拉一次。
    """
    with db.pg_conn() as conn:
        cur_pos = load_cursor(conn) if cursor is None else int(cursor)
    start = cur_pos
    totals: dict = {"snapshots": 0, "dup": 0, "products": 0,
                    "skipped_outcome": 0, "incomplete": 0, "invalid": 0}
    pages = fetched = 0

    while True:
        try:
            records, next_cursor, has_more = scraper.export_incremental(
                cur_pos, limit)
        except scraper.RetentionGapError as e:
            return {"ok": False, "cursor_from": start, "cursor_to": cur_pos,
                    "pages": pages, "fetched": fetched, "totals": totals,
                    "error": f"保留期缺口,已停止(游标仍为 {cur_pos},未推进):"
                             f"{e};需全量对账后 -p cursor=<新起点> 重启"}
        except scraper.ExportAuthError as e:
            return {"ok": False, "cursor_from": start, "cursor_to": cur_pos,
                    "pages": pages, "fetched": fetched, "totals": totals,
                    "error": f"导出鉴权失败(游标未推进):{e}"}

        pages += 1
        fetched += len(records)
        if records:
            with db.pg_conn() as conn:
                counts = ingest_batch(conn, records)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v     # outcome_* 键是动态的

        if next_cursor != cur_pos:
            with db.pg_conn() as conn:
                save_cursor(conn, next_cursor)
            cur_pos = next_cursor
        if not has_more or not records:
            break
        if max_pages and pages >= max_pages:
            logger.info("达到 max_pages=%d,本轮提前收尾(游标已落 %d)",
                        max_pages, cur_pos)
            break

    # 只有**跑完整轮**才刷水位线:中途出错(上面两个 return)不刷,
    # 否则下游会以为"摄取已追上"而去认账失败——那正是水位线要防的事。
    with db.pg_conn() as conn:
        touch_watermark(conn, cur_pos)
    return {"ok": True, "cursor_from": start, "cursor_to": cur_pos,
            "pages": pages, "fetched": fetched, "totals": totals, "error": None}


def touch_watermark(conn, cursor_value: int) -> None:
    """输入:连接 + 当前游标 → 输出:无(刷 ops.cursors 的摄取水位线)。

    **每轮跑完都刷,哪怕 0 条新数据**——这正是它和游标的区别:
    "这一轮我确实把增量流拉到底了" 与 "游标动没动" 是两个事实,
    下游要的是前者。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.cursors (name, value, updated_at)"
            " VALUES (%s, jsonb_build_object('cursor', %s::bigint), now())"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = now()", (WATERMARK_NAME, cursor_value))
    conn.commit()


def pump_batch(scraper, db, batch_name: str, *, limit: int = 500,
               max_pages: int = 400) -> dict:
    """输入:api.scraper + registry.db + 批次名 → 输出:该批摄取结果 dict。

    与 `pump`(全局增量泵)的分工(2026-08-19 所有者批准接入采集侧批次端点):
      pump        全局游标,独占推进,**必须先拿 product_ingest 锁**,
                  是产品中心的兜底全量补给线(product_chain 每天跑);
      pump_batch  批内游标,**每次调用都从 0 拉到底**,不落 ops.cursors、
                  不碰水位线、**不需要任何锁**——批内游标互不相干,
                  幂等靠 snapshots.source_id 的 ON CONFLICT DO NOTHING
                  (与全局泵重复摄取同一条记录也只是 dup 计数 +1)。
                  给 order_audit / product_audit / list_new 的同轮闭环用:
                  「我刚推的这一批,到底采到了什么」。

    返回 {ok, batch, pages, fetched, totals, coverage, gone, error}:
    - `ok=True` **当且仅当拉到底**(has_more=False)——调用方(尤其 order_audit
      的认账失败)拿它当「这批的事件流我全看过了」的凭据,半途而废不算 ok;
    - `gone=True` 表示批次在采集侧已不存在(404),数据要不回来了,
      调用方按"交给兜底超时收尾"处置;
    - 采集侧故障(鉴权/重试耗尽/端点未部署)一律 ok=False + error,
      **不抛**——同轮闭环是 best-effort,采集侧挂了不该拖垮调用链,
      但每一次失败都要出现在调用方摘要里(静默跳过 = 没人知道主路径坏了)。
    """
    totals: dict = {"snapshots": 0, "dup": 0, "products": 0,
                    "skipped_outcome": 0, "incomplete": 0, "invalid": 0}
    out = {"ok": False, "batch": batch_name, "pages": 0, "fetched": 0,
           "totals": totals, "coverage": None, "gone": False, "error": None}
    cur_pos = 0
    while True:
        try:
            records, next_cursor, has_more, meta = \
                scraper.export_batch_records(batch_name, cur_pos, limit)
        except LookupError as e:
            out.update(gone=True, error=str(e))
            return out
        except Exception as e:                                  # noqa: BLE001
            # 鉴权/参数/重试耗尽:本批这轮拉不了。告警 + 报给调用方,不抛
            logger.warning("批次 %s 摄取失败(本轮放弃,已入库 %d 条不回滚):%s",
                           batch_name, out["fetched"], e)
            out["error"] = str(e)
            return out

        out["pages"] += 1
        out["fetched"] += len(records)
        if meta.get("coverage") is not None:
            out["coverage"] = meta["coverage"]
        if records:
            with db.pg_conn() as conn:
                counts = ingest_batch(conn, records)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v     # outcome_* 键是动态的
        if not has_more:
            out["ok"] = True        # 拉到底才算 ok(空批也是 200 + has_more=False)
            return out
        if not records or next_cursor == cur_pos:
            # has_more=True 却空页/游标原地踏步:服务端异常,再拉只会死循环。
            # 不能声称 ok——调用方会拿 ok 当"这批我全看过了"去认账失败
            out["error"] = (f"批次导出异常:has_more=True 但"
                            f"{'返回空页' if not records else '游标未推进'}"
                            f"(cursor={cur_pos}),提前止损")
            logger.warning("批次 %s %s", batch_name, out["error"])
            return out
        cur_pos = next_cursor
        if out["pages"] >= max_pages:
            out["error"] = f"超过 {max_pages} 页仍未拉完(批次大得反常),提前止损"
            logger.warning("批次 %s %s", batch_name, out["error"])
            return out


def pump_batches(scraper, db, names: list[str], *, limit: int = 500,
                 max_pages: int = 400) -> tuple[list[dict], str]:
    """输入:批次名列表 → 输出:(逐批结果列表, 一行人读摘要)。

    逐批调 pump_batch 并汇总。摘要纪律:失败/批次消失必须点名(best-effort
    不等于静默),全成功给合计。max_pages 给 product_refresh 那种十几万
    ASIN 的大批放宽用(默认档按同轮闭环的小批定)。
    """
    results = [pump_batch(scraper, db, n, limit=limit, max_pages=max_pages)
               for n in names if n]
    if not results:
        return [], "按批摄取:0 批"
    t: dict = {}
    for r in results:
        for k, v in r["totals"].items():
            t[k] = t.get(k, 0) + v
    fetched = sum(r["fetched"] for r in results)
    n_ok = sum(1 for r in results if r["ok"])
    line = (f"按批摄取:{n_ok}/{len(results)} 批拉到底 / 拉取 {fetched} 条;"
            f"观测入库 {t.get('snapshots', 0)}(重复跳过 {t.get('dup', 0)}),"
            f"身份更新 {t.get('products', 0)}")
    if t.get("skipped_outcome"):
        dist = ",".join(f"{k[len('outcome_'):]}×{v}" for k, v in sorted(t.items())
                        if k.startswith("outcome_"))
        line += f",非 ok 采集只落观测 {t['skipped_outcome']}({dist})"
    bad = [r for r in results if not r["ok"]]
    if bad:
        line += ";⚠ " + ",".join(
            f"{r['batch']}:{'采集侧查不到' if r['gone'] else r['error']}"
            for r in bad)
    return results, line


def pump_summary(res: dict) -> str:
    """输入:pump 的返回 → 输出:一行人读摘要。"""
    if not res["ok"]:
        t = res["totals"]
        return (f"⛔ {res['error']}\n本轮已入库:观测 {t['snapshots']} 条 / "
                f"身份 {t['products']} 条")
    t = res["totals"]
    line = (f"增量摄取:{res['pages']} 页 / 拉取 {res['fetched']} 条;"
            f"观测入库 {t['snapshots']}(重复跳过 {t['dup']}),"
            f"身份更新 {t['products']};"
            f"游标 {res['cursor_from']} → {res['cursor_to']}")
    if t["skipped_outcome"]:
        dist = ",".join(f"{k[len('outcome_'):]}×{v}"
                        for k, v in sorted(t.items())
                        if k.startswith("outcome_"))
        line += f",非 ok 采集只落观测 {t['skipped_outcome']}({dist})"
    if t["incomplete"]:
        line += f",completeness_ok=false {t['incomplete']}(空值未覆盖旧值)"
    if t["invalid"]:
        line += f",⚠ 缺 asin/source_id 丢弃 {t['invalid']}"
    return line
