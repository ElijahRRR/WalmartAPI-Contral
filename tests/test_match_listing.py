"""match_listing 回归:预检候选/Item 构造/行状态机/dry-run 零提交/反哺器。"""

import contextlib

from api import feeds, feishu, items as items_api
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, match_feed, match_sheet
from workflows import match_listing as ml

STORE = {"name": "T1", "client_id": "c", "client_secret": "s", "proxy": None}


def test_spec_candidates_by_length():
    # 12 位走 upc,13-14 位走 gtin;短码 zfill;退化码/超长判无效
    assert match_feed.spec_candidates("012345678905")[0] == ("upc", "012345678905")
    assert match_feed.spec_candidates("12345678")[0] == ("upc", "000012345678")
    assert match_feed.spec_candidates("4006381333931")[0] == \
        ("gtin", "04006381333931")
    assert match_feed.spec_candidates("00000000000000") == []   # 退化码
    assert match_feed.spec_candidates("") == []
    assert match_feed.spec_candidates("123456789012345") == []  # >14 位


def test_build_match_item_five_fields_per_verified_sample():
    # 2026-08-07 对拍定稿:五字段,price/ShippingWeight 裸 number,
    # condition 缺省补 New,productIdentifiers 模板缺失时用预检结果兜底
    spec_raw = {"itemSpecPayload": {"MPItem": [{"Item": {
        "productIdentifiers": {"productId": "06432341052907",
                               "productIdType": "GTIN"}}}]}}
    item = match_feed.build_match_item(spec_raw, "PHUMWMT202608070001",
                                       "14.759", "0.4")
    assert item == {"sku": "PHUMWMT202608070001",
                    "condition": "New",
                    "productIdentifiers": {"productId": "06432341052907",
                                           "productIdType": "GTIN"},
                    "ShippingWeight": 0.4, "price": 14.76}
    # SPEC 无模板时 productIdentifiers 兜底
    bare = match_feed.build_match_item(None, "S", 1, 1,
                                       product_id="00012345678905",
                                       product_id_type="GTIN")
    assert bare["productIdentifiers"]["productId"] == "00012345678905"


def _wire(monkeypatch, sheet_rows, spec_results, stores=(STORE,)):
    calls = {"feeds": [], "writes": [], "events": []}
    monkeypatch.setattr(match_sheet, "read_rows", lambda: [dict(r) for r in sheet_rows])
    monkeypatch.setattr(match_sheet, "write_rows",
                        lambda ups, execute=True: (calls["writes"].extend(ups),
                                                   len(ups))[1] if execute else 0)
    monkeypatch.setattr(ml.stores_svc, "load_stores",
                        lambda names=None: list(stores))
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ml.product_events, "record_many",
                        lambda conn, rows: (calls["events"].extend(rows),
                                            len(rows))[1])
    monkeypatch.setattr(items_api, "search_walmart_spec",
                        lambda store, **kw: spec_results[next(iter(kw.values()))])
    # 发码桩:同 (店, GTIN) 复用同一个码(mint 的活码复用语义)
    calls["mint"] = []
    minted: dict = {}

    def fake_mint(conn, store, source_type, source_key, *, workflow=""):
        calls["mint"].append((store, source_type, source_key, workflow))
        return minted.setdefault((store, source_key),
                                 f"B{len(minted):011d}".replace("0", "2"))

    monkeypatch.setattr(ml.sku_codec, "mint", fake_mint)
    # 两道闸数据(2026-08-12 接入):默认全空=全放行,gate 专测单独喂
    monkeypatch.setattr(ml.risk_gate, "load_gate",
                        lambda conn: {"banned_pts": set(), "brands": set()})
    monkeypatch.setattr(ml.blacklist, "load_banned_asins", lambda conn: {})
    calls["sources"] = []
    monkeypatch.setattr(ml.listing_sources, "register",
                        lambda conn, rows: (calls["sources"].extend(rows),
                                            len(rows))[1])

    def fake_submit(store, ft, entries, *, workflow=""):
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": "F_M", "count": len(entries),
                 "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", fake_submit)
    return calls


def _row(rownum, upc, store="T1", status="", feed_id=""):
    return {"rownum": rownum, "upc": upc, "sku": "", "price": "9.99",
            "weight": "1.2", "store": store, "status": status, "gtin": "",
            "list_time": "", "feed_id": feed_id, "feed_result": "",
            "check_time": ""}


