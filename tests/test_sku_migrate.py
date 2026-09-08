"""存量改码工作流回归(SKU 改造批次 3 W1~W6)。

钉的都是"错了不报错"的那几件:
  · 前置六道闸任一不过就整店不改(改码是破坏动作,判不准就不做);
  · 三态定案只信**观测**,回执成功单独不定案(delete_not_effective 同款故障模式);
  · **先落库并 commit 再调接口**(未提交事务里 POST = 沃尔玛已受理、我们零记录);
  · dry-run 下 _settle 与 _migrate **都**零写(_settle 是全包写得最重的一段);
  · 节奏闸 1 → 10 → 按 limit,`-p limit=` 只能收紧;
  · 跟卖不入候选、不透明码不入候选;
  · 提交 failed 当场回滚,unknown **保持 pending 不回滚**(决策 F);
  · **库存只随改码 feed 写一次**(v5 的 `Item.inventory`,安全约束⑦),
    **定案不回写库存**(所有者 2026-09-07 定稿:同一份数据不走第二条写路径)。

守门(白名单/单一出处)一律在 tests/test_sku_guard.py,本文件只放行为测试。
"""

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from registry import schedule
from workflows import sku_migrate as sm

_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
#  夹具:假连接(按 SQL 片段喂返回值)+ 调用序记录
# ══════════════════════════════════════════════════════════════════════════════

class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows: list = []
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.conn.sqls.append((sql, args))
        cols, rows = self.conn.answer(sql)
        self.description = [type("D", (), {"name": c}) for c in cols]
        self._rows = list(rows)
        self.rowcount = len(rows)
        return self

    def executemany(self, sql, rows):
        self.conn.sqls.append((sql, list(rows)))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """假连接:`answers` 是 [(SQL 片段, (列名, 行))],按先匹配先用。"""

    def __init__(self, answers=(), log=None, tag="read"):
        self.answers = list(answers)
        self.sqls: list = []
        self.log = log if log is not None else []
        self.tag = tag
        self.committed = False

    def answer(self, sql):
        for frag, payload in self.answers:
            if frag in sql:
                return payload
        return ([], [])

    def cursor(self):
        return _Cur(self)

    def execute(self, sql, args=None):
        return self.cursor().execute(sql, args)

    def __enter__(self):
        self.log.append(f"open:{self.tag}")
        return self

    def __exit__(self, *a):
        self.committed = True
        self.log.append(f"commit:{self.tag}")
        return False


@pytest.fixture(autouse=True)
def _setup_limit_column_unset(monkeypatch):
    """「商品上限」列默认**读不到**(= 所有者还没建列)⇒ 上架上限走缺省 5000。

    钉住的是"单测一律不发飞书请求":不打这个桩时 `_headroom` 会真去调
    `store_limits.setup_limits()`,靠"表未登记抛 LookupError"侥幸得到空字典 ——
    哪天 token 进了环境变量,整个文件就开始打飞书。要钉列值的用例自己再打一次桩。
    """
    monkeypatch.setattr(sm.store_limits, "setup_limits", lambda: {})


@pytest.fixture(autouse=True)
def _open_submit_channel(monkeypatch):
    """既有用例默认在"提交通道可用"的世界里跑;通道停用那条闸另有专门用例钉。"""
    monkeypatch.setattr(sm, "SUBMIT_DISABLED", "")


def _wire(monkeypatch, *, enabled=("T1",), absent=(), note="", executing=0,
          cooldown=0, pending_feeds=(), dupes=()):
    """把 _preflight 的五道闸与订单体检全部打成"通过",逐条按需覆盖。"""
    monkeypatch.setattr(sm.stores_svc, "enabled_names", lambda: set(enabled))
    monkeypatch.setattr(sm.store_absence, "stale_or_note",
                        lambda conn, only=None: (set(absent), note))
    monkeypatch.setattr(sm.dispositions, "open_executing_count",
                        lambda conn, store: executing)
    monkeypatch.setattr(sm.feeds, "query_pending", lambda: list(pending_feeds))
    monkeypatch.setattr(sm.order_lines, "duplicate_po_lines",
                        lambda conn, days=120: list(dupes))
    monkeypatch.setattr(sm.stores_svc, "load_stores",
                        lambda filter_names=None: [{"name": "T1",
                                                    "client_id": "C1"}])
    return cooldown


def _read_conn(monkeypatch, answers, log=None, cooldown=0):
    """run() 里那条**只读**连接。写侧的短事务由各用例自己再打桩。"""
    ans = [("listing.retire_cooldown", (["count"], [(cooldown,)]))] + list(answers)
    conn = _Conn(ans, log=log, tag="read")
    monkeypatch.setattr(sm.db, "pg_conn", lambda *a, **k: conn)
    return conn


# ══════════════════════════════════════════════════════════════════════════════
#  W1 · 模块契约
# ══════════════════════════════════════════════════════════════════════════════

def test_module_flags_are_dangerous_and_store_scoped():
    """DANGEROUS 写错 = 调度里空转还报成功;两个开关都是 cli 的契约面。"""
    assert sm.DANGEROUS is True
    assert sm.SUPPORTS_STORE is True
    assert sm.FEED_TYPE == "MP_ITEM_MATCH"          # 2026-09-06 通道定案(§9.12)
    assert sm.SOURCE_TYPES == (sm.listing_sources.SOURCE_AMZ,)


def test_workflow_imports_no_other_workflow():
    """铁律 1:任何层都不许 import workflows —— 链是调度的事(cli 串联)。"""
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "workflows"):
            bad.append(node.module)
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.startswith("workflows")]
    assert not bad, f"改码工作流 import 了别的工作流:{bad}"


def test_feed_type_constant_is_the_only_place_that_names_a_feedtype():
    """feedType 只有 FEED_TYPE 一个出生地:换通道必须只有一个改动点。

    ⚠ 只看**可执行的行**(`#` 注释与模块 docstring 剔除):头注里要能把
    「MP_MAINTENANCE 为什么作废、MP_ITEM_MATCH 为什么是它」讲清楚,那是文档不是
    第二个出生地。真正的漂移长成 `feeds.submit_feed(store, "MP_ITEM_MATCH", …)`
    这种字面量 —— 那一行改不到,换回通道时就会一半新一半旧,而且不报错。
    """
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    body = src.replace(ast.get_docstring(ast.parse(src)) or "", "", 1)
    hits = [ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
            and any(t in ln for t in ('"MP_MAINTENANCE"', "'MP_MAINTENANCE'",
                                      '"MP_ITEM_MATCH"', "'MP_ITEM_MATCH'",
                                      '"MP_ITEM"', "'MP_ITEM'"))]
    assert len(hits) == 1 and hits[0].startswith("FEED_TYPE ="), hits


def test_no_self_imposed_feed_or_item_ceiling_lives_in_this_workflow():
    """源码守门:**可执行行**里不许再出现 `FEEDS_PER_STORE_PER_RUN` / `ITEMS_PER_FEED`。

    只看可执行行(`#` 注释与模块 docstring 剔除):头注里要能把「它们是形态 A 时代
    为 MP_MAINTENANCE 桶留量自设的、官方限的是 20 feed/h + 25MB、两道限都在 api 层」
    讲清楚,那是文档不是第二道闸。加回来的表现不会报错 —— 整店真跑只发前 N 条就停,
    而摘要读起来像官方配额(2026-09-06 A085朱丽霖:3371 个在线品只发了 1000 条)。
    """
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    body = src.replace(ast.get_docstring(ast.parse(src)) or "", "", 1)
    hits = [ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
            and any(t in ln for t in ("FEEDS_PER_STORE_PER_RUN", "ITEMS_PER_FEED"))]
    assert not hits, f"改码又自设每轮上限了(2026-09-07 所有者纠正:不设):{hits}"
    assert not hasattr(sm, "FEEDS_PER_STORE_PER_RUN")
    assert not hasattr(sm, "ITEMS_PER_FEED")


def test_sku_migrate_is_never_scheduled_but_is_named_in_the_manual_list():
    """永不进调度(R1):它是一次性、按批、人盯定案的破坏动作,而且与 13:00 的
    product_chain 抢同一个 MP_MAINTENANCE 桶。头注那份"手动清单"必须点它的名,
    否则下一个人读调度表会以为它是被漏掉的。"""
    scheduled = {w for j in schedule.JOBS for w in j["workflows"]}
    assert "sku_migrate" not in scheduled
    assert "sku_migrate" in (schedule.__doc__ or "")


# ══════════════════════════════════════════════════════════════════════════════
#  W2 · 前置闸
# ══════════════════════════════════════════════════════════════════════════════

def _preflight_out(monkeypatch, **kw):
    cooldown = _wire(monkeypatch, **kw)
    conn = _Conn([("listing.retire_cooldown", (["count"], [(cooldown,)]))])
    return sm._preflight(conn, "T1")


def test_disabled_store_is_refused(monkeypatch):
    ok, lines = _preflight_out(monkeypatch, enabled=())
    assert not ok and any("闸①在营" in ln and "⛔" in ln for ln in lines)


def test_stale_catalog_watermark_blocks_the_store(monkeypatch):
    ok, lines = _preflight_out(monkeypatch, absent=("T1",))
    assert not ok and any("闸②水位" in ln for ln in lines)


def test_absence_probe_failure_blocks_the_store(monkeypatch):
    """探测失败 ⇒ **不放行**(下游避让件的降级方向是"不避让",破坏件相反)。"""
    ok, lines = _preflight_out(monkeypatch, note="⚠ 缺席探测失败(RuntimeError)")
    assert not ok and any("闸②水位" in ln for ln in lines)


def test_executing_dispositions_report_but_do_not_block_the_store(monkeypatch):
    """⚠ 所有者 2026-09-04 复议:闸③**只报数,不拦整店**。

    改前是"整店 executing 非零即拦",而 `open_executing_count` 自己的头注说的是
    「改码会把**它等的那个 (店, SKU)** 键换掉」—— 危害逐 (店,SKU),按整店计数拦
    是粗放的过近似。生产事实:每天 13:00 价格/库存/标题三条链齐发,之后任何一家店
    都有几百条 executing ⇒ 按整店拦 = 改码永远开不了工。判据里也没有任何跨 SKU
    依赖(settle / settle_maintenance / expire_executing 全部逐 (店,SKU) 判)。

    真正的危害搬到了候选判据「无未了结处置」上,而且**更严**(连 suggested 一起拦)。
    """
    ok, lines = _preflight_out(monkeypatch, executing=3)
    assert ok, lines
    ln = next(ln for ln in lines if "闸③" in ln)
    assert "3 条 executing" in ln and "不拦整店" in ln     # 上下文要给人看见


def test_the_real_guard_moved_to_a_per_candidate_condition():
    """危害搬家了就要在新家钉住。三条:
    ① 逐 (店,SKU),不看别的 SKU;
    ② **只拦破坏组**(delete/retire)—— 维护组 title/price/inventory 不拦,
       所有者 2026-09-04:13:00 三条链齐发是常态,拦它等于改码永远开不了工;
    ③ 破坏组连 suggested 一起拦(它马上会被 claim 成一条打在旧码上的 DELETE)。
    """
    cond = next(sql for n, _w, sql in sm._CONDS if n == "无未了结破坏建议")
    assert "FROM ops.dispositions d" in cond
    assert "d.store = w.store" in cond and "d.sku = w.sku" in cond   # ①
    assert "d.settled_at IS NULL" in cond
    assert "'suggested'" in cond and "'executing'" in cond           # ③
    # ② 动作集从 dispositions 的常量派生,不在这儿手打第二份名单
    for a in sm.dispositions.DESTRUCTIVE_ACTIONS:
        assert f"'{a}'" in cond
    for a in sm.dispositions.MAINT_ACTIONS:
        assert f"'{a}'" not in cond, a
    assert cond in sm._SQL_CANDIDATES and cond in sm._SQL_WHY        # 两处同源
    why = next(w for n, w, _sql in sm._CONDS if n == "无未了结破坏建议")
    assert "删除成功" in why and "假确认" in why    # 最狠的那条后果要写在人话里


def test_executing_rows_are_named_and_the_two_groups_go_different_ways():
    """两组的去向 2026-09-08 起**不一样**(§9.15),摘要要分开说:

      · 维护组(title/price/inventory)的 executing **已随 rekey_open 迁到新码**,
        由维护链按新码观测落定(同 wpid = 同一条 listing,新码的现值就是那条 feed
        作用的对象);
      · 破坏组(delete/retire)**仍不迁**,滞留旧码等 expire_executing 收尾,
        而且它本不该出现在这里 —— 要额外喊「请人工核」。
    """
    import inspect
    src = inspect.getsource(sm._confirm)
    assert "executing_actions_on" in src
    # 先读后改:rekey 之后旧码名下的行已经搬走,再读就读不到了
    assert src.index("executing_actions_on") < src.index("rekey_open")
    assert "MAINT_ACTIONS" in src and "已迁到新码" in src
    assert "DESTRUCTIVE_ACTIONS" in src and "请人工核" in src


def _confirm_wired(monkeypatch, *, stranded=(), taken=()):
    """`_confirm` 的五处写全部打桩 → 只看它怎么点名。"""
    monkeypatch.setattr(sm.db, "pg_conn", lambda *a, **k: _Conn(tag="tx"))
    monkeypatch.setattr(sm.sku_codec, "settle_replacement", lambda *a, **k: None)
    monkeypatch.setattr(sm.upc_pool, "retag_sku", lambda *a, **k: None)
    monkeypatch.setattr(sm.walmart_catalog, "drop_node_rows", lambda *a, **k: 1)
    monkeypatch.setattr(sm.dispositions, "executing_actions_on",
                        lambda *a, **k: list(stranded))
    monkeypatch.setattr(sm.dispositions, "rekey_open",
                        lambda *a, **k: (1, list(taken)))
    return {"id": 1, "old_sku": "B0OLD00001", "new_sku": "AAAAAAAAAAAA",
            "source_type": "amz", "source_key": "B0OLD00001"}


def test_a_migrated_maintenance_executing_row_is_reported_as_migrated(monkeypatch):
    """维护组的 executing 迁走之后,摘要说的是「已迁到新码,由维护链按新码观测落定」
    —— 不再是「不迁,由 expire_executing 判成 ineffective」(A171罗尹鸿 691466 那句)。"""
    row = _confirm_wired(monkeypatch, stranded=["price"])
    warns = sm._confirm("T1", row)
    ln = next(ln for ln in warns if "price" in ln)
    assert "已迁到新码" in ln and "维护链" in ln
    assert "executed_at 不改" in ln          # 宽限期照旧从原提交时刻算
    assert "expire_executing" not in ln


def test_a_destructive_executing_row_is_still_never_migrated(monkeypatch):
    """破坏组的告警**原样保留**:不迁、滞留旧码、请人工核。"""
    row = _confirm_wired(monkeypatch, stranded=["delete"])
    warns = sm._confirm("T1", row)
    ln = next(ln for ln in warns if "delete" in ln)
    assert "不迁" in ln and "expire_executing" in ln and "请人工核" in ln
    assert "已迁到新码" not in ln


def test_a_maintenance_row_that_collides_is_named_not_reported_as_migrated(monkeypatch):
    """撞车(新码名下已有同动作未落定行)的**不算迁走**:只报「请人工处置」那一句。"""
    row = _confirm_wired(monkeypatch, stranded=["price"], taken=["price"])
    warns = sm._confirm("T1", row)
    assert any("请人工处置" in ln for ln in warns)
    assert not any("已迁到新码" in ln for ln in warns)


def test_open_retire_cooldown_blocks_the_store(monkeypatch):
    ok, lines = _preflight_out(monkeypatch, cooldown=2)
    assert not ok and any("闸④自愈链" in ln for ln in lines)


def test_pending_feed_log_row_blocks_the_store(monkeypatch):
    ok, lines = _preflight_out(monkeypatch, pending_feeds=[
        {"workflow": "sku_migrate", "store": "T1", "status": "submitted"}])
    assert not ok and any("闸⑤" in ln for ln in lines)


