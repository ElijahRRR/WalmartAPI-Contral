"""风险追溯引擎回归(services/risk_trace):波及展开的地基。

钉三件事:① 品牌归一只在 Python(SQL 侧一个归一函数都不写);
② 四证据源的 SQL 形状(身份键表达式逐字对齐索引、claims 不带 status 过滤);
③ still_listed 的三个条件(退市品 missing_since 也是 NULL 这个坑)。

沙箱 PG 集成用例在文件末尾:连不上就 skip,不让无 PG 的环境变红。
"""

import os
import pathlib
import re
import socket

import pytest

from services import risk_trace as rt

# ── 假连接 ───────────────────────────────────────────────────────────────────


class _Cur:
    """按 SQL 里出现的表名派发假结果的游标;execute 全部留痕供形状断言。"""

    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        self._rows = []
        for table, rows in self.conn.rows.items():
            if table in sql:
                self._rows = rows
                break

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls: list = []

    def cursor(self):
        return _Cur(self)


_T_ITEMS = "catalog.walmart_items"
_T_SOURCES = "catalog.listing_sources"
_T_EVENTS = "catalog.product_events"
_T_CLAIMS = "catalog.claims"
_T_PRODUCTS = "catalog.products"


# ── 归一:只在 Python,候选串两形去重 ────────────────────────────────────────

def test_brand_candidates_covers_both_forms_and_dedups():
    """原文小写形 + 压空格形;两形相同只留一个(绝大多数品牌走这条)。"""
    assert rt.brand_candidates("  Acme  Widgets ") == ["acme  widgets",
                                                       "acme widgets"]
    assert rt.brand_candidates("Acme") == ["acme"]
    assert rt.brand_candidates("") == [] and rt.brand_candidates(None) == []


def test_sql_never_normalizes_the_input_brand():
    """⚠ 仓里已并存三套不一致的品牌归一,SQL 里再写第四套 = 第四个各漂各的版本。

    候选串由 Python 生成、SQL 只做等值比 —— 所以 SQL 常量里不许出现拿**参数**
    做归一的写法(`lower(%(...)s)` 之类)。列上的 `lower(btrim(brand))` 是在比
    数据库存的那一侧,不算归一输入。
    """
    src = pathlib.Path("services/risk_trace.py").read_text()
    for m in re.finditer(r"(lower|upper|btrim|trim|casefold)\s*\(\s*%\(", src):
        pytest.fail(f"SQL 里对参数做了归一:{m.group(0)}")


def test_placeholder_brand_expands_to_nothing():
    """按 "OEM" 展开波及 = 一次成千上万个不相干产品的大面积误标。"""
    conn = _Conn()          # 一旦真去查库,calls 会留下痕迹
    for raw in ("OEM", "Generic", "无品牌", "  ", None, "unknown"):
        assert rt.asins_of_brand(conn, raw) == []
    assert conn.calls == []                     # 连查都没查


def test_stores_of_brand_says_none_instead_of_empty_for_placeholders():
    """"拒绝展开"与"查了但一家都没有"处置完全不同,调用方必须能分辨。"""
    conn = _Conn()
    bkey, asins, hits = rt.stores_of_brand(conn, "Generic", manufacturer="OEM")
    assert bkey is None and asins == [] and hits == {}


def test_manufacturer_leg_only_when_brand_is_a_placeholder():
    """brand 有真值时不许拿 manufacturer 再捞:代工厂给几十个品牌代工。"""
    conn = _Conn({_T_PRODUCTS: [("B0AAA",)], _T_CLAIMS: [], _T_ITEMS: [],
                  _T_SOURCES: [], _T_EVENTS: []})
    bkey, asins, _ = rt.stores_of_brand(conn, "Acme", manufacturer="Foxconn")
    assert bkey == "acme" and asins == ["B0AAA"]
    cands = conn.calls[0][1]["cands"]
    assert cands == ["acme"] and "foxconn" not in cands
    # brand 是占位符时反过来:用 manufacturer 的原文查,不能拿 "Generic" 去查
    conn2 = _Conn({_T_PRODUCTS: [("B0BBB",)]})
    bkey2, _, _ = rt.stores_of_brand(conn2, "Generic", manufacturer="Foxconn")
    assert bkey2 == "foxconn" and conn2.calls[0][1]["cands"] == ["foxconn"]


# ── 四证据源的 SQL 形状 ──────────────────────────────────────────────────────

