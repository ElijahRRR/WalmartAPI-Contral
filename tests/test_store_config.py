"""治理配置快照 diff 回归(services/store_config)。

钉住的都是"记错一条比不记更坏"的地方:
  ① **首次快照零事件**(首次观测不是变化,一期铁规的配置版);
  ② **缺键约定**:上一版没有这一列 ⇒ 不产事件;键在而值被清空 ⇒ 产事件;
  ③ **列在所有店同时消失** ⇒ 表结构变化,只告警不逐店刷屏;
  ④ **飞书失败不产事件、不覆盖快照**(把"读不到"记成"被清空"会造两轮假事件);
  ⑤ severity 按列(类目/渠道 high、限额/倍率 mid、目标值 info、登记但未分档 mid),
     0 与非 0 之间在容量/清零两列上是**开关**,单独一档;
  ⑥ `enabled=None` 是"列没读到"不是状态,null↔值 不产事件;
  ⑦ **未登记列只记列名不记值**(2026-09-01 生产实跑):飞书 `SourceID` 的值里
     含行内容的哈希,整表入快照 = 谁动一格就刷一批假 `store_limits_changed`。

沙箱 PG 集成用例在文件末尾:连不上就 skip。
"""

import os
import socket

import pytest

from registry import resources
from services import store_config as sc
from services import store_events as se

F = resources.RETIRE_LIMITS.fields


def _snap(limits=None, stores=None, scope=(), extra_cols=()):
    return {"v": sc.SNAPSHOT_VERSION, "limits": limits or {},
            "limits_extra_cols": list(extra_cols),
            "stores": stores or {}, "scope_excluded": list(scope)}


#: 飞书内部字段 `SourceID` 的**真实形状**(生产原样):base64 复合键,解开是
#: `7670866633484766507:H006詹松涛:a02d434c87d03739c341c5cb8996d719:1` ——
#: 第三段是**行内容的哈希**,行被编辑就变。它就是这次改口径的现场。
_SOURCE_ID = ("NzY3MDg2NjYzMzQ4NDc2NjUwNzpIMDA26Km55p2+5rabOmEwMmQ0MzRjODdkMD"
              "M3MzljMzQxYzVjYjg5OTZkNzE5OjE=")
#: 同一行被人编辑之后(只有哈希那一段变了)—— 配置一个字没改,它却变了
_SOURCE_ID_AFTER_EDIT = ("NzY3MDg2NjYzMzQ4NDc2NjUwNzpIMDA26Km55p2+5rabOmZmZmZm"
                         "ZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmOjE=")


# ── ① 首次快照 ────────────────────────────────────────────────────────────

def test_first_snapshot_never_produces_events():
    """没有上一版就没有"变化" —— 首轮上线时全表会长得像"全部新增"。"""
    cur = _snap(limits={"A085": {F.category1: "Home"}},
                stores={"A085": {"enabled": True}}, scope=("谭总",))
    assert sc.diff(None, cur) == []


def test_snapshot_version_bump_is_treated_as_first():
    """形状换了就当首次:两套形状硬比会产出满屏假事件。"""
    old = {"v": 0, "limits": {"A085": {F.category1: "Home"}}}
    assert sc.diff(old, _snap(limits={"A085": {F.category1: "Toys"}})) == []


def test_v1_snapshot_with_unregistered_values_produces_nothing():
    """★ v1→v2 的现场陷阱:生产库里那份 v1 快照**存着 SourceID 等未登记列的值**。

    v2 只存登记列,拿两版硬比的话那些列会被判成"在所有店上整体消失",刷出一批
    假的表结构/清空事件。没有可比的上一版就不是变化 —— 与首次快照同一条铁规。
    """
    v1 = {"v": 1,
          "limits": {"A085": {F.category1: "Home", "SourceID": _SOURCE_ID,
                              "序号": "1"}},
          "stores": {"A085": {"enabled": True}}, "scope_excluded": []}
    cur = _snap(limits={"A085": {F.category1: "Toys"}},
                stores={"A085": {"enabled": False}},
                extra_cols=("SourceID", "序号"))
    assert sc.diff(v1, cur) == []


# ── ② 缺键约定 ────────────────────────────────────────────────────────────

