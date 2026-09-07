"""跟卖(MP_ITEM_MATCH)业务积木:SPEC 预检候选与跟卖 Item 构造。

Item 结构 2026-08-07 与旧系统真实 feed 备份对拍定稿(所有者提供样本):
  {"sku": "<12 位不透明码>", "condition": "New",
   "productIdentifiers": {"productIdType": "GTIN", "productId": 14位},
   "ShippingWeight": 0.4, "price": 14.76}
五字段;price/ShippingWeight 裸 number。
构造基底 = SPEC 响应预填的 MPItem[0].Item(官方模板),叠加我方字段。

**2026-09-07 通道升 v5**(`registry.resources.FEED_SPEC_VERSIONS["MP_ITEM_MATCH"]`
= 5.0.20260607-22_38_54-api,v4.2 同日退役):信封由 api/feeds 换成 businessUnit 制
三字段 header,**Item 仍是 `{Item: {...}}` 包装、五字段仍然合规**(v5 的 Item
required 就是 productIdentifiers/sku/condition/ShippingWeight/price)。v5 新增的
可选 `inventory` 由 `build_match_item(inventory=...)` 带,只有改码链在用 ——
跟卖链不给这个参数,载荷逐字不变。规范原件在 refdata/specs/(守门测试读它)。

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

import json
import logging

from registry import paths

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


_ITEM_PROPERTY_NAMES: frozenset | None = None


def item_property_names() -> frozenset:
    """输入:无 → 输出:官方 v5 规范原件里 Item.properties 的键集合(进程内缓存一次)。

    唯一出处是 registry.paths.match_spec_file() 指向的原件;这里不手抄第二份字段清单。
    """
    global _ITEM_PROPERTY_NAMES
    if _ITEM_PROPERTY_NAMES is None:
        with open(paths.match_spec_file(), encoding="utf-8") as fh:
            spec = json.load(fh)
        props = (spec["properties"]["MPItem"]["items"]["properties"]["Item"]
                 .get("properties") or {})
        _ITEM_PROPERTY_NAMES = frozenset(props)
    return _ITEM_PROPERTY_NAMES


def build_match_item(spec_raw: dict | None, sku: str, price, weight,
                     product_id: str | None = None,
                     product_id_type: str | None = None,
                     inventory: tuple[int, str] | None = None) -> dict:
    """输入:SPEC 原始 item + sku/售价/重量(+ 预检出的 productId 兜底 + 可选库存)
    → 输出:MP_ITEM_MATCH 的 Item dict(**跟卖链与改码链共用的唯一构造点**)。

    基底取 SPEC 预填模板(itemSpecPayload.MPItem[0].Item);condition 缺省
    补 "New";模板没带 productIdentifiers 时用预检结果兜底填。

    小数位按官方规范原件的 `multipleOf`
    (refdata/specs/MP_ITEM_MATCH_5.0.20260607-22_38_54-api.json):
    price 0.01 ⇒ 2 位;ShippingWeight 0.001 ⇒ 3 位。
    (api/feeds._sanitize 还会把所有 float 收到 2 位 —— 2 位仍是 0.001 的整数倍,
     两处不冲突;想发 3 位小数的重量要先改 api 那一层。)

    **inventory 给了才带**(v5 起 `Item.inventory` 是可选数组,minItems 1,每项
    required=[quantity, fulfillmentCenterID] 且 additionalProperties=false):
      · 改码链(sku_migrate)**带** —— MP_ITEM_MATCH 是 REPLACE,载荷没带的字段
        会被当空值写,库存归零过一次(docs/sku_plan.md §9.12 第一级投放实录);
      · 跟卖链(match_listing)**不带** —— 新 offer 的库存由维护链正式出口写,
        与 v4.2 时代行为逐字一致(不给这个参数就一个字节都不多发)。
    fulfillmentCenterID 由调用方从 `services/store_limits.listing_fc` 取
    (上架链取 FC 的唯一入口),**本模块不猜节点**。
    """
    base = dict((((spec_raw or {}).get("itemSpecPayload") or {})
                 .get("MPItem") or [{}])[0].get("Item") or {})
    # v5 的 Item 是 additionalProperties=false:SPEC 预填模板(v4.2 时代实测带
    # productCategory)里规范外的键发出去 = 整批 DATA_ERROR 而本地毫无异常。
    # 按官方原件的 Item.properties 白名单过滤,丢掉的键**记日志计数**(真兜底三要件)。
    dropped = sorted(k for k in base if k not in item_property_names())
    for k in dropped:
        base.pop(k, None)
    if dropped:
        logger.info("MP_ITEM_MATCH 预填模板含 v5 规范外的键 %s,已按官方原件丢弃(sku=%s)",
                    dropped, sku)
    base["sku"] = str(sku)
    base["price"] = round(float(price), 2)
    # 重量留空默认 1 磅(旧 DEFAULT_WEIGHT 实证,2026-08-12 旧仓对照补回:
    # 旧系统运营可以不填重量;此前 float('') 抛异常把行打成"数据无效"卡死)
    w = str(weight or "").strip()
    base["ShippingWeight"] = round(float(w), 3) if w else 1.0
    base.setdefault("condition", "New")
    if "productIdentifiers" not in base and product_id:
        base["productIdentifiers"] = {"productIdType": product_id_type or "GTIN",
                                      "productId": str(product_id)}
    if inventory is not None:
        qty, fc = inventory
        base["inventory"] = [{"quantity": int(qty), "fulfillmentCenterID": str(fc)}]
    return base