def test_product_events_leg_matches_the_identity_index_verbatim():
    """⚠ 表达式索引必须与查询里的表达式**逐字一致**才会被用上。

    写成 `asin = ANY(...) OR sku = ANY(...)` 语义相同但用不上
    product_events_identity_idx,几百万行全表扫 —— 生产挂死过一次
    (audit_listing_conflicts 视图,schema.sql 表注)。
    """
    assert "coalesce(asin, sku) = ANY(%(asins)s::text[])" in rt._EVENTS_SQL
    assert "asin = ANY" not in rt._EVENTS_SQL.replace(
        "coalesce(asin, sku) = ANY", "")
    assert "store IS NOT NULL" in rt._EVENTS_SQL


def test_claims_leg_reads_released_rows_too():
    """released 行是"这个品牌当初属于谁"的唯一答案,按 status 过滤会让历史消失。"""
    assert "status" not in rt._CLAIMS_SQL
    assert "status" not in rt._CLAIMS_WITH_BRAND_SQL
    assert "kind = 'brand'" not in rt._CLAIMS_SQL          # 无 bkey 时不走品牌腿
    assert "kind = 'brand'" in rt._CLAIMS_WITH_BRAND_SQL


def test_items_leg_has_all_three_still_listed_conditions():
    """a+b 在 SQL 里(退市品 missing_since 也是 NULL);c 在册由调用方传进来。"""
    assert "missing_since IS NULL" in rt._ITEMS_SQL
    assert "coalesce(upper(lifecycle_status), 'ACTIVE') = 'ACTIVE'" in rt._ITEMS_SQL


def test_brand_leg_skipped_when_no_brand_key():
    """没有品牌键时走的是不含品牌腿的那条 SQL(显式路由,不靠 `= NULL` 空转)。"""
    conn = _Conn({_T_CLAIMS: []})
    rt.stores_of_asins(conn, ["B0AAA"])
    claims_sql = [s for s, _ in conn.calls if _T_CLAIMS in s]
    assert len(claims_sql) == 1 and "kind = 'brand'" not in claims_sql[0]
    conn2 = _Conn({_T_CLAIMS: []})
    rt.stores_of_asins(conn2, ["B0AAA"], brand_key_norm="acme")
    assert "kind = 'brand'" in [s for s, _ in conn2.calls if _T_CLAIMS in s][0]


def test_every_sql_param_is_cast_in_risk_trace():
    """与 test_store_events 同款 lint 护栏(dispositions 生产实炸三次的教训)。"""
    src = pathlib.Path("services/risk_trace.py").read_text()
    bad = []
    for m in re.finditer(r'^(_\w*SQL)\s*=\s*"""(.*?)"""', src, re.S | re.M):
        for pm in re.finditer(r"%\((\w+)\)s(?!\s*::)", m.group(2)):
            bad.append(f"{m.group(1)}.{pm.group(1)}")
    assert not bad, "这些 SQL 参数没写显式 ::类型:" + ", ".join(bad)


# ── 合并、在册过滤、排序 ────────────────────────────────────────────────────

def _rows_for_merge():
    return {
        _T_ITEMS: [("A085", True, ["B0AAA"], "2026-01-01", "2026-08-01"),
                   ("谭总9", False, ["B0BBB"], "2026-02-01", "2026-03-01")],
        _T_SOURCES: [("A085", ["B0AAA"], "2025-12-01", "2025-12-01")],
        _T_EVENTS: [("81刘何秀", ["B0AAA"], "2026-04-01", "2026-04-02")],
        _T_CLAIMS: [("谭总10", "brand", ["acme"], "2026-01-05", "2026-06-05")],
    }


def test_merge_unions_four_sources_and_labels_evidence():
    conn = _Conn(_rows_for_merge())
    hits = rt.stores_of_asins(conn, ["B0AAA", "B0BBB"], brand_key_norm="acme")
    assert hits["A085"]["evidence"] == [rt.EV_ITEM, rt.EV_SOURCE]
    assert hits["81刘何秀"]["evidence"] == [rt.EV_EVENT]
    assert hits["谭总10"]["evidence"] == [rt.EV_CLAIM_BRAND]
    # 品牌占用的 claim_key 是品牌键不是 ASIN,不许混进 asins 里
    assert hits["谭总10"]["asins"] == []
    # 时间跨源取 min/max
    assert hits["A085"]["first_at"] == "2025-12-01"
    assert hits["A085"]["last_at"] == "2026-08-01"


