"""MP_ITEM_MATCH v5 规范守门:载荷逐字对着**官方规范原件**校。

被测对象不是我们自己的想象,而是
`refdata/specs/MP_ITEM_MATCH_5.0.20260607-22_38_54-api.json` —— 所有者从开发者
门户下载的那一份 JSON Schema(draft-07)。这条守门存在的理由只有一个:
**`additionalProperties: false` 是这份规范里到处都是的东西** —— header 多一个
字段、Item 多一个字段、inventory 项多一个字段,沃尔玛都会整批退回来,而我们
在本地看不出任何异常(载荷长得很像对的)。v4.2 时代那套
`{processMode, subset, sellingChannel}` header 就是这样在 v5 里变成三个未知字段的。

所以这里**不写第二份字段清单**:required / properties / enum / multipleOf 全部
现读原件。以后谁改载荷,拦他的是官方原件而不是某个人的记忆。

⚠ 跟卖(match_listing)与改码(sku_migrate)**共用这一个 feedType**,两条链的
构造点也共用 `services/match_feed.build_match_item` —— 所以两条链的样例都在这里
过同一把尺子(改码那条多一个 `inventory`)。
"""

import json
import pathlib
from decimal import Decimal

import pytest

from api import feeds
from registry import resources
from services import match_feed

from registry import paths

SPEC_PATH = paths.match_spec_file()      # 路径只从 registry 取(铁律 3)


@pytest.fixture(scope="module")
def spec() -> dict:
    with SPEC_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def item_schema(spec) -> dict:
    """MPItem[].Item 的 schema(v5 仍是 `{Item: {...}}` 包装,不是 Orderable/Visible)。"""
    return spec["properties"]["MPItem"]["items"]["properties"]["Item"]


def _match_item() -> dict:
    """跟卖链的一条 Item(不带库存 —— 跟卖的现状,升 v5 后逐字不变)。"""
    return match_feed.build_match_item(
        None, "AN3WC0DE2345", 29.99, 0.82,
        product_id="00121678236703", product_id_type="GTIN")


def _migrate_item() -> dict:
    """改码链的一条 Item(**带库存**:v5 起库存随改码 feed 一起写)。"""
    return match_feed.build_match_item(
        None, "AN3WC0DE2345", 29.99, 0.82,
        product_id="00121678236703", product_id_type="GTIN",
        inventory=(30, "94301"))


# ── ① 版本串:只能是原件 enum 里的那个值 ────────────────────────────────────

def test_registered_version_is_the_one_the_official_spec_allows(spec):
    """`FEED_SPEC_VERSIONS["MP_ITEM_MATCH"]` 必须落在原件的 version enum 里。

    v4.2 那个 "4.2" 写在这里也不会有任何本地异常 —— 只有沃尔玛会退回来。
    """
    enum = spec["properties"]["MPItemFeedHeader"]["properties"]["version"]["enum"]
    assert resources.FEED_SPEC_VERSIONS["MP_ITEM_MATCH"] in enum
    # 文件名与 enum 同源(原件换版时两处一起变,不许只改一处)
    assert SPEC_PATH.stem.split("_", 3)[-1] in enum


# ── ② header:三字段封闭 ────────────────────────────────────────────────────

def test_payload_header_is_exactly_the_required_closed_set(spec):
    """header 的键集合 **== required**,一个不多一个不少。

    `additionalProperties: false` + required 三个 ⇒ 这个集合是**闭**的:
    v4.2 的 processMode / subset / sellingChannel 在 v5 里是未知字段。
    """
    hdr_schema = spec["properties"]["MPItemFeedHeader"]
    assert hdr_schema["additionalProperties"] is False
    payload = feeds.build_payload("MP_ITEM_MATCH", [_match_item()])

    assert set(payload) == set(spec["required"])          # 顶层两件
    assert set(payload) <= set(spec["properties"])
    hdr = payload["MPItemFeedHeader"]
    assert set(hdr) == set(hdr_schema["required"]) == {"businessUnit", "locale",
                                                       "version"}
    for key, val in hdr.items():
        enum = hdr_schema["properties"][key].get("enum")
        assert enum is None or val in enum, (key, val)
    # v4.2 的三件套一个都不许再出现在整条载荷里
    blob = json.dumps(payload)
    for dead in ("processMode", "subset", "sellingChannel", "mpsetupbymatch"):
        assert dead not in blob, dead


# ── ③ 条目:{Item: {...}} 包装,Item 的键都在 properties 里 ──────────────────

def _assert_item_conforms(item: dict, item_schema: dict) -> None:
    """输入:一条 Item + 原件的 Item schema → 输出:无(不合规就 assert 失败)。"""
    props = item_schema["properties"]
    assert item_schema["additionalProperties"] is False
    extra = set(item) - set(props)
    assert not extra, f"Item 出现规范外字段(additionalProperties=false):{extra}"
    missing = set(item_schema["required"]) - set(item)
    assert not missing, f"Item 缺必填字段:{missing}"

    assert item["productIdentifiers"]["productIdType"] in \
        props["productIdentifiers"]["properties"]["productIdType"]["enum"]
    assert set(item["productIdentifiers"]) == \
        set(props["productIdentifiers"]["required"])
    assert item["condition"] in props["condition"]["enum"]
    assert len(item["sku"]) <= props["sku"]["maxLength"]
    # multipleOf 用 Decimal 比:float 取模自己就有误差,拿它当判据会误报
    for field in ("price", "ShippingWeight"):
        step = Decimal(str(props[field]["multipleOf"]))
        assert Decimal(str(item[field])) % step == 0, (field, item[field])
        assert props[field]["minimum"] <= item[field] <= props[field]["maximum"]


