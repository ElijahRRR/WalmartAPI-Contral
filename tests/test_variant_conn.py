"""变体链的**连接与降级**回归(2026-08-17 对抗审查揪出来的五处)。

单开一个文件的理由:这五条里有三条是"1460 个测试全绿也没挡住"的那类 ——
连接拿到手就是关闭的、决策链某一段永远不被调用、载荷层最后一道类型闸有
catch-all。它们的共同点是**行为正确但被静默吞掉**,只有真调那段代码才照得出来。
"""

import collections
import contextlib

import pytest

from services import mp_conform as mc
from services import variant_group as vg
from workflows import list_new as ln


# ── M1 `db.pg_conn().__enter__()` 拿到的是已关闭连接 ────────────────────────

class _Cur:
    def __init__(self, conn, rows):
        self.conn = conn
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.conn.closed:
            raise RuntimeError("connection is closed")
        self.conn.queries.append(sql)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows=()):
        self.closed = False
        self.queries: list[str] = []
        self.rows = list(rows)

    def cursor(self):
        return _Cur(self, self.rows)

    def close(self):
        self.closed = True


def _fake_pg(conn):
    @contextlib.contextmanager
    def _cm(*a, **kw):
        try:
            yield conn
        finally:
            conn.close()
    return _cm


def _row(asin, fam, parent="P0", attrs="color_name=Black", store="M001"):
    return {"asin": asin, "store": store, "product_type": "PT",
            "_p": {"variant_attributes": attrs, "variation_asins": fam,
                   "parent_asin": parent}}


def test_family_lookup_actually_runs_its_query(monkeypatch):
    """⚠ 定稿③「同族已在架 ⇒ 沿用它的 variantGroupId」在生产里从未生效过。

    根因:`db.pg_conn().__enter__()` —— `registry.db.pg_conn` 是
    `@contextmanager`,临时 CM 对象在那一行之后立刻被回收,生成器被 close,
    `finally: conn.close()` 当场执行 ⇒ **拿到手的连接已经关闭**。于是那条
    SELECT 每行抛 "connection is closed",被 `_variant_plan` 自己的 except
    吞成一行 warning,`family_has_primary` 一并失效,全程不报错。

    这条用例真调 `_plan_variants`,断言那条查询**发出去了**、且拿到的组 ID
    是库里在架兄弟的那个(不是派生的)。
    """
    conn = _Conn(rows=[("B0SIB", "LEGACY_GID", "Yes")])
    monkeypatch.setattr(ln.db, "pg_conn", _fake_pg(conn))
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color"]}},
        "color": {"type": "string"}}})
    ready = [_row("B0ME", "B0SIB")]
    ln._plan_variants(ready, collections.defaultdict(int))
    assert conn.queries, "同族在架查询压根没发出去(连接是关闭的?)"
    vp = ready[0]["_vplan"]
    assert vp["group_id"] == "LEGACY_GID"      # 沿用在架的,不是 vg_ 派生
    assert vp["is_primary"] is False           # 在架兄弟已是主变体


def test_no_family_means_no_connection_at_all(monkeypatch):
    """家族只有自己时一次连接都不开 —— 闸门段本来不持有连接。"""
    opened = []

    @contextlib.contextmanager
    def _cm(*a, **kw):
        opened.append(1)
        yield _Conn()
    monkeypatch.setattr(ln.db, "pg_conn", _cm)
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    ln._plan_variants([_row("B0ME", "")],
                      collections.defaultdict(int))
    assert not opened


def test_remap_llm_path_does_not_blow_up_the_whole_run(monkeypatch):
    """⚠ 这条是不静默的那半边:`_remap_unmapped_dims` 只有 finally 没有 except,
    连接已关的话 `llm_cache` 抛出来会一路冒到 run(),**整条 list_new 失败**
    (dry-run 也一样)。修好后连接可用,重映射能正常返回。
    """
    conn = _Conn()
    monkeypatch.setattr(ln.db, "pg_conn", _fake_pg(conn))
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["pieceCount"]}},
        "pieceCount": {"type": "integer"}}})
    seen = {}

    def _llm(c, pt, dim, values, enum):
        seen["closed"] = c.closed
        return ("pieceCount", {a: 48 for a in values})
    monkeypatch.setattr(ln.variant_remap, "llm_remap", _llm)
    rows = [_row("B0A", "B0B", attrs="weird_dim=48 Color"),
            _row("B0B", "B0A", attrs="weird_dim=12 Colors")]
    for r in rows:
        r["_vplan"] = vg.plan(r["asin"], r["_p"]["variant_attributes"],
                              r["_p"]["variation_asins"], "P0", ["pieceCount"])
    ln._remap_unmapped_dims(rows)
    assert seen.get("closed") is False, "交给 LLM 层的连接是关闭的"
    assert [n for n, _ in rows[0]["_vplan"]["attr_pairs"]] == ["pieceCount"]


