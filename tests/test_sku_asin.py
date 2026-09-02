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


# ── 批量清洗两跳(2026-08-27 从两个清洗工作流收编)──────────────────────

class _Cur:
    def __init__(self, hits):
        self.hits, self.sql, self.args = hits, None, None

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None):
        self.sql, self.args = sql, args

    def fetchall(self):
        return list(self.hits.items())


class _Conn:
    def __init__(self, hits=None):
        self.cur = _Cur(hits or {})

    def cursor(self):
        return self.cur


def test_resolve_skus_two_hops_and_leaves_the_rest_alone():
    """模式提取 + 纯数字倒查 item id 两跳;**解析不了的不进映射**(留 NULL)。"""
    conn = _Conn({"102460018738": "XKJ-B0GXX75JN5-39.98"})
    skus = ["B0GXX75JN5", "JTZW-D01027HVK3W-38", "102460018738",
            "998877665544", "怪东西"]
    mapping, buckets = sa.resolve_skus(conn, skus)
    assert mapping == {"B0GXX75JN5": "B0GXX75JN5",
                       "JTZW-D01027HVK3W-38": "D01027HVK3W",
                       "102460018738": "B0GXX75JN5"}      # 倒查救回来的
    # 倒查不到的那个纯数字不进映射(绝不猜),其余照原形态计数
    assert "998877665544" not in mapping
    assert buckets == {"asin": 1, "wrapped": 1, "numeric": 2, "other": 1,
                       "numeric_resolved": 1}
    # 倒查只对纯数字发一次,且只发那两个
    assert "catalog.walmart_items" in conn.cur.sql
    assert conn.cur.args == (["102460018738", "998877665544"],)


def test_resolve_skus_skips_the_lookup_when_nothing_is_numeric():
    conn = _Conn()
    mapping, buckets = sa.resolve_skus(conn, ["B0GXX75JN5"])
    assert mapping == {"B0GXX75JN5": "B0GXX75JN5"}
    assert conn.cur.sql is None          # 一条 SQL 都不该发
    assert buckets == {"asin": 1}


def test_samples_only_reports_the_buckets_a_human_has_to_look_at():
    """只报 numeric/other:asin/wrapped 是提得出的,不需要人认。
    新形态先进「其他」桶带样本报出来,人认了再扩规则。"""
    skus = [f"{i:012d}" for i in range(7)] + ["怪A", "怪B"]
    _, buckets = sa.resolve_skus(_Conn(), skus)
    got = sa.samples(skus, buckets)
    assert set(got) == {"numeric", "other"}
    assert len(got["numeric"]) == 5          # 每桶前 5 个
    assert got["other"] == ["怪A", "怪B"]
    # 桶为空就不出现(摘要里不印一行空样本)
    assert sa.samples(["B0GXX75JN5"], {"asin": 1}) == {}


# ── 登记簿那一跳(SKU 改造批次 0a)────────────────────────────────────────────
#
# 身份从此有两条腿:登记簿 amz 行的 source_key 是权威键,模式提取只兜存量。
# 两条腿**必须同口径** —— 一条归一一条不归一,下游集合里就会同时存在
# 'B0ABCDEFGH' 与 'b0abcdefgh' 两个键,而"已在架"的判定正是按键取交集的。

class _RegCur:
    def __init__(self, rows):
        self.rows, self.sql, self.args, self.n = rows, None, None, 0

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None):
        self.sql, self.args, self.n = sql, args, self.n + 1

    def fetchall(self):
        return list(self.rows)


class _RegConn:
    def __init__(self, rows=()):
        self.cur = _RegCur(rows)

    def cursor(self):
        return self.cur


def test_resolve_agrees_with_extract_asin_on_every_legacy_shape():
    """未登记的存量行,resolve 必须与 extract_asin 逐个同值 —— 收口那天全仓
    十几处读侧一起换口径,只要这条不成立就是一次静默的全量行为变化。"""
    for sku in ("B0GXX75JN5", "XKJ-B0GXX75JN5-39.98", "A109-B08QF9XLMH-02",
                "102460018738", "怪东西", ""):
        assert sa.pick_asin(None, sku) == sa.extract_asin(sku), sku
        assert sa.resolve(_RegConn(), "T1", sku) == sa.extract_asin(sku), sku