def test_a_column_missing_in_prev_is_not_a_change():
    """上一版没有这一列的键 = 上一版还没这列(新列 / 那格原本是空的)。"""
    prev = _snap(limits={"A085": {F.category1: "Home"}})
    cur = _snap(limits={"A085": {F.category1: "Home", F.max_online: "3000"}})
    assert sc.diff(prev, cur) == []


def test_clearing_one_cell_is_a_change_even_though_feishu_drops_the_key():
    """★ 人把一格清空了:飞书对空单元格根本不返回那个键,快照里长得像"列没了"。

    只有这一家店的这一格没了(别家还在)⇒ 是清空,不是表头改名 ⇒ 产事件。
    类目1 被清空 = 该店从此不限类目,这必须留痕。
    """
    prev = _snap(limits={"A085": {F.category1: "Home"},
                         "A107": {F.category1: "Toys"}})
    cur = _snap(limits={"A085": {}, "A107": {F.category1: "Toys"}})
    evs = sc.diff(prev, cur)
    assert [(e["store"], e["event"], e["severity"]) for e in evs] == [
        ("A085", se.STORE_LIMITS_CHANGED, "high")]
    assert evs[0]["detail"] == {"field": F.category1, "old": "Home", "new": ""}


def test_empty_string_to_value_is_a_change_too():
    """键在、值由空串变成有值:人填上了,照样是变化。"""
    prev = _snap(limits={"A085": {F.channel_limit: ""}})
    cur = _snap(limits={"A085": {F.channel_limit: "fbm"}})
    evs = sc.diff(prev, cur)
    assert len(evs) == 1 and evs[0]["detail"]["old"] == ""


# ── ③ 列级消失 ────────────────────────────────────────────────────────────

def test_a_column_gone_from_every_store_is_a_schema_change_not_49_events(caplog):
    """表头改名会让所有 loader 静默读空 —— 要告警,但不该逐店刷 49 条事件。"""
    prev = _snap(limits={s: {F.lead_limit: "5", F.category1: "Home"}
                         for s in ("A085", "A107", "A200")})
    cur = _snap(limits={s: {F.category1: "Home"}
                        for s in ("A085", "A107", "A200")})
    with caplog.at_level("WARNING"):
        assert sc.diff(prev, cur) == []
    assert "所有店" in caplog.text


# ── ⑤ severity 按列 ───────────────────────────────────────────────────────

def _one(field, old, new):
    evs = sc.diff(_snap(limits={"A085": {field: old}}),
                  _snap(limits={"A085": {field: new}}))
    assert len(evs) == 1, (field, old, new)
    return evs[0]


def test_severity_by_column():
    # high:改错当天整店换行为(类目准入 / 配送限制)
    assert _one(F.category1, "Home", "Toys")["severity"] == "high"
    assert _one(F.category3, "", "Baby")["severity"] == "high"
    assert _one(F.channel_limit, "fba", "fbm")["severity"] == "high"
    # mid:改错影响一条链的节奏
    assert _one(F.max_daily_list, "50", "80")["severity"] == "mid"
    assert _one(F.max_daily_retire, "300", "100")["severity"] == "mid"
    assert _one(F.lead_limit, "7", "5")["severity"] == "mid"
    assert _one(F.fba_range1, "1.3", "1.4")["severity"] == "mid"
    assert _one(F.fbm_range2, "1.5", "1.6")["severity"] == "mid"
    # info:目标值只喂分配打分,改了不会让任何东西立刻做错事
    assert _one(F.target_gmv_daily, "500", "800")["severity"] == "info"
    assert _one(F.target_orders_daily, "5", "8")["severity"] == "info"


def test_zero_is_a_switch_on_two_columns_only():
    """0 在这两列上是**开关**不是数值:单店最大在线数 0 = 一件都不许再上;
    库存特殊要求 0 = 整店清零。两列之间调数值只是配额调整。"""
    assert _one(F.max_online, "3000", "0")["severity"] == "high"
    assert _one(F.max_online, "0", "3000")["severity"] == "high"
    assert _one(F.max_online, "3000", "2500")["severity"] == "mid"
    assert _one(F.inventory_note, "", "0")["severity"] == "high"
    assert _one(F.inventory_note, "0", "1")["severity"] == "high"
    # 空串不是 0(空=没填,不是"填了 0"):两边都不是 0 ⇒ mid
    assert _one(F.max_online, "3000", "")["severity"] == "mid"