_SPEC_OK = {"feed_type": "MP_ITEM_MATCH", "product_id": "00012345678905",
            "product_id_type": "GTIN", "product_type": "Cups",
            "title": "Cup", "asin": None,
            "raw": {"itemSpecPayload": {"MPItem": [{"Item": {
                "productIdentifiers": {"productId": "00012345678905"}}}]}}}
_SPEC_BUILD = {"feed_type": "MP_ITEM", "product_id": None,
               "product_id_type": None, "product_type": None,
               "title": None, "asin": None, "raw": None}


def test_dry_run_prechecks_but_submits_nothing(monkeypatch):
    """空跑:预检照跑(只读),**mint 与 register 零调用**,载荷里放占位码。

    mint 是写库函数,没有"这次不写"模式(conventions §六);空跑用
    sku_codec.DRYRUN_PLACEHOLDER 表达 —— 12 位但含 0,is_opaque 恒 False,
    永远不会被当成真码,也落不进那两条部分唯一索引。
    """
    calls = _wire(monkeypatch, [_row(2, "012345678905")],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    out = ml.run({"execute": False})
    assert calls["feeds"] == [] and calls["writes"] == []
    assert calls["mint"] == [] and calls["sources"] == []
    assert "可跟卖 1 行" in out and "Item 载荷(对拍用)" in out
    assert ml.sku_codec.DRYRUN_PLACEHOLDER in out


def test_execute_routes_and_terminal_states(monkeypatch):
    rows = [_row(2, "012345678905"),               # 可跟卖 → 提交
            _row(3, "111111111111"),               # 退化码 → 码无效
            _row(4, "012345678929"),               # MP_ITEM → 需完整建品
            _row(5, "012345678905", store="T9"),   # 店铺不识别
            _row(6, "012345678936", status="目录无")]   # 终态不再处理
    calls = _wire(monkeypatch, rows,
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678929": _SPEC_BUILD, "00012345678929": _SPEC_BUILD})
    out = ml.run({"execute": True})
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 1)]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][7] == "F_M" and by_row[2][8] == "处理中"   # I=feedId J=处理中
    # B 列留空 → sku_codec.mint 抽的不透明码(旧 PHUMWMT+日期+序号已删)
    assert by_row[2][0] == "B22222222222"
    assert calls["mint"] == [("T1", "match", "00012345678905", "match_listing")]
    assert by_row[3][4] == "码无效"
    assert by_row[4][4] == "需完整建品"
    assert by_row[5][4] == "店铺不识别"
    assert 6 not in by_row                          # 终态行不动
    ev = [e for e in calls["events"] if e["event"] == "match_submitted"]
    assert len(ev) == 1 and ev[0]["detail"]["feed_id"] == "F_M"
    assert "跟卖提交 1" in out
    # 来源登记簿:自动号由 mint **抽码即登记**(同一函数同一事务),
    # 所以这里零 register —— register 只给 B 列人工号(见下一条用例)
    assert calls["sources"] == []


def test_auto_sku_comes_from_mint_and_is_registered_before_submit(monkeypatch):
    """B 列留空的行:SKU = mint 抽的码,而且 **mint 早于 submit_feed**。

    「防重状态先落库再调接口」在跟卖侧的落法:发码与登记在提交前的短事务里
    commit,提交失败的行下一轮由 mint 的活码复用拿回同一个码,载荷一字不差 ⇒
    api/feeds 的 payload_key 在途防重仍然有效(旧的日期+序号生成器每轮取新号,
    这条护栏根本立不起来)。
    """
    seq: list[str] = []
    calls = _wire(monkeypatch, [_row(2, "012345678905")],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    real_mint = ml.sku_codec.mint

    def spy_mint(*a, **kw):
        seq.append("mint")
        return real_mint(*a, **kw)
    monkeypatch.setattr(ml.sku_codec, "mint", spy_mint)
    real_submit = feeds.submit_feed

    def spy_submit(*a, **kw):
        seq.append("submit")
        return real_submit(*a, **kw)
    monkeypatch.setattr(feeds, "submit_feed", spy_submit)

    ml.run({"execute": True})
    assert seq == ["mint", "submit"]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][0] == "B22222222222"