def test_another_workflows_pending_feed_does_not_block(monkeypatch):
    """闸⑤只管**本工作流自己**的在途:别的链在途是常态,拦它等于永远开不了工。"""
    ok, lines = _preflight_out(monkeypatch, pending_feeds=[
        {"workflow": "list_new", "store": "T1", "status": "submitted"},
        {"workflow": "sku_migrate", "store": "T2", "status": "pending"}])
    assert ok, lines


# ══════════════════════════════════════════════════════════════════════════════
#  W4 · 候选
# ══════════════════════════════════════════════════════════════════════════════

_CAND_COLS = ["store", "old_sku", "source_type", "source_key",
              "product_id", "product_id_type", "price", "avail_qty",
              "product_slow"]

#: 候选行的采集 slow 段。这个形状与 catalog.products.slow / amz_source 的
#: `attrs` 逐字同源(`mp_mapper.shipping_weight_ex` 的输入)。
#: ⚠ **必须带显式单位**:2026-09-06 起裸数字算"解析不出"(不假设是磅),
#: 写成 `0.82` 的话这条夹具行会落到兜底 1.0 磅 —— 那是另一档用例的事。
_SLOW = {"weight": {"package": "0.82 lbs"}}


def _cand(old_sku, pid="0001", src="amz", key=None, price=29.99, slow=_SLOW,
          qty=None):
    """候选行元组。`qty` = 旧码行最后观测的 `avail_qty`(2026-09-07 随通道升 v5
    带出):**缺省 None = 从没观测到**,那样的行载荷不带库存(不猜 0)。"""
    return ("T1", old_sku, src, key or "B0" + old_sku[-8:], pid, "GTIN",
            price, qty, slow)


def test_opaque_and_match_rows_are_excluded_by_the_candidate_sql():
    """两条排除写在 SQL 里(不是 Python 事后过滤):跟卖不迁(决策 D),
    已经是不透明码的不再改(改码只有一跳)。"""
    sql = sm._SQL_CANDIDATES
    assert "source_type = ANY(%(source_types)s::text[])" in sql
    assert sm.SOURCE_TYPES == ("amz",)                    # match 天然不在候选面
    assert "AND NOT (" in sql and "w.sku ~ " in sql       # 形态判据来自 codec 常量
    assert sm.sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku") in sql
    # 未了结的改码台账挡住重复发起(崩溃重入不会开第二条 pending)。
    # ⚠ `'double'` 是 2026-09-07 加的(所有者定稿 §9.14):双挂行**不再拦节奏闸**,
    # 于是这条判据成了它挡重复提交的**唯一**一道 —— 少了它,同一个旧码下一轮又会
    # 被选进候选面、又 mint 一个新码、又发一条 MP_ITEM_MATCH,一条双挂变三挂,
    # 而且回执全绿、摘要正常。部分唯一索引 sku_migrations_open_uidx 只管 pending,
    # 从此不覆盖 double 行,指望不上。
    assert "m.status IN ('pending', 'confirmed', 'stalled', 'double')" in sql
    # Product ID 取观测值,不取 UPC 池;**优先 GTIN**(所有者实测的模板就是 GTIN 14 位)
    assert "coalesce(w.gtin, w.upc)" in sql and "upc_pool" not in sql
    assert "WHEN w.gtin IS NOT NULL THEN 'GTIN'" in sql


def test_a_double_row_never_becomes_a_candidate_again():
    """双挂的旧码**永不重复提交**:候选判据「无未了结改码台账」含 'double'。

    所有者 2026-09-07:「中途不重复提交这种双挂的就可以。」double 行不占节奏闸,
    部分唯一索引 sku_migrations_open_uidx(WHERE status='pending')也不覆盖它 ——
    台账侧就靠这一条(登记簿侧另有「未在改」:双挂行的旧行仍 replaced_by=新码)。
    选取与解释两处同源(与其余判据同一条纪律)。
    """
    cond = next(sql for n, _w, sql in sm._CONDS if n == "无未了结改码台账")
    assert "'double'" in cond
    assert cond in sm._SQL_CANDIDATES and cond in sm._SQL_WHY
    why = next(w for n, w, _sql in sm._CONDS if n == "无未了结改码台账")
    assert "double" in why and "不许开第二条" in why      # 落选要说得出人话


def test_candidate_with_an_inflight_feed_is_skipped_and_named():
    conn = _Conn([("FROM catalog.walmart_items w", (_CAND_COLS,
                                                    [_cand("B0AAA00001"),
                                                     _cand("B0AAA00002", "0002")])),
                  ("FROM ops.feed_items", (["sku"], [("B0AAA00001",)]))])
    rows, notes = sm._candidates(conn, "T1", 10)
    assert [r["old_sku"] for r in rows] == ["B0AAA00002"]
    assert notes and "在途 feed" in notes[0]


def test_two_candidates_sharing_a_product_id_keep_only_the_first():
    """官方:一个 Product ID 只允许挂一个 SKU。同批撞号不去重 = 整批被拒。"""
    conn = _Conn([("FROM catalog.walmart_items w",
                   (_CAND_COLS, [_cand("B0AAA00001", "9"), _cand("B0AAA00002", "9")])),
                  ("FROM ops.feed_items", (["sku"], []))])
    rows, notes = sm._candidates(conn, "T1", 10)
    assert [r["old_sku"] for r in rows] == ["B0AAA00001"]
    assert any("Product ID 撞号" in n for n in notes)


def test_zero_cap_asks_the_database_nothing():
    """上限 0(前置闸未过 / settle_only / 上一批没清)⇒ 一条候选 SQL 都不发。"""
    conn = _Conn()
    assert sm._candidates(conn, "T1", 0) == ([], [])
    assert conn.sqls == []


# ══════════════════════════════════════════════════════════════════════════════
#  W4 · 点名 / 排除(2026-09-03:存量改码要能挑着做)
#
#  钉的是"错了不报错"的两件:① 点名/排除不许长出第二条候选 SQL(两条一漂,点名
#  跑的就不再是全量跑的那套闸);② 点名了却没出现的**必须有名有姓的理由** ——
#  静默丢的表现是摘要看起来像"这家店没候选",而所有者以为自己点的名生效了。
# ══════════════════════════════════════════════════════════════════════════════

_WHY_COLS = ["old_sku", "source_key"] + [f"c{i}" for i in range(len(sm._CONDS))]


def _why(old_sku, key=None, bad=()):
    """一行 `_SQL_WHY` 结果:默认十一条判据全真,`bad` 里点名的那几条置假(按短名)。"""
    return (old_sku, key or old_sku) + tuple(
        n not in bad for n, _w, _sql in sm._CONDS)


def _pick_conn(cand_rows=(), why_rows=(), inflight=()):
    """点名用的假连接。**理由 SQL 的答案必须排在候选之前**:两条 SQL 共用同一个
    FROM 片段(它们本来就同源),按片段匹配的假连接只认先来的那条。"""
    return _Conn([("AS c0", (_WHY_COLS, list(why_rows))),
                  ("FROM catalog.walmart_items w", (_CAND_COLS, list(cand_rows))),
                  ("FROM ops.feed_items", (["sku"], [(s,) for s in inflight]))])


def _args_of(conn, frag="LIMIT %(limit)s"):
    return [a for sql, a in conn.sqls if frag in sql][0]


def test_only_published_rows_are_candidates():
    """⚠ 所有者 2026-09-04 提的那条,而且是**必须**的一条:`missing_since IS NULL`
    只说"目录里还看得见",UNPUBLISHED / RETIRED / STAGE 全都满足它。

    为什么不能放它们进来:§4 六件实测的第 5 件正是「对 lifecycle=RETIRED 的 item
    是否可用 SkuUpdate」——官方零文档、本仓零实证。放进候选面 ⇒ ① 改不动就卡到
    72 小时 stalled、占着节奏闸名额;② 中间窗口里旧码"非 PUBLISHED 且未缺席",
    正好落进 problem_scan 的扫描面被建议 DELETE_ITEM —— 一次改码把商品永久删掉。

    ⚠ **不许加参数开关放开它**:那样就是两条口径,而"哪些状态能改码"是判据不是
    偏好(§六 双轨禁止)。要迁非 PUBLISHED 的行,先做第 5 件实测再改这条判据。
    """
    names = [n for n, _w, _sql in sm._CONDS]
    assert "已上架" in names
    cond = next(sql for n, _w, sql in sm._CONDS if n == "已上架")
    assert cond == "w.published_status = 'PUBLISHED'"
    # 选取与解释两处同源(与其余判据同一条纪律)
    assert cond in sm._SQL_CANDIDATES and cond in sm._SQL_WHY
    # 落选点名说得出人话,而且点名了 RETIRED 那类要能看见理由
    why = next(w for n, w, _sql in sm._CONDS if n == "已上架")
    assert "PUBLISHED" in why and "实测" in why


def test_a_named_but_unpublished_row_is_reported_not_silently_dropped():
    """点名了一个非 PUBLISHED 的旧码:不许静默消失,要逐条说"不满足 已上架"。"""
    conn = _pick_conn([], [_why("B0AAA00001", bad=("已上架",))])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00001"])
    assert rows == []
    # 点名用短名、落选点名用**人话**(摘要是给人读的,短名只在代码里)
    assert any("B0AAA00001" in n and "非 PUBLISHED" in n for n in notes), notes


def test_pick_and_exclude_are_conditions_on_the_one_candidate_sql():
    """**单一实现路径**:点名/排除是同一条候选 SQL 的参数化条件,不是第二条 SQL;
    而且十一条判据在"选取"与"解释"两处**逐字同源**(一漂就会出现"摘要说它满足
    条件,可它就是不在候选面上",谁也不报错)。"""
    sql = sm._SQL_CANDIDATES
    assert sql.count("FROM catalog.walmart_items w") == 1
    assert "%(only_skus)s" in sql and "%(only_keys)s" in sql
    assert "%(excl_skus)s" in sql and "%(excl_keys)s" in sql
    # 排除拼在点名**之后**且是 NOT ⇒ 既点名又排除时排除赢
    assert sql.index("%(only_skus)s") < sql.index("%(excl_skus)s")
    assert "%(unnamed)s::boolean" in sql            # 没点名 ⇒ 整个 OR 恒真
    for _n, _w, cond in sm._CONDS:
        assert cond in sql and cond in sm._SQL_WHY
    assert "LIMIT" not in sm._SQL_WHY               # 理由面不受本轮上限影响
    assert "%(excl_skus)s" not in sm._SQL_WHY       # 被排除的也要能说出口


def test_naming_a_sku_narrows_the_face_and_says_how_many_hit():
    conn = _pick_conn([_cand("B0AAA00001")], [_why("B0AAA00001")])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00001"])
    assert [r["old_sku"] for r in rows] == ["B0AAA00001"]
    args = _args_of(conn)
    assert args["only_skus"] == ["B0AAA00001"] and args["unnamed"] is False
    assert any("点名 1 个" in n and "命中 1 个" in n for n in notes)


def test_naming_by_asin_uses_the_registry_source_key():
    """所有者更习惯按 ASIN 说话:`-p asins=` 打在登记簿 `source_key` 上。"""
    conn = _pick_conn([_cand("B0AAA00001", key="B0ASIN0001")],
                      [_why("B0AAA00001", key="B0ASIN0001")])
    rows, notes = sm._candidates(conn, "T1", 10, only_keys=["B0ASIN0001"])
    assert [r["old_sku"] for r in rows] == ["B0AAA00001"]
    assert _args_of(conn)["only_keys"] == ["B0ASIN0001"]
    assert any("命中 1 个" in n for n in notes)


def test_a_named_row_that_misses_a_condition_is_named_with_the_reason():
    """点名了却不满足候选条件 ⇒ **逐条**说为什么,不静默丢。"""
    conn = _pick_conn([], [_why("B0AAA00002", bad=("活码",))])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00002"])
    assert rows == []
    assert any("命中 0 个" in n for n in notes)
    assert any("B0AAA00002" in n and "码已弃用" in n for n in notes)


def test_a_named_asin_that_misses_a_condition_names_both_asin_and_sku():
    conn = _pick_conn([], [_why("B0AAA00003", key="B0ASIN0003", bad=("在架",))])
    rows, notes = sm._candidates(conn, "T1", 10, only_keys=["B0ASIN0003"])
    assert rows == []
    assert any("B0ASIN0003(ASIN→B0AAA00003)" in n and "已缺席" in n for n in notes)


def test_a_named_row_the_store_never_heard_of_is_named_too():
    """拼错一个字母不许表现成"这家店没候选"。"""
    conn = _pick_conn([], [])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0TYPO0001"],
                                 only_keys=["B0TYPO0002"])
    assert rows == []
    assert any("B0TYPO0001" in n and "查无此 SKU" in n for n in notes)
    assert any("B0TYPO0002(ASIN)" in n and "查无此 source_key" in n for n in notes)


def test_a_named_row_blocked_by_the_inflight_gate_says_which_gate():
    """点名不放松逐候选的在途闸 —— 但落选要说清是被哪道闸挡的。"""
    conn = _pick_conn([_cand("B0AAA00001")], [_why("B0AAA00001")],
                      inflight=["B0AAA00001"])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00001"])
    assert rows == []
    assert any("命中 0 个" in n for n in notes)
    assert any(n.strip().startswith("· B0AAA00001:") and "在途 feed" in n
               for n in notes)


def test_exclude_beats_the_pick_and_says_so():
    """排除优先:同一条既被点名又被排除 ⇒ 不改,而且理由是"你自己排除了它"。"""
    conn = _pick_conn([], [_why("B0AAA00001")])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00001"],
                                 exclude_skus=["B0AAA00001"])
    assert rows == []
    args = _args_of(conn)
    assert args["excl_skus"] == ["B0AAA00001"] and args["only_skus"] == \
        ["B0AAA00001"]
    assert any("排除优先于点名" in n for n in notes)


def test_exclude_alone_keeps_the_rest_of_the_face_and_asks_no_reason_sql():
    """只给排除、不点名 ⇒ 其余照常按 SKU 升序取,理由 SQL 一条都不发。"""
    conn = _pick_conn([_cand("B0AAA00002", "0002")])
    rows, notes = sm._candidates(conn, "T1", 10, exclude_skus=["B0AAA00001"])
    assert [r["old_sku"] for r in rows] == ["B0AAA00002"]
    assert _args_of(conn)["unnamed"] is True
    assert not any("AS c0" in sql for sql, _ in conn.sqls)
    assert any("排除 -p exclude_skus 1 个" in n for n in notes)


def test_named_rows_beyond_the_cap_are_told_they_are_next_round():
    """点名 3 个、上限 1 个:剩下两个**不是**落选,是"没轮到",下轮还在候选面上。"""
    conn = _pick_conn([_cand("B0AAA00001")],
                      [_why("B0AAA00001"), _why("B0AAA00002"),
                       _why("B0AAA00003")])
    rows, notes = sm._candidates(conn, "T1", 1,
                                 only_skus=["B0AAA00001", "B0AAA00002",
                                            "B0AAA00003"])
    assert [r["old_sku"] for r in rows] == ["B0AAA00001"]
    assert _args_of(conn)["limit"] == 1
    assert any("点名 3 个" in n and "命中 1 个" in n for n in notes)
    assert any("B0AAA00002、B0AAA00003" in n and "没轮到它" in n for n in notes)


def test_parse_names_splits_dedupes_and_keeps_case_and_order():
    """逗号/空白/换行混排都要认(所有者是复制粘贴的);去重**保序**;
    大小写**一律不动**(SKU 大小写敏感,口径同 order_lines.norm_sku)。"""
    assert sm._parse_names("B0A, B0B\nB0C  B0D,,\n") == ["B0A", "B0B", "B0C",
                                                          "B0D"]
    assert sm._parse_names("B0A,B0A, B0A") == ["B0A"]
    assert sm._parse_names("b0a,B0A") == ["b0a", "B0A"]
    assert sm._parse_names("") == [] and sm._parse_names(None) == []
    assert sm._parse_names(" , ,, ") == []


