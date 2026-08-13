"""listing L2c 回归:PT spec 加载器、LLM JSON 提取与缓存键、MP_ITEM feed 收录。"""

import json

import pytest

from api import feeds, llm
from registry import resources
from services import llm_cache, product_events, pt_spec


# ── PT spec 加载器 ────────────────────────────────────────────────────────────

@pytest.fixture()
def spec_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    d = tmp_path / "specs" / "MP_ITEM" / resources.FEED_SPEC_VERSIONS["MP_ITEM"]
    d.mkdir(parents=True)
    (d / "_pt_index.json").write_text(
        json.dumps({"Cups": "Cups.json", "Ghost": "Ghost.json"}), "utf-8")
    (d / "_orderable.json").write_text(json.dumps({"properties": {"sku": {}}}),
                                       "utf-8")
    (d / "Cups.json").write_text(json.dumps({"properties": {"productName": {}}}),
                                 "utf-8")
    pt_spec.clear_caches()
    yield d
    pt_spec.clear_caches()


def test_pt_spec_loads_and_caches(spec_dir):
    assert "Cups" in pt_spec.known_pts()
    assert pt_spec.load_pt("Cups")["properties"] == {"productName": {}}
    assert pt_spec.load_pt("NoSuchPT") is None          # 未收录 PT 不炸,由调用方淘汰
    assert pt_spec.load_pt("Ghost") is None             # 索引有名文件缺失 → None
    assert pt_spec.orderable_spec()["properties"] == {"sku": {}}


def test_pt_index_tolerates_list_forms(spec_dir):
    # 生产实证 2026-08-07:旧拆分工具的 _pt_index.json 是 list 不是 dict
    idx_file = spec_dir / "_pt_index.json"
    idx_file.write_text(json.dumps(["Cups", "Other PT"]), "utf-8")
    pt_spec.clear_caches()
    assert pt_spec.known_pts() == {"Cups", "Other PT"}
    assert pt_spec.load_pt("Cups")["properties"] == {"productName": {}}  # 探测 Cups.json

    idx_file.write_text(json.dumps([{"pt": "Cups", "file": "Cups.json"}]), "utf-8")
    pt_spec.clear_caches()
    assert pt_spec.load_pt("Cups") is not None


def test_pt_filename_resolved_by_normalized_scan(spec_dir):
    # 生产实证:'3-in-1 Shampoo, Conditioner & Body Washes' 的清洗规则猜不中
    # → 不猜规则,按目录真实文件名规范化匹配(任何清洗规则都成立)
    weird_pt = "3-in-1 Shampoo, Conditioner & Body Washes"
    (spec_dir / "3-in-1 Shampoo- Conditioner - Body Washes.json").write_text(
        json.dumps({"properties": {"x": {}}}), "utf-8")
    (spec_dir / "_pt_index.json").write_text(json.dumps([weird_pt]), "utf-8")
    pt_spec.clear_caches()
    assert pt_spec.load_pt(weird_pt)["properties"] == {"x": {}}
    total, ok = pt_spec.coverage()
    assert (total, ok) == (1, 1)


def test_pt_spec_missing_dir_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    pt_spec.clear_caches()
    with pytest.raises(FileNotFoundError, match="MP_ITEM spec 未就位"):
        pt_spec.pt_index()
    pt_spec.clear_caches()


# ── LLM ──────────────────────────────────────────────────────────────────────

def test_llm_extract_json_tolerates_fences():
    assert llm._extract_json('{"a": 1}') == {"a": 1}
    assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._extract_json('前缀说明 {"a": {"b": 2}} 后缀') == {"a": {"b": 2}}
    with pytest.raises(ValueError, match="未找到 JSON"):
        llm._extract_json("没有对象")


def test_llm_cache_key_stable_and_order_independent():
    m = [{"role": "user", "content": "映射"}]
    k1 = llm_cache.cache_key(m, 0.2, 4096)
    k2 = llm_cache.cache_key(list(m), 0.2, 4096)
    assert k1 == k2 and len(k1) == 32
    assert llm_cache.cache_key(m, 0.3, 4096) != k1      # 温度参与键


def test_llm_model_for_purpose(monkeypatch):
    """批复 #1:env 逐用途覆盖,未配置回落默认;未登记用途 fail loud。"""
    monkeypatch.delenv("DEEPSEEK_MODEL_AUDIT_L1", raising=False)
    assert llm.model_for("audit_l1") == llm._MODEL          # 未配置回落
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L1", "deepseek-reasoner")
    assert llm.model_for("audit_l1") == "deepseek-reasoner"
    assert llm.model_for("default") == llm._MODEL
    with pytest.raises(ValueError, match="未登记的 LLM 用途"):
        llm.model_for("audit_l9")


def test_llm_cache_key_purpose_splits_keyspace(monkeypatch):
    """键内 model 经 model_for(purpose) 解析,与实际请求按构造同源;
    用途配了不同模型 → 键空间自动分离,配同模型 → 键相同(共享缓存)。"""
    m = [{"role": "user", "content": "审"}]
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", "deepseek-reasoner")
    assert llm_cache.cache_key(m, 0.2, 4096, purpose="audit_l3") \
        != llm_cache.cache_key(m, 0.2, 4096)
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", llm._MODEL)
    assert llm_cache.cache_key(m, 0.2, 4096, purpose="audit_l3") \
        == llm_cache.cache_key(m, 0.2, 4096)


# ── MP_ITEM feed 收录 ─────────────────────────────────────────────────────────

def test_mp_item_payload_header_exactly_three_fields():
    # 实证:官方 sample 7 字段是错的,实际只收 3 个;version 必须完整时间戳
    p = feeds.build_payload("MP_ITEM", [
        {"Orderable": {"sku": "B0X"}, "Visible": {"Cups": {"productName": "n"}}}])
    assert set(p["MPItemFeedHeader"].keys()) == {"businessUnit", "locale",
                                                 "version"}
    assert p["MPItemFeedHeader"]["version"] == "5.0.20260304-22_45_32-api"
    assert p["MPItem"][0]["Orderable"]["sku"] == "B0X"


def test_mp_item_chunk_skus_and_bucket():
    assert feeds._chunk_skus("MP_ITEM", [{"Orderable": {"sku": "B0X"}}]) == ["B0X"]
    from api import _client
    _client.rate_acquire("feeds.post.MP_ITEM", "cid_l2c_test")   # 已登记不抛
    assert product_events.feed_kind("MP_ITEM") == "list"
    assert product_events.receipt_in_ledger("list", "list_new")  # 生死类恒记