# ── M2 同一家族被切成两个组 ─────────────────────────────────────────────────

def test_group_id_is_stable_when_parent_column_is_mixed():
    """⚠ 判据必须是 `parent in 家族`,不是 `parent == 自己`。

    混合形态(部分行 parent 填自己、部分行填某个兄弟)下,写成后者会让
    填自己的那行走 min(家族)、填兄弟的那行走 parent,**同一家族两个组 ID**,
    而且不报错。旧仓正因这个坑无条件用 min(full_set)。
    """
    fam = ["B0009GGJ9G", "B0009GGJCI", "B0009GGJDW"]
    mixed = {vg.group_id("B0009GGJDW", "B0009GGJDW", fam),   # parent 填自己
             vg.group_id("B0009GGJDW", "B0009GGJ9G", fam),   # parent 填兄弟
             vg.group_id("B0009GGJCI", "B0009GGJDW", fam)}
    assert mixed == {"vg_B0009GGJ9G"}, mixed
    # 真族主(不在家族里)照旧按 parent 派生 —— 生产实见形态,ID 更好认,
    # 且**不改动已经发出去的组 ID**
    assert {vg.group_id("B0C9WGHMZS", a, fam) for a in fam} == \
        {"vg_B0C9WGHMZS"}


# ── M3 重映射链对它的主场景不可达 ───────────────────────────────────────────

def test_no_dim_still_carries_the_dims_for_remap():
    """一个维度都没映上时,那些维度名正是错位重映射存在的**唯一场景**。

    首版这里留空,于是接线侧按 `unmapped_dims` 入组的那段永远收不到它们 ——
    刚补迁的整条 variant_remap 对主场景(旧仓 Art Sets 案例)一次都不会被调。
    """
    p = vg.plan("A1", "color_name=48 Color", "A2", "P1",
                ["pieceCount", "count", "multipackQuantity"])
    assert p["code"] == "no_dim"
    assert p["unmapped_dims"] == ["color_name"]


def test_no_dim_group_is_upgraded_back_to_variant_after_remap(monkeypatch):
    """内置表把 no_dim 救回来之后,决策要升回变体口径 —— 否则决策说"有维度"、
    载荷层按单品发,两边打架。"""
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["pieceCount"]}},
        "pieceCount": {"type": "integer"}}})
    rows = [_row("A1", "A2", attrs="color_name=48 Color"),
            _row("A2", "A1", attrs="color_name=12 Colors")]
    for r in rows:
        r["product_type"] = "Art Sets"
        r["_vplan"] = vg.plan(r["asin"], r["_p"]["variant_attributes"],
                              r["_p"]["variation_asins"], "P0", ["pieceCount"])
        assert r["_vplan"]["mode"] == "single"       # 起点是退单品
    ln._remap_unmapped_dims(rows)
    for r, want in zip(rows, (48, 12)):
        vp = r["_vplan"]
        assert vp["mode"] == "variant" and vp["code"] == "variant"
        assert vp["attr_pairs"] == [("pieceCount", want)]
        assert vp["remapped_dims"] == ["color_name→pieceCount"]


# ── M4 载荷层最后一道类型闸的 catch-all ─────────────────────────────────────