def test_registry_key_wins_over_the_pattern():
    """登记簿优先:跟卖/自建/未来的不透明码,模式提不出的靠它;两者都在时
    以登记簿为准(它是上架时写下的事实,模式只是猜)。"""
    assert sa.pick_asin("B0REGISTER", "XKJ-B0GXX75JN5-39.98") == "B0REGISTER"


def test_a_lowercase_registry_key_is_normalized_like_the_pattern_does():
    """source_key 是运营在上架表里手填、原样落库的(读表只 strip 不 upper)。
    裸取会让一个小写 ASIN 变成下游集合里的垃圾键 —— 而真 ASIN 仍然缺席,
    于是"已在架"被判成"不在架",已上架的品被重新派工写回上架表。"""
    assert sa.pick_asin(" b0abcdefgh ", "SOMESKU") == "B0ABCDEFGH"
    assert sa.pick_asin("b0abcdefgh", "B0ABCDEFGH") == sa.extract_asin("B0ABCDEFGH")


def test_a_malformed_registry_key_falls_back_to_the_pattern():
    """**登记簿只是优先级,不是免检通道**:形态不对的键一律不当身份用。"""
    for bad in ("B0XXXXXXXX-2", "   ", "00842565531441", "1024600187", None, ""):
        assert sa.pick_asin(bad, "XKJ-B0GXX75JN5-39.98") == "B0GXX75JN5", bad
    assert sa.pick_asin("B0XXXXXXXX-2", "怪东西") is None      # 都提不出就是提不出


def test_unregistered_and_non_amz_rows_fall_back_to_the_pattern():
    """只认 source_type='amz':match 行的 source_key 是匹配 GTIN,拿它当 ASIN
    用会把一个 GTIN 灌进按 ASIN 建的所有集合里。"""
    conn = _RegConn()                       # 一行都查不到 = 未登记 / 非 amz
    got = sa.resolve_many(conn, [("T1", "B0GXX75JN5"), ("T1", "怪东西")])
    assert got == {("T1", "B0GXX75JN5"): "B0GXX75JN5"}      # 提不出的不进映射
    assert "source_type = 'amz'" in conn.cur.sql


def test_resolve_still_answers_for_an_abandoned_row():
    """**不按 abandoned_at 过滤**(消费方契约):旧码带着订单、售后回来时
    必须还查得到,查不到 = 那笔订单永远归不到产品上。"""
    assert "abandoned_at" not in sa._REG_SQL
    conn = _RegConn([("T1", "AK7QM2X9RT4W", "B0ABCDEFGH")])
    assert sa.resolve(conn, "T1", "AK7QM2X9RT4W") == "B0ABCDEFGH"


def test_resolve_many_is_one_query_for_many_pairs():
    """有界批量反查发**一条** SQL;十万对那种全表级取数不走这里(走 SQL 里的
    LEFT JOIN),不然就是把一次 JOIN 换成一次巨型数组传参。"""
    conn = _RegConn([("T1", "AK7QM2X9RT4W", "B0ABCDEFGH")])
    pairs = [("T1", "AK7QM2X9RT4W"), ("T1", "B0GXX75JN5"), ("T2", "怪东西")]
    got = sa.resolve_many(conn, pairs)
    assert got == {("T1", "AK7QM2X9RT4W"): "B0ABCDEFGH",
                   ("T1", "B0GXX75JN5"): "B0GXX75JN5"}
    assert conn.cur.n == 1
    assert conn.cur.args == (["T1", "T1", "T2"],
                             ["AK7QM2X9RT4W", "B0GXX75JN5", "怪东西"])
    assert sa.resolve_many(_RegConn(), []) == {}             # 空入参一条都不发


def test_an_opaque_code_is_invisible_to_extract_asin_but_resolvable():
    """12 位不透明码对模式提取必返 None —— **形态本身就是分流器**:提不出
    就说明该走登记簿了,不会有"猜出一个假 ASIN"的中间态。"""
    from services import sku_codec
    code = "AK7QM2X9RT4W"
    assert sku_codec.is_opaque(code) and sa.extract_asin(code) is None
    assert sa.pick_asin(None, code) is None
    assert sa.resolve(_RegConn([("T1", code, "B0ABCDEFGH")]), "T1", code) == "B0ABCDEFGH"
