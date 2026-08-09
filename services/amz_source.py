"""上架数据源 provider(listing L2d;读产品中心库,不直连采集服务)。

产品数据契约(provider 产出的统一形态,mapper/主链只认这个,来源无关):
  {"asin": str, "title": str, "brand": str|None, "category": str|None,
   "price": float|None,          # amz 现价(定价输入)
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
MAX_LEAD_DAYS = 12          # 配送 >12 天仍上架但库存写 0(旧值)

MARKETPLACE = "US"          # 上架目的地(契约:与 (marketplace,asin) 主键对齐)

# 同 ASIN 多邮编组取"最近一次采集"那组:价格/库存以最新观测为准。
# zip_verify == 'mismatch' 的观测不参与(请求邮编未生效,价格不属于该分组)。
_SQL = """
SELECT p.asin, p.title, p.brand, p.amazon_category, p.image_url,
       s.price, s.stock_state, s.raw
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT price, stock_state, raw
    FROM catalog.latest_snapshot ls
    WHERE ls.marketplace = p.marketplace AND ls.asin = p.asin
      AND coalesce(ls.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY ls.scraped_at DESC
    LIMIT 1
) s ON true
WHERE p.marketplace = %s AND p.asin = ANY(%s)
"""

# ⚠ 契约 v1 **没有数值库存**,只有 stock_state 三值封闭集(§5.1)。
# 旧系统的「库存 <5 淘汰」防的是亚马逊只剩三两件时上架导致超卖——这条信号
# 在新数据源下**不存在了**。此处不臆造大数字(直接进 feed 的 quantity.amount,
# 编大了就是超卖事故),而是:有货 → 一个保守固定值,后续由 maintenance 的
# inventory provider 按最新观测同步;无货/未知 → 不上架。
# 要恢复旧粒度,需采集侧在契约里补数值库存字段(届时删掉本常量改读真值)。
IN_STOCK_QTY = int(os.environ.get("AMZ_IN_STOCK_QTY", "10"))
_STOCK_BY_STATE = {"in_stock": IN_STOCK_QTY, "out_of_stock": 0}


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

    lead_days / channel 采集侧当前不产出(契约无此字段):留 None,
    主链按"未知"处理(不触发 >12 天清零规则);采集侧补齐后在此接上即可。
    """
    if not asins:
        return {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL, (MARKETPLACE, list(asins)))
        rows = cur.fetchall()

    out: dict[str, dict] = {}
    for (asin, title, brand, category, image_url, price, stock_state,
         raw) in rows:
        if not title:           # 身份层还没拿到标题 = 这条不够格喂上架链
            continue
        images = _images(raw) or ([image_url] if image_url else [])
        out[asin] = {
            "asin": asin, "title": title, "brand": brand, "category": category,
            "price": float(price) if price is not None else None,
            "stock": _STOCK_BY_STATE.get(str(stock_state or ""), None),
            "lead_days": None, "channel": None,
            "images": images, "attrs": _attrs(raw),
        }
    absent = [a for a in asins if a not in out]
    if absent:
        # 列出具体 ASIN:这批就是要推给采集服务补采的清单(接线期人工推,
        # 将来由选品/审核链保证"进上架表前必已采集")
        shown = ",".join(absent[:20]) + ("…" if len(absent) > 20 else "")
        logger.info("产品中心缺 %d/%d 个 ASIN 的可用数据(本轮跳过,采集后自动"
                    "续上):%s", len(absent), len(asins), shown)
    return out
