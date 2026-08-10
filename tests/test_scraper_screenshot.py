"""取截图接口的四种结局必须分得开(采集侧 2026-08-10 新增)。

采集侧刻意把「还没截好」「没有这条记录」「截图失败」拆成三个状态码,
就是为了让调用方有**该不该重试**的判据(旧的 /static/screenshots 路径上
后三种全是同一个 404,分不出来)。本文件守的就是这个区分不被抹平:
合并任意两种,调用方要么无限重试一张永远不会有的图,要么把「再等 10 秒」
当成失败丢掉,而服务端一切正常、不报任何错。
"""

import httpx
import pytest

from api import scraper


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("SCRAPER_BASE_URL", "http://scraper.test")
    monkeypatch.setenv("SCRAPER_EXPORT_TOKEN", "t0ken")


def _stub(monkeypatch, status: int, *, body=None, content=b""):
    def fake_get(url, **kw):
        req = httpx.Request("GET", url)
        if content:
            return httpx.Response(status, content=content, request=req)
        return httpx.Response(status, json=body or {}, request=req)
    monkeypatch.setattr(httpx, "get", fake_get)


def test_200_returns_png_bytes(monkeypatch):
    _stub(monkeypatch, 200, content=b"\x89PNG\r\n\x1a\n fake")
    assert scraper.fetch_screenshot("wm-audit-10001-x", "B001").startswith(b"\x89PNG")


def test_409_is_pending_not_failure(monkeypatch):
    """还没截好 → 稍后再来。当成失败的话这张图永远不会被取回。"""
    _stub(monkeypatch, 409, body={"detail": {"error": "screenshot_pending",
                                             "status": "processing"}})
    with pytest.raises(scraper.ScreenshotPending):
        scraper.fetch_screenshot("wm-audit-10001-x", "B001")


@pytest.mark.parametrize("status,detail", [
    (404, {"detail": "没有这条截图记录"}),
    (410, {"detail": {"error": "screenshot_failed",
                      "error_detail": "captcha", "retry_count": 3}}),
])
def test_404_and_410_are_gone_dont_retry(monkeypatch, status, detail):
    _stub(monkeypatch, status, body=detail)
    with pytest.raises(scraper.ScreenshotGone):
        scraper.fetch_screenshot("wm-audit-10001-x", "B001")


def test_pending_and_gone_are_distinct_types():
    """两类互不为父子——except 顺序写反也不会把「等等」吞成「别等了」。"""
    assert not issubclass(scraper.ScreenshotPending, scraper.ScreenshotGone)
    assert not issubclass(scraper.ScreenshotGone, scraper.ScreenshotPending)


def test_unexpected_status_is_generic_error(monkeypatch):
    """5xx 既不是"没有"也不是"没好":不该被归进那两类而失去告警。"""
    _stub(monkeypatch, 503, body={"detail": "upstream down"})
    with pytest.raises(RuntimeError) as e:
        scraper.fetch_screenshot("wm-audit-10001-x", "B001")
    assert not isinstance(e.value, (scraper.ScreenshotPending,
                                    scraper.ScreenshotGone))


# ── 截图清单(按批次一次拿完,不逐 ASIN 试探)────────────────────────────────

def test_screenshot_list_follows_cursor_to_the_end(monkeypatch):
    """自动翻页到 next_cursor 为空。只取第一页的话,一批 50 个 ASIN 里
    排在后面的图会永远拿不到,而两侧都不报错。"""
    pages = {
        None: {"items": [{"asin": "B001", "status": "done", "url": "/a.png"}],
               "next_cursor": "c2"},
        "c2": {"items": [{"asin": "B002", "status": "pending", "url": None}],
               "next_cursor": None},
    }
    seen = []

    def fake_get(url, **kw):
        params = kw.get("params") or {}
        seen.append(params)
        return httpx.Response(200, json=pages[params.get("cursor")],
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)

    items = scraper.screenshot_list("wm-audit-10001-x")
    assert [i["asin"] for i in items] == ["B001", "B002"]
    assert all(p["batch_name"] == "wm-audit-10001-x" for p in seen)


def test_screenshot_list_404_is_lookup_error(monkeypatch):
    _stub(monkeypatch, 404, body={"detail": "批次不存在"})
    with pytest.raises(LookupError):
        scraper.screenshot_list("wm-audit-10001-x")


# ── JSON 推送(一个邮编一个批次)───────────────────────────────────────────────

def test_submit_json_requires_a_zip(monkeypatch):
    """邮编是批次级参数且必填:漏了就等于按默认地区采,拿回来的价格
    审的是别的地区的单,而接口不会报错。"""
    with pytest.raises(ValueError):
        scraper.submit_json("wm-audit-x", ["B001"], zip_code="")
    with pytest.raises(ValueError):
        scraper.submit_json("wm-audit-x", [], zip_code="10001")


def test_submit_json_posts_batch_level_zip(monkeypatch):
    sent = {}

    def fake_post(url, **kw):
        sent.update(url=url, json=kw.get("json"))
        return httpx.Response(200, json={"batch_id": 7, "inserted": 1},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)

    res = scraper.submit_json("wm-audit-10001-x", ["B001"], zip_code="10001",
                              needs_screenshot=True)
    assert res["batch_id"] == 7
    assert sent["url"].endswith("/api/batches")
    assert sent["json"] == {"batch_name": "wm-audit-10001-x", "asins": ["B001"],
                            "zip_code": "10001", "needs_screenshot": True}


def test_submit_json_409_carries_existing_batch_id(monkeypatch):
    """撞名 = 上次其实推成功了(v4 绝不静默合并):拿既有 batch_id 接着轮询,
    别重推。这也是本端点可以安全重试的前提。"""
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        409, json={"detail": {"batch_id": 42, "batch_name": "wm-audit-10001-x"}},
        request=httpx.Request("POST", url)))
    with pytest.raises(scraper.BatchExistsError) as e:
        scraper.submit_json("wm-audit-10001-x", ["B001"], zip_code="10001")
    assert e.value.batch_id == 42


def test_submit_json_400_is_terminal_not_retried(monkeypatch):
    """400 conflicting_zip_for_asin 是编排错了,重试一万次也一样。"""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(1) or
                        httpx.Response(400, json={"detail": "conflicting_zip_for_asin"},
                                       request=httpx.Request("POST", url)))
    with pytest.raises(ValueError):
        scraper.submit_json("wm-audit-x", ["B001"], zip_code="10001")
    assert len(calls) == 1
