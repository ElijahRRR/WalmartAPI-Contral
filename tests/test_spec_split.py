"""450MB 单文件 spec 的流式拆分。

背景(docs/legacy_survey.md:1535/1665):官方 MP_ITEM v5 是一个 450MB 单 JSON,
`json.load` 膨胀成约 1.3GB Python 对象——旧系统跑 5048 行 xlsx 时 RSS 飙到
12GB 直接 OOM。旧仓 tools/split_mp_item_spec.py 拆成一目录小文件,**加载器
迁过来了、拆分工具没有**,于是换版就卡在这一步。
"""

import json

import pytest

from services import spec_split as ss


def _spec(pts: dict, orderable=None, header=None) -> bytes:
    return json.dumps({
        "properties": {
            "MPItemFeedHeader": header if header is not None else {"h": 1},
            "MPItem": {"type": "array", "items": {"properties": {
                "Orderable": orderable or {"required": ["sku"], "properties": {"sku": {}}},
                "Visible": {"properties": pts}}}},
        }}).encode("utf-8")


# ── 括号配对必须跳过字符串内部 ────────────────────────────────────────────

def test_braces_inside_strings_do_not_break_slicing():
    """spec 里满是描述文本,任何一句带 `}` 的都会让整份切错位 —— 而切出来的
    **还是合法 JSON**,错得看不出来,只会表现为某个 PT 的字段莫名其妙。"""
    raw = _spec({"Widgets": {"required": ["a"], "desc": '含 } 和 { 还有 "引号" 的说明'}})
    got = ss.slice_json(raw, ss.walk_path(raw, ss.PT_PATH))
    assert got["Widgets"]["required"] == ["a"]
    assert "引号" in got["Widgets"]["desc"]


def test_escaped_quote_in_string():
    raw = _spec({"W": {"d": 'a"b}c'}})     # 序列化后是 a\"b}c,转义引号后面还跟着 }
    assert ss.slice_json(raw, ss.walk_path(raw, ss.PT_PATH))["W"]["d"] == 'a"b}c'


# ── 结构走不通要**带着实际键名**报错,不许返回空 ──────────────────────────

def test_missing_path_reports_actual_keys():
    """静默返回空的代价:调用方会当成"官方一个 PT 都没有",拆出 0 个文件
    还一切正常。官方改层级名是常事,报出实际键名才能一眼改对。"""
    raw = json.dumps({"properties": {"MPItemFeedHeader": {}, "Something": {}}}).encode()
    with pytest.raises(KeyError) as e:
        ss.walk_path(raw, ss.PT_PATH)
    msg = str(e.value)
    assert "MPItem" in msg and "MPItemFeedHeader" in msg and "Something" in msg


# ── 逐成员遍历只切片、不解析值 ────────────────────────────────────────────

def test_iter_members_yields_spans_without_parsing():
    raw = _spec({"A": {"required": ["x"]}, "B": {"required": ["y"]}})
    span = ss.walk_path(raw, ss.PT_PATH)
    got = {k: json.loads(raw[vs:ve].decode()) for k, vs, ve in ss.iter_members(raw, span[0])}
    assert got["A"]["required"] == ["x"] and got["B"]["required"] == ["y"]


def test_iter_members_on_empty_object():
    assert list(ss.iter_members(b"{}", 0)) == []


def test_scan_value_handles_nested_arrays_and_scalars():
    buf = b'{"a": [1, {"b": [2]}], "c": true, "d": null, "e": -1.5e3}'
    got = {k: json.loads(buf[vs:ve].decode()) for k, vs, ve in ss.iter_members(buf, 0)}
    assert got == {"a": [1, {"b": [2]}], "c": True, "d": None, "e": -1500.0}


# ── 文件名清洗:PT 名里有逗号、斜杠、&、引号 ──────────────────────────────

def test_safe_filename_keeps_readable_and_strips_path_chars():
    assert ss.safe_filename("3-in-1 Shampoo, Conditioner & Body Washes") \
        == "3-in-1 Shampoo, Conditioner & Body Washes.json"
    assert "/" not in ss.safe_filename("A/B Widgets")
    assert ss.safe_filename('Say "Hi"').endswith(".json")


# ── 整链:拆 → 用真加载器读回来 ───────────────────────────────────────────

def test_split_then_load_roundtrip(tmp_path, monkeypatch):
    from services import pt_spec
    from workflows import spec_split as wf
    src = tmp_path / "5.0.20260608-18_15_07-api_MP_ITEM_0_0_en.json"
    src.write_bytes(_spec({"Widgets": {"required": ["a", "b"], "properties": {"a": {}, "b": {}}},
                           "Gad, gets": {"required": ["c"], "properties": {"c": {}}}}))
    out = tmp_path / "split"
    msg = wf.run({"src": str(src), "out": str(out)})
    assert "发现 PT 2 个" in msg and "自检:索引 2 个 PT,拆分文件解析到 2 个" in msg
    try:
        pt_spec.use_spec_dir(str(out))
        assert pt_spec.known_pts() == {"Widgets", "Gad, gets"}
        assert pt_spec.load_pt("Gad, gets")["required"] == ["c"]
        assert set(pt_spec.orderable_spec()["required"]) == {"sku"}
    finally:
        pt_spec.use_spec_dir(None)
    # 进程绝不能被留在新版 spec 上(上架链会拿新版去过旧版 header 的校验)
    assert pt_spec._OVERRIDE_DIR is None


def test_refuses_to_overwrite_the_live_spec_dir(tmp_path):
    """拆进上架链正在用的那份目录 = 拆坏了当场上不了架。"""
    from registry import paths
    from workflows import spec_split as wf
    src = tmp_path / "5.0.20260608-18_15_07-api_MP_ITEM_0_0_en.json"
    src.write_bytes(_spec({"W": {"required": ["a"]}}))
    with pytest.raises(ValueError, match="上架链正在用"):
        wf.run({"src": str(src), "out": str(paths.mp_item_spec_dir())})


def test_refuses_nonempty_dir_without_replace(tmp_path):
    from workflows import spec_split as wf
    src = tmp_path / "5.0.20260608-18_15_07-api_MP_ITEM_0_0_en.json"
    src.write_bytes(_spec({"W": {"required": ["a"]}}))
    out = tmp_path / "out"
    out.mkdir()
    (out / "旧东西.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="replace=1"):
        wf.run({"src": str(src), "out": str(out)})


def test_dry_run_writes_nothing(tmp_path):
    from workflows import spec_split as wf
    src = tmp_path / "5.0.20260608-18_15_07-api_MP_ITEM_0_0_en.json"
    src.write_bytes(_spec({"W": {"required": ["a"]}}))
    out = tmp_path / "out"
    msg = wf.run({"src": str(src), "out": str(out), "dry_run": True})
    assert "一个文件都没写" in msg and not out.exists()
    assert "内存峰值约等于它" in msg      # 提醒:峰值与文件总大小无关
