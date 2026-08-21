"""listing L2c 回归:PT spec 加载器、LLM JSON 提取与缓存键、MP_ITEM feed 收录。"""

import json
import re

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


def test_llm_thinking_disable_gated_by_flash_family(monkeypatch):
    """v4-flash 官方默认开 thinking,旧仓铁律显式关闭(所有者确认生产
    模型 2026-08-13);非 flash 家族不发该字段(未知字段可能被拒)。"""
    m = [{"role": "user", "content": "x"}]
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", "deepseek-v4-flash")
    body = llm._request_body(m, 0.2, 1500, "audit_l3")
    assert body["thinking"] == {"type": "disabled"}
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", "deepseek-chat")
    assert "thinking" not in llm._request_body(m, 0.2, 1500, "audit_l3")
    assert body["response_format"] == {"type": "json_object"}


def test_llm_model_for_purpose(monkeypatch):
    """批复 #1:env 逐用途覆盖,未配置回落默认;未登记用途 fail loud。"""
    monkeypatch.delenv("DEEPSEEK_MODEL_AUDIT_L1", raising=False)
    assert llm.model_for("audit_l1") == llm._default_model()          # 未配置回落
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L1", "deepseek-reasoner")
    assert llm.model_for("audit_l1") == "deepseek-reasoner"
    assert llm.model_for("default") == llm._default_model()
    with pytest.raises(ValueError, match="未登记的 LLM 用途"):
        llm.model_for("audit_l9")


def test_llm_cache_key_purpose_splits_keyspace(monkeypatch):
    """键内 model 经 model_for(purpose) 解析,与实际请求按构造同源;
    用途配了不同模型 → 键空间自动分离,配同模型 → 键相同(共享缓存)。"""
    m = [{"role": "user", "content": "审"}]
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", "deepseek-reasoner")
    assert llm_cache.cache_key(m, 0.2, 4096, purpose="audit_l3") \
        != llm_cache.cache_key(m, 0.2, 4096)
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", llm._default_model())
    assert llm_cache.cache_key(m, 0.2, 4096, purpose="audit_l3") \
        == llm_cache.cache_key(m, 0.2, 4096)


# ── MP_ITEM feed 收录 ─────────────────────────────────────────────────────────

def test_mp_item_payload_header_exactly_three_fields():
    # 实证:官方 sample 7 字段是错的,实际只收 3 个;version 必须完整时间戳
    p = feeds.build_payload("MP_ITEM", [
        {"Orderable": {"sku": "B0X"}, "Visible": {"Cups": {"productName": "n"}}}])
    assert set(p["MPItemFeedHeader"].keys()) == {"businessUnit", "locale",
                                                 "version"}
    # 版本串取自 registry 一处 —— 它同时决定 feed header 与 spec 目录,
    # 用例跟着 registry 走,别写死:写死了换版时这条会"通过"而生产已经不一致
    assert (p["MPItemFeedHeader"]["version"]
            == resources.FEED_SPEC_VERSIONS["MP_ITEM"])
    assert re.fullmatch(r"\d+\.\d+\.\d{8}-\d{2}_\d{2}_\d{2}-api",
                        p["MPItemFeedHeader"]["version"]), "必须完整时间戳"
    assert p["MPItem"][0]["Orderable"]["sku"] == "B0X"


def test_mp_item_chunk_skus_and_bucket():
    assert feeds._chunk_skus("MP_ITEM", [{"Orderable": {"sku": "B0X"}}]) == ["B0X"]
    from api import _client
    _client.rate_acquire("feeds.post.MP_ITEM", "cid_l2c_test")   # 已登记不抛
    assert product_events.feed_kind("MP_ITEM") == "list"
    assert product_events.receipt_in_ledger("list", "list_new")  # 生死类恒记


# ── LLM 缓存二级复用(2026-08-18 所有者定稿)────────────────────────────────

def test_prompt_attrs_drop_media_keys():
    """图片/视频 URL 不进提示词:系统禁止 LLM 输出媒体字段,进了纯是噪声,
    还让"慢采只刷新了图片列表"也打穿缓存 hash。其余属性原样保留。"""
    from services import mp_mapper
    msgs = mp_mapper.build_llm_messages("Cups", {"properties": {}}, {
        "title": "T", "brand": "B", "category": "C",
        "attrs": {"images": ["http://x/1.jpg"], "video_url": "http://x/v",
                  "manufacturer": "Acme", "weight": "1.2 lb"}})
    user = msgs[1]["content"]
    assert "1.jpg" not in user and "http://x/v" not in user
    assert "Acme" in user and "1.2 lb" in user


