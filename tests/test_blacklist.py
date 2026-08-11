"""黑名单收集(自产回路落库端):入选口径 + 品牌去重 + 尾段隔离。

入选集合 {B,C,E,F,G,K} 是钱和商机的判定(错收 = 商品永久不能重上架,
漏收 = 违规商品反复上架),这里的断言就是它的回归护栏。
"""

import pytest

from services import blacklist as bl


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.executed.append((sql, args))
        self._rows = []
        self.rowcount = 1
        if "FROM ops.dedupe" in sql:
            self._rows = [(k,) for k in self.conn.processed]
        elif "FROM catalog.products" in sql:
            self._rows = list(self.conn.brands.items())
        elif "INSERT INTO catalog.asin_blacklist" in sql:
            # 忠实模拟 DO NOTHING:已存在的行**不覆盖**(第一版夹具无条件
            # 覆盖,把"不覆盖镜像行"的用例测成了假红)
            asin = args[0]
            self.rowcount = 0 if asin in self.conn.asin_rows else 1
            if self.rowcount:
                self.conn.asin_rows[asin] = args
        elif "INSERT INTO catalog.brand_blacklist" in sql:
            key = args[0]
            self.rowcount = 0 if key in self.conn.brand_rows else 1
            if self.rowcount:
                self.conn.brand_rows[key] = args
        elif "INSERT INTO ops.dedupe" in sql:
            self.conn.marked.append(args[1])

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, processed=(), brands=None, brand_rows=None):
        self.processed = set(processed)
        self.brands = brands or {}
        self.asin_rows: dict = {}
        self.brand_rows: dict = dict(brand_rows or {})
        self.marked: list = []
        self.executed: list = []

    def cursor(self):
        return _Cur(self)


def _it(sku, cat, store="店A", reasons="some reason"):
    return {"sku": sku, "store": store, "category": cat, "reasons": reasons}


# ── ASIN 入选口径 ─────────────────────────────────────────────────────────────

def test_permanent_set_is_exactly_bcefgk():
    """入选集合逐字钉死(旧 blacklist_sync.py:18-21 + 所有者 2026-08-11 拍板:
    只收永久禁止类,13 类词表只是来源列格式)。改集合必须红给人看。"""
    assert bl.PERMANENT == {"B", "C", "E", "F", "G", "K"}


def test_record_asins_only_permanent_classes():
    """A(过期)/D(价格)这类可修复的绝不入选——进了会误杀重上架拦截。"""
    conn = _Conn()
    added = bl.record_asins(conn, [
        _it("B0PERM", "B"), _it("B0EXPIRE", "A"), _it("B0PRICE", "D"),
        _it("B0IP", "E"), _it("B0SYS", "L"), _it("B0OTHER", "Z"),
    ])
    assert added == 2
    assert set(conn.asin_rows) == {"B0PERM", "B0IP"}


def test_record_asins_first_entry_wins():
    """永久禁止 = 一次入选;重复撞见不刷新(DO NOTHING),计数也不虚报。"""
    conn = _Conn()
    conn.asin_rows["B0PERM"] = ("已存在",)
    assert bl.record_asins(conn, [_it("B0PERM", "B")]) == 0


def test_asin_source_label_format():
    """来源列格式 = 「沃尔玛-〈类名〉」(所有者定的表格约定)。"""
    conn = _Conn()
    bl.record_asins(conn, [_it("B0A", "K")])
    assert conn.asin_rows["B0A"][2] == "沃尔玛-审查"


def test_biz_cn_is_flagged_independently():
    """BIZ-CN 是独立维度(legacy_survey:2077),不能淹没在 C 品牌类里。"""
    conn = _Conn()
    bl.record_asins(conn, [
        _it("B0CN", "B", reasons="Prohibited, reference code BIZ-CN"),
        _it("B0X", "B", reasons="prohibited product policy")])
    assert conn.asin_rows["B0CN"][5] is True
    assert conn.asin_rows["B0X"][5] is False


# ── 品牌收集 ──────────────────────────────────────────────────────────────────

def test_collect_brands_only_c_and_e():
    """品牌收集只看 C(品牌)/E(知产)——B 禁售是产品级问题,不牵连品牌。"""
    conn = _Conn(brands={"B0C": "Nike", "B0B": "Sony"})
    st = bl.collect_brands(conn, [_it("B0C", "C"), _it("B0B", "B")])
    assert st["brand_new"] == 1
    assert list(conn.brand_rows) == ["nike"]