@pytest.mark.parametrize("prop,val,want", [
    ({"type": "array", "items": {"type": "string"}}, "Bamboo", ["Bamboo"]),
    ({"type": "array", "items": {"enum": ["Oak"]}}, "Bamboo", None),
    ({"type": "boolean"}, "Yes", None),
    # spec 没写 type 时 `_type_of` 默认 "string"(与 mp_conform 全模块同口径),
    # 所以走字符串分支、无枚举即放行 —— 这不是 catch-all,是显式的默认类型
    ({}, "whatever", "whatever"),
    ({"type": "integer"}, "48 Color", 48),        # 带量词也要能抽数
    ({"type": "integer"}, "24 Pack", 24),
    ({"type": "number"}, "1.5 x", 1.5),
    ({"type": "integer"}, "Kraft brown", None),
])
def test_coerce_never_passes_an_unknown_type_through(prop, val, want):
    """⚠ 首版最后一行是 `return val`,array/boolean 原样出去。

    `ensure_variant_bag` 是 conform 流水线里**最后一个**能改 visible 的类型闸
    (晚于 fix_type_mismatches / fix_invalid_enums),漏过去就直接发出去被拒。
    """
    assert mc._coerce_variant_value(prop, val) == want


# ── M5 属性全被剔时不接单品口径 ─────────────────────────────────────────────

def test_all_attrs_dropped_falls_through_to_the_single_item_branch():
    """⚠ spec 把三件套列**必填**的 PT,只 pop 掉三件套就 return 会让 validate
    每轮都报三件套缺失 ⇒ 这一行永远上不了架,而 note 却写着"退单品"。
    真正的退单品是回到单品口径:必填时用 SKU 占位补全。
    """
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "isPrimaryVariant": {"type": "string", "enum": ["Yes", "No"]},
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color"]}},
        "color": {"type": "string", "enum": ["Black"]}},
        "required": ["variantGroupId", "variantAttributeNames",
                     "isPrimaryVariant"]}
    p = vg.plan("A1", "color_name=Chartreuse", "A2", "P1", ["color"])
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert v["variantGroupId"] == "SKU1"           # 单品占位补上了
    assert v["isPrimaryVariant"] == "Yes"
    assert v["variantAttributeNames"] == ["color"]
    # 降级说明不许丢:静默降级 = 变体没生效而没人知道
    assert any("退单品口径" in n for n in notes)


def test_non_required_bag_is_still_removed_wholesale():
    """三件套非必填时仍是"整套剔除"(旧系统单品从不发变体字段)。"""
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color"]}},
        "color": {"type": "string", "enum": ["Black"]}}}
    p = vg.plan("A1", "color_name=Chartreuse", "A2", "P1", ["color"])
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert not any(k in v for k in mc._VARIANT_BAG)
    assert any("退单品口径" in n for n in notes)


# ── S2/S3 映射表的覆盖面 ────────────────────────────────────────────────────

def test_count_family_reaches_multipack_and_piece_count():
    """只收 multipackQuantity / pieceCount 的 PT 以前会整个维度丢失。"""
    assert vg.pick_walmart_dims(["number_of_items"],
                                ["multipackQuantity"]) == \
        [("number_of_items", "multipackQuantity")]
    assert vg.pick_walmart_dims(["unit_count"], ["pieceCount"]) == \
        [("unit_count", "pieceCount")]


def test_camel_fallback_catches_dims_outside_the_curated_table():
    """`_DIM_MAP` 是闭表;表外维度名与枚举同名时靠驼峰归一接住(零成本)。"""
    assert vg._camel("size_name") == "size"
    assert vg._camel("age_range") == "ageRange"
    assert vg.pick_walmart_dims(["age_range"], ["ageRange"]) == \
        [("age_range", "ageRange")]
    # 归一后仍不在枚举里的,照旧算映不上(交给错位重映射)
    assert vg.pick_walmart_dims(["age_range"], ["color"]) == []


# ── 加固:spec 没写 type: array 时也要读到变体枚举 ──────────────────────────

def test_variant_enum_is_read_regardless_of_declared_type():
    """⚠ `_type_of` 在 spec 没写 type 时默认 "string",于是通用 `_enum_of` 不去翻
    `items` ⇒ 该 PT 的变体枚举成了空 ⇒ **整批静默退单品**,而且看起来跟
    "这个类目不支持变体"一模一样,无从分辨。旧仓是无条件读 items.enum 的。
    """
    props = {"variantAttributeNames": {"items": {"enum": ["color", "size"]}}}
    assert mc.variant_attr_enum(props) == ["color", "size"]
    assert mc._enum_of(props["variantAttributeNames"]) is None   # 通用读法不变
    assert mc.variant_attr_enum({}) == []
    # 声明了 array 的正常形态照旧
    assert mc.variant_attr_enum({"variantAttributeNames": {
        "type": "array", "items": {"enum": ["color"]}}}) == ["color"]