# ══════════════════════════════════════════════════════════════════════════════
#  W5 · 节奏硬闸
# ══════════════════════════════════════════════════════════════════════════════

def _cap(confirmed, open_rows, asked, online=0):
    conn = _Conn([("FROM listing.sku_migrations",
                   (["confirmed", "open"], [(confirmed, open_rows)])),
                  ("count(*) FROM catalog.walmart_items",
                   (["n"], [(online,)]))])
    return sm._stage_cap(conn, "T1", asked)


def test_first_batch_is_capped_at_one():
    cap, note = _cap(0, 0, 100)
    assert cap == 1 and "第一级" in note


def test_stage_counts_confirmed_fleet_wide_but_open_rows_per_store():
    """所有者 2026-09-07 定稿:1 → 10 验的是通道(店无关),confirmed 按全船队数;
    pending/stalled 仍按店数(该店账没清就不发)。钉住 SQL 的两个作用域。"""
    q = sm._SQL_STAGE
    assert "(SELECT count(*) FROM listing.sku_migrations" in q
    assert "WHERE status = 'confirmed')" in q
    assert q.strip().endswith("WHERE store = %(store)s")
    assert "全船队" in _cap(12, 0, 100)[1]


def test_second_stage_is_capped_at_ten():
    cap, note = _cap(3, 0, 100)
    assert cap == 10 and "第二级" in note


def test_limit_can_only_tighten_never_loosen():
    assert _cap(50, 0, 5)[0] == 5             # 放行档:按 limit
    assert _cap(3, 0, 2)[0] == 2              # 第二级:limit 更小时按 limit
    assert _cap(0, 0, 999)[0] == 1            # 第一级:limit 大也压到 1


def test_no_self_imposed_per_run_item_ceiling_survives(monkeypatch):
    """节奏闸放行之后**不再叠**「每轮 1000 条」的自设硬顶(2026-09-07 所有者纠正)。

    那一层(2 个 feed × 500 条)是批次 3 还走 MP_MAINTENANCE(8/h,与 13:00 维护链
    共享)时**为维护链留桶**自设的,**不是官方限制**:官方对 MP_ITEM_MATCH 是
    20 feed/hour、单 feed 25MB,仓内两道限都已在 **api 层**(桶 15/h、切片
    1000 条/24MB)。留着的表现是整店真跑 3371 个在线品只发了 1000 条就停,
    而摘要说得像官方配额(2026-09-06 A085朱丽霖 实见)。

    ⚠ 这里把「商品上限」放到极大,是为了**把沃尔玛那道硬限(安全约束⑧)让开**——
    它与本条钉的自设硬顶不是一回事:那是我们拍的数,这是沃尔玛的整 feed 拒收线。
    """
    monkeypatch.setattr(sm.store_limits, "setup_limits", lambda: {"T1": 10 ** 7})
    cap, note = _cap(999, 0, 10 ** 6)
    assert cap == 10 ** 6                      # 放行档只按 -p limit,不再截
    assert "配额留量硬顶" not in note
    assert not hasattr(sm, "FEEDS_PER_STORE_PER_RUN")
    assert not hasattr(sm, "ITEMS_PER_FEED")


def test_no_new_submissions_while_pending_or_stalled_rows_exist():
    cap, note = _cap(20, 1, 10)
    assert cap == 0 and "只定案不提交" in note
    # 账没清那一档也要报余量:人看摘要时该知道这家店还剩多少位置
    assert "上架上限闸" in note


# ── 上架上限闸(安全约束⑧;2026-09-07 A131吕灿荣 整店被拒实证)────────────────

def test_headroom_defaults_to_the_walmart_item_setup_limit(monkeypatch):
    """该店没填「商品上限」⇒ 走缺省 5000(常量出生地只有 registry 一处)。

    余量 = 上限 − 现观测在架 item 数;摘要必须把三个数都报出来,否则
    "本轮只发了 100 个"看起来像节奏闸,而真实原因是这家店快满了。
    """
    from registry import resources
    assert resources.WALMART_ITEM_SETUP_LIMIT_DEFAULT == 5000
    cap, note = _cap(999, 0, 10 ** 6, online=4900)
    assert cap == 100                                   # 5000 − 4900
    assert "上架上限闸:上限 5000(缺省,该店未填「商品上限」),在架 4900," \
           "本轮最多 100" in note


def test_a_filled_column_overrides_the_default_limit(monkeypatch):
    """限额表填了就以列为准(所有者可在 Seller Center 查各店真实上限)。"""
    monkeypatch.setattr(sm.store_limits, "setup_limits", lambda: {"T1": 8000})
    cap, note = _cap(999, 0, 10 ** 6, online=4900)
    assert cap == 3100                                  # 8000 − 4900
    assert "上限 8000(限额表「商品上限」)" in note
    # 别的店填了不影响本店(按店读列,不是全船队一个数)
    monkeypatch.setattr(sm.store_limits, "setup_limits", lambda: {"T9": 8000})
    assert _cap(999, 0, 10 ** 6, online=4900)[0] == 100


def test_a_full_store_is_capped_at_zero_and_told_to_clean_up_first():
    """余量 ≤ 0 ⇒ 上限 0。

    不拦的后果就是 2026-09-07 A131吕灿荣 那一轮:2740 条分三个 feed 全部
    `feedStatus=ERROR`、`itemsReceived=0`,一条都没进沃尔玛,而摘要说"已提交 2740"。
    """
    cap, note = _cap(999, 0, 10 ** 6, online=5000)
    assert cap == 0
    assert "店内 item 数 5000 已达上架上限 5000" in note
    assert "先清理死档再改码" in note
    assert _cap(999, 0, 10 ** 6, online=5200)[0] == 0   # 超了也不给负数


def test_headroom_stacks_with_the_stage_gate_and_the_asked_limit():
    """三层取 min,谁小听谁的(节奏闸 / -p limit / 上架上限余量)。"""
    assert _cap(0, 0, 10 ** 6, online=0)[0] == 1        # 第一级 1 < 余量 5000
    assert _cap(3, 0, 10 ** 6, online=4995)[0] == 5     # 余量 5 < 第二级 10
    assert _cap(999, 0, 3, online=4000)[0] == 3         # -p limit 3 最小
    assert _cap(999, 0, 10 ** 6, online=4999)[0] == 1   # 余量 1 最小


def test_the_workflow_never_reads_feishu_itself(monkeypatch):
    """读限额表只走 services(`store_limits.setup_limits`),不在工作流里读飞书。"""
    import inspect
    src = inspect.getsource(sm)
    assert "store_limits.setup_limits" in src
    assert "feishu" not in src


# ══════════════════════════════════════════════════════════════════════════════
#  W3 · 三态判决(纯函数)
# ══════════════════════════════════════════════════════════════════════════════

def _obs(new_present=False, old_gone=False, fresh=True, hours=1,
         new_wpid=None, old_wpid=None, old_probe=None):
    return {"id": 1, "old_sku": "B0OLD00001", "new_sku": "AAAAAAAAAAAA",
            "source_type": "amz", "source_key": "B0OLD00001", "feed_id": "F1",
            "submitted_at": NOW - timedelta(hours=hours),
            "new_present": new_present, "old_gone": old_gone, "fresh": fresh,
            "new_wpid": new_wpid, "old_wpid": old_wpid, "old_probe": old_probe}


def test_confirmed_needs_new_present_and_old_gone():
    v, why = sm._verdict(_obs(True, True), ("success", ""), NOW)
    assert v == "confirmed" and "观测确认" in why


def test_receipt_success_alone_does_not_settle():
    """回执成功但观测里新码还没出现 ⇒ **不定案**(回执不是判据)。"""
    v, _ = sm._verdict(_obs(False, False, hours=1), ("success", ""), NOW)
    assert v == "pending"


def test_both_codes_live_is_a_double_listing_and_never_settles():
    v, why = sm._verdict(_obs(True, False, hours=100), ("success", ""), NOW)
    assert v == "double" and "同时在架" in why


def test_a_shadow_double_settles_confirmed_on_two_pieces_of_evidence():
    """判词 (a′):**同 wpid + 旧码单查 404** ⇒ confirmed(2026-09-08,§9.15)。

    A131吕灿荣 43 条停在 double 的改码里 41 条新旧 wpid **相同** —— 同 wpid =
    同一条 listing 原地换码,改码其实成功了;"旧码还在架"只是 2026-08-28 起
    列表接口把已删档案照旧吐回(僵尸列表,backlog §十三),单条 GET 是 404。
    按现状那 41 条永远停在 double:旧码永不弃码、UPC 永不改标、处置永不迁键。
    """
    v, why = sm._verdict(_obs(True, False, hours=100, new_wpid="W9",
                              old_wpid="W9", old_probe=404), None, NOW)
    assert v == "confirmed"
    assert "同 wpid" in why and "404" in why and "影子" in why


def test_a_same_wpid_double_whose_old_code_answers_200_stays_double():
    """单查 200 = 旧码**真的还在**沃尔玛那儿 ⇒ 仍判 double,不许定案。"""
    v, why = sm._verdict(_obs(True, False, hours=100, new_wpid="W9",
                              old_wpid="W9", old_probe=200), None, NOW)
    assert v == "double" and "同时在架" in why


def test_a_404_without_the_same_wpid_stays_double():
    """**两条证据缺一不可**:只有 404 而 wpid 不同 ⇒ 那是真的多了一条 listing
    (A131 那 2 条:B08DR3TKQK 两个 wpid 都 PUBLISHED;B09L3WXJ96 旧码是 RETIRED
    死档、新码是新建 item),仍判 double 交人工。"""
    v, _ = sm._verdict(_obs(True, False, hours=100, new_wpid="WNEW",
                            old_wpid="WOLD", old_probe=404), None, NOW)
    assert v == "double"
    # 反过来同理:只有同 wpid、没探测过(old_probe 为 None)也不许定案 —— 探不出来
    # 就当它是真双挂(fail-closed),不猜
    v2, _ = sm._verdict(_obs(True, False, hours=100, new_wpid="W9",
                             old_wpid="W9"), None, NOW)
    assert v2 == "double"


def test_failed_receipt_rolls_back():
    v, why = sm._verdict(_obs(False, True), ("failed", "ERR_1"), NOW)
    assert v == "rolled_back" and "ERR_1" in why


def test_timeout_after_a_fresh_sweep_rolls_back():
    v, _ = sm._verdict(_obs(False, False, fresh=True, hours=25), None, NOW)
    assert v == "rolled_back"


def test_timeout_without_a_fresh_sweep_does_not_roll_back():
    """观测没跑过新的一轮 ⇒ "新码没出现"可能只是我们还没去看,不许回滚。"""
    v, _ = sm._verdict(_obs(False, False, fresh=False, hours=25), None, NOW)
    assert v == "pending"


def test_stalled_is_named_for_humans_and_never_auto_settled():
    v, why = sm._verdict(_obs(False, False, fresh=False, hours=100), None, NOW)
    assert v == "stalled" and "不自动定案" in why


def test_observe_and_stale_hours_are_overridable_per_run():
    row = _obs(False, False, fresh=True, hours=5)
    row["_observe_hours"], row["_stale_hours"] = 4, 72
    assert sm._verdict(row, None, NOW)[0] == "rolled_back"


# ══════════════════════════════════════════════════════════════════════════════
#  W3 · 定案的后果(五处写)
# ══════════════════════════════════════════════════════════════════════════════

#: `_SQL_OBSERVE` 的列(顺序与 SQL 逐字对齐)。`status` 是 2026-09-07 加的:
#: 观测面从 pending 扩成 **pending ∪ double**(§9.14),`_settle` 要靠它分辨
#: "这轮刚判成双挂"与"上轮已经是 double"(后者一条 UPDATE 都不该发)。
#: 两列 wpid 是 2026-09-08 加的(§9.15):判词 (a′)「影子双挂」的第一条证据就是
#: **两码 wpid 相同**(同一条 listing 原地换码),第二条是旧码单查 404。
_OBS_COLS = ["id", "old_sku", "new_sku", "source_type", "source_key", "feed_id",
             "submitted_at", "status", "new_wpid", "old_wpid",
             "new_present", "old_gone", "fresh"]


def _settle_wired(monkeypatch, obs_rows, receipts=None, calls=None):
    calls = calls if calls is not None else []
    read = _Conn([("FROM listing.sku_migrations m", (_OBS_COLS, obs_rows))])
    tx = _Conn(log=calls, tag="tx")
    monkeypatch.setattr(sm.db, "pg_conn", lambda *a, **k: tx)
    monkeypatch.setattr(sm.feed_track, "item_results",
                        lambda fid: dict(receipts or {}))
    monkeypatch.setattr(sm.feed_track, "feed_statuses",
                        lambda fids: dict(getattr(_settle_wired, "feed_st", {})))
    monkeypatch.setattr(sm.sku_codec, "settle_replacement",
                        lambda c, s, o, n, v, r="": calls.append(
                            ("settle", s, o, n, v)))
    monkeypatch.setattr(sm.upc_pool, "retag_sku",
                        lambda c, triples: calls.append(("retag", list(triples))))
    monkeypatch.setattr(sm.dispositions, "rekey_open",
                        lambda c, s, o, n, asin=None: (
                            calls.append(("rekey", o, n, asin)), (1, []))[1])
    monkeypatch.setattr(sm.walmart_catalog, "drop_node_rows",
                        lambda c, s, sku: (calls.append(("drop", sku)), 1)[1])
    return read, calls, tx


#: 旧码真的缺席了的那种 confirmed(old_wpid 自然是 NULL:LEFT JOIN 取不到行)。
_OBS_COLS_CONFIRM = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001",
                     "F1", NOW - timedelta(hours=2), "pending", "W1", None,
                     True, True, True)


def test_confirmed_retags_upc_rekeys_dispositions_and_drops_node_rows(monkeypatch):
    read, calls, _tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1
    kinds = [c[0] for c in calls if isinstance(c, tuple)]
    assert kinds[:4] == ["settle", "retag", "rekey", "drop"]
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "confirmed") in calls
    assert ("retag", [("T1", "B0OLD00001", "AAAAAAAAAAAA")]) in calls


def test_confirmed_never_touches_the_listing_sheet(monkeypatch):
    """2026-09-06 所有者定稿:改码**不回写上架表 SKU 列**。

    身份映射的两处出口已经够了 —— 登记簿 `catalog.listing_sources` 与在线产品
    总表的「来源码」列;上架表是所有者会定期清理的**工作表**,SKU 列由上架链
    填。加回来的表现是:一张会被清空的表成了第二处身份映射,而且改码链要为它
    的写失败长出一条补写路径(双轨,§六)。
    """
    read, calls, tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1
    assert "sheet" not in counts and "sheet_lag" not in counts
    assert not any("上架表" in ln for ln in lines), lines
    assert not any("sheet_synced_at" in sql for sql, _ in tx.sqls)


def test_failed_receipt_settles_as_rolled_back_and_never_resubmits(monkeypatch):
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           NOW - timedelta(hours=2), "pending", None, None, False, True, True)
    read, calls, _tx = _settle_wired(
        monkeypatch, [row], receipts={"AAAAAAAAAAAA": ("failed", "ERR_9")})
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("_settle 永不提交任何东西"))
    counts, lines = _settle_at(sm, read, True)
    assert counts["rolled_back"] == 1
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "rolled_back") in calls
    assert any("未自动补交" in ln for ln in lines)


#: 一条"新码在架、旧码也在架"的观测行(同店双挂)。`status` 是它当前的台账状态。
def _double_row(status="pending", rid=1, new_wpid="WNEW", old_wpid="WOLD"):
    """缺省是**真双挂**:两码 wpid 不同 ⇒ 影子探测不碰它(`_shadow_candidates`),
    判词照旧走 (b)。同 wpid 的那种传 `new_wpid=old_wpid=…`(§9.15 的影子)。"""
    return (rid, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
            NOW - timedelta(hours=2), status, new_wpid, old_wpid,
            True, False, True)