def test_mint_transaction_does_not_span_precheck_calls(monkeypatch):
    """发码那个事务里**不许有沃尔玛调用**:_precheck 是逐行 SPEC 接口往返
    (固定出口代理 + 速率桶 + 退避)。

    几百行吊在一个事务里,mint 在登记簿上留的行锁要到整轮结束才释放,与
    list_new 的 mint 互相等锁 —— PG 上典型的长事务坏味道。所以分两趟:
    第一趟纯网络零事务,第二趟纯数据库零网络。
    """
    import contextlib as _c
    seq: list[str] = []
    calls = _wire(monkeypatch, [_row(2, "012345678905"),
                                _row(3, "012345678912")], _SPECS_TWO)

    @_c.contextmanager
    def spy_conn():
        seq.append("tx-in")
        try:
            yield None
        finally:
            seq.append("tx-out")

    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn", spy_conn)
    monkeypatch.setattr(items_api, "search_walmart_spec",
                        lambda store, **kw: (seq.append("spec"),
                                             _SPECS_TWO[next(iter(kw.values()))])[1])
    monkeypatch.setattr(ml.sku_codec, "mint",
                        lambda conn, st, so, key, *, workflow="": (
                            seq.append("mint"), "B22222222222")[1])
    ml.run({"execute": True})
    # 闸门加载那个事务(第一个 tx-in/out)之后才有 spec;发码事务里零 spec
    mint_tx = seq.index("mint")
    lo = max(i for i, v in enumerate(seq[:mint_tx]) if v == "tx-in")
    hi = min(i for i, v in enumerate(seq) if i > mint_tx and v == "tx-out")
    assert "spec" not in seq[lo:hi]
    assert seq.count("spec") == 2 and seq.index("spec") < lo
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 2)]