def test_store_order_uses_sort_key_not_sql_collation():
    """PG collation 在主排序级把中文整个忽略,排序是业务规则得写在代码里。"""
    conn = _Conn(_rows_for_merge())
    hits = rt.stores_of_asins(conn, ["B0AAA", "B0BBB"], brand_key_norm="acme")
    assert list(hits) == ["A085", "81刘何秀", "谭总9", "谭总10"]


def test_registered_filter_is_the_third_still_listed_condition():
    """死店的 walmart_items 行永久冻结为"在架"(allocation_plan §9.4)。"""
    conn = _Conn(_rows_for_merge())
    # 不传在册集合:只算 a+b,原始判定原样留着,registered=None 表示"没校验过"
    loose = rt.stores_of_asins(conn, ["B0AAA"])
    assert loose["A085"]["still_listed"] is True
    assert loose["A085"]["registered"] is None

    tight = rt.stores_of_asins(_Conn(_rows_for_merge()), ["B0AAA"],
                               registered={"谭总9"})
    assert tight["A085"]["still_listed"] is False       # 已从凭证表删掉的死店
    assert tight["A085"]["listed_in_items"] is True     # 原始判定不被抹掉
    assert tight["A085"]["registered"] is False


# ── 沙箱 PG 集成 ────────────────────────────────────────────────────────────
#
# ⚠ 这里的地址是**测试夹具**,不是生产资源(生产走 registry/db.pg_dsn() 的
# unix socket)。固定在非标准端口 55432 上正是为了不可能连到生产库 ——
# 本节会造数据(全部在一个最后回滚的事务里,不留残渣)。
_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = os.environ.get(
    "WALMART_TEST_PG_DSN",
    f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")

# 造数用的标记值:与库里既有数据绝无重叠,断言才敢写死
_A1, _A2 = "B0RISKTRACE01", "B0RISKTRACE02"
_BRAND = "Zqx Risktrace"
_BKEY = "zqx risktrace"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。

    连接只准走 registry/db(工程规范:禁止自行 psycopg.connect),所以改的是
    它读的那个环境变量而不是绕过它。
    """
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()          # 用例不留残渣;pg_conn 随后 commit 空事务


def _seed(conn):
    """输入:连接 → 输出:无。造一个品牌两个 ASIN 散落五店的历史。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.products (marketplace, asin, brand) "
            "VALUES ('US', %s, %s), ('US', %s, %s)", (_A1, _BRAND, _A2, _BRAND))
        cur.execute(
            "INSERT INTO catalog.walmart_items "
            "(store, sku, missing_since, lifecycle_status, last_seen_at) VALUES "
            "('A085', %s, NULL, NULL, now()),"          # 在架
            "('B012', %s, now(), NULL, now()),"         # 历史:已缺席
            "('R900', %s, NULL, 'RETIRED', now())",     # 退市:missing_since 也是 NULL
            (_A1, _A2, _A1))
        cur.execute(
            "INSERT INTO catalog.listing_sources (store, sku, source_type, source_key)"
            " VALUES ('A085', %s, 'amz', %s)", (_A1, _A1))
        cur.execute(
            "INSERT INTO catalog.claims (kind, claim_key, store, status, source,"
            " released_at) VALUES ('brand', %s, '谭总9', 'released', 'test', now())",
            (_BKEY,))
        cur.execute(
            "INSERT INTO catalog.product_events (sku, asin, store, event, source)"
            " VALUES (%s, %s, '81刘何秀', 'list_submitted', 'test')", (_A2, _A2))


@needs_pg
def test_pg_brand_expands_to_its_asins(pg):
    _seed(pg)
    assert rt.asins_of_brand(pg, _BRAND) == [_A1, _A2]
    assert rt.asins_of_brand(pg, "ZQX RISKTRACE") == [_A1, _A2]   # 小写形
    assert rt.asins_of_brand(pg, "  Zqx  Risktrace ") == [_A1, _A2]  # 压空格形