def test_double_listing_warns_and_settles_nothing(monkeypatch):
    """同店双挂:落**持久状态 double**,但**不定案**(2026-09-07 所有者定稿,§9.14)。

    所有者原话:「双挂的就让他继续挂着,等到我其他的处理完了,我再回头处理他,
    中途不重复提交这种双挂的就可以。」所以这一条钉三件:
      ① 台账写 double(新 SQL `_SQL_LEDGER_DOUBLE`,带 error 文案,不写 settled_at);
      ② **身份层一个字不动** —— 不 confirm、不 rollback,`settle_replacement` 一次
         都不调(旧行仍 replaced_by=新码的在途态,新码仍是活码);
      ③ 摘要那句要把"不拦后续、不会重复提交"说给人听(否则所有者看见 ⚠ 会以为
         又卡住了,而这次它恰恰不卡)。
    """
    read, calls, tx = _settle_wired(monkeypatch, [_double_row()])
    counts, lines = _settle_at(sm, read, True)
    assert counts["double"] == 1 and counts["confirmed"] == 0
    # ② 身份层没被碰过
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "settle"]
    # ① 过程账写了 double,而且**不写 settled_at**(double 不是终态)
    wrote = [(sql, args) for sql, args in tx.sqls if "status = 'double'" in sql]
    assert len(wrote) == 1, tx.sqls
    assert "settled_at" not in wrote[0][0]
    assert wrote[0][1]["id"] == 1 and wrote[0][1]["error"]
    assert "status <> 'double'" in wrote[0][0]          # 幂等半句在 SQL 里
    # ③ 人话
    ln = next(ln for ln in lines if "同店双挂" in ln)
    assert "已记 double" in ln
    assert "不拦后续提交" in ln and "不会重复提交" in ln


def test_a_row_that_is_already_double_is_not_written_again(monkeypatch):
    """上一轮已经记成 double 的行,这一轮仍判 double ⇒ **一条 UPDATE 都不发**。

    双挂要等所有者回头处置,期间每天每轮都会重判一次;不挡住就是每轮一条无谓的
    写(白写 WAL、把 error 刷成新时间的同一句话),而且看不出来。计数照旧要有 ——
    首行的「⚠ 同店双挂 N」是所有者的待办清单,不许因为"没写库"就漏报。
    """
    read, calls, tx = _settle_wired(monkeypatch, [_double_row(status="double")])
    counts, lines = _settle_at(sm, read, True)
    assert counts["double"] == 1                       # 计数照旧
    assert not tx.sqls and "open:tx" not in calls      # 连写事务都没开
    assert any("同店双挂" in ln for ln in lines)


def test_double_in_dry_run_writes_nothing(monkeypatch):
    """dry-run:只报「将记 double」,一行库都不写(与 _settle 其余四处写同纪律)。"""
    read, calls, tx = _settle_wired(monkeypatch, [_double_row()])
    counts, lines = _settle_at(sm, read, False)
    assert counts["double"] == 1
    assert not tx.sqls and "open:tx" not in calls
    assert any("[DRY-RUN] 将记 double" in ln for ln in lines)
    assert not any("已记 double" in ln for ln in lines)


def test_a_double_row_turns_into_confirmed_once_the_old_code_is_gone(monkeypatch):
    """所有者回头把旧码删掉之后:**不需要新逻辑**,下一轮自动走 confirmed。

    这条是 §9.14 整个设计的收口 —— double 行仍在 `_SQL_OBSERVE` 的面上(pending ∪
    double),`_verdict` 的六条规则一字未改,旧码一缺席就落到规则 (a)。漏了它的
    表现是:所有者手工清完了,而台账永远停在 double,旧码永不弃用、UPC 永不释放,
    **而且没有任何东西会报**。
    """
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           NOW - timedelta(hours=2), "double", "W1", None,
           True, True, True)   # 旧码已缺席
    read, calls, _tx = _settle_wired(monkeypatch, [row])
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1 and counts["double"] == 0
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "confirmed") in calls


# ══════════════════════════════════════════════════════════════════════════════
#  W3a · 影子双挂逐条单查(2026-09-08 所有者实证,docs/sku_plan.md §9.15)
#
#  探测是**读**(GET /v3/items/{sku},走 api/items → api/_client 的每店固定出口
#  代理与 items.get 桶),业务判断留在 `_verdict`(铁律 2:api 层不写业务判断)。
#  fail-closed:探不出来**不猜**,那些行照旧判 double —— 反过来(当 404)会拿一次
#  网络抖动去弃码、改 UPC、迁处置键,而且全程不报错。
# ══════════════════════════════════════════════════════════════════════════════

def _probe_wired(monkeypatch, rows, *, probe=None, boom=None, creds=("T1",)):
    """把 `_settle` 的读连接与探测两侧一起打桩 → (read, calls, tx, probe_log)。

    probe: {旧码: None(404) | dict(200)};boom: {旧码: 异常} —— 逐条抛。
    creds: None ⇒ load_stores 抛(凭证读不到);() ⇒ 返回空(店不可调用)。
    """
    read, calls, tx = _settle_wired(monkeypatch, rows)
    log = {"stores": 0, "gets": []}

    def _load(filter_names=None):
        log["stores"] += 1
        if creds is None:
            raise RuntimeError("飞书抖了")
        return [{"name": n, "client_id": "C1"} for n in creds]

    def _get(store, sku):
        log["gets"].append(sku)
        if boom and sku in boom:
            raise boom[sku]
        return (probe or {}).get(sku)

    monkeypatch.setattr(sm.stores_svc, "load_stores", _load)
    monkeypatch.setattr(sm.items_api, "get_item", _get)
    return read, calls, tx, log


def test_only_same_wpid_doubles_are_probed_and_credentials_load_once(monkeypatch):
    """**只对需要的行**单查:调用次数 = 同 wpid 的双挂行数,凭证只读一次。

    多探一条就是白烧 items.get 配额(A085 那条教训:454 SKU 单查 = 8 分钟);
    每行读一次凭证就是每行打一次飞书。真双挂(wpid 不同)与已缺席的行一条都不查。
    """
    rows = [_double_row(rid=1, new_wpid="W1", old_wpid="W1"),   # 影子:要查
            _double_row(rid=2, new_wpid="WNEW", old_wpid="WOLD"),  # 真双挂:不查
            _OBS_COLS_CONFIRM]                                   # 旧码已缺席:不查
    rows[1] = tuple(["B0OLD00002" if i == 1 else v
                     for i, v in enumerate(rows[1])])
    read, calls, tx, log = _probe_wired(monkeypatch, rows,
                                        probe={"B0OLD00001": None})
    counts, lines = _settle_at(sm, read, True)
    assert log["gets"] == ["B0OLD00001"]          # 只查了那一条
    assert log["stores"] == 1                     # 凭证只读一次
    assert counts["confirmed"] == 2 and counts["shadow"] == 1
    assert counts["double"] == 1                  # 真双挂原样留着
    assert any("影子改码定案" in ln for ln in lines)


def test_no_shadow_candidate_means_no_credentials_and_no_walmart_call(monkeypatch):
    """一条同 wpid 的双挂都没有 ⇒ **一次凭证都不读、一次沃尔玛都不调**。

    (纯定案的一轮本来就不该碰凭证 —— 定案不回写库存,所有者 2026-09-07 定稿。)
    """
    read, calls, tx, log = _probe_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    monkeypatch.setattr(sm.stores_svc, "load_stores",
                        lambda **k: pytest.fail("没有影子候选就不该读凭证"))
    monkeypatch.setattr(sm.items_api, "get_item",
                        lambda *a: pytest.fail("没有影子候选就不该调沃尔玛"))
    counts, _lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1 and counts["shadow"] == 0


def test_a_credential_failure_is_fail_closed_and_named(monkeypatch):
    """凭证读不到 ⇒ **不探测**,那些行照旧判 double,并**点名一次**。

    静默的表现是:所有者以为影子都自救了,而它们还停在 double —— 没有任何
    东西会报。判不准就判活(conventions §五)。
    """
    read, calls, tx, log = _probe_wired(
        monkeypatch, [_double_row(new_wpid="W1", old_wpid="W1")], creds=None)
    counts, lines = _settle_at(sm, read, True)
    assert counts["double"] == 1 and counts["shadow"] == 0
    assert log["gets"] == []
    ln = next(ln for ln in lines if "影子探测失败" in ln)
    assert "RuntimeError" in ln and "1 条按 double 处理" in ln


def test_a_store_that_cannot_be_called_is_fail_closed_and_named(monkeypatch):
    """店不在可调用列表里(没配代理/没凭证)⇒ 同样不探测、点名。**严禁直连**。"""
    read, calls, tx, log = _probe_wired(
        monkeypatch, [_double_row(new_wpid="W1", old_wpid="W1")], creds=())
    counts, lines = _settle_at(sm, read, True)
    assert counts["double"] == 1 and log["gets"] == []
    assert any("影子探测失败" in ln and "不在可调用列表" in ln for ln in lines)


def test_a_get_that_blows_up_is_fail_closed_and_named(monkeypatch):
    """单查抛异常(429/超时/代理波动)⇒ 那一条不挂探测结果 ⇒ 判 double,点名一次。

    另一条查得到的**照常定案** —— 逐条隔离:一条抖动不该把整批影子拖回 double。
    """
    rows = [_double_row(rid=1, new_wpid="W1", old_wpid="W1"),
            _double_row(rid=2, new_wpid="W2", old_wpid="W2")]
    rows[1] = tuple(["B0OLD00002" if i == 1 else
                     ("BBBBBBBBBBBB" if i == 2 else v)
                     for i, v in enumerate(rows[1])])
    read, calls, tx, log = _probe_wired(
        monkeypatch, rows, probe={"B0OLD00001": None},
        boom={"B0OLD00002": TimeoutError("代理波动")})
    counts, lines = _settle_at(sm, read, True)
    assert log["gets"] == ["B0OLD00001", "B0OLD00002"]
    assert counts["shadow"] == 1 and counts["confirmed"] == 1
    assert counts["double"] == 1
    ln = next(ln for ln in lines if "影子探测失败" in ln)
    assert "TimeoutError×1" in ln and "fail-closed" in ln


def test_dry_run_probes_but_writes_nothing(monkeypatch):
    """dry-run **照样探测**(只读),但一行库都不写 —— 空跑正是人眼确认
    "这批到底是影子还是真双挂"的那一步。"""
    read, calls, tx, log = _probe_wired(
        monkeypatch, [_double_row(new_wpid="W1", old_wpid="W1")],
        probe={"B0OLD00001": None})
    counts, lines = _settle_at(sm, read, False)
    assert log["gets"] == ["B0OLD00001"]              # 探了
    assert counts["confirmed"] == 1 and counts["shadow"] == 1
    assert not tx.sqls and "open:tx" not in calls     # 一行库都没写
    assert not [c for c in calls if isinstance(c, tuple)]
    ln = next(ln for ln in lines if "[DRY-RUN] 将定案" in ln)
    assert "confirmed" in ln and "同 wpid" in ln and "404" in ln


def test_a_row_already_double_turns_confirmed_through_the_shadow_probe(monkeypatch):
    """**已经是 double 的行**(A131 那 41 条)经 (a′) 转 confirmed,走现成 `_confirm`。

    `_SQL_OBSERVE` 的面本来就是 pending ∪ double(§9.14),所以这批不需要任何
    额外的取数改动;定案后果与"旧码真缺席"那条**逐字相同**,不新增写动作。
    """
    read, calls, tx, log = _probe_wired(
        monkeypatch, [_double_row(status="double", new_wpid="W1", old_wpid="W1")],
        probe={"B0OLD00001": None})
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1 and counts["double"] == 0
    kinds = [c[0] for c in calls if isinstance(c, tuple)]
    assert kinds[:4] == ["settle", "retag", "rekey", "drop"]   # 现成的五处后果
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "confirmed") in calls
    assert not any("status = 'double'" in sql for sql, _ in tx.sqls)  # 不再写 double


def test_the_first_line_counts_shadows_apart_from_real_doubles(monkeypatch):
    """首行「影子改码 N」与「⚠ 同店双挂 N」**分开报**:前者已解决,后者是待办。"""
    rows = [_double_row(rid=1, new_wpid="W1", old_wpid="W1"),
            _double_row(rid=2, new_wpid="WNEW", old_wpid="WOLD")]
    rows[1] = tuple(["B0OLD00002" if i == 1 else v
                     for i, v in enumerate(rows[1])])
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations m", (_OBS_COLS, rows)),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
    ])
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    monkeypatch.setattr(sm.stores_svc, "load_stores",
                        lambda filter_names=None: [{"name": "T1"}])
    monkeypatch.setattr(sm.items_api, "get_item", lambda store, sku: None)
    monkeypatch.setattr(sm.sku_codec, "settle_replacement", lambda *a, **k: None)
    monkeypatch.setattr(sm.upc_pool, "retag_sku", lambda *a, **k: None)
    monkeypatch.setattr(sm.dispositions, "rekey_open", lambda *a, **k: (1, []))
    monkeypatch.setattr(sm.dispositions, "executing_actions_on",
                        lambda *a, **k: [])
    monkeypatch.setattr(sm.walmart_catalog, "drop_node_rows", lambda *a, **k: 1)
    first = sm.run({"store": "T1", "execute": True,
                    "settle_only": "1"}).splitlines()[0]
    assert "影子改码 1" in first
    assert "⚠ 同店双挂 1" in first


def test_the_observe_face_covers_double_rows_too():
    """`_SQL_OBSERVE` 取 **pending ∪ double**,并把 status 选出来供幂等判断。"""
    assert "m.status IN ('pending', 'double')" in sm._SQL_OBSERVE
    assert "m.status = 'pending'" not in sm._SQL_OBSERVE
    assert "m.status," in sm._SQL_OBSERVE
    assert sm._DOUBLE == "double"


def test_double_rows_are_not_counted_by_the_stage_gate():
    """守门:节奏闸的 `open` **不含 double**(2026-09-07 所有者决定,§9.14)。

    双挂的旧码在沃尔玛后台删不掉(僵尸列表,backlog §十三),数进 open 就是拿一条
    删不掉的行把整店改码永久停摆(A131吕灿荣:2 条压着 500 条一条都发不出去)。
    加回来不会报错 —— 表现是每轮摘要都说"该店还有 N 条改码未定案",而所有者以为
    是自己没清账。**stalled 仍在里面**(所有者没说放它)。
    """
    q = sm._SQL_STAGE
    filt = q[q.index("FILTER"):q.index("AS open")]
    assert "'pending'" in filt and "'stalled'" in filt
    assert "'double'" not in filt, filt
    assert "§9.14" in q                       # 是所有者的决定,写在 SQL 注释里


def test_settle_in_dry_run_writes_nothing(monkeypatch):
    """_settle 是全包写得最重的一段,dry-run 下五处写**全部**跳过。"""
    read, calls, _tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    counts, lines = _settle_at(sm, read, False)
    assert counts["confirmed"] == 1                      # 判决照算
    assert not [c for c in calls if isinstance(c, tuple)]  # 写一次都没有
    assert "open:tx" not in calls                        # 连写事务都没开
    assert any("将定案" in ln for ln in lines)


_STORE_DICT = {"name": "T1", "client_id": "C1", "client_secret": "S1",
               "proxy": None}


def _settle_at(mod, read_conn, execute):
    """按当轮语义调 _settle(读连接与写事务分开,见 _settle 头注)。

    **不传店铺凭证**:定案不回写库存(所有者 2026-09-07 定稿),这一段不调
    任何沃尔玛写接口。
    """
    return mod._settle(read_conn, "T1", execute)