def test_registered_but_ungraded_column_is_mid_not_info():
    """登记进 registry 了、但没在 `_SEV_BY_ATTR` 分档的列(维护仓库就是一个,
    将来新加的闸同理):不知道就按 mid,当成"不值一提"会让它第一次生效时没人
    知道。**未登记列走不到这里** —— 它们压根不进快照的值字典。"""
    assert _one(F.maint_node, "1234", "5678")["severity"] == "mid"


# ── 行级事件 ──────────────────────────────────────────────────────────────

def test_row_added_and_removed():
    prev = _snap(limits={"A085": {F.category1: "Home"}})
    cur = _snap(limits={"A107": {F.category1: "Toys"}})
    evs = sc.diff(prev, cur)
    by = {e["event"]: e for e in evs}
    assert by[se.STORE_LIMITS_ROW_ADDED]["store"] == "A107"
    assert by[se.STORE_LIMITS_ROW_ADDED]["severity"] == "info"
    assert by[se.STORE_LIMITS_ROW_ADDED]["detail"]["row"] == {F.category1: "Toys"}
    assert by[se.STORE_LIMITS_ROW_REMOVED]["store"] == "A085"
    assert by[se.STORE_LIMITS_ROW_REMOVED]["severity"] == "mid"


# ── ⑥ 凭证表:在册与启用 ───────────────────────────────────────────────────

def test_enabled_changes_by_direction():
    """停用 = high(这家店不再经营,占用/上架/分配全线改判);启用 = info。"""
    off = sc.diff(_snap(stores={"A085": {"enabled": True}}),
                  _snap(stores={"A085": {"enabled": False}}))
    assert [(e["event"], e["severity"]) for e in off] == [
        (se.STORE_ENABLED_CHANGED, "high")]
    on = sc.diff(_snap(stores={"A085": {"enabled": False}}),
                 _snap(stores={"A085": {"enabled": True}}))
    assert on[0]["severity"] == "info"


def test_enabled_none_is_not_a_state():
    """None = 「启用」列整张表没读到(表头改名),不是"这家店停用了"。

    两个方向都不产事件 —— 一期「空=没抓到」铁规的配置版。
    """
    assert sc.diff(_snap(stores={"A085": {"enabled": True}}),
                   _snap(stores={"A085": {"enabled": None}})) == []
    assert sc.diff(_snap(stores={"A085": {"enabled": None}}),
                   _snap(stores={"A085": {"enabled": True}})) == []


def test_registered_and_deregistered():
    evs = sc.diff(_snap(stores={"A085": {"enabled": True}}),
                  _snap(stores={"A107": {"enabled": True}}))
    by = {e["event"]: e for e in evs}
    assert by[se.STORE_REGISTERED]["severity"] == "info"
    # 凭证表里整行没了 = store_release -p dead=1 会把它整店标缺席
    assert by[se.STORE_DEREGISTERED]["severity"] == "high"
    assert by[se.STORE_DEREGISTERED]["store"] == "A085"


# ── 规划外名单(全局事件)───────────────────────────────────────────────────

def test_scope_change_is_a_global_event_with_no_store():
    """规划范围是全局事实,不属于任何一家店(与 TRO 源头行同一形状)。"""
    evs = sc.diff(_snap(scope=("谭总",)), _snap(scope=("谭总", "李总")))
    assert len(evs) == 1
    e = evs[0]
    assert e["store"] is None and e["event"] == se.STORE_SCOPE_CHANGED
    assert e["severity"] == "mid"
    assert e["detail"] == {"old": ["谭总"], "new": ["谭总", "李总"]}
    assert sc.diff(_snap(scope=("谭总",)), _snap(scope=("谭总",))) == []


def test_every_governance_event_is_registered_and_classed():
    """diff 产的每个码都必须在账本登记过并归类 governance(record_many 会抛)。"""
    prev = _snap(limits={"A085": {F.category1: "Home"}},
                 stores={"A085": {"enabled": True}}, scope=())
    cur = _snap(limits={"A107": {F.category1: "Toys"}},
                stores={"A107": {"enabled": False}}, scope=("谭总",))
    evs = sc.diff(prev, cur)
    assert evs
    for e in evs:
        assert se.CLASS[e["event"]] == "governance"
        assert e["severity"] in se.SEVERITIES
        assert e["source"] == sc.SOURCE