@needs_pg
def test_pg_dirty_stored_brand_is_a_known_miss(pg):
    """⚠ 归一只在 Python 的**代价**,写清楚免得下次有人当 bug 修。

    候选串归一的是**输入**:输入带多余空白 → 压空格形能命中库里的干净值;
    反过来(库里存的是脏值、输入干净)等值比不上。这是接受的取舍 ——
    改法只有"SQL 侧再写一套归一",那就是仓里第四套各漂各的归一算法,
    而漏掉的这几个 ASIN 由另外三个证据源兜(见模块头注)。
    """
    _seed(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.products (marketplace, asin, brand)"
                    " VALUES ('US', 'B0RTDIRTY1', 'Zqx  Risktrace')")
    assert "B0RTDIRTY1" not in rt.asins_of_brand(pg, _BRAND)


@needs_pg
def test_pg_manufacturer_fallback_only_when_brand_is_empty(pg):
    """brand 空/占位符时真品牌在 manufacturer(risk_gate 双字段实证同源)。"""
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.products (marketplace, asin, brand, slow) VALUES"
            " ('US', 'B0RTMFR01', '', %s::jsonb),"        # brand 空 → 兜底腿算数
            " ('US', 'B0RTMFR02', 'OEM', %s::jsonb),"     # 占位符 → 兜底腿算数
            " ('US', 'B0RTMFR03', 'Other Brand', %s::jsonb)",  # 有真品牌 → 不算
            ('{"manufacturer": "Zqx Risktrace"}',) * 3)
    got = rt.asins_of_brand(pg, "Zqx Risktrace")
    assert got == ["B0RTMFR01", "B0RTMFR02"]


@needs_pg
def test_pg_traces_all_four_evidence_sources(pg):
    _seed(pg)
    bkey, asins, hits = rt.stores_of_brand(pg, _BRAND)
    assert bkey == _BKEY and asins == [_A1, _A2]
    # 展示序 = stores.sort_key:字母 → 数字 → 中文
    assert list(hits) == ["A085", "B012", "R900", "81刘何秀", "谭总9"]
    assert hits["A085"]["evidence"] == [rt.EV_ITEM, rt.EV_SOURCE]
    assert hits["B012"]["evidence"] == [rt.EV_ITEM]
    assert hits["81刘何秀"]["evidence"] == [rt.EV_EVENT]
    assert hits["谭总9"]["evidence"] == [rt.EV_CLAIM_BRAND]      # 只有 released 行
    # still_listed 只有 A085:B012 已缺席、R900 退市(missing_since 也是 NULL)、
    # 另两家压根没有在架表的行
    assert [s for s, v in hits.items() if v["still_listed"]] == ["A085"]
    assert hits["R900"]["listed_in_items"] is False


@needs_pg
def test_pg_registered_filter_drops_dead_stores(pg):
    """死店的在架行永久冻结,在册集合是 still_listed 的第三个条件。"""
    _seed(pg)
    _, _, hits = rt.stores_of_brand(pg, _BRAND, registered={"B012", "谭总9"})
    assert hits["A085"]["still_listed"] is False        # 不在册 = 死店
    assert hits["A085"]["listed_in_items"] is True
    assert hits["A085"]["registered"] is False


@needs_pg
def test_pg_product_claims_leg_is_not_filtered_by_status(pg):
    """产品占用腿同样读 released 行(货清干净后它是唯一的归属证据)。"""
    _seed(pg)
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.claims (kind, claim_key, store, status, source)"
            " VALUES ('product', %s, 'C777', 'released', 'test')", (_A1,))
    _, _, hits = rt.stores_of_brand(pg, _BRAND)
    assert hits["C777"]["evidence"] == [rt.EV_CLAIM_PRODUCT]
    assert hits["C777"]["asins"] == [_A1]


@needs_pg
def test_pg_new_indexes_exist_after_schema_apply(pg):
    """两个新索引:现有索引都带 status='active' 局部条件 / 主键反查不到 source_key。"""
    with pg.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='catalog'"
                    " AND indexname = ANY(%s::text[])",
                    (["claims_key_all_idx", "listing_sources_key_idx"],))
        assert sorted(r[0] for r in cur.fetchall()) == [
            "claims_key_all_idx", "listing_sources_key_idx"]


@needs_pg
def test_pg_identity_index_is_actually_used(pg):
    """身份键腿走的是 product_events_identity_idx,不是全表扫(生产挂死过)。

    ⚠ 空表上 PG 会挑顺序扫(那才是对的),所以这里只在计划里确认**表达式对得上**
    索引定义 —— 强制 enable_seqscan=off 让规划器必须给出索引路径。
    """
    with pg.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN " + rt._EVENTS_SQL, {"asins": [_A1]})
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert "product_events_identity_idx" in plan, plan
