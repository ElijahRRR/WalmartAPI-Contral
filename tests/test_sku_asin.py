"""SKU→ASIN 清洗规则:所有者给的三个真实样本是铁证用例,一票否决。

规则唯一出处 services/sku_asin;record_many 写事件时自动清洗,
sku_normalize 批量补存量——两条路径都吃这一份规则。
"""

from services import sku_asin as sa


# ── 三个真实样本(所有者 2026-08-11)──────────────────────────────────────────

def test_owner_samples_extract_exactly():
    assert sa.extract_asin("JTZW-D01027HVK3W-38") == "D01027HVK3W"
    assert sa.extract_asin("XKJ-B0GXX75JN5-39.98") == "B0GXX75JN5"
    assert sa.extract_asin("YP-B09TDMGVRW-188.88") == "B09TDMGVRW"
    # 前缀含数字 + 价格前导零(2026-08-11 生产实证的第 4 形态,208 个)
    assert sa.extract_asin("A109-B08QF9XLMH-02") == "B08QF9XLMH"


def test_plain_asin_passes_through():
    assert sa.extract_asin("B0GXX75JN5") == "B0GXX75JN5"
    assert sa.extract_asin(" b0gxx75jn5 ") == "B0GXX75JN5"   # 大小写/空白归一


def test_numeric_item_id_is_not_an_asin():
    """纯数字是 walmart item id——10 位纯数字也**不是** ASIN(裸 ASIN 规则
    要求至少含一个字母),提取返 None 走倒查,绝不冒充。"""
    assert sa.extract_asin("102460018738") is None
    assert sa.extract_asin("1024600187") is None
    assert sa.classify("102460018738") == "numeric"


def test_unknown_shapes_return_none_not_guesses():
    """认不出的形态返 None 进「其他」桶报告——规则不全是常态,猜是事故。"""
    for weird in ("", None, "ABC", "A-B-C", "JTZW-38", "X" * 40,
                  "JTZW-D01027HVK3W"):        # 缺价格段的两段式:未见实例,不猜
        assert sa.extract_asin(weird) is None, weird
        assert sa.classify(weird) == "other", weird


def test_classify_buckets():
    assert sa.classify("B0GXX75JN5") == "asin"
    assert sa.classify("YP-B09TDMGVRW-188.88") == "wrapped"


def test_record_many_autofills_asin_column():
    """实时链路的清洗接线:record_many 每行自动算 asin(提不出存 None)。"""
    from services import product_events as pe

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, seq):
            self.conn.sql = sql
            self.conn.rows = list(seq)

        def __init__(self, conn): self.conn = conn

    class _Conn:
        def cursor(self): return _Cur(self)

    conn = _Conn()
    pe.record_many(conn, [
        {"sku": "XKJ-B0GXX75JN5-39.98", "event": pe.PROBLEM_CATEGORIZED,
         "source": "t"},
        {"sku": "102460018738", "event": pe.PROBLEM_CATEGORIZED, "source": "t"},
    ])
    assert "asin" in conn.sql
    assert conn.rows[0][1] == "B0GXX75JN5"      # 三段式 → 提取
    assert conn.rows[1][1] is None              # item id → NULL 等倒查
