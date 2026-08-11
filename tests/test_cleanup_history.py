"""旧问题商品历史导入:时间线折叠 + 三种旧格式解析 + 导入器纪律。

数据在生产 Mac 上,这里全部用构造数据钉转换规则——骨架先行,
数据到位后预览模式负责校准真实列名/JSON 形态。
"""

from datetime import datetime, timezone

import pytest

from services import cleanup_history as ch
from services import product_events as pe
from workflows import cleanup_history_import as wf


def _row(ts, store, sku, cat, reason=""):
    return {"run_ts": ts, "store": store, "sku": sku,
            "category": cat, "reason": reason}


# ── 时间线折叠(导入不膨胀的关键)────────────────────────────────────────────

def test_timeline_collapses_consecutive_same_category():
    """连续 N 轮报同一类错 = 1 条事件。41.7 万行 per-run 快照进来,
    落库的是变迁序列——这就是所有者拍的「时间线形态」。"""
    rows = [_row(1, "S", "B0A", "C"), _row(2, "S", "B0A", "C"),
            _row(3, "S", "B0A", "C"), _row(4, "S", "B0A", "E"),
            _row(5, "S", "B0A", "E"), _row(6, "S", "B0A", "C")]
    events, st = ch.timeline(rows)
    assert st == {"rows": 6, "kept": 3, "unknown_category": 0}
    assert [e["detail"]["category"] for e in events] == ["C", "E", "C"]
    assert [e["occurred_at"] for e in events] == [1, 4, 6]   # 变迁时刻,不是末次


def test_timeline_tracks_pairs_independently():
    """(store, sku) 各自独立折叠——A 店的 C 不吸收 B 店的 C。"""
    rows = [_row(1, "A", "B0A", "C"), _row(2, "B", "B0A", "C"),
            _row(3, "A", "B0A", "C")]
    events, st = ch.timeline(rows)
    assert st["kept"] == 2
    assert {(e["store"], e["detail"]["category"]) for e in events} == \
        {("A", "C"), ("B", "C")}


def test_timeline_keeps_unknown_and_empty_categories():
    """认不出的类别照样入时间线(码 NULL、原文随行)——丢掉它等于把
    「曾报过一种看不懂的错」从病历里抹掉。2026-05-14 前 category 为空的
    行也是合法历史。"""
    rows = [_row(1, "S", "B0A", ""), _row(2, "S", "B0A", "怪类别"),
            _row(3, "S", "B0A", "C")]
    events, st = ch.timeline(rows)
    assert st == {"rows": 3, "kept": 3, "unknown_category": 1}
    assert events[0]["detail"]["category"] is None
    assert events[0]["detail"]["raw_category"] is None          # 空 ≠ 未识别
    assert events[1]["detail"]["raw_category"] == "怪类别"       # 原文保住
    assert events[2]["detail"]["raw_category"] is None


def test_timeline_distinct_unknowns_do_not_collapse():
    """两个不同的未识别类别归一后都是 None,但折叠键用原文——
    「怪类别甲 → 怪类别乙」是真实变迁,拿 None 当键会把它吞掉。"""
    events, _ = ch.timeline([_row(1, "S", "B0A", "怪类别甲"),
                             _row(2, "S", "B0A", "怪类别乙"),
                             _row(3, "S", "B0A", "怪类别乙")])
    assert [e["detail"]["raw_category"] for e in events] == ["怪类别甲", "怪类别乙"]


def test_timeline_empty_to_known_is_a_transition():
    """空类别 → C 是一次真实变迁,不能因为起点是 NULL 就吞掉。"""
    events, _ = ch.timeline([_row(1, "S", "B0A", ""), _row(2, "S", "B0A", "C")])
    assert len(events) == 2


# ── 类别归一(码字母 / 中文名两种存法都认,认不出不猜)───────────────────────

@pytest.mark.parametrize("raw,want", [
    ("C", "C"), ("c", "C"), (" K ", "K"), ("Z", "Z"),
    ("品牌", "C"), ("知产", "E"), ("其他", "Z"), ("禁售", "B"),
    ("", None), (None, None), ("不存在的类", None),
])
def test_norm_category(raw, want):
    assert ch.norm_category(raw) == want


def test_code_name_maps_stay_in_sync_with_rules():
    """映射表是 problem_products._RULES 的投影——那边加类别这里自动跟上,
    抄死清单就会漂(事件码清单漂过一次了,别再来)。"""
    from services.problem_products import _RULES
    for code, (name, _) in _RULES.items():
        assert ch.CODE_TO_NAME[code] == name
        assert ch.NAME_TO_CODE[name] == code
    assert ch.CODE_TO_NAME["Z"] == "其他"