# ── ④ 飞书失败 ────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=None):
        self.conn.sqls.append((sql, args))
        if "INSERT INTO ops.cursors" in sql:
            self.conn.saved = args["value"]

    def executemany(self, sql, seq):
        self.conn.sqls.append((sql, list(seq)))
        if "INSERT INTO ops.store_events" in sql:
            self.conn.events.extend(seq)

    def fetchone(self):
        return (self.conn.stored,) if self.conn.stored is not None else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, stored=None):
        self.stored, self.saved = stored, None
        self.sqls: list = []
        self.events: list = []

    def cursor(self):
        return _Cur(self)


def test_feishu_failure_records_nothing_and_keeps_the_old_snapshot(monkeypatch):
    """★ 绝不把"飞书挂了"记成"配置被清空"。

    记了的话下一轮读到了还会再记一遍"配置被改回来" —— 两轮假事件把真事件
    淹掉,而账本只追加,删不掉。
    """
    prev = _snap(limits={"A085": {F.category1: "Home"}})
    conn = _Conn(stored=prev)

    def boom():
        raise RuntimeError("飞书 504")

    monkeypatch.setattr(sc, "take_snapshot", boom)
    events, warn = sc.check_and_record(conn)
    assert events == [] and conn.events == []
    assert conn.saved is None, "快照被覆盖了 —— 下一轮会拿假的当上一版"
    assert "不产事件" in warn and "飞书" in warn


def test_check_and_record_lands_events_and_saves_the_new_snapshot(monkeypatch):
    prev = _snap(limits={"A085": {F.category1: "Home"}})
    cur = _snap(limits={"A085": {F.category1: "Toys"}})
    conn = _Conn(stored=prev)
    monkeypatch.setattr(sc, "take_snapshot", lambda: cur)
    events, warn = sc.check_and_record(conn)
    assert warn is None
    assert [e["event"] for e in events] == [se.STORE_LIMITS_CHANGED]
    assert len(conn.events) == 1
    assert conn.saved is not None and "Toys" in conn.saved


def test_version_migration_skips_the_diff_but_still_saves_and_says_so(
        monkeypatch):
    """★ v1(存着未登记列的值)→ v2:不产事件、**但快照要覆盖**(与飞书失败
    那条路径正好相反),而且摘要必须**明说**这一轮为什么没比对。

    静默的话摘要上只显示"无变更",而这一轮真发生的配置改动确实丢了一轮。
    """
    v1 = {"v": 1, "limits": {"A085": {F.category1: "Home",
                                      "SourceID": _SOURCE_ID}},
          "stores": {"A085": {"enabled": True}}, "scope_excluded": []}
    cur = _snap(limits={"A085": {F.category1: "Toys"}},
                stores={"A085": {"enabled": False}}, extra_cols=("SourceID",))
    conn = _Conn(stored=v1)
    monkeypatch.setattr(sc, "take_snapshot", lambda: cur)
    events, warn = sc.check_and_record(conn)
    assert events == [] and conn.events == []
    assert conn.saved is not None and '"v": 2' in conn.saved
    assert _SOURCE_ID not in conn.saved, "新快照里不许再有未登记列的值"
    assert warn and "没比对" in warn and "v1" in warn and "v2" in warn
    # 下一轮(两版都是 v2)恢复正常比对
    conn2 = _Conn(stored=cur)
    monkeypatch.setattr(sc, "take_snapshot",
                        lambda: _snap(limits={"A085": {F.category1: "Baby"}},
                                      stores={"A085": {"enabled": False}},
                                      extra_cols=("SourceID",)))
    events2, warn2 = sc.check_and_record(conn2)
    assert warn2 is None
    assert [e["event"] for e in events2] == [se.STORE_LIMITS_CHANGED]


def test_first_run_saves_the_snapshot_without_events(monkeypatch):
    """首轮:一条事件都不产,但快照必须落 —— 不落的话永远停在"首次"。"""
    conn = _Conn(stored=None)
    monkeypatch.setattr(sc, "take_snapshot",
                        lambda: _snap(limits={"A085": {F.category1: "Home"}}))
    events, warn = sc.check_and_record(conn)
    assert (events, warn) == ([], None)
    assert conn.events == [] and conn.saved is not None


# ── 拍快照:密钥绝不进快照 ─────────────────────────────────────────────────

