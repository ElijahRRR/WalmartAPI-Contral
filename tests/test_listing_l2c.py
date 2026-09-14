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


def test_llm_thinking_disable_gated_by_registry(monkeypatch):
    """DeepSeek 官方默认开 thinking,旧仓铁律显式关闭;**登记表说了才发**
    (2026-09-10 由 `"flash" in model` 子串匹配改成 registry.LLM_THINKING)。

    两个旧别名故意不登记 —— 官方没记过它们认不认这个字段,保持今天已验证的
    "不下发"行为,不替它们做假设。
    """
    m = [{"role": "user", "content": "x"}]
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", model)
        body = llm._request_body(m, 0.2, 1500, "audit_l3")
        assert body["thinking"] == {"type": "disabled"}, model
        assert body["response_format"] == {"type": "json_object"}
    monkeypatch.setenv("DEEPSEEK_MODEL_AUDIT_L3", "deepseek-chat")
    assert "thinking" not in llm._request_body(m, 0.2, 1500, "audit_l3")


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
    assert llm._default_model() == "deepseek-flash"
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    assert llm._default_model() == "deepseek-v4-pro"        # 立刻生效
    monkeypatch.setenv("DEEPSEEK_MODEL", "   ")             # 空白视同未配置
    assert llm._default_model() == "deepseek-flash"


def test_default_model_is_not_a_retired_alias():
    """缺省值不许是官方已宣布停用的旧别名,且必须能算出钱、能压掉思考模式。

    踩上去的后果不是"某个功能怪怪的",是**全仓 LLM 调用在别名切断当天
    一起失败**。缺省值 2026-09-10 由 v4-flash 切成 **deepseek-flash**
    (= V4.1 Flash 的正式 id,判据是 GET /models),这条用例是三件事的守门人:
    不是旧别名、算得出钱(经 llm_priced_model 折)、thinking 显式 disabled。
    """
    from registry import resources
    from api import llm

    assert llm._DEFAULT_MODEL == "deepseek-flash"
    assert llm._DEFAULT_MODEL not in resources.LLM_RETIRED_MODELS
    # 缺省值必须能算出钱 —— 经别名折算后要落在价表里
    assert (resources.llm_priced_model(llm._DEFAULT_MODEL)
            in resources.LLM_PRICING)
    # 缺省值必须触发 thinking disabled(DeepSeek 家族官方默认开 thinking)
    body = llm._request_body([{"role": "user", "content": "x"}], 0.2, 100,
                             "default")
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}


def test_thinking_switch_comes_from_the_registry_not_a_substring(monkeypatch):
    """2026-09-10:thinking 门控从 `"flash" in model` 子串匹配改成登记表。

    子串门控在缺省模型切成 `deepseek-v4-pro` 的那一刻**整条失效**(名字里
    没有 flash),而那正是它最该生效的时候。表里没有的模型不下发该字段
    (未知模型可能拒未知字段),但必须**点名警告**——静默跑在思考模式下会多花
    输出 token 且出参形状可能变。
    """
    import logging

    from registry import resources
    from api import llm

    # 登记过的正式产品名:一律显式 disabled(名字里有没有 flash 都一样)
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        monkeypatch.setenv("DEEPSEEK_MODEL", model)
        body = llm._request_body([{"role": "user", "content": "x"}], 0.2, 100,
                                 "default")
        assert body["thinking"] == {"type": "disabled"}, model

    # **判据是登记表不是名字**:名字里带 flash 但没登记的,一样不下发 ——
    # 旧的子串门控会给它发,这条差异就是"改成登记表"这件事本身
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v9-flash-unknown")
    llm._THINKING_WARNED.discard("deepseek-v9-flash-unknown")
    assert "thinking" not in llm._request_body(
        [{"role": "user", "content": "x"}], 0.2, 100, "default")

    # 未登记模型:不下发字段,且点名一次
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v9-unknown")
    llm._THINKING_WARNED.discard("deepseek-v9-unknown")
    logger = logging.getLogger("api.llm")
    seen: list = []
    h = logging.Handler()
    h.emit = lambda rec: seen.append(rec.getMessage())
    logger.addHandler(h)
    try:
        body = llm._request_body([{"role": "user", "content": "x"}], 0.2, 100,
                                 "default")
    finally:
        logger.removeHandler(h)
    assert "thinking" not in body
    assert any("未登记 thinking" in m and "deepseek-v9-unknown" in m
               for m in seen), seen
    # 每个模型只吵一次,不刷满日志
    assert "deepseek-v9-unknown" in llm._THINKING_WARNED
    assert resources.llm_thinking("deepseek-v9-unknown") is None