# ══════════════════════════════════════════════════════════════════════════════
#  W3b · 守门:定案**不回写库存**(所有者 2026-09-07 定稿)
#
#  所有者原话:「按我们现在修改 sku 的流程,已经不需要再提交一次改库存了。」
#  MP_ITEM_MATCH 升 v5 之后改码 feed **自带 inventory**(qty = 旧码最后观测的
#  avail_qty,FC 走 listing_fc),定案时再 PUT /v3/inventory 写一遍就是**同一份
#  数据的第二条写路径**(§六 双轨):A131 第一批定案时 `_restore_inventory` 真逐条
#  写了一遍 —— 新码行在库里的 avail_qty 还没被下一轮库存拉取更新,所以它判"不
#  相等",既烧库存接口配额,又可能把提交之后**已经卖掉**的数量原样写回去。
#  加回来的表现不会报错:库存悄悄多了第二条写路径,而摘要读起来像"补救成功"。
#  ⚠ 载荷带库存那一路是**主路**,用例在下面的 W3c 段(`_item_of` / `_migrate`)。
# ══════════════════════════════════════════════════════════════════════════════

def test_settling_never_calls_the_inventory_api():
    """源码守门:**可执行行**里不许再出现 `put_inventory` / `_restore_inventory` /
    `inventory_restored`,也不许再把 `api.inventory` import 进来。

    只看可执行行(`#` 注释与模块 docstring 剔除):头注与注释里要能把"为什么删了、
    别加回来"讲清楚,那是文档不是第二条写路径。
    """
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    body = src.replace(ast.get_docstring(ast.parse(src)) or "", "", 1)
    hits = [ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
            and any(t in ln for t in ("put_inventory", "_restore_inventory",
                                      "inventory_restored", "_SQL_INV_QTY",
                                      "_SQL_LEDGER_INV"))]
    assert not hits, f"改码定案又回写库存了(所有者 2026-09-07 定稿:不回写):{hits}"
    assert not hasattr(sm, "_restore_inventory")
    assert not hasattr(sm, "_SQL_INV_QTY")
    assert not hasattr(sm, "_SQL_LEDGER_INV")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "api":
            imported |= {a.name for a in node.names}
    assert "inventory" not in imported, f"api.inventory 不再是改码链的依赖:{imported}"


def test_the_first_line_no_longer_carries_an_inventory_restore(monkeypatch):
    """cli 的链通知只取首行:定案段里**不再有**「库存回写 N」那一格,也不再有
    「⚠ 库存回写失败」;整轮定案一次受管仓表、一次沃尔玛写接口都不碰。"""
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations m", (_OBS_COLS, [_OBS_COLS_CONFIRM])),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [])),
    ])
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    monkeypatch.setattr(sm.sku_codec, "settle_replacement",
                        lambda *a, **k: None)
    monkeypatch.setattr(sm.upc_pool, "retag_sku", lambda *a, **k: None)
    monkeypatch.setattr(sm.dispositions, "rekey_open",
                        lambda *a, **k: (1, []))
    monkeypatch.setattr(sm.walmart_catalog, "drop_node_rows", lambda *a, **k: 1)
    monkeypatch.setattr(sm.dispositions, "executing_actions_on",
                        lambda *a, **k: [])
    monkeypatch.setattr(sm.store_limits, "maint_nodes",
                        lambda: pytest.fail("定案不回写库存,不该读受管仓表"))
    out = sm.run({"store": "T1", "execute": True, "settle_only": "1"})
    first = out.splitlines()[0]
    assert "定案 confirmed 1" in first
    assert "库存回写" not in out                # 首行与明细里都不再有这句
    assert "定案:confirmed 1" in out


# ══════════════════════════════════════════════════════════════════════════════
#  W4 · 先落库后提交 / 回执落账
# ══════════════════════════════════════════════════════════════════════════════

def _migrate_wired(monkeypatch, outcome="submitted", feed_id="F9"):
    calls: list = []
    conns: list = []

    def _pg(*a, **k):
        c = _Conn(log=calls, tag=f"tx{len(conns)}")
        c.answers = [("RETURNING id", (["id"], [(100 + len(conns),)]))]
        conns.append(c)
        return c

    monkeypatch.setattr(sm.db, "pg_conn", _pg)
    monkeypatch.setattr(sm.sku_codec, "mint_replacement",
                        lambda c, s, old, st, key, workflow="": (
                            calls.append(("mint", old)), "AAAAAAAAAAA" + old[-1])[1])
    monkeypatch.setattr(sm.sku_codec, "settle_replacement",
                        lambda c, s, o, n, v, r="": calls.append(("settle", o, v)))

    def _submit(store, ft, items, workflow=""):
        calls.append(("submit", ft, workflow, len(items), items))
        return [{"outcome": outcome, "feed_id": feed_id, "count": len(items)}]

    monkeypatch.setattr(sm.feeds, "submit_feed", _submit)
    return calls, conns


def _rows_for(n=2, slow=_SLOW):
    """候选行(`_candidates` 的输出形状)。重量**不是**一列现成的数:
    载荷、预览、台账三处都从 `product_slow` 现解析(`_weight_of`)。"""
    return [{"store": "T1", "old_sku": f"B0AAA0000{i}", "source_type": "amz",
             "source_key": f"B0AAA0000{i}", "product_id": f"00{i}",
             "product_id_type": "GTIN", "price": 29.99, "product_slow": slow}
            for i in range(1, n + 1)]


def test_registry_rows_are_committed_before_submit_feed_is_called(monkeypatch):
    """**安全铁律**:mint + pending 台账的事务必须在 POST 之前提交。

    照"一个大事务包到底"写的话,进程死在 POST 之后、with 退出之前,新码行与
    pending 台账全部 rollback,而沃尔玛已经受理 —— 新码成了一条没有出身的
    孤儿行,而且不报错。
    """
    calls, _ = _migrate_wired(monkeypatch)
    sm._migrate({"name": "T1"}, _rows_for(2), True)
    order = [c if isinstance(c, str) else c[0] for c in calls]
    assert order.index("commit:tx0") < order.index("submit")
    assert order.count("mint") == 2


def test_payload_uses_the_new_code_and_the_observed_product_id(monkeypatch):
    calls, _ = _migrate_wired(monkeypatch)
    rows = _rows_for(1)
    sm._migrate({"name": "T1"}, rows, True)
    submit = [c for c in calls if isinstance(c, tuple) and c[0] == "submit"][0]
    assert submit[1] == "MP_ITEM_MATCH" and submit[2] == "sku_migrate"
    item = submit[4][0]
    assert item["sku"] == rows[0]["new_sku"] != rows[0]["old_sku"]
    assert item["productIdentifiers"] == {"productId": "001",
                                          "productIdType": "GTIN"}
    # 现挂价与发货重量**原样发回去**(MP_ITEM_MATCH 是 REPLACE:载荷给什么线上就
    # 变成什么;发默认值 = 一次改码顺手改了售价/运费,而且回执全绿)
    assert item["price"] == 29.99 and item["ShippingWeight"] == 0.82
    assert item["condition"] == "New"
    assert "SkuUpdate" not in item          # 通道不需要这个开关字段(形态 A/B 已废)
    # 这几行没观测到库存(`_rows_for` 缺省不给 avail_qty)⇒ 载荷不带 inventory,
    # 而且**一次飞书都不读**(见下面那条守门)
    assert "inventory" not in item


# ── 库存随改码 feed 一起发(2026-09-07 通道升 v5;安全约束⑦)────────────────
#  v4.2 时代载荷里没有库存字段,REPLACE 把「没带」当 0 写 —— 第一级投放把旧码
#  最后观测的 30 件写成了 0(§9.12)。v5 的 Item 有可选 inventory[],于是库存
#  跟着载荷走,**这是本工作流写库存的唯一一条路**(定案不再回写,所有者
#  2026-09-07 定稿)。下面四条钉的是"错了不报错"的那几件:
#  qty 从哪一列来、FC 从哪个入口来、判不出时**不猜**、以及不该读飞书时不读。

def _nodes(monkeypatch, ok=None, skipped=None):
    """把受管仓入口打桩,并记录 `managed_nodes` 被调了几次(一轮只许一次)。"""
    seen: list = []

    def _mn(stores=None):
        seen.append(stores)
        return dict(ok or {}), dict(skipped or {})

    monkeypatch.setattr(sm.store_limits, "managed_nodes", _mn)
    return seen


def test_payload_carries_the_last_observed_inventory_with_the_listing_fc(monkeypatch):
    """qty = 旧码行最后观测的 `avail_qty`,FC = 上架链同一个入口 `listing_fc`。

    这两件错了都不报错:qty 拿错列 ⇒ 改码顺手改了库存;FC 自己拼一个 ⇒ 货
    写到别的节点(正是 `store_limits.resolve_node` 拼命避免的那件事)。
    """
    calls, _ = _migrate_wired(monkeypatch)
    seen = _nodes(monkeypatch, ok={"T1": "N1"})
    rows = _rows_for(2)
    for r, q in zip(rows, (30, 0)):
        r["avail_qty"] = q
    _counts, lines = sm._migrate({"name": "T1"}, rows, True)
    items = [c for c in calls if isinstance(c, tuple) and c[0] == "submit"][0][4]
    assert items[0]["inventory"] == [{"quantity": 30, "fulfillmentCenterID": "N1"}]
    assert items[1]["inventory"] == [{"quantity": 0, "fulfillmentCenterID": "N1"}]
    assert len(seen) == 1                      # 飞书受管仓表**一轮只读一次**
    assert any("载荷带库存 2/2" in ln for ln in lines)


def test_a_row_that_was_never_observed_carries_no_inventory(monkeypatch):
    """`avail_qty` 为 NULL ⇒ **不带** inventory(不猜 0:REPLACE 会照写 0)。"""
    calls, _ = _migrate_wired(monkeypatch)
    _nodes(monkeypatch, ok={"T1": "N1"})
    rows = _rows_for(2)
    rows[0]["avail_qty"] = 7                   # 一行有观测,另一行没有
    _counts, lines = sm._migrate({"name": "T1"}, rows, True)
    items = [c for c in calls if isinstance(c, tuple) and c[0] == "submit"][0][4]
    assert items[0]["inventory"] == [{"quantity": 7, "fulfillmentCenterID": "N1"}]
    assert "inventory" not in items[1]
    assert any("载荷带库存 1/2" in ln for ln in lines)


def test_a_store_whose_managed_node_fails_validation_sends_no_inventory(monkeypatch):
    """受管仓校验失败 ⇒ **不带库存、不回落 Partner ID**,摘要点名(fail-closed)。

    回落 Partner ID 才是最坏的那条:货被写到旧节点,而且全程不报错。
    """
    calls, _ = _migrate_wired(monkeypatch)
    _nodes(monkeypatch, skipped={"T1": "「维护仓库」填的 N9 不在该店发货节点列表里"})
    monkeypatch.setattr(sm.store_limits, "listing_fc",
                        lambda *a, **k: pytest.fail("校验失败的店不许回落 Partner ID"))
    rows = _rows_for(1)
    rows[0]["avail_qty"] = 30
    _counts, lines = sm._migrate({"name": "T1"}, rows, True)
    items = [c for c in calls if isinstance(c, tuple) and c[0] == "submit"][0][4]
    assert "inventory" not in items[0]         # 改码照发,只是不带库存
    assert any("受管仓校验失败" in ln and "T1" in ln for ln in lines)
    assert any("载荷带库存 0/1" in ln for ln in lines)


def test_no_observed_qty_means_the_feishu_table_is_not_even_read(monkeypatch):
    """本轮没有一行观测到库存 ⇒ **一次飞书、一次沃尔玛都不调**(零成本零行为变化)。"""
    _migrate_wired(monkeypatch)
    monkeypatch.setattr(sm.store_limits, "managed_nodes",
                        lambda stores=None: pytest.fail("没有库存可带就不该读受管仓表"))
    counts, _lines = sm._migrate({"name": "T1"}, _rows_for(2), True)
    assert counts["submitted"] == 2


def test_the_preview_names_the_inventory_of_every_line():
    """dry-run 逐行标「库存 N → FC xxx」/「不带库存(未观测)」。

    预览与真发共用 `_item_of` 的同一条判据,所以纸面上的那句就是发出去的那件事。
    """
    rows = _rows_for(2)
    rows[0]["avail_qty"] = 30
    lines = sm._preview(rows, "N1")
    body = "\n".join(lines)
    assert "库存 30 → FC N1" in body
    assert "不带库存(未观测)" in body
    assert "库存:1/2 条随载荷带库存" in body


def test_submitted_slices_land_the_feed_id_and_stay_pending(monkeypatch):
    calls, conns = _migrate_wired(monkeypatch, outcome="submitted")
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(2), True)
    assert counts == {"submitted": 2, "unknown": 0, "rolled_back": 0}
    sqls = [sql for c in conns for sql, _ in c.sqls]
    assert any("feed_id = %(feed_id)s" in s and "submitted_at = now()" in s
               for s in sqls)
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "settle"]


def test_failed_post_rolls_back_immediately(monkeypatch):
    """4xx 与 token/代理阶段失败都由 api/feeds 判成 failed = **确认未达**,
    回滚安全:旧码复活、新码弃掉,下一轮重来会抽新码。"""
    calls, _ = _migrate_wired(monkeypatch, outcome="failed", feed_id=None)
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(2), True)
    assert counts["rolled_back"] == 2 and counts["submitted"] == 0
    assert [c for c in calls if isinstance(c, tuple) and c[0] == "settle"] == [
        ("settle", "B0AAA00001", "rolled_back"),
        ("settle", "B0AAA00002", "rolled_back")]
    assert any("不自动补交" in ln for ln in lines)


def test_unknown_outcome_stays_pending_and_is_not_rolled_back(monkeypatch):
    """决策 F:unknown = 不知道到没到。回滚会造出没有出身的孤儿码,而且不报错。"""
    calls, _ = _migrate_wired(monkeypatch, outcome="unknown", feed_id=None)
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(1), True)
    assert counts == {"submitted": 0, "unknown": 1, "rolled_back": 0}
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "settle"]
    assert any("保持 pending 不回滚" in ln for ln in lines)


def test_slice_results_line_up_with_their_own_rows(monkeypatch):
    """逐片对位错一位 = 整批结局落到别人行上,而且不报错(iter_result_slices)。"""
    calls: list = []
    conns: list = []

    def _pg(*a, **k):
        c = _Conn(log=calls, tag=f"tx{len(conns)}")
        c.answers = [("RETURNING id", (["id"], [(100 + len(conns),)]))]
        conns.append(c)
        return c

    monkeypatch.setattr(sm.db, "pg_conn", _pg)
    monkeypatch.setattr(sm.sku_codec, "mint_replacement",
                        lambda c, s, old, st, key, workflow="": "A" + old[-11:])
    monkeypatch.setattr(sm.sku_codec, "settle_replacement",
                        lambda c, s, o, n, v, r="": calls.append(("settle", o, v)))
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "F1", "count": 1},
                         {"outcome": "failed", "feed_id": None, "count": 2}])
    counts, _ = sm._migrate({"name": "T1"}, _rows_for(3), True)
    assert counts["submitted"] == 1 and counts["rolled_back"] == 2
    rolled = [c[1] for c in calls if isinstance(c, tuple) and c[0] == "settle"]
    assert rolled == ["B0AAA00002", "B0AAA00003"]      # 第一条才是 submitted


