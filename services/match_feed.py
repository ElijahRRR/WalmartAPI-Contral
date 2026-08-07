"""跟卖(MP_ITEM_MATCH)业务积木:SPEC 预检候选、跟卖 Item 构造、SKU 规则。

⚠ 两处待与旧系统实证对拍(所有者提供旧 feed 备份
match_listing/logs/match_*.json 后定稿,--execute 前必须完成):
  1. build_match_item 的 Item 字段名(price/ShippingWeight 的确切形态);
  2. match_sku 的 SKU 生成规则(旧系统 B 列由脚本生成,规则未入档)。
构造基底 = SPEC 响应预填的 MPItem[0].Item(官方模板,productIdentifiers
已带),只叠加我方字段——最大限度贴官方,最小化猜测面。
"""

import logging

logger = logging.getLogger("services.match_feed")


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


def match_sku(product_id: str) -> str:
    """输入:SPEC 匹配到的规范化 productId → 输出:跟卖 SKU。

    ⚠ 暂定规则 = productId 原样(待对拍第 2 项;MP_ITEM_MATCH 按 sku
    REPLACE,SKU 一旦上线即永久身份,--execute 前必须定稿)。
    """
    return str(product_id)


def build_match_item(spec_raw: dict | None, sku: str, price, weight) -> dict:
    """输入:SPEC 原始 item + sku/售价/重量 → 输出:MP_ITEM_MATCH 的 Item dict。

    基底取 SPEC 预填模板(itemSpecPayload.MPItem[0].Item,含官方给的
    productIdentifiers 等),叠加我方字段。⚠ price/ShippingWeight 字段形态
    待对拍第 1 项。
    """
    base = dict((((spec_raw or {}).get("itemSpecPayload") or {})
                 .get("MPItem") or [{}])[0].get("Item") or {})
    base["sku"] = str(sku)
    base["price"] = round(float(price), 2)
    base["ShippingWeight"] = round(float(weight), 2)
    return base