def test_every_model_we_actually_run_has_a_thinking_row():
    """我们真会去跑的模型(价表键 + 路由期键)都必须在 LLM_THINKING 里有一行。

    两张表分开维护,漏一行就是"某个模型静默跑在思考模式下"——它不会报错,
    只会悄悄多花输出钱。这条用例是那个漏项的守门人。
    ⚠ 退役名**故意不在范围内**:官方没记过它们认不认 `thinking`,
    保持今天已验证的"不下发"行为,不替它们做假设(见 LLM_THINKING 注释)。
    """
    from registry import resources

    known = set(resources.LLM_PRICING) | set(resources.LLM_ROUTED_MODELS)
    missing = known - set(resources.LLM_THINKING)
    assert not missing, f"这些模型缺 LLM_THINKING 登记:{sorted(missing)}"
    assert not (set(resources.LLM_THINKING)
                & set(resources.LLM_RETIRED_MODELS)), "退役名不该登记 thinking"


def test_official_routing_window_is_named_every_round():
    """官方路由期必须每轮进摘要,**并且分清生效前后**:生效前 v4-pro 是真 Pro
    价在扣钱(比 Flash 贵四倍多),2026-09-14 12:00(北京)起才按 Flash 计费。

    只说"实际跑 V4.1 Flash、按 Flash 计费"会让人以为现在就便宜了 —— 那是
    2026-09-10 漏掉生效日期时写下的话。路由期结束(V4.1 Pro 上线)单价与实际
    模型又会变,官方不来通知,所以让每轮摘要自己报当下这一段。
    """
    import datetime as dt
    from registry import resources
    from services import llm_cost

    row = {"calls": 1, "prompt": 0, "completion": 1_000_000,
           "cache_hit": 0, "cache_miss": 0}
    stats = {("deepseek-v4-pro", "audit_l3", "offpeak"): row}
    starts = resources.LLM_PRO_ROUTING_STARTS
    before = "\n".join(llm_cost.summarize(stats, 0, starts - dt.timedelta(days=1)))
    assert "官方路由期" in before and "2026-09-14 12:00" in before
    assert "¥13.50" in before                  # 生效前:Pro 自己的价(出 13.5)
    after = "\n".join(llm_cost.summarize(stats, 0, starts))
    assert "官方路由期" in after and "已生效" in after
    assert "¥4.00" in after                    # 生效后:按 Flash(出 4 元/百万)
    assert "LLM_ROUTED_MODELS" in after        # 复核动作指到具体那张表
    # 没用路由期模型的轮次不该出现这条提醒
    plain = "\n".join(llm_cost.summarize(
        {("deepseek-flash", "audit_l3", "offpeak"): row}))
    assert "官方路由期" not in plain


def test_legacy_alias_is_priced_and_called_out():
    """旧别名要能算出钱(折叠到正式产品名),同时在摘要里点名警告。

    停用日期(2026-07-24)已过还在用 = 随时可能整条链一起挂,而且不会预警。
    """
    from registry import resources
    from services import llm_cost

    assert resources.llm_priced_model("deepseek-chat") == "deepseek-flash"
    row = {"calls": 1, "prompt": 0, "completion": 1_000_000,
           "cache_hit": 0, "cache_miss": 0}
    assert (llm_cost.cost_of("deepseek-chat", "peak", row)
            == llm_cost.cost_of("deepseek-flash", "peak", row))
    out = "\n".join(llm_cost.summarize(
        {("deepseek-chat", "audit_l3", "peak"): row}))
    assert "已退役的模型名" in out and "deepseek-flash" in out
    assert "无计价" not in out          # 别名不该再报"无计价"


def test_cache_key_folds_alias_but_still_separates_real_models(monkeypatch):
    """键里必须有 model,但**换标签不算换模型**(2026-08-21 所有者提问)。

    · 不含 model = "换了模型还吃旧模型的出参",而且不报错 —— 换模型多半正是
      为了换答案质量,拿旧答案顶上把这件事整个抵消掉;
    · 但 deepseek-chat 只是 deepseek-v4-flash(非思考)的旧别名,**同一个模型**,
      折叠进同一个键空间(这对存量缓存是历史资产,不因两者都已退役而改);
    · deepseek-reasoner 是**思考模式**,行为不同,**必须**另分键空间 ——
      计价可以合并(同一张价表),缓存身份不行;
    · **deepseek-flash(V4.1)与 deepseek-v4-flash(V4)是两个模型**,名字只差
      一个版本号,键空间必须分开 —— 共用就是拿 V4 的答案冒充 V4.1。
    """
    from registry import resources
    from services import llm_cache

    m = [{"role": "user", "content": "映射"}]
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    k_alias = llm_cache.cache_key(m, 0.2, 4096)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    assert llm_cache.cache_key(m, 0.2, 4096) == k_alias      # 同一个模型,同一个键
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    assert llm_cache.cache_key(m, 0.2, 4096) != k_alias      # 真换模型,换键空间
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    assert llm_cache.cache_key(m, 0.2, 4096) != k_alias      # 思考模式,不许共用
    # 折叠只影响缓存身份,不影响计价:reasoner 仍走 v4-flash 那张价表
    assert resources.llm_priced_model("deepseek-reasoner") == "deepseek-flash"
    assert resources.llm_cache_model("deepseek-reasoner") == "deepseek-reasoner"
    # V4.1 与 V4 只差一个版本号,键空间必须分开(2026-09-10 切模型的核心前提)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    assert llm_cache.cache_key(m, 0.2, 4096) != k_alias
    assert resources.llm_cache_model("deepseek-flash") == "deepseek-flash"