def test_failed_match_row_reuses_the_same_code_next_round(monkeypatch):
    """同 (店, GTIN) 连跑两轮拿到**同一个码**(mint 的活码复用)。

    跟卖侧此前缺的正是这条护栏:旧生成器每轮给新序号 ⇒ 载荷漂 ⇒ 在途防重
    失效 ⇒ 同一个品被重复提交,而且每次都换一个 SKU。
    """
    calls = _wire(monkeypatch, [_row(2, "012345678905")],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    ml.run({"execute": True})
    first = calls["writes"][0][1][0]
    calls["writes"].clear()
    ml.run({"execute": True})                    # 第二轮:表上那行仍是留空的
    assert calls["writes"][0][1][0] == first     # 复用活码,不是新码
    assert [c[2] for c in calls["mint"]] == ["00012345678905"] * 2


def test_listing_sources_register_first_wins():
    from services import listing_sources

    class _Conn:
        def __init__(self):
            self.sqls = []

        def cursor(self):
            return self

        def executemany(self, sql, rows):
            self.sqls.append((sql, list(rows)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    conn = _Conn()
    n = listing_sources.register(conn, [
        {"store": "T1", "sku": "S1", "source_type": "match",
         "source_key": "G1", "workflow": "match_listing"}])
    assert n == 1
    sql, rows = conn.sqls[0]
    assert "ON CONFLICT (store, sku) DO NOTHING" in sql   # 首次登记优先
    assert rows[0] == ("T1", "S1", "match", "G1", "match_listing")
    assert listing_sources.register(conn, []) == 0


def test_manual_sku_takes_priority(monkeypatch):
    """B 列人工填号优先(旧系统习惯):**不 mint**,但要 register 进登记簿。

    人工号在提交**前**登记,而不是等提交成功:登记是「这个串归谁」的事实,
    与提交成不成功无关。提交成功才登记会让被拒的人工号成为维护链眼里的孤儿
    —— source_type 路由不到,落进 unknown,而 unknown 不参与任何自动动作,
    这批货就永久退出自动化了。
    """
    row_manual = _row(2, "012345678905")
    row_manual["sku"] = "MY-OWN-001"
    row_auto = _row(3, "012345678912")
    spec2 = dict(_SPEC_OK, product_id="00012345678912")
    calls = _wire(monkeypatch, [row_manual, row_auto],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678912": spec2, "00012345678912": spec2})
    ml.run({"execute": True})
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][0] == "MY-OWN-001"
    assert by_row[3][0] == "B22222222222"           # 只有留空的那行发码
    assert calls["mint"] == [("T1", "match", "00012345678912", "match_listing")]
    assert calls["sources"] == [{
        "store": "T1", "sku": "MY-OWN-001", "source_type": "match",
        "source_key": "00012345678905", "workflow": "match_listing"}]


def test_gate_reason_two_gates():
    """跟卖两道闸纯函数:风控 > ASIN 黑名单;字段缺失跳过该道闸。

    防呆=黑名单,不看删除史(所有者口径 2026-08-12:按拉黑类别拦,
    因产品问题删过的重上是正常经营)。"""
    gate = {"banned_pts": {"BannedPT"}, "brands": {"badbrand"}}
    banned = {"B0BANNED01": ("E", "沃尔玛-知产")}
    g = ml._gate_reason
    spec = lambda **kw: {"feed_type": "MP_ITEM_MATCH", "product_id": None,
                         "product_type": None, "brand": None, "asin": None,
                         **kw}
    assert g(spec(product_type="BannedPT"), gate, {}) \
        == "风控拦截:禁售类目:BannedPT"
    assert g(spec(brand="BadBrand"), gate, {}) \
        == "风控拦截:黑名单品牌:BadBrand"
    assert g(spec(asin="B0BANNED01"), gate, banned) \
        == "ASIN黑名单:沃尔玛-知产(E类)"
    # 交叉不出 ASIN/品牌 → 跳过对应闸,放行;不在黑名单的 ASIN 照常放行
    assert g(spec(), gate, banned) is None
    assert g(spec(asin="B0CLEAN001"), gate, banned) is None


def test_gated_row_terminal_not_submitted(monkeypatch):
    """命中闸的行写 F 终态、不提交、不烧序号;下轮不再重复预检。"""
    rows = [_row(2, "012345678905"),       # 干净行 → 提交
            _row(3, "012345678912")]       # SPEC 交叉出黑名单 ASIN → 拦
    spec_bad = dict(_SPEC_OK, product_id="00012345678912", asin="B0BANNED01")
    calls = _wire(monkeypatch, rows,
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678912": spec_bad, "00012345678912": spec_bad})
    monkeypatch.setattr(ml.blacklist, "load_banned_asins",
                        lambda conn: {"B0BANNED01": ("E", "沃尔玛-知产")})
    out = ml.run({"execute": True})
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 1)]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[3][4].startswith("ASIN黑名单:沃尔玛-知产")   # F 列终态
    assert by_row[3][7] == ""                                  # 无 feedid
    assert calls["mint"] == [("T1", "match", "00012345678905", "match_listing")]
    assert by_row[2][0] == "B22222222222"      # 被拦的行不发码
    assert "ASIN黑名单" in out and "跟卖提交 1" in out


_STORE2 = {"name": "T2", "client_id": "c2", "client_secret": "s", "proxy": None}
_STORE_A = {"name": "A085", "client_id": "cA", "client_secret": "s", "proxy": None}
_SPEC_2 = dict(_SPEC_OK, product_id="00012345678912")
_SPECS_TWO = {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
              "012345678912": _SPEC_2, "00012345678912": _SPEC_2}


def test_multi_slice_results_line_up_and_keep_the_failed_wording(monkeypatch):
    """submit_feed 只回 count 不回条目,对位走 api/feeds.iter_result_slices
    —— 错一位就是整批结局落到别人行上,而且**不报错**。

    四档计数尾巴走 notify_fmt.feed_outcome_tail:本件的 failed 档字样
    「提交被拒」(problem_product_cleanup 是「提交失败」)逐字不变。
    """
    calls = _wire(monkeypatch, [_row(2, "012345678905"),
                                _row(3, "012345678912")], _SPECS_TWO)

    def sliced(store, ft, entries, *, workflow=""):
        return [{"feed_id": "F_A", "count": 1, "outcome": "submitted"},
                {"feed_id": None, "count": 1, "outcome": "failed"}]
    monkeypatch.setattr(feeds, "submit_feed", sliced)

    out = ml.run({"execute": True})
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][7] == "F_A" and by_row[2][8] == "处理中"   # 第一片 = 第 2 行
    assert by_row[3][7] == "" and by_row[3][8] == "提交被拒"    # 第二片 = 第 3 行
    assert len(calls["events"]) == 1                           # 被拒那片不入病历
    assert "  T1:跟卖提交 1,⚠ 提交被拒 1(查日志)" in out


def test_failed_store_gets_one_serial_second_pass(monkeypatch):
    """店级重试标准①(所有者定稿 2026-08-26):失败店跑完别人后串行补试一遍,
    补试跑的是**同一个** _one_store(单一落地路径);救回了就不点名缺席。"""
    from socksio.exceptions import ProtocolError
    calls = _wire(monkeypatch, [_row(2, "012345678905"),
                                _row(3, "012345678912", store="T2")],
                  _SPECS_TWO, stores=(STORE, _STORE2))
    monkeypatch.setattr(ml.store_retry.time, "sleep", lambda s: None)
    tries = []

    def flaky(store, ft, entries, *, workflow=""):
        tries.append(store["name"])
        if store["name"] == "T2" and tries.count("T2") == 1:
            raise ProtocolError("Malformed reply")   # 08-26 事故同款,补试即好
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": f"F_{store['name']}", "count": len(entries),
                 "outcome": "submitted"}]
    monkeypatch.setattr(feeds, "submit_feed", flaky)

    out = ml.run({"execute": True})
    assert tries.count("T2") == 2          # 首轮 + 补试各一次,不多试
    assert "⚠ 缺席" not in out              # 救回了就不点名
    assert "  T2:跟卖提交 1" in out
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[3][7] == "F_T2"          # 补试的回写照常落表


