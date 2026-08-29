"""变体维度错位重映射 + 同组标题差异化(旧仓 Phase 0.8 / Feature B 补迁回归)。

两条都是 2026-08-17 所有者批「都补」后迁入的。用例钉的是**三层的边界**与
**幂等**,不是"能跑"。
"""

import pytest

from services import variant_remap as vr
from services import variant_title as vt

_ENUM = ["color", "count", "multipackQuantity", "pieceCount", "size"]


# ── ① 枚举内检查在**调用方**(2026-08-27 定案):判据在
#    `list_new._remap_unmapped_dims`(`unmapped_dims` + `if not enum: continue`
#    + 全同/单成员两道过滤),本模块只留 ②③ 两个显式函数供它路由。
#    钉这一层的用例见本文件末尾"接线:list_new 那一段"。

# ── ② 内置错位表:四条件全满足才用 ───────────────────────────────────────────

def test_hardcoded_maps_stationery_color_to_piece_count():
    """旧仓原始案例:文具类目 color_name=48 Color 说的**不是颜色是件数**。"""
    got = vr.hardcoded("Art Sets", "color_name",
                       {"A1": "48 Color", "A2": "12 Colors"}, _ENUM)
    assert got == ("pieceCount", {"A1": 48, "A2": 12})


@pytest.mark.parametrize("pt,dim,values,enum,why", [
    ("Gift Bags", "color_name", {"A1": "48 Color"}, _ENUM, "PT 不在表里"),
    ("Art Sets", "size_name", {"A1": "48"}, _ENUM, "维度不在表里"),
    ("Art Sets", "color_name", {"A1": "48"}, ["color"], "候选属性名不在枚举里"),
    ("Art Sets", "color_name", {"A1": "48", "A2": "Red"}, _ENUM,
     "有成员的值解析不成数字"),
])
def test_hardcoded_returns_none_rather_than_half_a_mapping(pt, dim, values,
                                                           enum, why):
    """任一条件不满足就整份放弃交 LLM —— **半套映射比不映射更坏**(旧仓同款)。"""
    assert vr.hardcoded(pt, dim, values, enum) is None, why


@pytest.mark.parametrize("raw,want", [
    ("48 Color", 48), ("12 Colors", 12), ("24 Pack", 24), ("100 Pcs", 100),
    ("8", 8), ("6 Piece", 6), ("3 set", 3), ("5x", 5),
])
def test_numeric_shapes_from_legacy(raw, want):
    assert vr.hardcoded("Markers", "color_name", {"A": raw}, _ENUM)[1]["A"] \
        == want


# ── ③ LLM 兜底:校验不过一律降级 ─────────────────────────────────────────────

class _FakeCache:
    def __init__(self):
        self.store = {}


def _wire(monkeypatch, resp, cache=None, calls=None):
    cache = cache if cache is not None else {}
    monkeypatch.setattr(vr.llm_cache, "cache_key",
                        lambda m, t, mt, purpose="default":
                        m[0]["content"][:64])
    monkeypatch.setattr(vr.llm_cache, "get", lambda conn, k: cache.get(k))
    monkeypatch.setattr(vr.llm_cache, "put",
                        lambda conn, k, v, purpose=None: cache.__setitem__(k, v))

    def _chat(messages, **kw):
        if calls is not None:
            calls.append(messages)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(vr.llm, "chat_json", _chat)
    return cache


def test_llm_key_must_be_inside_the_enum(monkeypatch):
    """LLM 给了枚举外的名字 → 整份丢弃。发一个 PT 不认的属性名会让整条被拒。"""
    _wire(monkeypatch, {"walmart_key": "bagSize", "rationale": "x"})
    assert vr.llm_remap(object(), "Gift Bags", "size_name",
                        {"A1": "S", "A2": "L"}, _ENUM) is None


def test_llm_null_means_dont_force_it(monkeypatch):
    _wire(monkeypatch, {"walmart_key": None, "rationale": "语义对不上"})
    assert vr.llm_remap(object(), "Gift Bags", "size_name",
                        {"A1": "S"}, _ENUM) is None


def test_llm_failure_degrades_quietly_to_not_sending_that_dim(monkeypatch):
    """LLM 挂了只是"这个维度不发",不是整组退单品、更不是整行失败。"""
    _wire(monkeypatch, RuntimeError("boom"))
    assert vr.llm_remap(object(), "Gift Bags", "size_name",
                        {"A1": "S"}, _ENUM) is None


