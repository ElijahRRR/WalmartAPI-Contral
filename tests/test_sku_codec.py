"""12 位不透明码的编码规则与码的生命周期(services/sku_codec)。

本模块在批次 0a **只建不接线**,所以这份用例是它落地当天唯一的证据。
每条用例的 docstring 写清它防的是**哪一种静默失效** —— 抽码/弃码这类动作
出错时不会报错,只会在几周后表现成"同店两条同内容 listing"或"一批 UPC
凭空少了"。
"""

import inspect

import pytest

from registry import resources
from services import listing_sources, product_events, sku_codec, upc_pool


# ── 假连接:照抄 tests/test_sku_asin.py 的 _Cur/_Conn 写法 ────────────────────

class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def rowcount(self):
        return self.conn.rowcount

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        self.conn.route(sql, args)

    def executemany(self, sql, seq):
        self.conn.calls.append((sql, list(seq)))

    def fetchone(self):
        return self.conn.next_row


class _Conn:
    """脚本化的假连接:按 SQL 形状路由,每种查询的返回值由构造参数排队给出。"""

    def __init__(self, live=None, minted=None, abandon_row=None, rowcount=1):
        self.live = list(live or [])          # 每次复用查询的返回值(元组或 None)
        self.minted = list(minted or [])      # 每次 INSERT 是否拿到行(bool)
        self.abandon_row = abandon_row        # UPDATE ... RETURNING 的那一行
        self.rowcount = rowcount
        self.calls: list = []
        self.next_row = None

    def cursor(self):
        return _Cur(self)

    def route(self, sql, args):
        s = sql.strip()
        if s.startswith("SELECT sku FROM catalog.listing_sources"):
            self.next_row = self.live.pop(0) if self.live else None
        elif "INSERT INTO catalog.listing_sources" in s:
            ok = self.minted.pop(0) if self.minted else True
            self.next_row = (args[1],) if ok else None
        elif "UPDATE catalog.listing_sources" in s:
            self.next_row = self.abandon_row
        else:
            self.next_row = None

    def inserts(self):
        return [args for sql, args in self.calls
                if isinstance(sql, str) and "INSERT INTO catalog.listing_sources" in sql]

    def burns(self):
        return [(sql, args) for sql, args in self.calls
                if isinstance(sql, str) and "catalog.upc_pool" in sql]

    def events(self):
        for sql, rows in self.calls:
            if isinstance(sql, str) and "catalog.product_events" in sql:
                return rows
        return []


# ── 形态判据 ─────────────────────────────────────────────────────────────────

def test_is_opaque_rejects_every_legacy_shape():
    """存量形态一个都不许被当成新码 —— 判错就等于拿存量行去撞新码的唯一索引,
    而且 source_of 会给它编一个来源出来。"""
    for legacy in ("B0ABCDEFGH", "XKJ-B0GXX75JN5-39.98", "JTZW-D01027HVK3W-38",
                   "102460018738", "PHUMWMT12345", "", None,
                   "AK7QM2X9RT4",          # 11 位:短一位
                   "AK7QM2X9RT4WX",        # 13 位:长一位
                   "AK7QM2X9RT4O",         # 含剔掉的 O
                   "AK7QM2X9RT-W"):        # 含分隔符
        assert not sku_codec.is_opaque(legacy), legacy


def test_is_opaque_needs_a_letter_so_numeric_item_ids_never_pass():
    """12 位纯数字的沃尔玛 item id 若恰好不含 0/1,前两条判据会放它过去 ——
    「至少一个字母」这条不能省,不然平台侧编号会被当成我们抽的码
    (schema.sql 两条部分唯一索引的 `AND sku ~ '[A-Z]'` 与它同口径)。"""
    assert not sku_codec.is_opaque("234567892345")     # 12 位纯数字,全在字母表内
    assert sku_codec.is_opaque("23456789234A")


def test_alphabet_excludes_the_confusable_glyphs():
    """0/O、1/I/L、U 必须不在字母表里:码要人抄、要 OCR,混淆对会变成
    "查无此码"的工单;U 是避免抽出脏词。"""
    for ch in "01OILU":
        assert ch not in sku_codec._ALPHABET, ch
    assert len(sku_codec._ALPHABET) == 30
    assert len(set(sku_codec._ALPHABET)) == 30
    assert sku_codec._LEN == 1 + sku_codec._RANDOM_LEN == 12


def test_placeholder_can_never_pass_as_an_opaque_code():
    """空跑占位码必须永远判否:判是的那一刻,一个 dry-run 的字符串就可能被
    下游当成真码去对账、去落库。"""
    assert len(sku_codec.DRYRUN_PLACEHOLDER) == sku_codec._LEN
    assert not sku_codec.is_opaque(sku_codec.DRYRUN_PLACEHOLDER)
    assert sku_codec.source_of(sku_codec.DRYRUN_PLACEHOLDER) is None


# ── 来源字母(0a-01:registry 只登记取值)────────────────────────────────────