def test_store_snapshot_only_asks_feishu_for_two_columns(monkeypatch):
    """★ 凭证表里有 ClientSecret 与代理密码 —— 快照落在 ops.cursors(没有
    凭证快照那样的 chmod 600),所以只准点名取「店铺」「启用」两列。"""
    seen = {}

    def _list(table, *, field_names=None, **kw):
        seen["fields"] = field_names
        return [{"fields": {resources.STORE_CREDENTIALS.fields.store: "A085",
                            resources.STORE_CREDENTIALS.fields.enabled: True}}]

    monkeypatch.setattr(sc.feishu, "list_records", _list)
    out = sc._stores_snapshot()
    cf = resources.STORE_CREDENTIALS.fields
    assert seen["fields"] == [cf.store, cf.enabled]
    assert out == {"A085": {"enabled": True}}


def test_enabled_is_none_when_the_column_is_gone_from_the_whole_sheet(monkeypatch):
    """整张表一行都没带这个键 ⇒ 列被改名/删了 ⇒ 全记 None(不产事件);
    个别行缺键仍按 stores.is_enabled 的既定口径「缺省视为启用」。"""
    cf = resources.STORE_CREDENTIALS.fields
    monkeypatch.setattr(sc.feishu, "list_records",
                        lambda *a, **k: [{"fields": {cf.store: "A085"}},
                                         {"fields": {cf.store: "A107"}}])
    assert sc._stores_snapshot() == {"A085": {"enabled": None},
                                     "A107": {"enabled": None}}
    monkeypatch.setattr(
        sc.feishu, "list_records",
        lambda *a, **k: [{"fields": {cf.store: "A085", cf.enabled: False}},
                         {"fields": {cf.store: "A107"}}])
    assert sc._stores_snapshot() == {"A085": {"enabled": False},
                                     "A107": {"enabled": True}}


def _limits_rows(monkeypatch, rows):
    """输入:飞书记录的 fields 列表 → 输出:seen(记下 field_names 传了没)。"""
    seen = {}

    def _list(table, *, field_names=None, **kw):
        seen["fields"] = field_names
        return [{"fields": f} for f in rows]

    monkeypatch.setattr(sc.feishu, "list_records", _list)
    return seen


def test_limits_snapshot_keeps_registered_columns_and_only_names_the_rest(
        monkeypatch):
    """★ 整表照拉(要看得见多出来的列),但**值只收登记列**。

    未登记列只留列名 —— 记了值就等于把噪音换个地方存。值存**原文**
    (空串≠没填≠垃圾值)。
    """
    seen = _limits_rows(monkeypatch, [
        {F.store: " A085 ", F.category1: [{"text": "Home"}],
         F.max_online: 3000, "SourceID": _SOURCE_ID, "序号": 1},
        {F.store: ""},                              # 无店名的行整行丢掉
    ])
    limits, extra = sc._limits_snapshot()
    assert seen["fields"] is None, "裁剪在 API 那一步 = 看不见多出来的列"
    assert limits == {"A085": {F.category1: "Home", F.max_online: "3000"}}
    assert extra == ["SourceID", "序号"]
    assert _SOURCE_ID not in str(limits), "未登记列的值绝不许进快照"


def test_registered_column_list_comes_from_registry_not_a_copy():
    """白名单必须遍历 registry 登记项(`_fields` 造的是 SimpleNamespace,
    取全部值只能走 vars());抄一份清单会跟 registry 各漂各的。"""
    cols = sc._registered_limits_cols()
    assert F.store not in cols, "店铺列是键,不进值字典"
    assert {F.category1, F.channel_limit, F.max_online, F.maint_node} <= cols
    assert "SourceID" not in cols


def test_unregistered_column_value_change_produces_no_event(monkeypatch):
    """★ 本次改口径的现场:`SourceID` 的值里含**行内容的哈希**,谁动一下那张
    表的任何一格它就跟着变。整表入快照的后果是凭空刷出一批 store_limits_changed
    (所有者实跑第一轮 4 条变更里 2 条是这种噪音)。

    口径:未登记列**没有任何代码消费它**,改了系统行为一个字节都不变 ——
    不是治理事件。
    """
    _limits_rows(monkeypatch, [{F.store: "A085", F.category1: "Home",
                                "SourceID": _SOURCE_ID}])
    limits1, extra1 = sc._limits_snapshot()
    _limits_rows(monkeypatch, [{F.store: "A085", F.category1: "Home",
                                "SourceID": _SOURCE_ID_AFTER_EDIT}])
    limits2, extra2 = sc._limits_snapshot()
    assert limits1 == limits2 and extra1 == extra2 == ["SourceID"]
    assert sc.diff(_snap(limits=limits1, extra_cols=extra1),
                   _snap(limits=limits2, extra_cols=extra2)) == []