def test_llm_cache_key_ignores_sample_values(monkeypatch):
    """⚠ 缓存键按 (PT, 维度, 枚举) 定案,**不含样本值**。

    同一个变体组的 variantAttributeNames 必须跨兄弟/跨批次一致(旧仓设计文档
    自己强调的);按样本值缓存的话,分批上架时取值不同 ⇒ 不命中 ⇒ 同一个
    (PT, 维度) 可能被判成两个不同的 key,把已经建好的组弄坏。
    """
    calls = []
    cache = _wire(monkeypatch, {"walmart_key": "size", "rationale": "尺寸"},
                  calls=calls)
    a = vr.llm_remap(object(), "Gift Bags", "size_name",
                     {"A1": "Small", "A2": "Large"}, _ENUM)
    b = vr.llm_remap(object(), "Gift Bags", "size_name",
                     {"B1": "8 inch", "B2": "16 inch"}, _ENUM)   # 值完全不同
    assert a[0] == b[0] == "size"
    assert len(calls) == 1 and len(cache) == 1        # 第二次命中缓存
    # 但提示词里**要有**样本值(语义推断靠它)
    assert "Small" in calls[0][0]["content"]


# ── 接线:list_new 那一段 ────────────────────────────────────────────────────

def _lrow(asin, pt, attrs, pairs, unmapped, gid="vg_G"):
    return {"asin": asin, "store": "M001", "product_type": pt,
            "_p": {"variant_attributes": attrs},
            "_vplan": {"mode": "variant", "group_id": gid, "code": "variant",
                       "is_primary": True, "attr_pairs": list(pairs),
                       "unmapped_dims": list(unmapped), "family_size": 2}}


_SPEC_ART = {"properties": {
    "variantAttributeNames": {"type": "array",
                              "items": {"enum": ["pieceCount", "color"]}},
    "pieceCount": {"type": "integer"}}}


def test_wiring_keeps_mapped_dims_and_only_fills_the_unmapped(monkeypatch):
    """⚠ 与旧仓的关键差异:**只补映不上的,不推翻已经映上的**。

    旧仓 remap 一触发整组改用一个 key(提示词明写"所有兄弟共用同一个"),
    于是 color 映得上而 size 映不上的组会连 color 一起丢掉、退成单维。
    """
    from workflows import list_new as ln
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: _SPEC_ART)
    rows = [_lrow("A1", "Art Sets", "color_name=48 Color",
                  [("color", "Red")], ["color_name"]),
            _lrow("A2", "Art Sets", "color_name=12 Colors",
                  [("color", "Navy")], ["color_name"])]
    ln._remap_unmapped_dims(rows)
    for r in rows:
        names = [n for n, _ in r["_vplan"]["attr_pairs"]]
        assert names == ["color", "pieceCount"]      # 原有的还在,新的补上了
        assert r["_vplan"]["unmapped_dims"] == []
        assert r["_vplan"]["remapped_dims"] == ["color_name→pieceCount"]


def test_wiring_table_hit_dials_neither_the_llm_nor_the_database(monkeypatch):
    """表命中就到此为止:不问 LLM,**连接一次都不开**。

    2026-08-27 定案(路由收归调用方、删掉 services 里那个把两层串起来的
    `remap_dim`)之后,这条判据只剩接线侧一处 —— 原来由 remap_dim 的
    `conn is None` 分支各守一半,两套并存。
    """
    from workflows import list_new as ln
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: _SPEC_ART)
    monkeypatch.setattr(ln.variant_remap, "llm_remap",
                        lambda *a, **k: pytest.fail("表命中了还问 LLM"))
    monkeypatch.setattr(ln.db, "pg_conn",
                        lambda *a, **k: pytest.fail("表命中了还开连接"))
    rows = [_lrow("A1", "Art Sets", "color_name=48 Color",
                  [("color", "Red")], ["color_name"]),
            _lrow("A2", "Art Sets", "color_name=12 Colors",
                  [("color", "Navy")], ["color_name"])]
    ln._remap_unmapped_dims(rows)
    for r in rows:
        assert [n for n, _ in r["_vplan"]["attr_pairs"]] == ["color",
                                                             "pieceCount"]


def test_wiring_never_dials_llm_for_a_degenerate_dimension(monkeypatch):
    """本轮取值全同的维度**不送 LLM**(礼品袋组实证:size_name 三条全同)。

    送去问只会得到一个"看着对"的 key,然后成员们带着同一个值发出去 ——
    声明了没有差异的维度等于没声明,还白花一次调用。
    """
    from workflows import list_new as ln
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color",
                                                     "sizeDescriptor"]}}}})
    boom = lambda *a, **k: pytest.fail("不该问 LLM")   # noqa: E731
    monkeypatch.setattr(ln.variant_remap, "llm_remap", boom)
    same = "size_name=1 Count (Pack of 100)"
    rows = [_lrow("B1", "Gift Bags", same, [("color", "Brown")], ["size_name"]),
            _lrow("B2", "Gift Bags", same, [("color", "Navy")], ["size_name"])]
    ln._remap_unmapped_dims(rows)
    for r in rows:
        assert [n for n, _ in r["_vplan"]["attr_pairs"]] == ["color"]
        assert r["_vplan"]["degenerate_dims"] == ["size_name"]


