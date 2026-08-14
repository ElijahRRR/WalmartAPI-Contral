"""补采工作流测试(离线;分类纯函数 + 目标 SQL 口径 + 前缀隔离)。

要害:那 8 万空壳行大头是 pt_backfill 用**删除历史**建的占位行,亚马逊多半
已下架——不分来路一股脑推,等于给采集器压几万个必然 not_found 的任务。
"""

from services import scrape_batches as batches
from workflows import scrape_missing as sm


def test_classify_failure_beats_snapshot():
    """失败台账优先于快照:采失败压根不产出快照行,有失败就是更新的证据。"""
    assert sm.classify(None, None) == "never_tried"
    assert sm.classify("ok", None) == "ok_but_thin"
    assert sm.classify("degraded", None) == "had_snapshot"
    assert sm.classify("ok", "captcha") == "retryable"      # 失败更新 → 压过快照
    assert sm.classify("ok", "not_found") == "hard_failed"


def test_retryable_set_is_the_shared_one():
    """可重试类型不另立一份:与 order_audit/product_refresh 同源。"""
    for t in ("captcha", "timeout", "blocked", "network"):
        assert t in batches.RETRYABLE
        assert sm.classify(None, t) == "retryable"
    for t in batches.TERMINAL:
        assert sm.classify(None, t) == "hard_failed"


def test_default_push_excludes_terminal_and_thin():
    """默认不推两类:终局失败(已下架)与采到过但字段仍缺(重采还是那样)。"""
    assert "hard_failed" not in sm._DEFAULT_PUSH
    assert "ok_but_thin" not in sm._DEFAULT_PUSH
    assert set(sm._DEFAULT_PUSH) <= set(sm._CLASSES)


def test_targets_sql_shape():
    """目标口径:三层缺失取并集;履历用每 ASIN 最新一条(快照/失败各一)。"""
    sql = sm._TARGETS_SQL
    assert "no_title OR no_path OR no_node" in sql
    assert sql.count("DISTINCT ON") == 2                 # 快照、失败各取最新
    assert "ORDER BY s.asin, s.scraped_at DESC" in sql
    assert "ORDER BY f.asin, f.recorded_at DESC" in sql
    assert "marketplace = 'US'" in sql


def test_dangerous_and_batch_naming():
    assert sm.DANGEROUS is True                          # 会压几万个采集任务
    assert sm.BATCH_PREFIX.endswith("-")


def test_asin_shape_filter():
    """非 ASIN 形态(旧库 SKU 归一残留)不推给采集器,且过滤数要报出来。"""
    ok = ("B0FDW1J3NZ", "B000EEGAOW")
    bad = ("XKJ-B0FDW1J3NZ-39.98", "12345678", "", "b0fdw1j3nz")
    assert all(sm._ASIN_RE.fullmatch(a) for a in ok)
    assert not any(sm._ASIN_RE.fullmatch(a) for a in bad)
