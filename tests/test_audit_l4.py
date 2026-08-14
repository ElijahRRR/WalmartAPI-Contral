"""L4 视觉审核单测(离线:不打网,vision_json / fetch_images / httpx 全部 mock)。

覆盖:选图与归一、aggressive 双作用、verdict 本地重算(忽略模型 verdict)、
confidence 严格大小写(已知缺陷钉住)、image_url 回填、五条失败路径全部 pass
(绝无 pending)、部分图失败留痕、api/llm_vision 的 env/装配/重试。
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from api import llm_vision
from services import audit_l4
from services.audit_models import L1Info, ProductInfo


@dataclass
class FakeL3:
    reason_text: str | None = None


def _product(**kw) -> ProductInfo:
    base = dict(asin="B0TEST0001", title="Plain Kitchen Towel Set",
                brand="Acme", bullet_points=["b1", "b2", "b3", "b4"])
    base.update(kw)
    return ProductInfo(**base)


def _l1(pt=None, cat=None) -> L1Info:
    return L1Info(walmart_product_type=pt, walmart_category=cat)


@pytest.fixture
def vision(monkeypatch):
    """mock 掉取图与视觉调用,记录入参。"""
    calls = {"fetch": [], "vision": []}

    def fake_fetch(urls):
        calls["fetch"].append(list(urls))
        return [(u, f"data:image/jpeg;base64,{i}") for i, u in enumerate(urls)]

    def fake_vision(images_b64, **kw):
        calls["vision"].append({"images": list(images_b64), **kw})
        return calls.get("reply", {"verdict": "pass", "image_issues": []})

    monkeypatch.setattr(llm_vision, "fetch_images", fake_fetch)
    monkeypatch.setattr(llm_vision, "vision_json", fake_vision)
    return calls


# ---------------------------------------------------------------- 常量与提示词


def test_constants_pinned():
    assert audit_l4.L4_TEMPERATURE == 0.0        # 判定必须确定性
    assert audit_l4.L4_MAX_TOKENS == 1024
    assert audit_l4.L4_MAX_IMAGES == 5
    assert llm_vision.IMG_DL_CONCURRENCY == 30
    assert llm_vision.IMG_DL_TIMEOUT == 8.0
    # 视觉请求 120s / 连接 10s(不是文本链的 180s)
    assert llm_vision._TIMEOUT.read == 120 and llm_vision._TIMEOUT.connect == 10


def test_addon_starts_with_blank_line():
    """addon 以换行开头 —— 拼接处的空行是提示词字节契约。"""
    assert audit_l4.L4_AGGRESSIVE_ADDON.startswith("\n\n---\n")
    assert audit_l4.L4_SYSTEM_PROMPT.startswith("你是沃尔玛 Marketplace 视觉合规审核 AI")
    assert audit_l4.L4_SYSTEM_PROMPT.rstrip().endswith("未识别的图不要编造 issue")


def test_l4_does_not_use_llm_cache():
    """合同 L4-4:L4 不走 llm_cache。"""
    src = Path(audit_l4.__file__).read_text(encoding="utf-8")
    assert "from services import llm_cache" not in src
    assert "llm_cache.get(" not in src and "llm_cache.put(" not in src


def test_user_prompt_field_widths():
    p = _product(title="T" * 250, brand="", bullet_points=["a", "b", "c", "d"])
    txt = audit_l4.build_l4_user_prompt(p, _l1(), FakeL3("R" * 200))
    assert "标题: " + "T" * 200 + "\n" in txt
    assert "品牌字段: (空)" in txt
    assert "Walmart PT: (unknown)" in txt
    assert "Walmart 类目: (空)" in txt
    assert "  - a\n  - b\n  - c\n" in txt and "  - d" not in txt
    assert "L3 reason: " + "R" * 150 + "\n" in txt
    assert "{img_count}" in txt          # 字面占位符,由 judge_l4 替换


def test_user_prompt_no_bullets_and_no_l3():
    txt = audit_l4.build_l4_user_prompt(_product(bullet_points=[]), _l1(), None)
    assert "  (无)" in txt
    assert "L3 文本审已标注" not in txt


# ---------------------------------------------------------------- 选图


def test_select_images_order_dedupe_and_limit():
    urls = [f"http://img/{i}.jpg" for i in range(7)]
    picked = audit_l4.select_l4_images(urls + [urls[0]])
    assert picked == urls[:5]            # 原序 + 去重 + ≤5


def test_select_images_normalizes_and_explodes():
    picked = audit_l4.select_l4_images([
        "http://img/a._AC_SL1500_.\nhttp://img/b?x=1",
        "ftp://nope/c.jpg", "", {"src": "http://img/d"},
        {"url": "http://img/e.jpg", "is_main": True}, 42,
    ])
    # is_main 的排在最前(旧仓分桶);裸串/无扩展名补 .jpg;非 http 丢弃
    assert picked == ["http://img/e.jpg", "http://img/a._AC_SL1500_.jpg",
                      "http://img/b.jpg", "http://img/d.jpg"]


def test_select_images_empty():
    assert audit_l4.select_l4_images(None) == []
    assert audit_l4.select_l4_images([{"url": None}, {"nope": 1}]) == []


# ---------------------------------------------------------------- aggressive


@pytest.mark.parametrize("pt,cat,title,expect", [
    ("Stickers", None, "Plain towel", True),          # PT 关键词
    (None, "Seasonal Decor", "Plain towel", True),     # 类目关键词
    (None, None, "Adult Halloween Costume", True),     # title 关键词
    (None, None, "50 Pcs Stickers Pack for Laptop", True),   # 大杂烩贴纸
    (None, None, "12 Pcs Stickers Pack", False),       # <20 张不算
    ("Kitchen Towels", "Home", "Plain towel", False),
])
def test_should_aggressive_offensive(pt, cat, title, expect):
    assert audit_l4.should_aggressive_offensive(
        _l1(pt, cat), _product(title=title)) is expect


def test_aggressive_appends_addon_and_lowers_threshold(vision):
    vision["reply"] = {"verdict": "pass", "image_issues": [
        {"image_index": 0, "issue": "offensive", "confidence": "medium"}]}
    out = audit_l4.judge_l4(_product(), _l1(pt="Stickers"),
                            images=["http://img/a.jpg"])
    assert out.verdict == "reject"       # aggressive:offensive medium 也算命中
    assert audit_l4.L4_AGGRESSIVE_ADDON in vision["vision"][0]["system_prompt"]
    hit = [h for h in out.hits if h.rule_code == "l4_vision_violation"][0]
    assert hit.detail["aggressive_offensive"] is True
    assert hit.detail["aggressive_escalated"] is True


def test_same_issue_without_aggressive_is_pass(vision):
    vision["reply"] = {"verdict": "reject", "image_issues": [
        {"image_index": 0, "issue": "offensive", "confidence": "medium"}]}
    out = audit_l4.judge_l4(_product(), _l1(pt="Kitchen Towels"),
                            images=["http://img/a.jpg"])
    assert out.verdict == "pass"
    assert audit_l4.L4_AGGRESSIVE_ADDON not in vision["vision"][0]["system_prompt"]


# ---------------------------------------------------------------- 判定


def test_model_verdict_is_ignored_reject_to_pass(vision):
    """钉住:模型自报 verdict=reject 但全是 low → 本地重算为 pass。"""
    vision["reply"] = {"verdict": "reject", "image_issues": [
        {"image_index": 0, "issue": "big_brand_logo", "confidence": "low"}]}
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    assert out.verdict == "pass"
    assert out.hits == []
    assert len(out.image_issues) == 1    # 全量 issues 仍返回(pass 也可能非空)


def test_model_verdict_is_ignored_pass_to_reject(vision):
    vision["reply"] = {"verdict": "pass", "image_issues": [
        {"image_index": 0, "issue": "cartoon_ip", "confidence": "high"}],
        "reasoning": "Mickey"}
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    assert out.verdict == "reject"
    hit = out.hits[0]
    assert (hit.stage, hit.rule_code, hit.penalty) == ("L4", "l4_vision_violation", -100)
    assert list(hit.detail) == ["issues", "all_issues", "reasoning", "images_scanned",
                                "aggressive_offensive", "aggressive_escalated",
                                "images_fetched", "images_total"]
    assert hit.detail["reasoning"] == "Mickey" and hit.detail["images_scanned"] == 1


def test_confidence_is_case_sensitive_known_defect(vision):
    """已知缺陷照迁(spec §6.1 / 合同 L4-7):模型回 'High' 不算命中。"""
    vision["reply"] = {"verdict": "reject", "image_issues": [
        {"image_index": 0, "issue": "big_brand_logo", "confidence": "High"}]}
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    assert out.verdict == "pass"


def test_issues_get_image_url_and_keep_index(vision):
    vision["reply"] = {"image_issues": [
        {"image_index": 1, "issue": "celebrity", "confidence": "high"},
        {"image_index": 9, "issue": "celebrity", "confidence": "low"},
        {"issue": "no index", "confidence": "low"},
        "not-a-dict",
    ]}
    out = audit_l4.judge_l4(_product(), _l1(),
                            images=["http://img/a.jpg", "http://img/b.jpg"])
    assert out.image_issues[0]["image_index"] == 1               # 旧键保留
    assert out.image_issues[0]["image_url"] == "http://img/b.jpg"  # 新键补齐
    assert "image_url" not in out.image_issues[1]                # 越界不编造
    assert "image_url" not in out.image_issues[2]
    assert out.image_issues[3] == "not-a-dict"


def test_img_count_uses_fetched_not_candidates(monkeypatch, vision):
    monkeypatch.setattr(llm_vision, "fetch_images",
                        lambda urls: [(urls[0], "data:image/jpeg;base64,0")])
    audit_l4.judge_l4(_product(), _l1(),
                      images=[f"http://img/{i}.jpg" for i in range(3)])
    assert "请逐张检查以下 1 张产品图" in vision["vision"][0]["user_prompt"]


def test_partial_download_continues_with_trace(monkeypatch, vision):
    monkeypatch.setattr(llm_vision, "fetch_images",
                        lambda urls: [(urls[0], "d0"), (urls[2], "d2")])
    vision["reply"] = {"image_issues": [
        {"image_index": 1, "issue": "cartoon_ip", "confidence": "high"}]}
    out = audit_l4.judge_l4(_product(), _l1(),
                            images=[f"http://img/{i}.jpg" for i in range(3)])
    partial = [h for h in out.hits if h.rule_code == "l4_images_partial"][0]
    assert partial.penalty == 0
    assert partial.detail == {"images_fetched": 2, "images_total": 3}
    violation = [h for h in out.hits if h.rule_code == "l4_vision_violation"][0]
    assert violation.detail["images_fetched"] == 2
    assert violation.detail["images_total"] == 3
    # image_index=1 对齐的是**实际发送**的第二张(原第 3 张)
    assert out.image_issues[0]["image_url"] == "http://img/2.jpg"


def test_vision_call_params(vision):
    audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    call = vision["vision"][0]
    assert call["temperature"] == 0.0 and call["max_tokens"] == 1024
    assert call["images"] == ["data:image/jpeg;base64,0"]


def test_at_most_five_images_sent(vision):
    audit_l4.judge_l4(_product(), _l1(),
                      images=[f"http://img/{i}.jpg" for i in range(9)])
    assert len(vision["fetch"][0]) == 5


# ---------------------------------------------------------------- 五条失败路径


def _assert_pass_alert(out, rule_code):
    """合同 L4-8:失败一律 pass + penalty=0 告警 hit,**绝无 pending**。"""
    assert out.verdict == "pass"
    assert out.verdict != "pending"
    assert out.image_issues == []
    codes = [h.rule_code for h in out.hits]
    assert rule_code in codes
    assert all(h.penalty == 0 and h.stage == "L4" for h in out.hits)


def test_f1_no_images(vision):
    out = audit_l4.judge_l4(_product(), _l1(), images=[])
    _assert_pass_alert(out, "l4_no_images")
    assert vision["vision"] == []        # 零视觉调用


def test_f2_all_downloads_failed(monkeypatch, vision):
    monkeypatch.setattr(llm_vision, "fetch_images", lambda urls: [])
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    _assert_pass_alert(out, "l4_images_unavailable")
    assert vision["vision"] == []


def test_f3_llm_retries_exhausted(monkeypatch, vision):
    def boom(*a, **k):
        raise RuntimeError("视觉 LLM 调用连续 3 次失败")
    monkeypatch.setattr(llm_vision, "vision_json", boom)
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    _assert_pass_alert(out, "l4_llm_error")


def test_f4_bad_json_never_pending(monkeypatch, vision):
    def boom(*a, **k):
        raise ValueError("LLM 回复中未找到 JSON 对象")
    monkeypatch.setattr(llm_vision, "vision_json", boom)
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    _assert_pass_alert(out, "l4_bad_json")


def test_f4_empty_reply(vision):
    vision["reply"] = {}
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    _assert_pass_alert(out, "l4_bad_json")


def test_f5_schema_violation(vision):
    vision["reply"] = {"verdict": "reject", "image_issues": {"oops": 1}}
    out = audit_l4.judge_l4(_product(), _l1(), images=["http://img/a.jpg"])
    _assert_pass_alert(out, "l4_bad_schema")


def test_judge_requires_images_or_conn():
    with pytest.raises(ValueError):
        audit_l4.judge_l4(_product(), _l1())


# ---------------------------------------------------------------- 取图 SQL


class _Cur:
    def __init__(self, rows):
        self.rows = list(rows)       # 每次 execute 消费一个结果
        self.sqls = []
        self.args = None
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args):
        self.sqls.append(sql)
        self.args = args
        self.row = self.rows.pop(0) if self.rows else None

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, *rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur


def test_load_product_images_primary_products_slow():
    """评审 P0 修正:主源 = products.slow->'images'(契约 v1 顶层必填段);
    命中主源就不再查快照兜底。"""
    conn = _Conn((["http://img/a.jpg", "http://img/b.jpg"],))
    assert audit_l4.load_product_images(conn, "B0X") == [
        "http://img/a.jpg", "http://img/b.jpg"]
    assert conn.cur.args == ("US", "B0X")
    assert len(conn.cur.sqls) == 1
    assert "catalog.products" in conn.cur.sqls[0]
    assert "jsonb_array_length" in conn.cur.sqls[0]


def test_load_product_images_snapshot_fallback():
    """products.slow 无图 → 回落最新含图快照(真兜底:同函数内、记日志)。"""
    conn = _Conn(None, (["http://img/c.jpg"],))
    assert audit_l4.load_product_images(conn, "B0X") == ["http://img/c.jpg"]
    assert len(conn.cur.sqls) == 2
    assert "catalog.snapshots" in conn.cur.sqls[1]
    assert "ORDER BY s.scraped_at DESC" in conn.cur.sqls[1]


def test_load_product_images_absent():
    assert audit_l4.load_product_images(_Conn(None, None), "B0X") == []
    assert audit_l4.load_product_images(_Conn((None,), (None,)), "B0X") == []


# ---------------------------------------------------------------- api/llm_vision


def test_ark_env_defaults_and_override(monkeypatch):
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    monkeypatch.delenv("ARK_VISION_MODEL", raising=False)
    assert llm_vision.base_url() == "https://ark.cn-beijing.volces.com/api/v3"
    assert llm_vision.vision_model() == "doubao-seed-1-6-flash-250615"
    assert llm_vision._endpoint().endswith("/api/v3/chat/completions")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example/api/v3/")
    monkeypatch.setenv("ARK_VISION_MODEL", "doubao-seed-1-6-250615")
    assert llm_vision._endpoint() == "https://ark.example/api/v3/chat/completions"
    assert llm_vision.vision_model() == "doubao-seed-1-6-250615"


def test_ark_api_key_fail_loud(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    with pytest.raises(LookupError):
        llm_vision.vision_json(["data:image/jpeg;base64,x"],
                               system_prompt="s", user_prompt="u")


@dataclass
class _Resp:
    status_code: int
    payload: dict = field(default_factory=dict)
    text: str = ""

    def json(self):
        return self.payload


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


def test_vision_json_message_assembly(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return _ok('{"verdict": "pass", "image_issues": []}')

    monkeypatch.setattr(llm_vision.httpx, "post", fake_post)
    out = llm_vision.vision_json(["d0", "d1"], system_prompt="SYS",
                                 user_prompt="USR")
    assert out == {"verdict": "pass", "image_issues": []}
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    # text 在前、图在后 —— 这个顺序就是 image_index 的语义基准
    assert msgs[1]["content"][0] == {"type": "text", "text": "USR"}
    assert [c["image_url"]["url"] for c in msgs[1]["content"][1:]] == ["d0", "d1"]
    assert seen["body"]["temperature"] == 0.0
    assert seen["body"]["max_tokens"] == 1024
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["timeout"].read == 120 and seen["timeout"].connect == 10


def test_vision_json_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    monkeypatch.setattr(llm_vision.time, "sleep", lambda *_: None)
    seq = [_Resp(503), _ok('{"image_issues": []}')]
    monkeypatch.setattr(llm_vision.httpx, "post",
                        lambda *a, **k: seq.pop(0))
    assert llm_vision.vision_json(["d"], system_prompt="s", user_prompt="u") \
        == {"image_issues": []}


def test_vision_json_4xx_raises_without_retry(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    calls = []

    def post(*a, **k):
        calls.append(1)
        return _Resp(400, text="bad request")

    monkeypatch.setattr(llm_vision.httpx, "post", post)
    with pytest.raises(ValueError):
        llm_vision.vision_json(["d"], system_prompt="s", user_prompt="u")
    assert len(calls) == 1


def test_vision_json_exhausted_raises_runtime(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    monkeypatch.setattr(llm_vision.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm_vision.httpx, "post", lambda *a, **k: _Resp(500))
    with pytest.raises(RuntimeError):
        llm_vision.vision_json(["d"], system_prompt="s", user_prompt="u")


class _FakeImgClient:
    """httpx.Client 替身:按 URL 给固定响应。"""

    table: dict = {}

    def __init__(self, **kw):
        self.kw = kw
        _FakeImgClient.last_kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _FakeImgClient.last_headers = headers
        r = self.table.get(url)
        if r is None:
            raise RuntimeError("boom")
        return r


class _ImgResp:
    def __init__(self, status, content, ctype=""):
        self.status_code, self.content = status, content
        self.headers = {"content-type": ctype}


def test_fetch_images_keeps_order_drops_failures(monkeypatch):
    _FakeImgClient.table = {
        "http://img/a.jpg": _ImgResp(200, b"AA", "image/jpeg; charset=x"),
        "http://img/b.png": _ImgResp(200, b"BB", "text/html"),
        "http://img/c.jpg": _ImgResp(404, b""),
        "http://img/d.webp": _ImgResp(200, b"", "image/webp"),
    }
    monkeypatch.setattr(llm_vision.httpx, "Client", _FakeImgClient)
    out = llm_vision.fetch_images(["http://img/a.jpg", "http://img/b.png",
                                   "http://img/c.jpg", "http://img/d.webp",
                                   "http://img/miss.jpg"])
    assert [u for u, _ in out] == ["http://img/a.jpg", "http://img/b.png"]
    assert out[0][1] == "data:image/jpeg;base64,QUE="
    # content-type 不是 image/* → 按 URL 后缀推 mime
    assert out[1][1].startswith("data:image/png;base64,")
    assert _FakeImgClient.last_headers == {"User-Agent": "Mozilla/5.0"}
    assert _FakeImgClient.last_kw["timeout"] == 8.0
    assert _FakeImgClient.last_kw["follow_redirects"] is True


def test_fetch_images_empty():
    assert llm_vision.fetch_images([]) == []