def test_the_whole_batch_goes_out_in_one_submit_call_and_lands_per_slice(monkeypatch):
    """候选 2300 条 = **一次** `submit_feed` 调用,切片交给 api 层,台账逐片对位。

    2026-09-07 所有者纠正:本层不再自设「每轮 2 个 feed × 500 条」——「一个 feed 提
    1000 个,直到全部提交完」。切片是 api 层的事(`_SLICE_LIMITS["MP_ITEM_MATCH"]`
    = 1000 条 / 24MB),这里用**真** `_slices` 切,钉三件:
      · submit_feed 只被调一次(不是自己分批调 N 次);
      · 回来 3 片 ⇒ 3 个**各不相同**的 feed_id;
      · `iter_result_slices` 的对位没错位 —— 第 k 片的台账 id 正是 rows 里第 k 段
        那几行(错一位 = 整片结局落到别人行上,而且不报错)。
    """
    from api import feeds as feeds_api

    ids = iter(range(1000, 1000 + 2300))
    conns: list = []

    class _IdConn(_Conn):
        def answer(self, sql):
            return (["id"], [(next(ids),)]) if "RETURNING id" in sql else ([], [])

    def _pg(*a, **k):
        c = _IdConn(tag=f"tx{len(conns)}")
        conns.append(c)
        return c

    monkeypatch.setattr(sm.db, "pg_conn", _pg)
    monkeypatch.setattr(sm.sku_codec, "mint_replacement",
                        lambda c, s, old, st, key, workflow="": "A" + old[2:])
    submits: list = []

    def _submit(store, ft, items, workflow=""):
        submits.append((ft, workflow, len(items)))
        return [{"outcome": "submitted", "feed_id": f"F{i}", "count": len(sl)}
                for i, sl in enumerate(feeds_api._slices(ft, items), start=1)]

    monkeypatch.setattr(sm.feeds, "submit_feed", _submit)

    counts, _lines = sm._migrate({"name": "T1"}, _rows_for(2300), True)

    assert submits == [("MP_ITEM_MATCH", "sku_migrate", 2300)]   # 一次,整批
    assert counts == {"submitted": 2300, "unknown": 0, "rolled_back": 0}
    landed = [(args["feed_id"], args["ids"])
              for c in conns for sql, args in c.sqls
              if "feed_id = %(feed_id)s" in sql]
    assert [f for f, _ in landed] == ["F1", "F2", "F3"]          # feed_id 各不相同
    assert [len(i) for _, i in landed] == [1000, 1000, 300]
    assert [i[0] for _, i in landed] == [1000, 2000, 3000]       # 逐片对位不错行


def test_dry_run_mints_nothing_and_posts_nothing(monkeypatch):
    calls, _ = _migrate_wired(monkeypatch)
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(2), False)
    assert counts == {"submitted": 0, "unknown": 0, "rolled_back": 0}
    assert not calls                                    # 连事务都没开
    assert any("将改码 2 个" in ln for ln in lines)
    assert any(sm.sku_codec.DRYRUN_PLACEHOLDER in ln for ln in lines)


# ══════════════════════════════════════════════════════════════════════════════
#  W6 · run()
# ══════════════════════════════════════════════════════════════════════════════

def test_store_param_is_required_and_refuses_with_the_refused_mark():
    """cli 认 ⛔ 前缀记 refused(不是 success),而且不抛异常。"""
    out = sm.run({"execute": True})
    assert out.startswith("⛔") and "store" in out


def test_zero_candidates_is_success_not_failure(monkeypatch):
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [])),
    ])
    out = sm.run({"store": "T1", "execute": True})
    assert not out.startswith("⛔")
    assert "本轮提交 0" in out


def test_first_line_carries_the_four_warnings(monkeypatch):
    """cli 的链通知只取首行(notify_fmt.first_line_of):告警落在下面 = 只进日志。"""
    _wire(monkeypatch, dupes=[{"store": "T1", "po_id": "PO1", "line_number": 1,
                               "n": 2, "skus": []}])
    read = _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations m", (_OBS_COLS,
            [(1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
              NOW - timedelta(hours=200), "pending", "WNEW", "WOLD",
              True, False, True),
             (2, "B0OLD00002", "BBBBBBBBBBBB", "amz", "B0OLD00002", "F1",
              datetime.now(timezone.utc) - timedelta(hours=200), "pending",
              None, None, False, False, False)])),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(0, 2)])),
    ])
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    first = sm.run({"store": "T1", "execute": True}).splitlines()[0]
    assert "同店双挂 1" in first
    assert "超期未定案 1" in first
    assert "订单双行 1 组" in first
    # 「上架表 SKU 列未同步 N 行」这条告警**已删**(2026-09-06 定稿:改码不回写
    # 上架表)—— 首行不许再冒出它,冒出来就说明回写路径被谁加回来了
    assert "上架表" not in first


def test_dry_run_prefix_stays_at_the_head_of_the_first_line(monkeypatch):
    _wire(monkeypatch, dupes=[{"store": "T1", "po_id": "PO1", "line_number": 1,
                               "n": 2, "skus": []}])
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(0, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [_cand("B0AAA00001")])),
        ("FROM ops.feed_items", (["sku"], [])),
    ])
    out = sm.run({"store": "T1", "execute": False, "dry_run": True})
    first = out.splitlines()[0]
    assert first.startswith("🧪 [DRY-RUN] sku_migrate")
    assert "订单双行" in first                     # 告警仍在首行,只是排在前缀之后


def test_the_summary_reports_the_headroom_in_dry_run_too(monkeypatch):
    """上架上限闸在**空跑**里也要报:dry-run 是人眼确认那一步,
    "这家店只剩 2 个位置"必须在真跑之前就看得见(安全约束⑧)。"""
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("count(*) FROM catalog.walmart_items", (["n"], [(4998,)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [_cand("B0AAA00001")])),
        ("FROM ops.feed_items", (["sku"], [])),
    ])
    out = sm.run({"store": "T1", "execute": False})
    assert "上架上限闸:上限 5000(缺省,该店未填「商品上限」),在架 4998," \
           "本轮最多 2" in out


def test_a_full_store_submits_nothing_and_says_why(monkeypatch):
    """余量 ≤ 0:候选一条都不查(上限 0),摘要点名先清理死档。"""
    _wire(monkeypatch)
    read = _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("count(*) FROM catalog.walmart_items", (["n"], [(5000,)])),
    ])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("店满了不许提交"))
    out = sm.run({"store": "T1", "execute": True})
    assert "店内 item 数 5000 已达上架上限 5000" in out and "本轮提交 0" in out
    assert not any("FROM catalog.walmart_items w" in sql for sql, _ in read.sqls)


def test_a_blocked_preflight_still_settles_but_never_submits(monkeypatch):
    """闸拦的是"发新的",**不是"定案"**。

    反过来写会死锁:一条不相干的 executing 处置就能让整店的 pending 永远定不了案,
    而那些旧码正被缺席抑制着 —— 没有任何东西会报。
    """
    # 闸③改成只报数之后,这里换用仍然会拦的闸④(自愈链在途退役)
    _wire(monkeypatch)
    read = _read_conn(monkeypatch, [], cooldown=1)
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("前置闸未过不许提交"))
    out = sm.run({"store": "T1", "execute": True})
    assert "前置闸未过" in out and "闸④自愈链" in out
    assert "本轮提交 0" in out
    # 定案面照查(账要清),候选面一条都不查(上限被压到 0)
    assert any("FROM listing.sku_migrations m" in sql for sql, _ in read.sqls)
    assert not any("FROM catalog.walmart_items w" in sql for sql, _ in read.sqls)


def test_settle_only_settles_and_never_submits(monkeypatch):
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations m", (_OBS_COLS, [])),
    ])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("settle_only 不许提交"))
    # 只定案的那一轮**不问上限**:上架上限闸要读飞书限额表,一次飞书抖动
    # 不该把"清账"炸掉(与"闸拦的是发新的、不是定案"同一条)
    monkeypatch.setattr(sm.store_limits, "setup_limits",
                        lambda: pytest.fail("settle_only 不该读限额表"))
    out = sm.run({"store": "T1", "execute": True, "settle_only": "1"})
    assert "settle_only" in out and "本轮提交 0" in out


def test_a_bad_limit_is_refused_not_guessed():
    out = sm.run({"store": "T1", "limit": "十个", "execute": True})
    assert out.startswith("⛔") and "limit" in out


def test_naming_five_still_bows_to_the_stage_cap(monkeypatch):
    """点名**不越闸**:零 confirmed 的店点名 5 个,本轮照样只放 1 个,
    其余"没轮到"(不是落选)—— 而且这句话必须出现在**首行**(cli 只取首行)。"""
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("AS c0", (_WHY_COLS, [_why(f"B0AAA0000{i}") for i in range(1, 6)])),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(0, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [_cand("B0AAA00001")])),
        ("FROM ops.feed_items", (["sku"], [])),
    ])
    out = sm.run({"store": "T1", "execute": False,
                  "skus": "B0AAA00001, B0AAA00002\nB0AAA00003 B0AAA00004,"
                          "B0AAA00005"})
    first = out.splitlines()[0]
    assert "点名 5 个,命中 1 个" in first
    assert "节奏闸本轮只放 1 个,其余下轮" in first
    assert "本轮上限 1" in out                       # 节奏闸自己那行还在
    assert "没轮到它" in out                         # 其余四个逐条有交代


def test_naming_nothing_that_matches_says_so_instead_of_looking_empty(monkeypatch):
    """点名 2 个、命中 0 个 ⇒ 摘要**明说**,不许看起来像"这家店没候选"。"""
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("AS c0", (_WHY_COLS, [_why("B0AAA00001", bad=("未在改",))])),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [])),
    ])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("零命中不许提交"))
    out = sm.run({"store": "T1", "execute": True,
                  "skus": "B0AAA00001,B0TYPO0001"})
    assert "点名 2 个,命中 0 个" in out.splitlines()[0]
    assert "B0AAA00001" in out and "已经指向新码" in out
    assert "B0TYPO0001" in out and "查无此 SKU" in out


def test_naming_never_beats_a_blocked_gate_and_says_it_never_looked(monkeypatch):
    """闸未过(或 settle_only)⇒ 上限 0 ⇒ **一条候选 SQL 都不发**。
    这时也必须说清"点了 N 个、一个都没查",否则看起来像点名没生效。"""
    _wire(monkeypatch)
    read = _read_conn(monkeypatch, [], cooldown=1)
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("前置闸未过不许提交"))
    out = sm.run({"store": "T1", "execute": True, "skus": "B0AAA00001,B0AAA00002"})
    first = out.splitlines()[0]
    assert "点名 2 个,命中 0 个" in first and "本轮上限 0,一个都没发" in first
    assert "点名不越闸" in out
    assert not any("FROM catalog.walmart_items w" in sql for sql, _ in read.sqls)


def test_exclude_reaches_the_sql_from_run(monkeypatch):
    """`-p exclude_skus=` / `-p exclude_asins=` 一路传到候选 SQL 的参数上
    (解析在 run,过滤在 SQL —— 不在 Python 里事后剔,那是第二条选取路径)。"""
    _wire(monkeypatch)
    read = _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
        ("FROM catalog.walmart_items w", (_CAND_COLS, [_cand("B0AAA00002",
                                                             "0002")])),
        ("FROM ops.feed_items", (["sku"], [])),
    ])
    out = sm.run({"store": "T1", "execute": False,
                  "exclude_skus": "B0AAA00001, B0AAA00001",
                  "exclude_asins": "B0ASIN0009"})
    args = [a for sql, a in read.sqls if "LIMIT %(limit)s" in sql][0]
    assert args["excl_skus"] == ["B0AAA00001"]          # 去重
    assert args["excl_keys"] == ["B0ASIN0009"]
    assert args["unnamed"] is True                      # 只排除、没点名
    assert "排除 -p exclude_skus 1 个" in out
    assert "点名" not in out.splitlines()[0]


# ══════════════════════════════════════════════════════════════════════════════
#  沙箱 PG 集成:全部 SQL 与两条状态迁移在**真库**上跑一遍
#
#  ⚠ 地址是**测试夹具**,不是生产资源(生产走 registry/db.pg_dsn())。固定在非
#  标准端口 55432 上正是为了不可能连到生产库;造的数据全在一个最后回滚的事务里。
#  假连接测得了调用序与分支,测不出「这条 SQL 语法对不对、列名有没有写错」——
#  而本工作流的 SQL 全是新写的,拼错一个列名在单测里一路绿灯。
# ══════════════════════════════════════════════════════════════════════════════

import contextlib   # noqa: E402  —— 集成段自带的依赖,与上面的单测段分开
import socket       # noqa: E402

_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data"


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")

