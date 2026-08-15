"""跟卖(MP_ITEM_MATCH)业务积木:SPEC 预检候选、跟卖 Item 构造、SKU 规则。

Item 结构 2026-08-07 与旧系统真实 feed 备份对拍定稿(所有者提供样本):
  {"sku": "PHUMWMT202606280001", "condition": "New",
   "productIdentifiers": {"productIdType": "GTIN", "productId": 14位},
   "ShippingWeight": 0.4, "price": 14.76}
五字段;price/ShippingWeight 裸 number。SKU 规则(所有者澄清 2026-08-07:
旧系统 SKU 是人工编号填入):新系统 B 列**人工优先,留空自动生成**——
沿用其编号格式 PHUMWMT + 提交日期 YYYYMMDD + 当日 4 位序号,序号从
ops.feed_items 已用最大值续(重启/重跑不重号)。
构造基底 = SPEC 响应预填的 MPItem[0].Item(官方模板),叠加我方字段。
"""

import logging

logger = logging.getLogger("services.match_feed")

SKU_PREFIX = "PHUMWMT"      # 跟卖 SKU 固定前缀(旧系统实证样本)


def spec_candidates(code: str) -> list[tuple[str, str]]:
    """输入:运营填的商品码 → 输出:[(参数名 upc|gtin, 值)] 预检候选序列。

    旧系统实证:upc(12位)/gtin(13-14位)是不同参数,传错位数查不到;
    Excel 丢前导 0 用 zfill 补;全相同数字的退化码直接判无效不查。
    """
    v = "".join(ch for ch in str(code).strip() if ch.isdigit())
    if not v or len(v) > 14 or len(set(v)) == 1:
        return []
    out: list[tuple[str, str]] = []
    if len(v) <= 12:
        out.append(("upc", v.zfill(12)))
        out.append(("gtin", v.zfill(14)))
    else:
        out.append(("gtin", v.zfill(14)))
    return out


def next_serial_start(conn, date_str: str) -> int:
    """输入:连接 + YYYYMMDD → 输出:今日下一个可用序号(跨店全局)。

    从 ops.feed_items 已提交的跟卖 SKU 续号(权威台账,重启/重跑不重号;
    序号有缺口无害,重号才致命——MP_ITEM_MATCH 按 sku REPLACE)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(right(sku, 4)) FROM ops.feed_items "
                    "WHERE feed_type = 'MP_ITEM_MATCH' AND sku LIKE %s",
                    (f"{SKU_PREFIX}{date_str}%",))
        row = cur.fetchone()
    try:
        return int(row[0]) + 1 if row and row[0] else 1
    except (TypeError, ValueError):
        return 1


def make_sku(date_str: str, serial: int) -> str:
    """输入:YYYYMMDD + 序号 → 输出:跟卖 SKU(旧系统实证格式)。"""
    return f"{SKU_PREFIX}{date_str}{serial:04d}"


def build_match_item(spec_raw: dict | None, sku: str, price, weight,
                     product_id: str | None = None,
                     product_id_type: str | None = None) -> dict:
    """输入:SPEC 原始 item + sku/售价/重量(+ 预检出的 productId 兜底)
    → 输出:MP_ITEM_MATCH 的 Item dict(五字段,2026-08-07 对拍定稿)。

    基底取 SPEC 预填模板(itemSpecPayload.MPItem[0].Item);condition 缺省
    补 "New";模板没带 productIdentifiers 时用预检结果兜底填。
    """
    base = dict((((spec_raw or {}).get("itemSpecPayload") or {})
                 .get("MPItem") or [{}])[0].get("Item") or {})
    base["sku"] = str(sku)
    base["price"] = round(float(price), 2)
    # 重量留空默认 1 磅(旧 DEFAULT_WEIGHT 实证,2026-08-12 旧仓对照补回:
    # 旧系统运营可以不填重量;此前 float('') 抛异常把行打成"数据无效"卡死)
    w = str(weight or "").strip()
    base["ShippingWeight"] = round(float(w), 2) if w else 1.0
    base.setdefault("condition", "New")
    if "productIdentifiers" not in base and product_id:
        base["productIdentifiers"] = {"productIdType": product_id_type or "GTIN",
                                      "productId": str(product_id)}
    return base