def test_match_chain_item_conforms(item_schema):
    payload = feeds.build_payload("MP_ITEM_MATCH", [_match_item()])
    for entry in payload["MPItem"]:
        assert set(entry) == {"Item"}          # MPItem[] 每项 required=['Item']
        _assert_item_conforms(entry["Item"], item_schema)
    assert "inventory" not in payload["MPItem"][0]["Item"]   # 跟卖链现状:不带


def test_migrate_chain_item_carries_inventory_in_spec_shape(item_schema):
    """改码链的 Item 带 `inventory`,项的键集合 **== {quantity, fulfillmentCenterID}**。"""
    inv_schema = item_schema["properties"]["inventory"]
    payload = feeds.build_payload("MP_ITEM_MATCH", [_migrate_item()])
    item = payload["MPItem"][0]["Item"]
    _assert_item_conforms(item, item_schema)

    rows = item["inventory"]
    assert isinstance(rows, list) and len(rows) >= inv_schema["minItems"]
    row_schema = inv_schema["items"]
    assert row_schema["additionalProperties"] is False
    for row in rows:
        assert set(row) == set(row_schema["required"]) == {"quantity",
                                                           "fulfillmentCenterID"}
        assert isinstance(row["quantity"], int) and not isinstance(row["quantity"], bool)
        assert row_schema["properties"]["quantity"]["minimum"] <= row["quantity"]
        assert isinstance(row["fulfillmentCenterID"], str) and row["fulfillmentCenterID"]
        assert len(row["fulfillmentCenterID"]) <= \
            row_schema["properties"]["fulfillmentCenterID"]["maxLength"]


def test_the_workflow_builder_also_conforms(item_schema):
    """改码链的**真构造点** `sku_migrate._item_of` 也过同一把尺子。

    积木合规不等于工作流合规:`_item_of` 才是每天真发出去的那条路径
    (它决定带不带 inventory、qty 从哪一列来)。
    """
    from workflows import sku_migrate as sm

    row = {"old_sku": "B0AAA00001", "source_type": "amz", "source_key": "B0AAA00001",
           "product_id": "00121678236703", "product_id_type": "GTIN",
           "price": 29.99, "avail_qty": 30,
           "product_slow": {"weight": {"package": "0.82 lbs"}}}
    with_inv = sm._item_of(row, "AN3WC0DE2345", "94301")
    payload = feeds.build_payload("MP_ITEM_MATCH", [with_inv])
    _assert_item_conforms(payload["MPItem"][0]["Item"], item_schema)
    assert payload["MPItem"][0]["Item"]["inventory"] == [
        {"quantity": 30, "fulfillmentCenterID": "94301"}]

    # 未观测到库存 / 受管仓判不出 ⇒ 不带这个字段(**不猜 0**:REPLACE 会照写)
    for item in (sm._item_of({**row, "avail_qty": None}, "AN3WC0DE2345", "94301"),
                 sm._item_of(row, "AN3WC0DE2345", None)):
        assert "inventory" not in item
        _assert_item_conforms(item, item_schema)


def test_prefill_keys_outside_the_official_item_schema_are_dropped(spec, caplog):
    """v4.2 时代 SPEC 预填实测带 productCategory(sku_plan §9.12),而 v5 的 Item 是
    additionalProperties=false:不过滤就是整批退而本地无异常。过滤按原件白名单,
    丢掉的键要进日志(真兜底三要件:同函数内、触发记日志、条件明确)。"""
    import logging
    raw = {"itemSpecPayload": {"MPItem": [{"Item": {
        "productIdentifiers": {"productId": "00630995821917", "productIdType": "GTIN"},
        "productCategory": "Home Decor, Kitchen, & Other"}}]}}
    with caplog.at_level(logging.INFO, logger="services.match_feed"):
        item = match_feed.build_match_item(raw, "AVW476VD6W3H", 39, 0.72)
    allowed = set(spec["properties"]["MPItem"]["items"]["properties"]["Item"]["properties"])
    assert "productCategory" not in item
    assert set(item) <= allowed
    assert item["productIdentifiers"]["productId"] == "00630995821917"
    assert any("productCategory" in r.getMessage() for r in caplog.records)


def test_item_property_names_come_from_the_official_file(spec):
    allowed = set(spec["properties"]["MPItem"]["items"]["properties"]["Item"]["properties"])
    assert set(match_feed.item_property_names()) == allowed
    assert {"sku", "price", "ShippingWeight", "condition", "productIdentifiers",
            "inventory"} <= allowed