_STORE = "PGMIG_T1"
_OLD, _ASIN = "B0PGMIG001", "B0PGMIG001"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务最后一律回滚)。

    `db.pg_conn` 一并改道到**同一条**连接且不真提交:工作流内部的"短事务"在
    这里退化成同一事务里的一段 —— 测的是 SQL 与状态迁移,不是 psycopg 的提交。
    调用序/提交时点那条纪律由上面的假连接用例钉(test_registry_rows_are_
    committed_before_submit_feed_is_called)。
    """
    import os
    monkeypatch.setenv("WALMART_PG_DSN", os.environ.get("WALMART_TEST_PG_DSN", _DSN))
    from registry import db as real_db
    with real_db.pg_conn() as conn:
        @contextlib.contextmanager
        def _same(*a, **k):
            yield conn
        monkeypatch.setattr(sm.db, "pg_conn", _same)
        try:
            yield conn
        finally:
            conn.rollback()


def _seed(conn, sku, source_type="amz", source_key=None, upc="000000000001",
          missing=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.listing_sources "
            "(store, sku, source_type, source_key, workflow) "
            "VALUES (%s, %s, %s, %s, 'test_sku_migrate')",
            (_STORE, sku, source_type, source_key or sku))
        cur.execute(
            "INSERT INTO catalog.walmart_items "
            "(store, sku, upc, published_status, last_seen_at, missing_since) "
            "VALUES (%s, %s, %s, 'PUBLISHED', now(), %s)",
            (_STORE, sku, upc, missing))


@needs_pg
def test_pg_candidates_take_only_legacy_amz_live_rows(pg):
    """候选面在真库上跑一遍:跟卖不迁、已是新码的不迁、缺席的不迁、无号的不迁。"""
    _seed(pg, _OLD)                                             # ✓ 该迁
    _seed(pg, "B0PGMIG002", source_type="match", source_key="00012345678905")
    _seed(pg, "AG4ARD234567")                                   # 已是不透明码
    _seed(pg, "B0PGMIG003", missing="now()".replace("now()", "2026-01-01"))
    with pg.cursor() as cur:                                    # 无 upc/gtin
        cur.execute("INSERT INTO catalog.listing_sources "
                    "(store, sku, source_type, source_key, workflow) "
                    "VALUES (%s, 'B0PGMIG004', 'amz', 'B0PGMIG004', 't')", (_STORE,))
        cur.execute("INSERT INTO catalog.walmart_items "
                    "(store, sku, published_status, last_seen_at) "
                    "VALUES (%s, 'B0PGMIG004', 'PUBLISHED', now())", (_STORE,))
    rows, _notes = sm._candidates(pg, _STORE, 50)
    assert [r["old_sku"] for r in rows] == [_OLD]
    assert rows[0]["product_id"] == "000000000001"
    assert rows[0]["product_id_type"] == "UPC"


@needs_pg
def test_pg_pick_and_exclude_run_through_the_same_candidate_sql(pg):
    """点名/排除在**真库**上跑一遍:`%(unnamed)s::boolean` 与 `= ANY(...::text[])`
    的语法、空数组的行为、以及"点名不放松任何一条判据"。假连接测不出这些。"""
    _seed(pg, _OLD)                                            # ✓ 该迁
    _seed(pg, "B0PGMIG005", upc="000000000002")                # ✓ 该迁
    _seed(pg, "B0PGMIG006", source_type="match",
          source_key="00012345678905", upc="000000000003")     # 跟卖:不迁
    # 没点名 ⇒ 与改造前逐字等价(两条 amz 活码都在)
    assert [r["old_sku"] for r in sm._candidates(pg, _STORE, 50)[0]] == \
        [_OLD, "B0PGMIG005"]
    # 按旧码点名 / 按 ASIN(source_key)点名
    assert [r["old_sku"] for r in
            sm._candidates(pg, _STORE, 50, only_skus=[_OLD])[0]] == [_OLD]
    assert [r["old_sku"] for r in
            sm._candidates(pg, _STORE, 50,
                           only_keys=["B0PGMIG005"])[0]] == ["B0PGMIG005"]
    # 并集:两个点名参数一起给
    assert [r["old_sku"] for r in
            sm._candidates(pg, _STORE, 50, only_skus=[_OLD],
                           only_keys=["B0PGMIG005"])[0]] == [_OLD, "B0PGMIG005"]
    # 排除优先(既点名又排除)
    rows, notes = sm._candidates(pg, _STORE, 50, only_skus=[_OLD],
                                 exclude_skus=[_OLD])
    assert rows == [] and any("排除优先于点名" in n for n in notes)
    # 只排除、不点名:其余照常
    assert [r["old_sku"] for r in
            sm._candidates(pg, _STORE, 50,
                           exclude_skus=[_OLD])[0]] == ["B0PGMIG005"]
    # 点名一条跟卖 ⇒ 落选**并给理由**(点名不放松判据,也不静默丢)
    rows, notes = sm._candidates(pg, _STORE, 50, only_skus=["B0PGMIG006"])
    assert rows == [] and any("B0PGMIG006" in n and "跟卖不迁" in n for n in notes)


@needs_pg
def test_pg_pending_pointers_and_ledger_land_together(pg, monkeypatch):
    """mint + pending 台账落库:两条指针 + 一条过程账,状态自洽。"""
    _seed(pg, _OLD)
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "FPG1",
                          "count": len(items)}])
    rows, _ = sm._candidates(pg, _STORE, 10)
    counts, _lines = sm._migrate({"name": _STORE}, rows, True)
    assert counts["submitted"] == 1
    new_sku = rows[0]["new_sku"]
    assert sm.sku_codec.is_opaque(new_sku)
    with pg.cursor() as cur:
        cur.execute("SELECT replaced_by, replaced_at IS NOT NULL FROM "
                    "catalog.listing_sources WHERE store=%s AND sku=%s",
                    (_STORE, _OLD))
        assert cur.fetchone() == (new_sku, True)
        cur.execute("SELECT status, feed_id, submitted_at IS NOT NULL, feed_type "
                    "FROM listing.sku_migrations WHERE store=%s AND old_sku=%s",
                    (_STORE, _OLD))
        assert cur.fetchone() == ("pending", "FPG1", True, sm.FEED_TYPE)
    # 同一店同一旧码不会再入候选面(崩溃重入不会开第二条 pending)
    assert sm._candidates(pg, _STORE, 10)[0] == []


@needs_pg
def test_pg_confirm_moves_identity_upc_dispositions_and_node_rows(pg, monkeypatch):
    """confirmed 的五处后果在真库上逐条核:身份 / UPC / 处置 / 节点库存 / 台账。

    (第六处「上架表 SKU 列回写」2026-09-06 已删:改码不回写上架表。)"""
    _seed(pg, _OLD)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.upc_pool (upc, status, asin, store, sku) "
                    "VALUES ('000000000001', 'used', %s, %s, %s)",
                    (_ASIN, _STORE, _OLD))
        cur.execute("INSERT INTO ops.dispositions (store, sku, source, action) "
                    "VALUES (%s, %s, 'scan', 'relist')", (_STORE, _OLD))
        cur.execute("INSERT INTO catalog.item_node_inventory "
                    "(store, sku, ship_node, avail_qty, seen_at) "
                    "VALUES (%s, %s, 'N1', 3, now())", (_STORE, _OLD))
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "FPG2",
                          "count": len(items)}])
    rows, _ = sm._candidates(pg, _STORE, 10)
    sm._migrate({"name": _STORE}, rows, True)
    new_sku = rows[0]["new_sku"]
    # 观测:新码在架、旧码缺席、水位新鲜
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.walmart_items "
                    "(store, sku, upc, published_status, last_seen_at) "
                    "VALUES (%s, %s, '000000000001', 'PUBLISHED', now())",
                    (_STORE, new_sku))
        cur.execute("UPDATE catalog.walmart_items SET missing_since = now() "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
        cur.execute("UPDATE listing.sku_migrations SET submitted_at = "
                    "now() - interval '2 hours' WHERE store=%s", (_STORE,))
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    # ⚠ 定案**不回写库存**(所有者 2026-09-07 定稿):库存随改码 feed 一起写,
    # 这一段一次沃尔玛写接口都不调 —— 连店铺凭证都不取。
    monkeypatch.setattr(sm.store_limits, "maint_nodes",
                        lambda: pytest.fail("定案不回写库存,不该读受管仓表"))
    counts, _lines = sm._settle(pg, _STORE, True)
    assert counts["confirmed"] == 1 and "sheet" not in counts
    assert "inventory" not in counts and "inventory_failed" not in counts
    with pg.cursor() as cur:
        cur.execute("SELECT abandoned_at IS NOT NULL, abandoned_reason "
                    "FROM catalog.listing_sources WHERE store=%s AND sku=%s",
                    (_STORE, _OLD))
        assert cur.fetchone() == (True, "sku_update")
        # 不烧 UPC:号还是那个号,只是挂到了新码名下(status/asin 不动)
        cur.execute("SELECT status, asin, sku FROM catalog.upc_pool "
                    "WHERE upc = '000000000001'")
        assert cur.fetchone() == ("used", _ASIN, new_sku)
        cur.execute("SELECT sku, asin FROM ops.dispositions WHERE store=%s", (_STORE,))
        assert cur.fetchone() == (new_sku, _ASIN)
        cur.execute("SELECT count(*) FROM catalog.item_node_inventory "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
        assert cur.fetchone()[0] == 0
        # sheet_synced_at **恒 NULL**(2026-09-06 起改码不回写上架表;列保留只为
        # 不动存量库)—— 非空就说明回写路径被谁加回来了
        cur.execute("SELECT status, settled_at IS NOT NULL, sheet_synced_at "
                    "IS NULL FROM listing.sku_migrations WHERE store=%s", (_STORE,))
        assert cur.fetchone() == ("confirmed", True, True)
        # detail 里**没有** inventory_restored(定案不回写库存);提交时写的
        # 凭据(product_id/price/weight)原样还在
        cur.execute("SELECT detail ->> 'inventory_restored', detail ->> 'price' "
                    "FROM listing.sku_migrations WHERE store=%s", (_STORE,))
        restored, price = cur.fetchone()
        assert restored is None and price is not None
        # 新码的出生事件进了病历,旧码记的是 sku_replaced(不是 item_missing)
        cur.execute("SELECT sku, event FROM catalog.product_events "
                    "WHERE store=%s ORDER BY sku", (_STORE,))
        assert set(cur.fetchall()) == {(new_sku, "sku_replaced"),
                                       (_OLD, "sku_replaced")}
    # 别名视图开始出货(五处历史判据靠它继承一跳)
    with pg.cursor() as cur:
        cur.execute("SELECT sku, alias_sku FROM catalog.sku_aliases WHERE store=%s",
                    (_STORE,))
        assert cur.fetchall() == [(new_sku, _OLD)]


@needs_pg
def test_pg_rollback_revives_the_old_code_and_burns_nothing(pg, monkeypatch):
    """回滚弃的是**新码**(免费),旧码回到活码,下一轮可以重来;UPC 一个都不烧。"""
    _seed(pg, _OLD)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.upc_pool (upc, status, asin, store, sku) "
                    "VALUES ('000000000001', 'used', %s, %s, %s)",
                    (_ASIN, _STORE, _OLD))
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "failed", "feed_id": None, "count": len(items)}])
    rows, _ = sm._candidates(pg, _STORE, 10)
    counts, _lines = sm._migrate({"name": _STORE}, rows, True)
    assert counts["rolled_back"] == 1
    new_sku = rows[0]["new_sku"]
    with pg.cursor() as cur:
        cur.execute("SELECT replaced_by, abandoned_at FROM catalog.listing_sources "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
        assert cur.fetchone() == (None, None)          # 旧码复活
        cur.execute("SELECT abandoned_reason FROM catalog.listing_sources "
                    "WHERE store=%s AND sku=%s", (_STORE, new_sku))
        assert cur.fetchone() == ("sku_update_failed",)
        cur.execute("SELECT status FROM catalog.upc_pool WHERE upc='000000000001'")
        assert cur.fetchone() == ("used",)             # 号没被烧
        cur.execute("SELECT status, error FROM listing.sku_migrations "
                    "WHERE store=%s", (_STORE,))
        status, err = cur.fetchone()
        assert status == "rolled_back" and err
    # 回滚之后**可以再改一次码**(认领唯一索引只算活行),而且是一个新码
    rows2, _ = sm._candidates(pg, _STORE, 10)
    assert [r["old_sku"] for r in rows2] == [_OLD]
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "FPG3",
                          "count": len(items)}])
    sm._migrate({"name": _STORE}, rows2, True)
    assert rows2[0]["new_sku"] != new_sku


@needs_pg
def test_pg_stage_cap_and_observe_read_the_real_ledger(pg, monkeypatch):
    """节奏闸与定案判据读的是真表:pending 未清 ⇒ 上限 0;清完零 confirmed ⇒ 1。"""
    _seed(pg, _OLD)
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "FPG4",
                          "count": len(items)}])
    assert sm._stage_cap(pg, _STORE, 100)[0] == 1          # 空账:第一级
    rows, _ = sm._candidates(pg, _STORE, 1)
    sm._migrate({"name": _STORE}, rows, True)
    cap, note = sm._stage_cap(pg, _STORE, 100)
    assert cap == 0 and "只定案不提交" in note              # 有 pending:先清账
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    counts, _ = sm._settle(pg, _STORE, True)
    assert counts["pending"] == 1                          # 观测还没跑,不定案


@needs_pg
def test_pg_a_double_row_frees_the_gate_but_never_resubmits(pg, monkeypatch):
    """同店双挂在真库上走一遍(2026-09-07 所有者定稿,§9.14)。四件:

      ① 记 double(不是终态:settled_at 仍空;身份层旧行仍 replaced_by=新码);
      ② 节奏闸**放开**(open 只数 pending/stalled)—— 这就是 A131吕灿荣 那 2 条
         压着 500 条的解法;
      ③ 这一条旧码**不再入候选**(不重复提交);
      ④ 所有者回头把旧码删掉、观测记了缺席 ⇒ 下一轮**自动 confirmed**,不需要
         任何新逻辑(`_verdict` 六条规则一字未改)。
    """
    _seed(pg, _OLD)
    monkeypatch.setattr(sm.feeds, "submit_feed", lambda store, ft, items, workflow="":
                        [{"outcome": "submitted", "feed_id": "FPG5",
                          "count": len(items)}])
    rows, _ = sm._candidates(pg, _STORE, 10)
    sm._migrate({"name": _STORE}, rows, True)
    new_sku = rows[0]["new_sku"]
    # 观测:新码在架,而旧码**也**还在架(后台删不掉的那种)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.walmart_items "
                    "(store, sku, upc, published_status, last_seen_at) "
                    "VALUES (%s, %s, '000000000002', 'PUBLISHED', now())",
                    (_STORE, new_sku))
        cur.execute("UPDATE listing.sku_migrations SET submitted_at = "
                    "now() - interval '2 hours' WHERE store=%s", (_STORE,))
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    assert sm._stage_cap(pg, _STORE, 100)[0] == 0          # 记 double 之前:pending 挡着
    counts, _lines = sm._settle(pg, _STORE, True)
    assert counts["double"] == 1 and counts["confirmed"] == 0
    with pg.cursor() as cur:                                # ①
        cur.execute("SELECT status, settled_at IS NULL, error IS NOT NULL "
                    "FROM listing.sku_migrations WHERE store=%s", (_STORE,))
        assert cur.fetchone() == ("double", True, True)
        cur.execute("SELECT replaced_by, abandoned_at FROM catalog.listing_sources "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
        assert cur.fetchone() == (new_sku, None)           # 身份层一个字没动
    assert sm._stage_cap(pg, _STORE, 100)[0] > 0           # ② 闸放开了
    assert sm._candidates(pg, _STORE, 10)[0] == []         # ③ 不重复提交
    # 再判一次仍是 double,而且**不重复写库**(幂等)
    assert sm._settle(pg, _STORE, True)[0]["double"] == 1
    # ④ 所有者回头把旧码删掉,catalog_sync 记了缺席 ⇒ 下一轮自动 confirmed
    with pg.cursor() as cur:
        cur.execute("UPDATE catalog.walmart_items SET missing_since = now() "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
    counts2, _lines2 = sm._settle(pg, _STORE, True)
    assert counts2["confirmed"] == 1
    with pg.cursor() as cur:
        cur.execute("SELECT status, settled_at IS NOT NULL "
                    "FROM listing.sku_migrations WHERE store=%s", (_STORE,))
        assert cur.fetchone() == ("confirmed", True)
        cur.execute("SELECT abandoned_reason FROM catalog.listing_sources "
                    "WHERE store=%s AND sku=%s", (_STORE, _OLD))
        assert cur.fetchone() == ("sku_update",)           # 弃旧码走现成路径


# ══════════════════════════════════════════════════════════════════════════════
#  隔离与降级(一条坏行不许拖垮整轮;飞书抖动不许让身份回滚)
# ══════════════════════════════════════════════════════════════════════════════

def test_one_bad_row_does_not_stop_the_others(monkeypatch):
    """一条定案炸了(撞唯一索引/连接抖动)⇒ 点名 + 记日志,其余照定案。

    不隔离的话一条卡住的行会让**其余全部**改码永远定不了案 —— 而它们的旧码
    正被缺席抑制着,没人会报。
    """
    rows = [(1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
             NOW - timedelta(hours=2), "pending", "W1", None, True, True, True),
            (2, "B0OLD00002", "BBBBBBBBBBBB", "amz", "B0OLD00002", "F1",
             NOW - timedelta(hours=2), "pending", "W2", None, True, True, True)]
    read, calls, _tx = _settle_wired(monkeypatch, rows)
    boom = {"n": 0}

    def _settle_or_boom(c, s, o, n, v, r=""):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("模拟撞唯一索引")
        calls.append(("settle", s, o, n, v))

    monkeypatch.setattr(sm.sku_codec, "settle_replacement", _settle_or_boom)
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1 and counts["failed"] == 1
    assert ("settle", "T1", "B0OLD00002", "BBBBBBBBBBBB", "confirmed") in calls
    assert any("定案失败" in ln and "B0OLD00001" in ln for ln in lines)


def test_limit_zero_is_taken_literally(monkeypatch):
    """`-p limit=0` = 「只定案不提交」的手动开关,不许被静默当成缺省 10。"""
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(50, 0)])),
    ])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("limit=0 不许提交"))
    out = sm.run({"store": "T1", "execute": True, "limit": "0"})
    assert "本轮上限 0" in out


def test_rows_that_never_left_the_building_are_named_never_auto_settled(monkeypatch):
    """"落库了但没发出去"(pending 且 submitted_at 为空)只点名,**不自动定案**。

    进程死在 POST 之前是"确定没发",死在 POST 之后是"不知道到没到",从台账上
    分不出来 —— 判不准就判活。它们也不进 `_SQL_OBSERVE`(那条只取已提交的)。
    """
    read = _Conn([("listing.retire_cooldown", (["count"], [(0,)])),
                  ("status = 'pending' AND submitted_at IS NULL", (
                      ["id", "old_sku", "new_sku", "created_at"],
                      [(5, "B0OLD00005", "EEEEEEEEEEEE", NOW)]))])
    tx = _Conn(tag="tx")
    monkeypatch.setattr(sm.db, "pg_conn", lambda *a, **k: tx)
    counts, lines = sm._settle(read, "T1", True)
    assert counts["unsent"] == 1
    assert counts["confirmed"] == counts["rolled_back"] == 0
    assert not tx.sqls                       # 一条写都没有
    assert any("落库未提交" in ln and "不自动定案" in ln for ln in lines)


def test_every_candidate_is_minted_and_submitted_no_silent_truncation(monkeypatch):
    """**本轮候选面有几条就 mint 几条、发几条**,没有第二道截断(2026-09-07)。

    删掉的那道「超配额留量就只发前 N 个」是形态 A 时代自设的。留着的表现:整店
    真跑 3371 个在线品只发 1000 条就停,摘要却说得像官方配额 —— 而所有者以为
    整店发完了。候选面的唯一决定者是 `_stage_cap` × `-p limit`。
    """
    calls, _ = _migrate_wired(monkeypatch)
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(5), True)
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "mint"]) == 5
    assert counts["submitted"] == 5
    assert not any("超配额留量" in ln for ln in lines)


def test_submit_channel_is_open_by_default_after_the_2026_09_06_rewire(monkeypatch):
    """缺省**为空 = 不停闸**(2026-09-06 通道定案 MP_ITEM_MATCH 之后)。

    此前它非空,因为形态 A(MP_MAINTENANCE + SkuUpdate)被官方 spec 原件证伪。
    钉住缺省值本身:留一个忘了清的停闸串在里面,表现是每一轮都"只定案不提交"
    而摘要看起来完全正常 —— 所有者会以为改码在跑。
    """
    import importlib
    fresh = importlib.reload(sm)
    assert fresh.SUBMIT_DISABLED == "", \
        f"提交通道被停闸了:{fresh.SUBMIT_DISABLED}(定案了吗?)"


def test_a_non_empty_submit_disabled_forces_cap_zero(monkeypatch):
    """停闸**机制保留**:非空 ⇒ 本轮 cap=0(只定案不提交),而且 dry-run 也不列
    候选(列了就是"将改码 N 个"的误导)。定案要留着:已发出去的 pending 靠观测
    反证走 rolled_back 把旧码复活,停闸期没人替它们收尾就是一批孤儿。"""
    monkeypatch.setattr(sm, "SUBMIT_DISABLED", "测试:通道停用")
    _wire(monkeypatch)
    read = _read_conn(monkeypatch, [])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("通道停用不许提交"))
    for execute in (True, False):
        out = sm.run({"store": "T1", "execute": execute, "skus": "B0AAA00001"})
        assert "提交通道停用" in out and "本轮提交 0" in out
        assert not any("FROM catalog.walmart_items w" in sql for sql, _ in read.sqls)
    assert any("FROM listing.sku_migrations m" in sql for sql, _ in read.sqls)   # 定案面照查


# ══════════════════════════════════════════════════════════════════════════════
#  W4 · REPLACE 会覆盖线上现值:价格与重量两条判据(2026-09-06 通道切 MP_ITEM_MATCH)
#
#  MP_ITEM_MATCH 的 processMode 是 **REPLACE**:载荷里给了什么,线上那条 item 的
#  对应字段就变成什么。所以价格与重量必须把**现值原样发回去**,采不到现值的行
#  一律不许发 —— 发一个默认值就是拿一次改码顺手改了售价/运费,而且回执全绿、
#  摘要正常、没有任何东西会报。
# ══════════════════════════════════════════════════════════════════════════════

def test_only_the_live_price_is_a_replace_condition_now():
    """REPLACE 覆盖语义下**只有价格**是"原样发回去"的判据(`_CONDS` 单一出处)。

    重量那条(「有采集重量」`(p.slow -> 'weight') IS NOT NULL`)**已删**
    —— 2026-09-06 晚所有者定稿:改码时重量一律按新口径重写,解析不出或
    > 11 磅写 1 磅。留着它等于"新口径管不到存量行",而它拦下的恰恰是最该被
    改正的那批(线上重量正是老实现按错单位发上去的)。
    """
    names = [n for n, _w, _sql in sm._CONDS]
    assert "有现挂价格" in names
    assert "有采集重量" not in names                 # 删了,别加回来
    assert len(sm._CONDS) == 10                      # 十条判据(原十一条)
    price = next(sql for n, _w, sql in sm._CONDS if n == "有现挂价格")
    assert price == "(w.price IS NOT NULL AND w.price > 0)"
    assert price in sm._SQL_CANDIDATES and price in sm._SQL_WHY
    assert "REPLACE" in next(w for n, w, _s in sm._CONDS if n == "有现挂价格")
    # 判据没了,SQL 里也不许再有那个粗判据的残句
    for sql in (sm._SQL_CANDIDATES, sm._SQL_WHY):
        assert "(p.slow -> 'weight') IS NOT NULL" not in sql


def test_the_candidate_sql_still_carries_the_slow_blob_for_the_parser():
    """`catalog.products` 的 LEFT JOIN **保留**:它不再是判据来源,而是重量的
    **输入**(slow 段 → `mp_mapper.shipping_weight_ex`)。

    关联键是**登记簿 source_key**(= ASIN),与上架链同一把钥匙;必须是 LEFT:
    没采过的行现在照样能改码(重量写 1 磅),INNER 会让它整行消失。
    """
    for sql in (sm._SQL_CANDIDATES, sm._SQL_WHY):
        assert "LEFT JOIN catalog.products p" in sql
        assert "p.asin = ls.source_key" in sql
        assert "p.marketplace = %(marketplace)s" in sql
    assert "p.slow AS product_slow" in sm._SQL_CANDIDATES
    # 旧码行最后观测的库存也在候选面上(2026-09-07 升 v5:载荷自己带库存)。
    # 它**不是判据** —— 没观测到的行照样改码,只是不带 inventory
    assert "w.avail_qty AS avail_qty" in sm._SQL_CANDIDATES
    assert "avail_qty" not in " ".join(sql for _n, _w, sql in sm._CONDS)
    conn = _Conn([("FROM catalog.walmart_items w", (_CAND_COLS, [_cand("B0AAA00001")])),
                  ("FROM ops.feed_items", (["sku"], []))])
    sm._candidates(conn, "T1", 10)
    args = [a for sql, a in conn.sqls if "LIMIT %(limit)s" in sql][0]
    assert args["marketplace"] == sm.amz_source.MARKETPLACE   # 口径唯一出处


def test_a_row_whose_weight_cannot_be_parsed_is_no_longer_dropped():
    """`weight={"package": "N/A"}` / 没采过 / 裸数字:**照样进候选**,重量写 1 磅。

    2026-09-06 晚改口之前这三种都被 Python 侧第二层剔掉并点名。现在它们是
    正常候选 —— 兜底不静默:预览逐行标出、摘要给兜底行数(见下面两条)。
    """
    conn = _Conn([("FROM catalog.walmart_items w",
                   (_CAND_COLS, [_cand("B0AAA00001", slow={"weight": {"package": "N/A"}}),
                                 _cand("B0AAA00002", "0002", slow={}),
                                 _cand("B0AAA00003", "0003",
                                       slow={"weight": {"package": 300}}),
                                 _cand("B0AAA00004", "0004")])),
                  ("FROM ops.feed_items", (["sku"], []))])
    rows, notes = sm._candidates(conn, "T1", 10)
    assert [r["old_sku"] for r in rows] == ["B0AAA00001", "B0AAA00002",
                                            "B0AAA00003", "B0AAA00004"]
    assert not any("跳过" in n and "重量" in n for n in notes), notes
    assert sm._weight_of(rows[0]) == (1.0, "no_weight")
    assert sm._weight_of(rows[1]) == (1.0, "no_weight")
    assert sm._weight_of(rows[2]) == (1.0, "no_unit")     # 裸数字不当磅(事故形态)
    assert sm._weight_of(rows[3]) == (0.82, "parsed")


def test_a_parsed_weight_is_sent_as_is(monkeypatch):
    """解析得出来的重量**原样进载荷**(磅):REPLACE 把线上那格覆盖成它。"""
    calls, _ = _migrate_wired(monkeypatch)
    rows = _rows_for(1, slow={"weight": {"package": "12.8 ounces"}})
    sm._migrate({"name": "T1"}, rows, True)
    item = [c for c in calls if isinstance(c, tuple) and c[0] == "submit"][0][4][0]
    assert item["ShippingWeight"] == 0.8          # 12.8 oz = 0.8 lb(官方换算)


def test_an_over_cap_weight_is_written_as_one_pound_and_named_in_the_preview():
    """> 11 磅 ⇒ 写 1 磅(所有者 2026-09-06 定稿),而且**在预览里点名**。

    这条钉的是"分得清":预览只打一个 `1.0` 的话,"真 1 磅"与"兜底 1 磅"在纸面
    上一模一样,人眼确认就确认了个寂寞。
    """
    rows = [_rows_for(1, slow={"weight": {"package": "20 lbs"}})[0],
            _rows_for(1, slow={"weight": {"package": "3.5 pounds"}})[0]]
    rows[1]["old_sku"] = "B0AAA00002"
    lines = sm._preview(rows)
    body = "\n".join(lines)
    assert "重量 1.0 磅(兜底:超 11 磅)" in body
    assert "重量 3.5 磅(parsed)" in body
    assert "其中 1 行重量按 1.0 磅兜底(超 11 磅 1)" in body
    assert "有意为之" in body                    # 覆盖是有意的,不是"不得已"
    # 载荷样例与真发出去的那条同一份代码
    assert "'ShippingWeight': 1.0" in body or '"ShippingWeight": 1.0' in body


def test_the_ledger_detail_records_the_weight_reason(monkeypatch):
    """台账 `detail` 里 weight 旁边记 reason:光看 `weight=1.0` 复盘时分不出
    "真 1 磅"与"兜底 1 磅",而这两件的处置完全不同。"""
    import json as _json
    calls, conns = _migrate_wired(monkeypatch)
    sm._migrate({"name": "T1"}, _rows_for(1, slow={"weight": {"package": "N/A"}}), True)
    args = [a for c in conns for sql, a in c.sqls if "RETURNING id" in sql][0]
    detail = _json.loads(args["detail"])
    assert detail["weight"] == 1.0 and detail["weight_reason"] == "no_weight"
    assert detail["price"] == 29.99                      # 价格仍是现值原样发回
    ok = [a for c in conns for sql, a in c.sqls if "RETURNING id" in sql]
    good = _json.loads(ok[0]["detail"])
    assert set(good) >= {"product_id", "product_id_type", "price", "weight",
                         "weight_reason"}


def test_a_named_row_without_a_parsable_weight_is_a_hit_now():
    """点名一个采不到重量的旧码:它**命中**(不再落选),重量按 1 磅发。"""
    conn = _pick_conn([_cand("B0AAA00001", slow={"weight": {}})],
                      [_why("B0AAA00001")])
    rows, notes = sm._candidates(conn, "T1", 10, only_skus=["B0AAA00001"])
    assert [r["old_sku"] for r in rows] == ["B0AAA00001"]
    assert any("命中 1 个" in n for n in notes), notes
    assert not any("shipping_weight" in n for n in notes), notes


# ══════════════════════════════════════════════════════════════════════════════
#  W3 · 守门:改码**不回写上架表**(2026-09-06 所有者定稿)
#
#  「我们批量修改在线产品的 sku 无需回填上架表行,上架表我经常会清理,我们的 sku
#  和对应的来源码已经填写到在线产品表格中了。上架表中的 sku 列由上架的填写即可。」
#  —— 身份映射的出口是登记簿 catalog.listing_sources + 在线产品总表「来源码」列;
#  上架表 SKU 列只由上架链写。原来那条 _sync_sheet(按旧码/ASIN 定位 + 写 SKU 列 +
#  盖 sheet_synced_at + 每轮补写)连同缺口 G-4 的三条用例一起删了。
# ══════════════════════════════════════════════════════════════════════════════

def test_sku_migrate_never_writes_the_listing_sheet_sku_column():
    """源码守门:**可执行行**里不许再出现 `listing_sheet.write_sku_col` /
    `sheet_synced_at` / `_sync_sheet`。

    只看可执行行(`#` 注释与模块 docstring 剔除):头注与注释里要能把"为什么删了、
    别加回来"讲清楚,那是文档不是第二条路径。加回来的表现不会报错 —— 一张所有者
    定期清理的工作表悄悄变成第二处身份映射,而且改码链要为它的写失败长出补写路径。
    """
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    body = src.replace(ast.get_docstring(ast.parse(src)) or "", "", 1)
    hits = [ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
            and any(t in ln for t in ("listing_sheet", "write_sku_col",
                                      "sheet_synced_at", "_sync_sheet"))]
    assert not hits, f"改码又回写上架表了(所有者 2026-09-06 定稿:不回写):{hits}"
    assert not hasattr(sm, "_sync_sheet")
    assert not hasattr(sm, "_SQL_LEDGER_SHEET_OK")
    assert not hasattr(sm, "_SQL_UNSYNCED")


# ══════════════════════════════════════════════════════════════════════════════
#  反哺器防串扰:改码与跟卖共用 MP_ITEM_MATCH 之后,回执不许串表
# ══════════════════════════════════════════════════════════════════════════════

def test_match_sheet_reflector_only_reads_its_own_workflows_receipts(monkeypatch):
    """跟卖表反哺器按 **workflow 正向过滤** ops.feed_items。

    2026-09-06 起改码(sku_migrate)与跟卖(match_listing)**共用 MP_ITEM_MATCH**,
    feed_type 已经分不开两条链。不过滤的表现是一条改码回执被写进跟卖表的
    「feed 结果」列(行还是跟卖那一行,结论是别人的),而且不报错。
    """
    import inspect

    from services import feed_track, match_sheet
    assert match_sheet.WORKFLOW == "match_listing"
    for fn in (feed_track.item_results, feed_track.item_errors):
        assert inspect.signature(fn).parameters["workflow"].default is None
    src = inspect.getsource(match_sheet.sync_from_ledger)
    assert src.count("workflow=WORKFLOW") == 2      # item_results + item_errors
    # 过滤真的进了 SQL(而不是收下参数就扔)
    conn = _Conn()
    monkeypatch.setattr(feed_track.db, "pg_conn", lambda *a, **k: conn)
    feed_track.item_results("F1", workflow="match_listing")
    sql, args = conn.sqls[0]
    assert "AND workflow = %s" in sql and args == ("F1", "match_listing")
    conn.sqls.clear()
    feed_track.item_results("F1")                   # 不给就不过滤(逐字节旧行为)
    assert "workflow" not in conn.sqls[0][0] and conn.sqls[0][1] == ("F1",)


def test_listing_sheet_heal_only_reads_list_new_receipts():
    """上架表 Unknown 自愈同理加正向过滤 `f.workflow = 'list_new'`。

    只按 `feed_type='MP_ITEM'` 认的话,任何将来往 MP_ITEM 里发东西的链都会被读成
    本行的上架回执 —— 一条别人的 failed 会把「是否上架」写成 No 并把行推进限次
    重试通道,而**负向误写正是这段代码最防的那件事**(2026-06-09 事故语义)。
    """
    from services import listing_sheet
    sql = listing_sheet._SQL_HEAL_RECEIPT
    assert "f.feed_type = 'MP_ITEM'" in sql
    assert "f.workflow = 'list_new'" in sql


def test_a_missing_receipt_on_a_rejected_feed_rolls_back_at_once(monkeypatch):
    """2026-09-07 A131:整 feed 被拒(feed 级 ERROR、itemsReceived=0),当时的轮询把台账
    落成 missing 而不是 failed ⇒ 旧判据要等 24h 观测反证。feed_log 已 failed 就是确凿的
    「一条都没发出去」,按 failed 当场回滚。"""
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           datetime.now(timezone.utc) - timedelta(hours=1), "pending",
           None, None, False, False, True)
    _settle_wired.feed_st = {"F1": "failed"}
    try:
        read, calls, _tx = _settle_wired(monkeypatch, [row],
                                         receipts={"AAAAAAAAAAAA": ("missing", "")})
        counts, lines = _settle_at(sm, read, True)
    finally:
        _settle_wired.feed_st = {}
    assert counts["rolled_back"] == 1 and counts.get("pending", 0) == 0
    assert any("整 feed 被拒的回执 1 条按 failed 定案" in ln for ln in lines)
    assert any(c[0] == "settle" and c[4] == "rolled_back" for c in calls)


def test_a_missing_receipt_on_a_processed_feed_still_waits(monkeypatch):
    """feed 正常 PROCESSED 但明细里查无这个 SKU:仍是 missing,不许当 failed 回滚。
    (_settle 用真时钟判观测期,所以夹具的 submitted_at 也按真时钟给:1 小时前。)"""
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           datetime.now(timezone.utc) - timedelta(hours=1), "pending",
           None, None, False, False, True)
    _settle_wired.feed_st = {"F1": "done"}
    try:
        read, calls, _tx = _settle_wired(monkeypatch, [row],
                                         receipts={"AAAAAAAAAAAA": ("missing", "")})
        counts, lines = _settle_at(sm, read, True)
    finally:
        _settle_wired.feed_st = {}
    assert counts.get("rolled_back", 0) == 0 and counts["pending"] == 1