def test_still_failed_store_is_absent_in_the_first_line(monkeypatch):
    """标准③:补试仍失败 ⇒ **不炸整轮**,缺席店带归类词点名在摘要**首行**
    (链通知只发成功步骤的首行,写在后面等于只写进日志);逐店那行照旧。"""
    from socksio.exceptions import ProtocolError
    calls = _wire(monkeypatch, [_row(2, "012345678905"),
                                _row(3, "012345678912", store="T2")],
                  _SPECS_TWO, stores=(STORE, _STORE2))
    monkeypatch.setattr(ml.store_retry.time, "sleep", lambda s: None)

    def down(store, ft, entries, *, workflow=""):
        if store["name"] == "T2":
            raise ProtocolError("Malformed reply")   # 补试也不好
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": "F_M", "count": len(entries),
                 "outcome": "submitted"}]
    monkeypatch.setattr(feeds, "submit_feed", down)

    out = ml.run({"execute": True})
    first = out.splitlines()[0]
    assert "⚠ 缺席 1 店:T2(代理波动)——已串行补试仍失败," \
           "本轮不炸链(未提交行下轮重试)" in first
    assert "  ⚠ T2:提交异常已跳过(代理波动:" in out and "下轮重试" in out
    # 一家店的失败不吃掉别人的成果:好店照常提交、照常回写
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 1)]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][7] == "F_M" and 3 not in by_row


def test_scale_gate_holds_the_second_pass_and_keeps_its_note(monkeypatch):
    """规模闸(store_retry 2026-08-26 对抗校验):失败店超 max(3, 总数//5) 判
    系统性故障,**一家都不补试** —— 串行补试只会把故障时长按店数放大。

    止损原文必须落进摘要(调用方义务,见 serial_second_pass 第四条),
    首行中段同时改说「超补试规模闸未补试」而不是「已串行补试仍失败」。
    """
    from socksio.exceptions import ProtocolError
    stores = [dict(STORE, name=f"T{i}", client_id=f"c{i}") for i in range(1, 5)]
    rows = [_row(2 + i, upc, store=f"T{i + 1}")
            for i, upc in enumerate(("012345678905", "012345678912",
                                     "012345678905", "012345678912"))]
    _wire(monkeypatch, rows, _SPECS_TWO, stores=tuple(stores))
    monkeypatch.setattr(ml.store_retry.time, "sleep", lambda s: None)
    tries = []

    def down(store, ft, entries, *, workflow=""):
        tries.append(store["name"])
        raise ProtocolError("Malformed reply")
    monkeypatch.setattr(feeds, "submit_feed", down)

    out = ml.run({"execute": True})
    assert sorted(tries) == ["T1", "T2", "T3", "T4"]    # 4 > max(3, 4//5):一家都不补
    assert "超补试规模闸未补试(疑似系统性故障)" in out.splitlines()[0]
    assert "本轮不逐店补试" in out          # 止损原文进摘要,不是只进日志


def test_summary_lines_stay_in_store_name_order_after_a_second_pass(monkeypatch):
    """补试是**跑完别人之后**才补的,但摘要行序不许跟着补试次序走 ——
    同一轮跑两次输出得一样,否则没法对拍(list_new 同款纪律)。"""
    from socksio.exceptions import ProtocolError
    _wire(monkeypatch, [_row(2, "012345678905", store="A085"),
                        _row(3, "012345678912")],
          _SPECS_TWO, stores=(_STORE_A, STORE))
    monkeypatch.setattr(ml.store_retry.time, "sleep", lambda s: None)
    seen = []

    def flaky(store, ft, entries, *, workflow=""):
        seen.append(store["name"])
        if store["name"] == "A085" and seen.count("A085") == 1:
            raise ProtocolError("Malformed reply")
        return [{"feed_id": f"F_{store['name']}", "count": len(entries),
                 "outcome": "submitted"}]
    monkeypatch.setattr(feeds, "submit_feed", flaky)

    out = ml.run({"execute": True}).splitlines()
    assert seen == ["A085", "T1", "A085"]      # 先跑完别人,再回头补试
    assert [ln for ln in out if "跟卖提交" in ln] == \
        ["  A085:跟卖提交 1", "  T1:跟卖提交 1"]