def test_source_letters_are_distinct_and_inside_the_alphabet():
    """字母互不相同、都在字母表里、都是字母(不是数字)。重了 = source_of
    永远答错一半;不在字母表里 = 抽出来的码落不进部分唯一索引。"""
    letters = resources.SKU_SOURCE_LETTERS
    assert set(letters) <= {listing_sources.SOURCE_AMZ, listing_sources.SOURCE_MATCH,
                            listing_sources.SOURCE_SELF, listing_sources.SOURCE_1688}
    assert len(set(letters.values())) == len(letters)
    for source_type, ch in letters.items():
        assert ch in sku_codec._ALPHABET and ch.isalpha(), (source_type, ch)


def test_source_of_returns_none_until_the_owner_picks_letters(monkeypatch):
    """字母表未定 / 首字母未登记时返 None,**不猜**(缺省不猜是安全红线)。
    存量 SKU 的首位字母什么都不代表,所以只对不透明码回答。"""
    assert sku_codec.source_of("AK7QM2X9RT4W") == "amz"
    assert sku_codec.source_of("B0ABCDEFGH") is None            # 存量裸 ASIN
    assert sku_codec.source_of("ZK7QM2X9RT4W") is None          # 字母没登记
    monkeypatch.setattr(resources, "SKU_SOURCE_LETTERS", {})
    assert sku_codec.source_of("AK7QM2X9RT4W") is None


# ── mint ─────────────────────────────────────────────────────────────────────

def test_mint_reuses_the_live_row_instead_of_drawing():
    """同 (店, 来源, 源头键) 已有活码就复用 —— 依据是「一个 Product ID 只能挂
    一个 SKU」这条官方约束:抽新码去上同一个 Product ID 必撞,还白烧一个 UPC。"""
    conn = _Conn(live=[("AK7QM2X9RT4W",)])
    got = sku_codec.mint(conn, "T1", "amz", "B0ABCDEFGH", workflow="list_new")
    assert got == "AK7QM2X9RT4W"
    assert conn.inserts() == []                       # 一次都没抽
    sql = conn.calls[0][0]
    assert "abandoned_at IS NULL" in sql and "replaced_by IS NULL" in sql


def test_mint_draws_and_registers_in_the_same_call():
    """抽码与登记同一函数同一事务 —— 「抽了没登记」这种中间态一旦存在,
    下一轮会再抽一个新码去上同一个品。"""
    conn = _Conn()
    code = sku_codec.mint(conn, "T1", "amz", "B0ABCDEFGH", workflow="list_new")
    assert sku_codec.is_opaque(code)
    assert code[0] == resources.SKU_SOURCE_LETTERS["amz"]
    assert conn.inserts() == [("T1", code, "amz", "B0ABCDEFGH", "list_new")]
    assert "ON CONFLICT DO NOTHING" in conn.calls[1][0]


def test_mint_returns_the_other_process_code_on_a_live_key_conflict(caplog):
    """并发双 mint:INSERT 落空后重跑复用查询,查到就返回**对方那个码**。

    防的是把并发诊断成随机源故障:合成一个 catch-all 重抽分支的话,这里会
    连撞 5 次(对方的活码键一直在)最后抛"随机源坏了",排障方向全错。
    """
    import logging
    before = sku_codec._concurrent_mint
    conn = _Conn(live=[None, ("AOTHER234567",)], minted=[False])
    with caplog.at_level(logging.WARNING, logger="services.sku_codec"):
        got = sku_codec.mint(conn, "T1", "amz", "B0ABCDEFGH", workflow="list_new")
    assert got == "AOTHER234567"
    assert len(conn.inserts()) == 1                    # 只抽了一次就认输复用
    assert sku_codec._concurrent_mint == before + 1
    assert any("并发 mint" in m for m in caplog.messages)


def test_mint_redraws_on_a_random_collision_then_raises_loudly(caplog):
    """真·随机撞码(复用查询仍查不到)才重抽;抽满上限还撞就抛错。

    30^11 的空间下连撞 5 次不是运气问题,是随机源坏了 —— 静默兜底会让一个
    坏掉的随机源持续发出重复码,而重复码 = 两个品共用一条沃尔玛记录。
    """
    import logging
    before = sku_codec._sku_redraws
    conn = _Conn(minted=[False] * sku_codec._MAX_DRAWS)
    with caplog.at_level(logging.INFO, logger="services.sku_codec"):
        with pytest.raises(RuntimeError, match="撞码"):
            sku_codec.mint(conn, "T1", "amz", "B0ABCDEFGH", workflow="list_new")
    assert len(conn.inserts()) == sku_codec._MAX_DRAWS
    assert sku_codec._sku_redraws == before + sku_codec._MAX_DRAWS
    assert any("重抽" in m for m in caplog.messages)


def test_mint_has_no_dry_run_switch():
    """写库函数不设「这次不写」模式:两条路径迟早各自演化,而 dry-run 那条
    没人跑。空跑由调用方不调 mint + 用 DRYRUN_PLACEHOLDER 表达(单一路径)。"""
    params = inspect.signature(sku_codec.mint).parameters
    assert "dry_run" not in params and "execute" not in params
    assert list(params) == ["conn", "store", "source_type", "source_key", "workflow"]


