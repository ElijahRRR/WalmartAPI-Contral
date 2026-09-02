"""跟卖(MP_ITEM_MATCH)业务积木:SPEC 预检候选与跟卖 Item 构造。

Item 结构 2026-08-07 与旧系统真实 feed 备份对拍定稿(所有者提供样本):
  {"sku": "<12 位不透明码>", "condition": "New",
   "productIdentifiers": {"productIdType": "GTIN", "productId": 14位},
   "ShippingWeight": 0.4, "price": 14.76}
五字段;price/ShippingWeight 裸 number。
构造基底 = SPEC 响应预填的 MPItem[0].Item(官方模板),叠加我方字段。

**本模块不生成 SKU**(2026-09-02,SKU 改造批次 2):跟卖 SKU 由
`services/sku_codec.mint` 抽 12 位不透明码(跟卖表 B 列人工号优先不变,
人工号走 `listing_sources.register` 登记)。旧的 `SKU_PREFIX` / `make_sku` /
`next_serial_start`(PHUMWMT + 提交日期 + 当日 4 位序号,从 ops.feed_items
续号)**已删**:① 把上架日期写进 SKU,与货源隐匿目标直接冲突;② 每轮重发
取到新序号 ⇒ 载荷漂 ⇒ api/feeds 的 payload_key 在途防重失效;③ 留着它就是
第二条发码路径(conventions §六:一个能力一条实现路径),而误用不会报错。
存量 PHUMWMT 行不受影响:读路径全格式通吃,它们只在飞书 B 列与
ops.feed_items 历史里。
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