def test_new_unregistered_column_is_one_schema_event_not_per_cell():
    """"有人往表里加了一列"本身值得知道 —— 但只此**一条**,store=NULL、info、
    detail 里只有列名没有值。"""
    prev = _snap(limits={"A085": {F.category1: "Home"},
                         "A107": {F.category1: "Toys"}},
                 extra_cols=("SourceID",))
    cur = _snap(limits={"A085": {F.category1: "Home"},
                        "A107": {F.category1: "Toys"}},
                extra_cols=("SourceID", "备注", "序号"))
    evs = sc.diff(prev, cur)
    assert len(evs) == 1
    e = evs[0]
    assert e["store"] is None
    assert e["event"] == se.STORE_LIMITS_COLUMNS_CHANGED
    assert e["severity"] == "info"
    assert e["detail"] == {"added": ["备注", "序号"], "removed": []}


def test_unregistered_column_gone_is_the_same_one_event():
    evs = sc.diff(_snap(extra_cols=("SourceID", "备注")),
                  _snap(extra_cols=("SourceID",)))
    assert [(e["store"], e["event"], e["detail"]) for e in evs] == [
        (None, se.STORE_LIMITS_COLUMNS_CHANGED,
         {"added": [], "removed": ["备注"]})]
    # 没动就没事件(列名集合按集合比,顺序不算变化)
    assert sc.diff(_snap(extra_cols=("b", "a")), _snap(extra_cols=("a", "b"))) == []


# ── 沙箱 PG 集成 ──────────────────────────────────────────────────────────
#
# ⚠ 地址是**测试夹具**(生产走 registry/db.pg_dsn() 的 unix socket);非标准
# 端口 55432 正是为了不可能连到生产库。整场事务最后回滚。
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


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


@needs_pg
def test_pg_snapshot_roundtrip_and_one_real_diff_round(pg, monkeypatch):
    """真库上跑一轮:首轮零事件只落快照,第二轮按 diff 落治理事件。"""
    first = _snap(limits={"ZQX配置店": {F.category1: "Home",
                                        F.max_online: "3000"}},
                  stores={"ZQX配置店": {"enabled": True}}, scope=("谭总",))
    monkeypatch.setattr(sc, "take_snapshot", lambda: first)
    assert sc.check_and_record(pg) == ([], None)
    assert sc.load_snapshot(pg) == first          # ::jsonb 铸型 + 读回原形

    second = _snap(limits={"ZQX配置店": {F.category1: "Home",
                                         F.max_online: "0"}},
                   stores={"ZQX配置店": {"enabled": False}}, scope=("谭总",))
    monkeypatch.setattr(sc, "take_snapshot", lambda: second)
    events, warn = sc.check_and_record(pg)
    assert warn is None
    assert {(e["event"], e["severity"]) for e in events} == {
        (se.STORE_LIMITS_CHANGED, "high"),        # 最大在线数 3000→0 = 开关
        (se.STORE_ENABLED_CHANGED, "high")}
    with pg.cursor() as cur:
        cur.execute("SELECT event, severity, detail FROM ops.store_events "
                    "WHERE store = %s::text ORDER BY event", ("ZQX配置店",))
        got = cur.fetchall()
    assert [r[0] for r in got] == [se.STORE_ENABLED_CHANGED,
                                   se.STORE_LIMITS_CHANGED]
    assert got[1][2]["field"] == F.max_online
    assert sc.load_snapshot(pg) == second


def test_every_sql_param_is_cast_in_store_config():
    """与 store_events 同款 lint 护栏(dispositions 生产实炸三次的教训)。"""
    import pathlib
    import re
    src = pathlib.Path("services/store_config.py").read_text()
    bad = []
    for m in re.finditer(r'^(_\w*SQL)\s*=\s*(?:"""(.*?)"""|"(.*?)")',
                         src, re.S | re.M):
        for pm in re.finditer(r"%\((\w+)\)s(?!\s*::)", m.group(2) or m.group(3)):
            bad.append(f"{m.group(1)}.{pm.group(1)}")
    assert not bad, "这些 SQL 参数没写显式 ::类型:" + ", ".join(bad)
