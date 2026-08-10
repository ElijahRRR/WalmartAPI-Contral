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