def test_wiring_single_visible_member_uses_the_table_only(monkeypatch):
    """本轮只看见 1 个成员时判不了有没有差异 → 只走内置表,不问 LLM。"""
    from workflows import list_new as ln
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: _SPEC_ART)
    monkeypatch.setattr(ln.variant_remap, "llm_remap",
                        lambda *a, **k: pytest.fail("不该问 LLM"))
    rows = [_lrow("A1", "Art Sets", "color_name=48 Color", [], ["color_name"])]
    ln._remap_unmapped_dims(rows)
    assert rows[0]["_vplan"]["attr_pairs"] == [("pieceCount", 48)]


def test_wiring_refuses_to_write_two_dims_into_one_attribute(monkeypatch):
    """重映到一个**已被占用**的属性名 → 跳过。两个维度写同一个属性 = 载荷自相矛盾。"""
    from workflows import list_new as ln
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: _SPEC_ART)
    monkeypatch.setattr(ln.variant_remap, "hardcoded",
                        lambda *a, **k: ("color", {"A1": 1, "A2": 2}))
    rows = [_lrow("A1", "Art Sets", "color_name=48 Color",
                  [("color", "Red")], ["color_name"]),
            _lrow("A2", "Art Sets", "color_name=12 Colors",
                  [("color", "Navy")], ["color_name"])]
    ln._remap_unmapped_dims(rows)
    for r in rows:
        assert [n for n, _ in r["_vplan"]["attr_pairs"]] == ["color"]
        assert r["_vplan"].get("remapped_dims") in (None, [])


def test_titles_are_grouped_by_the_payload_group_id(monkeypatch):
    """分组键用**载荷里的** variantGroupId:mp_conform 可能已把三件套整套剔掉,
    那种行不该再被当成同组成员参与"标题是不是全同"的判定。"""
    from workflows import list_new as ln

    def _p(name, gid, color):
        v = {"productName": name, "variantAttributeNames": ["color"],
             "color": color}
        if gid:
            v["variantGroupId"] = gid
        return {"visible": v}

    prepped = [_p("Tee", "g1", "Red"), _p("Tee", "g1", "Navy"),
               _p("Tee", None, "Black")]      # 第三条已退单品,不参与
    assert ln._differentiate_titles(prepped) == 2
    assert prepped[2]["visible"]["productName"] == "Tee"


# ── Feature B:同组标题差异化 ────────────────────────────────────────────────

def _m(name, **attrs):
    v = {"productName": name, "variantAttributeNames": list(attrs)}
    v.update(attrs)
    return {"visible": v}


def test_titles_all_identical_get_dimension_suffix():
    members = [_m("Paper Gift Bags", color="Red"),
               _m("Paper Gift Bags", color="Navy")]
    assert vt.differentiate(members) == 2
    assert [m["visible"]["productName"] for m in members] == [
        "Paper Gift Bags - Red", "Paper Gift Bags - Navy"]


def test_multi_dim_suffix_follows_attribute_name_order():
    m = [_m("Tee", color="Red", size="M"), _m("Tee", color="Red", size="L")]
    vt.differentiate(m)
    assert m[0]["visible"]["productName"] == "Tee - Red, M"


def test_existing_differences_are_left_alone():
    """**只补差异,不统一格式** —— 亚马逊给的差异比我们拼的后缀好。"""
    m = [_m("Small Paper Gift Bags", color="Red"),
         _m("Large Paper Gift Bags", color="Navy")]
    assert vt.differentiate(m) == 0
    assert m[0]["visible"]["productName"] == "Small Paper Gift Bags"


def test_single_member_and_empty_titles_are_untouched():
    assert vt.differentiate([_m("Only One", color="Red")]) == 0
    assert vt.differentiate([_m("", color="Red"), _m("", color="Navy")]) == 0


def test_idempotent_across_reruns():
    """同一批重跑/失败重提不许叠成 ` - Red - Red`。"""
    m = [_m("Tee", color="Red"), _m("Tee", color="Navy")]
    vt.differentiate(m)
    first = [x["visible"]["productName"] for x in m]
    assert vt.differentiate(m) == 0          # 已经有差异了,第二轮不动
    assert [x["visible"]["productName"] for x in m] == first


def test_truncation_keeps_the_suffix_not_the_base():
    """超 199 字截 base 保 suffix —— 后缀才是差异所在,截掉它等于白做。"""
    long_name = "X" * 195
    m = [_m(long_name, color="Red"), _m(long_name, color="Navy")]
    vt.differentiate(m)
    for x, want in zip(m, ("Red", "Navy")):
        got = x["visible"]["productName"]
        assert len(got) <= vt.MAX_LEN and got.endswith(f" - {want}")


def test_no_attribute_value_means_no_suffix():
    """变体属性被 mp_conform 剔掉了(值不合 enum)→ 拼不出后缀就不动标题,
    不许拼出个 ` - ` 空尾巴。"""
    m = [{"visible": {"productName": "Tee",
                      "variantAttributeNames": ["color"]}},
         {"visible": {"productName": "Tee",
                      "variantAttributeNames": ["color"]}}]
    assert vt.differentiate(m) == 0
    assert m[0]["visible"]["productName"] == "Tee"