def test_collect_brands_dedupes_by_brand_not_sku():
    """去重按品牌(所有者澄清 2026-08-11):同品牌第二个 SKU 不重复入选,
    但 ASIN 照样标已处理——它的品牌已经在名单里了。"""
    conn = _Conn(brands={"B0A": "Nike", "B0B": "NIKE"})
    st = bl.collect_brands(conn, [_it("B0A", "C"), _it("B0B", "C")])
    assert st["brand_new"] == 1 and st["brand_known"] == 1
    assert sorted(conn.marked) == ["B0A", "B0B"]
    assert conn.brand_rows["nike"][1] in ("Nike", "NIKE")   # casefold 键去重


def test_collect_brands_skips_processed_asins():
    """已处理 ASIN 跳过(历史 2,609 个已导入 ops.dedupe)——否则重复采。"""
    conn = _Conn(processed={"B0OLD"}, brands={"B0OLD": "Nike"})
    st = bl.collect_brands(conn, [_it("B0OLD", "C")])
    assert st == {"brand_new": 0, "brand_known": 0, "no_brand": 0, "skipped": 1}
    assert conn.brand_rows == {} and conn.marked == []


def test_collect_brands_missing_brand_stays_unmarked():
    """产品中心缺 brand 的 ASIN **不标已处理**——标了就永远漏了,
    等 product_ingest 补上品牌后下一轮自然重试。"""
    conn = _Conn(brands={})            # 产品中心查无此 ASIN
    st = bl.collect_brands(conn, [_it("B0NEW", "E")])
    assert st["no_brand"] == 1
    assert conn.marked == [] and conn.brand_rows == {}


def test_collect_brands_does_not_overwrite_mirror_rows():
    """risk_sync 镜像来的行不覆盖(DO NOTHING)——镜像行是飞书人工登记的
    真值,自产行只补空白。"""
    conn = _Conn(brands={"B0A": "Nike"}, brand_rows={"nike": ("镜像行",)})
    st = bl.collect_brands(conn, [_it("B0A", "C")])
    assert st["brand_new"] == 0 and st["brand_known"] == 1
    assert conn.brand_rows["nike"] == ("镜像行",)
    assert conn.marked == ["B0A"]      # 品牌已在名单,ASIN 该标已处理


# ── 尾段隔离(cleanup 主链不受收集失败影响)──────────────────────────────────

def test_cleanup_tail_never_breaks_the_main_chain(monkeypatch):
    from workflows import problem_product_cleanup as wf
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("库炸了")))
    note = wf._collect_blacklists([_it("B0A", "B")])
    assert "黑名单收集失败" in note      # 返回摘要而不是抛异常


def test_cleanup_tail_skips_uncategorized_items(monkeypatch):
    """stage-pending/在途行没有 category,不参与收集(和归类事件同口径)。"""
    from workflows import problem_product_cleanup as wf
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda: pytest.fail("没归类的行不该碰库"))
    assert wf._collect_blacklists([{"sku": "B0A", "store": "S",
                                    "reasons": "x"}]) == ""


# ── DELETE 防重口径(所有者拍板 2026-08-11:滚动 48h,仅限无终态)────────────

def test_inflight_sql_caps_submitted_at_48h():
    """三个半条,只能断言 SQL 文本(时间条件在 PG 侧求值,夹具盖不住):

    ① submitted 拦但 48h 封顶——超时 = feed 丢了,继续拦是永久漏删;
    ② success 待观测拦到重扫为止,重扫仍在 = 删除没生效,直接重发不等 48h
       (条件里**不许**出现对 success 的时间限制);
    ③ failed 不拦(WHERE 里根本不出现)。
    """
    from workflows import problem_product_cleanup as wf
    sql = wf._SQL_INFLIGHT
    assert "f.status = 'submitted'" in sql
    assert "interval '48 hours'" in sql
    # ② success 那一支不带时间窗:它的解除条件是"被重扫",不是"过了多久"
    success_leg = sql[sql.index("'success'"):]
    assert "interval" not in success_leg
    assert "resolved_at > w.last_seen_at" in success_leg
    assert "'failed'" not in sql