# ── 两个 JSON 的解析(形态待生产机实证,认不出抛错不猜)───────────────────────

def test_parse_seen_both_shapes():
    assert sorted(ch.parse_seen({"B0A": ["C", "E"], "B0B": "品牌"})) == \
        [("B0A", "C"), ("B0A", "E"), ("B0B", "C")]
    assert ch.parse_seen([["B0A", "C"], ["B0B", "E"]]) == \
        [("B0A", "C"), ("B0B", "E")]


def test_parse_seen_unknown_shape_raises_with_sample():
    with pytest.raises(ValueError, match="形态未识别"):
        ch.parse_seen("garbage")


def test_parse_brand_cache():
    processed, pending = ch.parse_brand_cache(
        {"processed_asins": ["B0A", "B0B"], "pending_batches": [{"id": 7}]})
    assert processed == ["B0A", "B0B"] and pending == [{"id": 7}]
    assert ch.parse_brand_cache({}) == ([], [])          # 真空文件:合法


def test_swapped_files_are_rejected_not_silently_empty():
    """seen/brand 两个 -p 参数传反必须当场炸(2026-08-11 生产实操差点踩到)。

    不炸的话两个解析器都会"成功"导入 0 条——累计数缺一笔、品牌防重缺一笔,
    摘要看着正常,没人知道。按形状指纹互相拒绝。
    """
    brand_shape = {"processed_asins": ["B0A"], "pending_batches": []}
    seen_shape = {"B0A": ["C"]}
    with pytest.raises(ValueError, match="传反"):
        ch.parse_seen(brand_shape)
    with pytest.raises(ValueError, match="传反"):
        ch.parse_brand_cache(seen_shape)


def test_seen_all_unrecognized_raises():
    """非空文件解析出 0 对 = 类别全认不出,报错别静默。"""
    with pytest.raises(ValueError, match="0 对"):
        ch.parse_seen({"B0A": ["不存在的类"]})


def test_run_rejects_missing_file_before_heavy_work(monkeypatch):
    """文件不存在要在**事件导入之前**拦住——实操中路径打错,48.5 万行
    导完才炸在 JSON 上,摘要全丢。早失败 = 不浪费一轮重活。"""
    monkeypatch.setattr(wf.db, "legacy_cleanup_conn",
                        lambda: pytest.fail("文件检查必须在连旧库之前"))
    out = wf.run({"seen": "/no/such/file.json"})
    assert out.startswith("⛔") and "/no/such/file.json" in out


# ── 导入器纪律 ────────────────────────────────────────────────────────────────

def test_record_many_carries_occurred_at():
    """历史事件必须带旧 run_ts 落库——占位符验证 coalesce(%s, now()) 在 SQL 里。"""
    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, seq):
            captured["sql"] = sql
            captured["rows"] = list(seq)

    class _Conn:
        def cursor(self): return _Cur()

    ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    pe.record_many(_Conn(), [{"sku": "B0A", "event": pe.PROBLEM_CATEGORIZED,
                              "source": wf.SOURCE, "occurred_at": ts}])
    assert "coalesce(%s, now())" in captured["sql"]
    assert captured["rows"][0][-1] == ts


def test_importer_wipes_only_its_own_source():
    """擦净重灌只删自己导入的行——实时链路写的 problem_categorized
    (source=problem_product_cleanup)一根手指都不能碰。
    只能断言 SQL 文本:夹具喂什么都盖不住 WHERE 子句少个条件。"""
    assert wf._WIPE_SQL.strip() == \
        "DELETE FROM catalog.product_events WHERE source = %s"
    assert wf.SOURCE == "cleanup_history_import"


def test_importer_column_probe_reports_actuals():
    """预设列对不上时必须把真实列名打出来——骨架先行,生产机上第一跑
    就是来校准列名的,报『缺列』不报『实际有什么』等于让人再跑一轮。"""
    class _Cur:
        def __init__(self, rows): self._rows = rows
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a): pass
        def fetchall(self): return self._rows
        def fetchone(self): return (0,)

    class _Legacy:
        def cursor(self, name=None):
            return _Cur([("run_ts",), ("shop",), ("sku",)])   # 列名对不上

    import workflows.cleanup_history_import as w
    cols, _ = w._probe(_Legacy())
    missing = w._NEED_COLS - cols
    assert "store" in missing and "category" in missing
