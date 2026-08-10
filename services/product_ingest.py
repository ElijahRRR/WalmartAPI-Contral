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
    slow, updated_at)
VALUES (%(marketplace)s, %(asin)s, %(title)s, %(brand)s, %(amazon_category)s,
        %(image_url)s, %(slow_hash)s, %(slow)s::jsonb, now())
ON CONFLICT (marketplace, asin) DO UPDATE SET
    title = COALESCE(EXCLUDED.title, catalog.products.title),
    brand = COALESCE(EXCLUDED.brand, catalog.products.brand),
    amazon_category = COALESCE(EXCLUDED.amazon_category,
                               catalog.products.amazon_category),
    image_url = COALESCE(EXCLUDED.image_url, catalog.products.image_url),
    slow_hash = COALESCE(EXCLUDED.slow_hash, catalog.products.slow_hash),
    slow = COALESCE(EXCLUDED.slow, catalog.products.slow),
    updated_at = now()
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
    return {
        "marketplace": rec.get("marketplace") or "US",
        "asin": rec.get("asin"),
        "title": _blank_to_none(slow.get("title")),
        "brand": _blank_to_none(slow.get("brand")),
        "amazon_category": _category(slow),
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
    if counts["skipped_outcome"]:
        detail = ",".join(f"{k}:{v}" for k, v in sorted(outcomes.items())
                          if k != OUTCOME_OK)
        logger.info("非 ok 采集 %d 条只落观测层(%s)",
                    counts["skipped_outcome"], detail)
    return counts
