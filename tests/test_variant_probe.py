"""variant_probe 回归:字面提示只提候选,不做判定。

这条工作流的全部价值在于**把三头分开**(采集侧给了什么 / 我们怎么映 / PT 允许
什么)。它一旦把"字面相近"当成判定,就会重演旧仓 Phase 0.8 那个坑:文具类目的
`color_name=48 Color` 其实是件数,字面看着像颜色。
"""

from workflows import variant_probe as vp


def test_hints_are_literal_and_ordered_by_usability():
    """字面相近的枚举名按"能不能写值"排序:properties 里没定义的排后面。

    枚举里有、properties 里没有的属性,发出去是"说按它分组却写不了值" ——
    这种候选不该排在人眼看到的第一个。
    """
    enum = ["color", "bagSize", "assembledProductLength", "count"]
    props = {"color", "count", "bagSize"}          # assembledProductLength 无定义
    got = vp._hints("size_name", enum, props)
    assert got == ["bagSize", "assembledProductLength"]


def test_hints_return_empty_rather_than_guessing():
    """一个都不相近就返回空 —— 报"这个 PT 可能真不按这个维度分组",不硬凑。"""
    assert vp._hints("size_name", ["color", "count"], {"color", "count"}) == []
    assert vp._hints("没登记的维度", ["color"], {"color"}) == []


def test_requires_a_target():
    """既没给 asins 也没给 pt 时宁炸不吞(否则输出一个空报告像"查过了没问题")。"""
    import pytest
    with pytest.raises(ValueError, match="asins"):
        vp.run({})


def test_probe_pt_distinguishes_three_failure_modes(monkeypatch):
    """取不到 spec / PT 不支持变体 / 支持但我们映不上 —— 三种成因修法不同,不许混。"""
    monkeypatch.setattr(vp.pt_spec, "load_pt", lambda pt: None)
    assert "取不到 PT" in "\n".join(vp._probe_pt("Whatever"))

    monkeypatch.setattr(vp.pt_spec, "load_pt",
                        lambda pt: {"properties": {"color": {}}})
    assert "没有 variantAttributeNames 枚举" in "\n".join(vp._probe_pt("X"))

    monkeypatch.setattr(vp.pt_spec, "load_pt", lambda pt: {"properties": {
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["bagSize"]}},
        "bagSize": {"type": "string"}}})
    out = "\n".join(vp._probe_pt("Gift Bags"))
    assert "允许的变体维度 1 个" in out and "一个都映不上" in out
