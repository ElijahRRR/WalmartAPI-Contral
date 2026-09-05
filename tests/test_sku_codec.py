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

    def fetchall(self):
        # record_many 的登记簿反查(批次 0b):本夹具不造登记行 ⇒ 走形态腿
        return []


class _Conn:
    """脚本化的假连接:按 SQL 形状路由,每种查询的返回值由构造参数排队给出。"""

    def __init__(self, live=None, minted=None, abandon_row=None, rowcount=1,
                 old_row=None, pending=None, marked=True, cleared=True):
        self.live = list(live or [])          # 每次复用查询的返回值(元组或 None)
        self.minted = list(minted or [])      # 每次 INSERT 是否拿到行(bool)
        self.abandon_row = abandon_row        # UPDATE ... RETURNING 的那一行
        self.old_row = old_row                # 改码:旧行现状(replaced_by, abandoned_at, 来源, 源头键)
        self.pending = list(pending or [])    # 改码:在途新码查询的返回值(元组或 None)
        self.marked = marked                  # 改码:旧行进入 pending 是否改到了行
        self.cleared = cleared                # 回滚:旧行指针是否清到了行
        self.rowcount = rowcount
        self.calls: list = []
        self.commits = 0                      # 积木不许自己 commit(归调用方)
        self.next_row = None

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def route(self, sql, args):
        s = sql.strip()
        if s.startswith("SELECT sku FROM catalog.listing_sources"):
            self.next_row = self.live.pop(0) if self.live else None
        elif s.startswith("SELECT replaced_by"):
            self.next_row = self.old_row
        elif s.startswith("SELECT n.sku"):
            self.next_row = self.pending.pop(0) if self.pending else None
        elif "INSERT INTO catalog.listing_sources" in s:
            ok = self.minted.pop(0) if self.minted else True
            self.next_row = (args[1],) if ok else None
        elif "SET replaced_by = NULL" in s:
            self.next_row = (args[1],) if self.cleared else None
        elif "SET replaced_by = %s" in s:
            self.next_row = (args[2],) if self.marked else None
        elif "UPDATE catalog.listing_sources" in s:
            self.next_row = self.abandon_row
        else:
            self.next_row = None

    def inserts(self):
        return [args for sql, args in self.calls
                if isinstance(sql, str) and "INSERT INTO catalog.listing_sources" in sql]

    def updates(self, needle: str):
        return [args for sql, args in self.calls
                if isinstance(sql, str) and "UPDATE catalog.listing_sources" in sql
                and needle in sql]

    def burns(self):
        return [(sql, args) for sql, args in self.calls
                if isinstance(sql, str) and "catalog.upc_pool" in sql]

    def events(self):
        for sql, rows in self.calls:
            if isinstance(sql, str) and "catalog.product_events" in sql:
                return rows
        return []

    def event_calls(self):
        """每一次 record_many 各算一次(改码定案会写两条:旧码一条、新码一条)。"""
        return [rows for sql, rows in self.calls
                if isinstance(sql, str) and "catalog.product_events" in sql]


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

    match 行的 source_key 是匹配 GTIN,拿它当 (店, ASIN) 去烧号会误伤别人的号。
    烧成什么状态**只看 _BURN_STATUS 这一张分派表**(批次 2 决策 D):
    sku_locked→burned_lock、delete_verified→burned_delete、
    upc_conflict→conflict(那个值的语义就是"号被别人占了",写 burned_* 反而把
    "是谁先占了号"这条排障线索盖掉);sku_update 不烧。
    """
    conn = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    assert sku_codec.abandon(conn, "T1", "AK7QM2X9RT4W",
                             sku_codec.ABANDON_SKU_LOCKED) is True
    assert conn.burns() and conn.burns()[0][1] == (
        upc_pool.BURN_LOCK, ["T1"], ["B0ABCDEFGH"])

    match = _Conn(abandon_row=("match", "00842565531441"))
    assert sku_codec.abandon(match, "T1", "BK7QM2X9RT4W",
                             sku_codec.ABANDON_DELETE_VERIFIED) is True
    assert match.burns() == []                            # 只标不烧

    # 撞库:批次 2 起**也烧**(决策 B:码与 UPC 一起换),状态写 conflict
    conflict = _Conn(abandon_row=("amz", "B0ABCDEFGH"))
    sku_codec.abandon(conflict, "T1", "AK7QM2X9RT4W",
                      sku_codec.ABANDON_UPC_CONFLICT)
    assert conflict.burns()[0][1] == (upc_pool.CONFLICT, ["T1"], ["B0ABCDEFGH"])
    assert set(sku_codec._BURN_STATUS) == {sku_codec.ABANDON_DELETE_VERIFIED,
                                           sku_codec.ABANDON_SKU_LOCKED,
                                           sku_codec.ABANDON_UPC_CONFLICT}
    assert set(sku_codec._BURN_STATUS.values()) == {
        upc_pool.BURN_DELETE, upc_pool.BURN_LOCK, upc_pool.CONFLICT}


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


# ── 改码:mint_replacement(批次 3 地基,C1/C3)────────────────────────────────

def test_mint_replacement_writes_both_pointers_in_one_transaction():
    """新码行 replaces=旧码、旧行 replaced_by=新码,**同一事务两条指针一次写完**。

    只写一头的后果是静默的:只写新行 ⇒ 旧行仍是活码,下一轮 mint 把它当活码复用,
    改码从没发生过;只写旧行 ⇒ 新码没有出身,sources_backfill 判 unknown、
    整条自动化对它退出。commit **不在这里**:先落库再调接口是调用方的动作。
    """
    conn = _Conn(old_row=(None, None, "amz", "B0ABCDEFGH"), pending=[None])
    code = sku_codec.mint_replacement(conn, "T1", "B0ABCDEFGH", "amz", "B0ABCDEFGH")
    assert sku_codec.is_opaque(code)
    assert code[0] == resources.SKU_SOURCE_LETTERS["amz"]
    assert conn.inserts() == [("T1", code, "amz", "B0ABCDEFGH",
                               "sku_migrate", "B0ABCDEFGH")]
    assert conn.updates("SET replaced_by = %s") == [(code, "T1", "B0ABCDEFGH")]
    assert conn.commits == 0        # 积木不自己 commit(事务边界归调用方)


def test_mint_replacement_is_idempotent_and_returns_the_same_code():
    """pending 行已落库、feed 还没发就崩了 ⇒ 下一轮必须拿回**同一个码**。

    换码 = 载荷变了 = feeds.payload_key 防重不命中 = 同一个 item 被改两次码。
    """
    conn = _Conn(old_row=("AN3WC0DE2345", None, "amz", "B0ABCDEFGH"),
                 pending=[("AN3WC0DE2345",)])
    got = sku_codec.mint_replacement(conn, "T1", "B0ABCDEFGH", "amz", "B0ABCDEFGH")
    assert got == "AN3WC0DE2345"
    assert conn.inserts() == []                      # 一次都没抽
    assert conn.updates("SET replaced_by = %s") == []


def test_mint_replacement_never_reuses_the_live_row():
    """与 mint 语义相反:明知有活行也要新码 —— 所以它**根本不跑**复用查询。

    给 mint 加一个 force 开关就是同一个能力两条语义分支(conventions §六);
    真跑了复用查询,改码会把旧码原样返回,一整轮改码静默变成空操作。
    """
    conn = _Conn(old_row=(None, None, "amz", "B0ABCDEFGH"), pending=[None],
                 live=[("B0ABCDEFGH",)])
    code = sku_codec.mint_replacement(conn, "T1", "B0ABCDEFGH", "amz", "B0ABCDEFGH")
    assert code != "B0ABCDEFGH" and sku_codec.is_opaque(code)
    assert not any(str(sql).strip().startswith("SELECT sku FROM catalog.listing_sources")
                   for sql, _ in conn.calls)


def test_mint_replacement_returns_the_other_process_code_on_live_key_conflict(caplog):
    """并发双改码:INSERT 落空后重跑在途查询,查到就返回**对方那个码**。

    合成一个 catch-all 重抽分支的话,这里会连撞 5 次(对方的 replaces 键一直在)
    最后抛"随机源坏了",排障方向全错 —— 与 mint 同款分支纪律。
    """
    import logging
    before = sku_codec._concurrent_mint
    conn = _Conn(old_row=(None, None, "amz", "B0ABCDEFGH"),
                 pending=[None, ("AOTHER234567",)], minted=[False])
    with caplog.at_level(logging.WARNING, logger="services.sku_codec"):
        got = sku_codec.mint_replacement(conn, "T1", "B0ABCDEFGH", "amz",
                                         "B0ABCDEFGH")
    assert got == "AOTHER234567"
    assert len(conn.inserts()) == 1                  # 只抽了一次就认输复用
    assert sku_codec._concurrent_mint == before + 1
    assert any("并发改码" in m for m in caplog.messages)


def test_mint_replacement_refuses_an_unregistered_or_abandoned_old_sku():
    """旧码没登记 / 已弃码 ⇒ **fail loud**,不猜。

    没登记就改码 = 新码继承了一个我们说不清出身的品(sources_backfill 判 unknown,
    自动化对它全线退出);对已弃码的行再改码 = 把一条我们已经当它不存在的沃尔玛
    记录又改了一次。
    """
    with pytest.raises(ValueError, match="不在登记簿"):
        sku_codec.mint_replacement(_Conn(old_row=None), "T1", "B0X", "amz", "B0X")
    import datetime
    gone = _Conn(old_row=(None, datetime.datetime(2026, 9, 1), "amz", "B0X"))
    with pytest.raises(ValueError, match="已弃码"):
        sku_codec.mint_replacement(gone, "T1", "B0X", "amz", "B0X")


def test_mint_skips_a_row_that_is_being_replaced():
    """mint 的复用查询带 `replaced_by IS NULL`:在途改码的旧行**不算活码**。

    不带这一条,pending 期间 list_new 对同一 (店, 来源, 源头键) 调 mint 会拿回
    那个已经宣告要退休的旧码,把它再发一次 MP_ITEM。条件必须与活码部分唯一索引
    逐字相同(守门 test_mint_live_row_filter_matches_the_index_condition)。
    """
    assert "abandoned_at IS NULL AND replaced_by IS NULL" in sku_codec._SQL_LIVE
    conn = _Conn(live=[None])          # 旧行被 replaced_by 谓词滤掉 ⇒ 查不到活码
    code = sku_codec.mint(conn, "T1", "amz", "B0ABCDEFGH", workflow="list_new")
    assert sku_codec.is_opaque(code) and code != "B0ABCDEFGH"


# ── 改码:settle_replacement(批次 3 地基,C2)────────────────────────────────

_OLD, _NEW = "B0ABCDEFGH", "AN3WC0DE2345"


def test_confirmed_abandons_old_row_with_sku_update_and_does_not_burn_upc():
    """定案 confirmed:旧行走**唯一的弃码出口** abandon(reason='sku_update'),
    **不烧 UPC**(GTIN 仍挂在同一个 item 上,烧了下次 claim 会去领新号 =
    自造 SKU_LOCKED),并给新码补一条 sku_replaced 出生事件。"""
    import json
    conn = _Conn(abandon_row=("amz", _OLD))
    sku_codec.settle_replacement(conn, "T1", _OLD, _NEW, "confirmed",
                                 reason="observed")
    assert conn.burns() == []                       # sku_update 不在烧号分派表里
    (reason, replaced_by, store, sku) = conn.updates("SET abandoned_at")[0]
    assert (reason, replaced_by, store, sku) == (
        sku_codec.ABANDON_SKU_UPDATE, _NEW, "T1", _OLD)
    old_ev, new_ev = conn.event_calls()             # 旧码一条、新码一条
    assert old_ev[0][0] == _OLD and old_ev[0][3] == product_events.SKU_REPLACED
    assert new_ev[0][0] == _NEW and new_ev[0][3] == product_events.SKU_REPLACED
    assert json.loads(new_ev[0][6])["old_sku"] == _OLD


def test_rolled_back_clears_replaced_by_and_abandons_the_new_code():
    """定案 rolled_back:旧行的 replaced_by/replaced_at 清空(回到活码,下轮可重来),
    新码显式弃掉(reason='sku_update_failed')而不是删行 —— 登记簿的行永不 DELETE,
    弃掉的码从此不会被任何人抽到,这就是"码是免费的、失败就换一个"。"""
    conn = _Conn(abandon_row=("amz", _OLD))
    sku_codec.settle_replacement(conn, "T1", _OLD, _NEW, "rolled_back",
                                 reason="feed rejected")
    assert conn.updates("SET replaced_by = NULL") == [("T1", _OLD, _NEW)]
    (reason, replaced_by, store, sku) = conn.updates("SET abandoned_at")[0]
    assert reason == sku_codec.ABANDON_SKU_UPDATE_FAILED
    assert (replaced_by, store, sku) == (None, "T1", _NEW)
    assert conn.burns() == []                       # 新码从来没配过 UPC
    assert conn.event_calls()[0][0][3] == product_events.SKU_ABANDONED


def test_settle_replacement_is_idempotent():
    """两支都幂等:已定案再调 = no-op(不重复记事件、不重复烧号)。

    定案会被重放:_settle 每轮跑一次,崩溃重入也会再跑一次。
    """
    done = _Conn(abandon_row=None)                  # 旧行早已弃码
    sku_codec.settle_replacement(done, "T1", _OLD, _NEW, "confirmed")
    assert done.event_calls() == [] and done.burns() == []
    back = _Conn(abandon_row=None, cleared=False)   # 指针早已清、新码早已弃
    sku_codec.settle_replacement(back, "T1", _OLD, _NEW, "rolled_back")
    assert back.event_calls() == []
    with pytest.raises(ValueError, match="改码判词"):
        sku_codec.settle_replacement(_Conn(), "T1", _OLD, _NEW, "stalled")


# ── SQL 侧的形态判据(C6)─────────────────────────────────────────────────────

def test_opaque_sql_predicate_is_derived_from_the_alphabet():
    """SQL 侧的不透明码谓词由 `_ALPHABET` / `_LEN` **派生**,不是手打的第二份正则。

    手打就是第二个字母表之家:两份一漂,SQL 判据与 is_opaque 会对"什么是新码"
    给出不同答案,而且全程不报错。
    """
    expected = ("({col} ~ '^[" + sku_codec._ALPHABET + "]{{"
                + str(sku_codec._LEN) + "}}$' AND {col} ~ '[A-Z]')")
    assert sku_codec.OPAQUE_SQL_PREDICATE == expected
    sql = sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku")
    assert f"w.sku ~ '^[{sku_codec._ALPHABET}]{{12}}$'" in sql
    assert "w.sku ~ '[A-Z]'" in sql          # 「至少一个字母」那半条不能漏
    assert "{col}" not in sql