def test_title_spec_compatible_is_symmetric_not_membership():
    """所有者思路的修正版:只验"在旧标题命中过"的值;推断值(旧标题本就
    没有)跳过;数字 token 集合相等是双向护栏。"""
    from services import mp_mapper as m
    resp = {"visible": {"material": "Paper", "count": "48",
                        "color": "Brown"}, "orderable": {}}
    old = "Kraft Brown Gift Bags 48 Pcs"
    # 措辞变了但规格词/数字都在 → 复用("Paper" 是推断值,不验)
    assert m.title_spec_compatible(old, "Gift Bags, Brown, 48 Pcs, Bulk", resp)
    # 颜色词丢了(旧命中新未命中)→ 重打
    assert not m.title_spec_compatible(old, "Kraft White Gift Bags 48 Pcs", resp)
    # 数字变了(48→100)→ 重打;新增数字(48 且 100)也算变 → 重打
    assert not m.title_spec_compatible(old, "Kraft Brown Gift Bags 100 Pcs", resp)
    assert not m.title_spec_compatible(old, "Kraft Brown Bags 48 Pcs 100 Pack", resp)


def test_title_hit_word_boundary():
    """"4" 不许配进 "48";词匹配大小写不敏感、按词边界。"""
    from services import mp_mapper as m
    assert not m._title_hit("4", "Gift Bags 48 Pcs")
    assert m._title_hit("48", "Gift Bags 48 Pcs")
    assert m._title_hit("brown", "Dark Brown Bags")
    assert not m._title_hit("row", "Brown Bags")


def test_reuse_sig_ignores_title_pins_hard_inputs():
    """签名对文案钝感(标题变不换签名),对硬条件敏感:变体属性变了、
    spec 字段面变了都必须换签名(= 不许复用,直接重打)。"""
    from services import mp_mapper as m
    spec = {"properties": {"material": {"type": "string"}},
            "required": ["material"]}
    p1 = {"title": "旧标题", "brand": "B", "category": "C",
          "variant_attributes": "color_name=Red"}
    s1 = m.reuse_sig("Cups", spec, p1)
    assert s1 == m.reuse_sig("Cups", spec, {**p1, "title": "新标题"})
    assert s1 != m.reuse_sig("Cups", spec,
                             {**p1, "variant_attributes": "color_name=Blue"})
    spec2 = {"properties": {"material": {"type": "string"},
                            "capacity": {"type": "string"}},
             "required": ["material", "capacity"]}
    assert s1 != m.reuse_sig("Cups", spec2, p1)


class _FakeCur:
    def __init__(self, rows):
        self._rows, self.calls = list(rows), []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows=()):
        self.cur = _FakeCur(rows)

    def cursor(self):
        return self.cur


def test_llm_cache_put_and_find_reusable_metadata():
    """put 带二级元数据四列;find_reusable 取最近一条并解析 jsonb。"""
    conn = _FakeConn()
    llm_cache.put(conn, "K1", {"a": 1}, asin="B0X", pt="Cups",
                  src_title="T 48 Pcs", reuse_sig="SIG")
    sql, params = conn.cur.calls[0]
    assert "asin, pt, src_title, reuse_sig" in sql
    assert params[3:] == ("B0X", "Cups", "T 48 Pcs", "SIG")

    got = llm_cache.find_reusable(
        _FakeConn([( '{"visible": {"x": 1}}', "旧标题")]), "B0X", "Cups", "SIG")
    assert got == ({"visible": {"x": 1}}, "旧标题")
    assert llm_cache.find_reusable(_FakeConn(), "B0X", "Cups", "SIG") is None


def test_default_model_is_call_time_not_an_import_snapshot(monkeypatch):
    """缺省模型必须 **call-time 求值**(2026-08-21)。

    cli.py 的约定是「.env 先于一切业务 import 加载,registry 各函数
    call-time 求值即可拿到」。模块级 `os.environ.get` 是 import 快照,
    只在"import 恰好晚于 load_dotenv"时碰巧正确;换个入口就静默吃缺省值,
    而且看不出来 —— 只有账单和摘要里的模型名会变。
    """
    from api import llm

    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    assert llm._default_model() == "deepseek-chat"
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    assert llm._default_model() == "deepseek-v4-flash"      # 立刻生效
    monkeypatch.setenv("DEEPSEEK_MODEL", "   ")             # 空白视同未配置
    assert llm._default_model() == "deepseek-chat"


def test_legacy_alias_is_priced_and_called_out():
    """旧别名要能算出钱(折叠到正式产品名),同时在摘要里点名警告。

    停用日期(2026-07-24)已过还在用 = 随时可能整条链一起挂,而且不会预警。
    """
    from registry import resources
    from services import llm_cost

    assert resources.llm_priced_model("deepseek-chat") == "deepseek-v4-flash"
    row = {"calls": 1, "prompt": 0, "completion": 1_000_000,
           "cache_hit": 0, "cache_miss": 0}
    assert (llm_cost.cost_of("deepseek-chat", "peak", row)
            == llm_cost.cost_of("deepseek-v4-flash", "peak", row))
    out = "\n".join(llm_cost.summarize(
        {("deepseek-chat", "audit_l3", "peak"): row}))
    assert "已宣布停用的旧别名" in out and "deepseek-v4-flash" in out
    assert "无计价" not in out          # 别名不该再报"无计价"
