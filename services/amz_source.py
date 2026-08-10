"""上架数据源 provider(listing L2d;读产品中心库,不直连采集服务)。

产品数据契约(provider 产出的统一形态,mapper/主链只认这个,来源无关):
  {"asin": str, "title": str, "brand": str|None, "category": str|None,
   "price": float|None,          # amz 单价
   "shipping": float|None,       # 运费(定价输入 = 单价 + 运费;None≠0)
   "stock": int|None,            # amz 可见库存(<5 淘汰,MIN_INVENTORY)
   "lead_days": int|None,        # 配送时长(>12 天上架但库存写 0)
   "channel": "FBA"|"FBM"|None,  # 定价区间路由
   "images": [url, ...],         # 已按防御性排序(来源侧 set() 去重打乱顺序)
   "attrs": {...}}               # 其余原始属性(LLM 映射输入)

数据来源(2026-08-08 接通):`catalog.products`(身份层,product_ingest 摄取)
+ `catalog.latest_snapshot`(观测层每 (marketplace,asin,采集参数) 组最新一条)。
**不直连采集服务**——全项目只有 workflows/product_ingest 碰采集器(漏斗铁律)。

数据缺席的 ASIN 不出现在结果里(主链按"数据缺失"跳过该行,不写终态,
数据到位后自动续上)。
"""

import logging
import os

from registry import db

logger = logging.getLogger("services.amz_source")

MIN_INVENTORY = 5           # 库存 <5 不上架(旧 MIN_INVENTORY_THRESHOLD)
MAX_LEAD_DAYS = 8           # 配送 >8 天:上架但库存写 0 / 维护时清零
                            # (所有者定稿 2026-08-09 从旧值 12 收紧到 8;
                            #  list_new 与 maintenance 共用这一个口径)

MARKETPLACE = "US"          # 上架目的地(契约:与 (marketplace,asin) 主键对齐)

# 同 ASIN 多邮编组取"最近一次采集"那组:价格/库存以最新观测为准。
# zip_verify == 'mismatch' 的观测不参与(请求邮编未生效,价格不属于该分组)。
_SQL = """
SELECT p.asin, p.title, p.brand, p.amazon_category, p.image_url, p.slow,
       s.price, s.stock_state, s.stock_count, s.delivery_days, s.raw,
       s.fulfillment, s.shipping
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT price, stock_state, stock_count, delivery_days, raw, shipping,
           raw ->> 'is_fba' AS fulfillment
    FROM catalog.latest_snapshot ls
    WHERE ls.marketplace = p.marketplace AND ls.asin = p.asin
      AND coalesce(ls.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY ls.scraped_at DESC
    LIMIT 1
) s ON true
WHERE p.marketplace = %s AND p.asin = ANY(%s)
"""

# 有货但**数量没采到**时的保守铺货量(亚马逊高库存时不显示具体数)。
# 只在 stock_count IS NULL 且 stock_state='in_stock' 时才用得上——
# 采到了真值就用真值,**绝不用它覆盖 stock_count=0**(那是确实缺货)。
IN_STOCK_QTY = int(os.environ.get("AMZ_IN_STOCK_QTY", "10"))


def _images(raw) -> list[str]:
    """输入:snapshot.raw → 输出:图片 URL 列表(字典序,防采集侧 set() 乱序)。"""
    if not isinstance(raw, dict):
        return []
    imgs = ((raw.get("slow") or {}).get("images")
            if isinstance(raw.get("slow"), dict) else None) or raw.get("images")
    if not isinstance(imgs, (list, tuple)):
        return []
    return sorted(str(u) for u in imgs if u)


def _attrs(raw) -> dict:
    """输入:snapshot.raw → 输出:LLM 映射输入用的原始属性(取 slow 段)。"""
    if not isinstance(raw, dict):
        return {}
    slow = raw.get("slow")
    return dict(slow) if isinstance(slow, dict) else {k: v for k, v in raw.items()
                                                     if k not in ("fast",)}


def fetch_products(asins: list[str]) -> dict[str, dict]:
    """输入:ASIN 列表 → 输出:{asin: 产品数据契约 dict}(缺席的不出现)。

    ⚠ **stock / lead_days 的 None 与 0 是两回事**(契约 3b):None = 本次没采到,
    0 = 采到了确实是 0。本函数**只搬运真值,不做 or 0 兜底**;"数量未知怎么办"
    是业务判断,归调用方(list_new)——见 stock_state 字段。
    """
    if not asins:
        return {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL, (MARKETPLACE, list(asins)))
        rows = cur.fetchall()

    out: dict[str, dict] = {}
    for (asin, title, brand, category, image_url, slow, price, stock_state,
         stock_count, delivery_days, raw, fulfillment, shipping) in rows:
        if not title:           # 身份层还没拿到标题 = 这条不够格喂上架链
            continue
        # attrs 首选身份层的 slow 全量段(卖点/描述/重量/尺寸/变体都在这里);
        # raw 是契约裁剪过的载荷,只是老行的兜底
        attrs = dict(slow) if isinstance(slow, dict) else _attrs(raw)
        images = (sorted(str(u) for u in (attrs.get("images") or []) if u)
                  or _images(raw) or ([image_url] if image_url else []))
        out[asin] = {
            "asin": asin, "title": title, "brand": brand, "category": category,
            "price": float(price) if price is not None else None,
            # 运费:定价输入是**落地价 = 单价 + 运费**(所有者定稿 2026-08-10)。
            # None ≠ 0——None 是"这次没采到"(落地价算不出来,调用方不定价),
            # 0.0 是采集侧确认的 FREE
            "shipping": float(shipping) if shipping is not None else None,
            "stock": stock_count,               # int | None(None ≠ 0)
            "stock_state": str(stock_state) if stock_state else None,
            "lead_days": delivery_days,         # int | None(None ≠ 0)
            # 配送方式(定价区间路由):采集侧 raw.is_fba,拿不到就是 None
            # ——调用方按"未知不定价"处理,绝不猜(猜错一档 = 拿错倍率)
            "channel": (str(fulfillment).strip().upper()
                        if fulfillment else None),
            "images": images, "attrs": attrs,
        }
    absent = [a for a in asins if a not in out]
    if absent:
        # 列出具体 ASIN:这批就是要推给采集服务补采的清单(接线期人工推,
        # 将来由选品/审核链保证"进上架表前必已采集")
        shown = ",".join(absent[:20]) + ("…" if len(absent) > 20 else "")
        logger.info("产品中心缺 %d/%d 个 ASIN 的可用数据(本轮跳过,采集后自动"
                    "续上):%s", len(absent), len(asins), shown)
    return out
