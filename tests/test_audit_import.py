"""audit_import 的纯函数与契约测试(批次 A)。

搬迁工具的危险面在"静默丢数据"与"表序破坏外键"——两者都钉在纯函数/常量上,
离线可测;真实拷贝路径由 dry-run 体检 + 行数校验在生产兜底。
"""

from workflows import audit_import as ai


def test_tables_contract():
    """13 张表、runs 在 hits 之前(外键)、不搬清单里的表绝不在列。"""
    assert len(ai.TABLES) == 13
    assert ai.TABLES.index("audit_runs") < ai.TABLES.index("audit_hits")
    for banned in ("products", "llm_cache", "sync_runs",
                   "llm_usage", "llm_route_events"):
        assert banned not in ai.TABLES


def test_fk_pair_selected_together():
    """单选外键对的任一张,都必须成对处理且 runs 在前。"""
    assert ai._pick_tables({"table": "audit_hits"}) == ["audit_runs", "audit_hits"]
    assert ai._pick_tables({"table": "audit_runs"}) == ["audit_runs", "audit_hits"]
    assert ai._pick_tables({}) == list(ai.TABLES)


def test_diff_columns_identical():
    cols = [("a", "text"), ("b", "jsonb")]
    fatal, warn, common = ai.diff_columns(cols, cols)
    assert not fatal and not warn and common == ["a", "b"]


def test_diff_columns_source_only_is_fatal():
    """源独有列 = 照搬丢数据,必须致命而非警告。"""
    fatal, _, common = ai.diff_columns(
        [("a", "text"), ("extra", "text")], [("a", "text")])
    assert len(fatal) == 1 and "extra" in fatal[0]
    assert common == ["a"]


def test_diff_columns_type_mismatch_is_fatal():
    fatal, _, common = ai.diff_columns(
        [("a", "text")], [("a", "jsonb")])
    assert len(fatal) == 1 and "类型不一致" in fatal[0]
    assert common == []          # 类型不符的列不进拷贝列序


def test_diff_columns_target_only_is_warning():
    """目标独有列(如反推表多猜的 synced_at)只警告——导入吃默认值。"""
    fatal, warn, common = ai.diff_columns(
        [("a", "text")], [("a", "text"), ("synced_at", "timestamp with time zone")])
    assert not fatal and len(warn) == 1 and common == ["a"]


def test_identity_tables_registered():
    """带自增 id 的三张表都在 setval 清单——漏一张,导入后 INSERT 撞主键。"""
    assert set(ai._IDENTITY) == {"audit_runs", "audit_hits", "walmart_error_records"}