def test_model_ids_track_the_models_endpoint_not_the_docs_page():
    """模型名的唯一判据是 `GET /models` 的返回,不是官方定价页。

    所有者 2026-09-10 实测原始输出:
        HTTP 200
        {"object":"list","data":[
          {"id":"deepseek-flash","object":"model","owned_by":"deepseek"},
          {"id":"deepseek-v4-pro","object":"model","owned_by":"deepseek"}]}
    只有这两个可调。同一天上午的定价页却还列着 deepseek-v4-flash /
    deepseek-v4-flash-vision-exp、更新日志最新一条还是 8/21 —— **文档站滞后于
    线上**(当天 15:21 复核时页面才追上,只剩这两列)。所以价表键与 thinking
    登记都必须是这两个,退役名只留在折算表里 —— 判据永远是 /models,不是页面。
    """
    from registry import resources
    from api import llm

    callable_ids = {"deepseek-flash", "deepseek-v4-pro"}
    assert set(resources.LLM_THINKING) == callable_ids
    assert set(resources.LLM_PRICING) <= callable_ids
    assert llm._DEFAULT_MODEL in callable_ids
    # 退役名一个都不许出现在这两张"当前可用"的表里
    assert not (set(resources.LLM_RETIRED_MODELS)
                & (set(resources.LLM_PRICING) | set(resources.LLM_THINKING)))
    # 但退役名必须还能折算出钱(历史用量要算得出来)
    for m in resources.LLM_RETIRED_MODELS:
        assert resources.llm_priced_model(m) in resources.LLM_PRICING, m


def test_thinking_field_rejection_degrades_once_and_loudly(monkeypatch):
    """`deepseek-flash` 认不认 thinking 字段官方无文档、无法预先实测。

    万一它拒收,不能让一个字段把全链 LLM 调用挂掉:**400 且报文提到 thinking**
    ⇒ 摘掉该字段重发一次、告警、本进程内不再下发(兜底三要件:同函数内 /
    记日志 / 条件明确非 catch-all)。其它 400 一律照旧抛错,不许借道降级。
    """
    import logging

    import httpx as _httpx
    from api import llm

    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-secret")
    llm._THINKING_REJECTED.discard("deepseek-flash")
    sent: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(dict(json))          # 记副本:请求体是原地摘字段的同一个对象
        if "thinking" in json:
            return _httpx.Response(400, text='{"error":{"message":"thinking not supported"}}')
        return _httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    seen: list = []
    h = logging.Handler()
    h.emit = lambda rec: seen.append(rec.getMessage())
    logging.getLogger("api.llm").addHandler(h)
    try:
        assert llm.chat_json([{"role": "user", "content": "x"}]) == {"ok": 1}
    finally:
        logging.getLogger("api.llm").removeHandler(h)

    assert len(sent) == 2                        # 第一次带字段被拒,第二次摘掉
    assert "thinking" in sent[0] and "thinking" not in sent[1]
    assert any("拒收 thinking" in m for m in seen), seen
    assert "deepseek-flash" in llm._THINKING_REJECTED
    # 记住之后,后续请求体里不再出现该字段
    assert "thinking" not in llm._request_body(
        [{"role": "user", "content": "x"}], 0.2, 100, "default")

    # 其它 400 不许借这条路降级 —— 照旧抛错
    llm._THINKING_REJECTED.discard("deepseek-flash")
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _httpx.Response(
        400, text='{"error":{"message":"invalid max_tokens"}}'))
    with pytest.raises(ValueError, match="LLM 请求被拒 HTTP 400"):
        llm.chat_json([{"role": "user", "content": "x"}], max_retries=1)
    assert "deepseek-flash" not in llm._THINKING_REJECTED