def test_match_sheet_sync_from_ledger(monkeypatch):
    monkeypatch.setattr(resources, "MATCH_SHEET",
                        Spreadsheet(name="跟卖表", token="TOK", sheet_id="SID",
                                    columns=resources.MATCH_SHEET.columns))
    sheet_rows = [
        ["012", "SKU_A", "9.99", "1", "T1", "可跟卖", "G1",
         "2026-08-07", "F1", "处理中", ""],
        ["013", "SKU_B", "9.99", "1", "T1", "可跟卖", "G2",
         "2026-08-07", "F1", "处理中", ""],
        ["014", "SKU_C", "9.99", "1", "T1", "可跟卖", "G3",
         "2026-08-07", "F2", "处理中", ""],
    ]
    writes = []
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: len(sheet_rows) + 1)
    monkeypatch.setattr(feishu, "sheet_values_rows",
                        lambda s, c1, c2, r1, r2, **kw:
                        list(enumerate(sheet_rows, r1)))
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    ledger = {"F1": {"SKU_A": ("success", ""), "SKU_B": ("failed", "ERR_M")},
              "F2": {"SKU_C": ("submitted", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})

    out = match_sheet.sync_from_ledger()
    w = {rng: vals[0] for rng, vals in writes}
    assert w["B2:K2"][8] == "成功" and w["B2:K2"][9] != ""      # J 结果 K 时间
    assert w["B3:K3"][8] == "失败:ERR_M"
    assert "B4:K4" not in w                                     # F2 未落定不动
    assert "回填 2 行" in out


def test_match_weight_defaults_to_one_pound():
    """旧 DEFAULT_WEIGHT 实证(2026-08-12 补回):重量留空默认 1 磅,
    不再抛异常把行打成'数据无效'卡死。"""
    from services import match_feed
    item = match_feed.build_match_item({}, "SKU1", "9.99", "")
    assert item["ShippingWeight"] == 1.0
    item2 = match_feed.build_match_item({}, "SKU1", "9.99", "2.5")
    assert item2["ShippingWeight"] == 2.5


# ── 店铺事件账本(运营类:每店每轮一条)────────────────────────────────────

def _capture_rounds(monkeypatch):
    got: list = []
    monkeypatch.setattr(ml.store_events, "record_round",
                        lambda conn, source, event, per_store:
                        (got.append((source, event, dict(per_store))),
                         len(per_store))[1])
    return got


def test_execute_records_one_round_event_per_store(monkeypatch):
    _wire(monkeypatch, [_row(2, "012345678905")],
          {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    got = _capture_rounds(monkeypatch)
    ml.run({"execute": True})
    assert len(got) == 1
    source, event, per_store = got[0]
    assert (source, event) == ("match_listing", ml.store_events.MATCH_ROUND)
    assert per_store == {"T1": {"submitted": 1}}


def test_a_store_that_blew_up_still_leaves_a_row(monkeypatch):
    """★ 异常店也留一条:计数可能全 0(第一片就炸),而"这家店这一轮炸了"
    本身就是要能按时间线对齐的事实(封店那天它是不是也在炸)。"""
    from socksio.exceptions import ProtocolError
    _wire(monkeypatch, [_row(2, "012345678905")],
          {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    monkeypatch.setattr(ml.store_retry.time, "sleep", lambda s: None)

    def boom(store, ft, entries, *, workflow=""):
        raise ProtocolError("Malformed reply")

    monkeypatch.setattr(feeds, "submit_feed", boom)
    got = _capture_rounds(monkeypatch)
    ml.run({"execute": True})
    assert got[0][2] == {"T1": {"exception": True}}


def test_dry_run_records_no_round_event(monkeypatch):
    _wire(monkeypatch, [_row(2, "012345678905")],
          {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    got = _capture_rounds(monkeypatch)
    ml.run({"execute": False})
    assert got == []
