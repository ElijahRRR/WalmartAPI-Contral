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
    # ⚠ not_found **不是** error_type(采集侧枚举里没有这个值,见下条测试),
    # 它是 outcome 且走成功路径。这里用真的终局 error_type 举例
    assert sm.classify("ok", "parse_error") == "hard_failed"


def test_retryable_set_is_the_shared_one():
    """可重试类型不另立一份:与 order_audit/product_refresh 同源。"""
    for t in ("captcha", "timeout", "blocked", "network"):
        assert t in batches.RETRYABLE
        assert sm.classify(None, t) == "retryable"
    for t in batches.TERMINAL:
        assert sm.classify(None, t) == "hard_failed"


def test_variant_offset_is_retry_later_not_terminal():
    """⚠ 采集仓库源码钉住(worker/engine.py:1501-1508):variant 偏移"多由
    Amazon A/B test、地域缓存、库存差异引起",成因**非确定性**;采集侧 cap=1
    不重试是配额保护+防数据中毒的策略,不是"重试也没用"。它自己永远不会再试
    (NO_AUTO_RETRY 唯一成员,force=true 也不越过)⇒ 只有本侧提新批次这一条路。
    过了冷却期才推——立刻重推撞的是同一个 A/B 分桶。"""
    assert "variant_offset" not in batches.TERMINAL
    assert "variant_offset" in batches.RETRY_LATER
    assert sm.classify(None, "variant_offset", 30) == "retry_later"
    assert sm.classify(None, "variant_offset", 1) == "cooling"
    assert "retry_later" in sm._DEFAULT_PUSH and "cooling" not in sm._DEFAULT_PUSH


def test_not_found_is_an_outcome_not_an_error_type():
    """404 在采集侧走**成功**路径(success=True、任务 done),只体现在
    outcome='not_found';其注释明写"下一轮定时采集自动纠正"。下架后重新上架
    是常事 ⇒ 同样归 retry_later + 冷却,不是终局。"""
    assert "not_found" not in batches.ERROR_TYPES
    assert sm.classify("not_found", None, 30) == "retry_later"
    assert sm.classify("not_found", None, 1) == "cooling"


def test_cooldown_missing_age_does_not_freeze_forever():
    """天龄取不到时按已冷却放行:宁可多推一次,也不要因为缺个时间戳
    把一整类永久卡死在 cooling 里(静默卡死是本仓一贯要防的)。"""
    assert sm.classify(None, "variant_offset", None) == "retry_later"


def test_order_chain_keeps_the_wider_terminal_set():
    """⚠ 两个集合服务两个决策窗口,别"顺手统一口径":
    产品库补采的尺度是周(可以等冷却),订单链的尺度是小时(一张单等着发货,
    冷却两周再给答案等于没答案)。所以订单链取 TERMINAL | RETRY_LATER。"""
    from services import order_audit
    assert order_audit._TERMINAL_FOR_ORDER == batches.TERMINAL | batches.RETRY_LATER
    assert "variant_offset" in order_audit._TERMINAL_FOR_ORDER


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