def test_mint_refuses_an_unmapped_source_type():
    """来源字母没登记就抛错并点名 registry 常量 —— 缺省不猜:随手兜一个字母
    出去,码的首位从此对不上来源,而且不报错。"""
    with pytest.raises(ValueError, match="SKU_SOURCE_LETTERS"):
        sku_codec.mint(_Conn(), "T1", "unknown", "X", workflow="list_new")


# ── abandon ──────────────────────────────────────────────────────────────────

def test_abandon_refuses_an_unregistered_reason():
    """弃码点只有四个。第五个原因出现时应该是有人改了词表,而不是随手传了
    个字符串进来(否则"为什么弃的"这一列会长出永远没人查的分叉)。"""
    with pytest.raises(ValueError, match="未登记的弃码原因"):
        sku_codec.abandon(_Conn(), "T1", "AK7QM2X9RT4W", "product_clear_retire")


def test_abandon_is_idempotent():
    """已弃或不存在 ⇒ 返 False 且**不再记事件、不再烧号**。重放一次就多烧一个
    UPC 的话,自愈链重试会把号池吃空。"""
    conn = _Conn(abandon_row=None)
    assert sku_codec.abandon(conn, "T1", "AK7QM2X9RT4W",
                             sku_codec.ABANDON_DELETE_VERIFIED) is False
    assert conn.burns() == [] and conn.events() == []
    sql = conn.calls[0][0]
    assert "abandoned_at IS NULL" in sql                  # 只动活行


def test_abandon_burns_only_for_amz_rows_and_only_for_burning_reasons():
    """码与 UPC 同寿命,但只有 amz 行的 source_key 才是 ASIN。

    match 行的 source_key 是匹配 GTIN,拿它当 (店, ASIN) 去烧号会误伤别人的号;
    upc_conflict 不烧是因为该号已被撞库处置标成 conflict,再烧一次会把"撞库"
    这个真相盖掉。
    """
    conn = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    assert sku_codec.abandon(conn, "T1", "AK7QM2X9RT4W",
                             sku_codec.ABANDON_SKU_LOCKED) is True
    assert conn.burns() and conn.burns()[0][1][1] == ["B0ABCDEFGH"]

    match = _Conn(abandon_row=("match", "00842565531441"))
    assert sku_codec.abandon(match, "T1", "BK7QM2X9RT4W",
                             sku_codec.ABANDON_DELETE_VERIFIED) is True
    assert match.burns() == []                            # 只标不烧

    conflict = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    sku_codec.abandon(conflict, "T1", "AK7QM2X9RT4W", sku_codec.ABANDON_UPC_CONFLICT)
    assert conflict.burns() == []
    assert set(sku_codec._BURN_STATUS) == {sku_codec.ABANDON_DELETE_VERIFIED,
                                           sku_codec.ABANDON_SKU_LOCKED}
    assert set(sku_codec._BURN_STATUS.values()) == {upc_pool.BURN_DELETE,
                                                    upc_pool.BURN_LOCK}


def test_abandon_never_burns_on_sku_update():
    """改码时 item 还在、UPC 还绑着 —— 烧号等于白烧一个号,还得再领一个。"""
    conn = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    assert sku_codec.abandon(conn, "T1", "AK7QM2X9RT4W",
                             sku_codec.ABANDON_SKU_UPDATE,
                             replaced_by="AN3WC0DE2345") is True
    assert conn.burns() == []


def test_abandon_records_the_ledger_event_with_source_key_in_detail():
    """detail **必带 source_key**:不透明码在 product_events.asin 列里提不出来,
    list_new 的代际过滤读的正是 detail->>'source_key';不带它,代际过滤会退化成
    跨码累计,重试上限提前触顶。"""
    import json
    conn = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    sku_codec.abandon(conn, "T1", "AK7QM2X9RT4W", sku_codec.ABANDON_DELETE_VERIFIED)
    row = conn.events()[0]
    assert row[0] == "AK7QM2X9RT4W" and row[3] == product_events.SKU_ABANDONED
    detail = json.loads(row[6])
    assert detail["source_key"] == "B0ABCDEFGH"
    assert detail["reason"] == sku_codec.ABANDON_DELETE_VERIFIED
    assert detail["old_sku"] == "AK7QM2X9RT4W" and detail["source_type"] == "amz"
    assert detail["burned_upcs"] == 1

    # 改码走 sku_replaced,detail 多一个 new_sku
    rep = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    sku_codec.abandon(rep, "T1", "AK7QM2X9RT4W", sku_codec.ABANDON_SKU_UPDATE,
                      replaced_by="AN3WC0DE2345")
    row = rep.events()[0]
    assert row[3] == product_events.SKU_REPLACED
    assert json.loads(row[6])["new_sku"] == "AN3WC0DE2345"


def test_the_two_code_events_are_registered_in_the_ledger():
    """record_many 对未登记事件码 fail loud —— 不先登记,abandon 在批次 2
    接线的那一天才会炸(而那天是在生产上)。"""
    assert product_events.SKU_ABANDONED in product_events.EVENTS
    assert product_events.SKU_REPLACED in product_events.EVENTS
