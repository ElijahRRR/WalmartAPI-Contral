"""存量改码工作流回归(SKU 改造批次 3 W1~W6)。

钉的都是"错了不报错"的那几件:
  · 前置六道闸任一不过就整店不改(改码是破坏动作,判不准就不做);
  · 三态定案只信**观测**,回执成功单独不定案(delete_not_effective 同款故障模式);
  · **先落库并 commit 再调接口**(未提交事务里 POST = 沃尔玛已受理、我们零记录);
  · dry-run 下 _settle 与 _migrate **都**零写(_settle 是全包写得最重的一段);
  · 节奏闸 1 → 10 → 按 limit,`-p limit=` 只能收紧;
  · 跟卖不入候选、不透明码不入候选;
  · 提交 failed 当场回滚,unknown **保持 pending 不回滚**(决策 F)。

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
    assert sm.FEED_TYPE == "MP_MAINTENANCE"
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
    """feedType 只有 FEED_TYPE 一个出生地:形态 A→B 的切换必须只有一个改动点。"""
    src = (_ROOT / "workflows" / "sku_migrate.py").read_text(encoding="utf-8")
    body = src.replace(ast.get_docstring(ast.parse(src)) or "", "", 1)
    hits = [ln for ln in body.splitlines()
            if ('"MP_MAINTENANCE"' in ln or "'MP_MAINTENANCE'" in ln
                or '"MP_ITEM"' in ln)]
    assert len(hits) == 1 and hits[0].startswith("FEED_TYPE ="), hits


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


def test_executing_disposition_blocks_the_store(monkeypatch):
    ok, lines = _preflight_out(monkeypatch, executing=3)
    assert not ok
    assert any("闸③处置" in ln and "3 条 executing" in ln for ln in lines)


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
              "product_id", "product_id_type"]


def _cand(old_sku, pid="0001", src="amz", key=None):
    return ("T1", old_sku, src, key or "B0" + old_sku[-8:], pid, "UPC")


def test_opaque_and_match_rows_are_excluded_by_the_candidate_sql():
    """两条排除写在 SQL 里(不是 Python 事后过滤):跟卖不迁(决策 D),
    已经是不透明码的不再改(改码只有一跳)。"""
    sql = sm._SQL_CANDIDATES
    assert "source_type = ANY(%(source_types)s::text[])" in sql
    assert sm.SOURCE_TYPES == ("amz",)                    # match 天然不在候选面
    assert "AND NOT (" in sql and "w.sku ~ " in sql       # 形态判据来自 codec 常量
    assert sm.sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku") in sql
    # 未了结的改码台账挡住重复发起(崩溃重入不会开第二条 pending)
    assert "m.status IN ('pending', 'confirmed', 'stalled')" in sql
    # Product ID 取观测值,不取 UPC 池
    assert "coalesce(w.upc, w.gtin)" in sql and "upc_pool" not in sql


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
    """一行 `_SQL_WHY` 结果:默认七条判据全真,`bad` 里点名的那几条置假(按短名)。"""
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
    而且七条判据在"选取"与"解释"两处**逐字同源**(一漂就会出现"摘要说它满足
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

def _cap(confirmed, open_rows, asked):
    conn = _Conn([("FROM listing.sku_migrations",
                   (["confirmed", "open"], [(confirmed, open_rows)]))])
    return sm._stage_cap(conn, "T1", asked)


def test_first_batch_is_capped_at_one():
    cap, note = _cap(0, 0, 100)
    assert cap == 1 and "第一级" in note


def test_second_stage_is_capped_at_ten():
    cap, note = _cap(3, 0, 100)
    assert cap == 10 and "第二级" in note


def test_limit_can_only_tighten_never_loosen():
    assert _cap(50, 0, 5)[0] == 5             # 放行档:按 limit
    assert _cap(3, 0, 2)[0] == 2              # 第二级:limit 更小时按 limit
    assert _cap(0, 0, 999)[0] == 1            # 第一级:limit 大也压到 1


def test_quota_headroom_is_a_hard_ceiling():
    """节奏闸放行之后仍有配额留量硬顶:一次最多 FEEDS_PER_STORE_PER_RUN 个 feed。"""
    cap, note = _cap(999, 0, 10 ** 6)
    assert cap == sm.FEEDS_PER_STORE_PER_RUN * sm.ITEMS_PER_FEED
    assert "配额留量硬顶" in note


def test_no_new_submissions_while_pending_or_stalled_rows_exist():
    cap, note = _cap(20, 1, 10)
    assert cap == 0 and "只定案不提交" in note


# ══════════════════════════════════════════════════════════════════════════════
#  W3 · 三态判决(纯函数)
# ══════════════════════════════════════════════════════════════════════════════

def _obs(new_present=False, old_gone=False, fresh=True, hours=1):
    return {"id": 1, "old_sku": "B0OLD00001", "new_sku": "AAAAAAAAAAAA",
            "source_type": "amz", "source_key": "B0OLD00001", "feed_id": "F1",
            "submitted_at": NOW - timedelta(hours=hours),
            "new_present": new_present, "old_gone": old_gone, "fresh": fresh}


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
#  W3 · 定案的后果(六处写)
# ══════════════════════════════════════════════════════════════════════════════

def _settle_wired(monkeypatch, obs_rows, unsynced=(), receipts=None, calls=None):
    calls = calls if calls is not None else []
    read = _Conn([("FROM listing.sku_migrations m", (
                      ["id", "old_sku", "new_sku", "source_type", "source_key",
                       "feed_id", "submitted_at", "new_present", "old_gone",
                       "fresh"], obs_rows)),
                  ("status = 'confirmed' AND sheet_synced_at IS NULL", (
                      ["id", "old_sku", "new_sku", "source_type", "source_key"],
                      list(unsynced)))])
    tx = _Conn(log=calls, tag="tx")
    monkeypatch.setattr(sm.db, "pg_conn", lambda *a, **k: tx)
    monkeypatch.setattr(sm.feed_track, "item_results",
                        lambda fid: dict(receipts or {}))
    monkeypatch.setattr(sm.sku_codec, "settle_replacement",
                        lambda c, s, o, n, v, r="": calls.append(
                            ("settle", s, o, n, v)))
    monkeypatch.setattr(sm.upc_pool, "retag_sku",
                        lambda c, triples: calls.append(("retag", list(triples))))
    monkeypatch.setattr(sm.dispositions, "rekey_suggested",
                        lambda c, s, o, n, asin=None: (
                            calls.append(("rekey", o, n, asin)), (1, []))[1])
    monkeypatch.setattr(sm.walmart_catalog, "drop_node_rows",
                        lambda c, s, sku: (calls.append(("drop", sku)), 1)[1])
    monkeypatch.setattr(sm.listing_sheet, "read_rows",
                        lambda upto=None: [{"store": "T1", "asin": "B0OLD00001",
                                            "sku": "", "rownum": 7}])
    monkeypatch.setattr(sm.listing_sheet, "write_sku_col",
                        lambda ups, execute=True: (
                            calls.append(("sheet", list(ups), execute)),
                            len(ups))[1])
    return read, calls, tx


_OBS_COLS_CONFIRM = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001",
                     "F1", NOW - timedelta(hours=2), True, True, True)


def test_confirmed_retags_upc_rekeys_dispositions_and_drops_node_rows(monkeypatch):
    read, calls, _tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1
    kinds = [c[0] for c in calls if isinstance(c, tuple)]
    assert kinds[:4] == ["settle", "retag", "rekey", "drop"]
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "confirmed") in calls
    assert ("retag", [("T1", "B0OLD00001", "AAAAAAAAAAAA")]) in calls


def test_confirmed_writes_the_sku_column_outside_the_transaction(monkeypatch):
    """飞书回写必须在事务**之后**:外部 IO 不进数据库事务,它失败不该让身份回滚。"""
    read, calls, tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    _settle_at(sm, read, True)
    order = [c if isinstance(c, str) else c[0] for c in calls]
    assert order.index("commit:tx") < order.index("sheet")
    sheet_call = [c for c in calls if isinstance(c, tuple) and c[0] == "sheet"][0]
    assert sheet_call[1] == [(7, "AAAAAAAAAAAA")] and sheet_call[2] is True
    # 写成功才盖 sheet_synced_at(盖不上的行下一轮再来)
    assert any("sheet_synced_at = now()" in sql for sql, _ in tx.sqls)


def test_unsynced_confirmed_rows_are_retried_next_round(monkeypatch):
    """一次飞书写失败之后该行已是 confirmed、不再进 pending 判决面 ——
    没有补写路径它的 SKU 列就永远停在旧码,而且不报错。"""
    read, calls, _tx = _settle_wired(
        monkeypatch, [],
        unsynced=[(9, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001")])
    counts, lines = _settle_at(sm, read, True)
    assert counts["sheet"] == 1
    assert any("补写候选 1 行" in ln for ln in lines)


def test_rows_the_sheet_cannot_locate_are_named_and_left_unsynced(monkeypatch):
    read, calls, _tx = _settle_wired(
        monkeypatch, [], unsynced=[(9, "B0OLD00099", "AAAAAAAAAAAA", "amz",
                                    "B0MISSING1")])
    counts, lines = _settle_at(sm, read, True)
    assert counts["sheet"] == 0 and counts["sheet_lag"] == 1
    assert any("上架表找不到行" in ln for ln in lines)


def test_failed_receipt_settles_as_rolled_back_and_never_resubmits(monkeypatch):
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           NOW - timedelta(hours=2), False, True, True)
    read, calls, _tx = _settle_wired(
        monkeypatch, [row], receipts={"AAAAAAAAAAAA": ("failed", "ERR_9")})
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("_settle 永不提交任何东西"))
    counts, lines = _settle_at(sm, read, True)
    assert counts["rolled_back"] == 1
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "rolled_back") in calls
    assert any("未自动补交" in ln for ln in lines)


def test_double_listing_warns_and_settles_nothing(monkeypatch):
    row = (1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
           NOW - timedelta(hours=2), True, False, True)
    read, calls, _tx = _settle_wired(monkeypatch, [row])
    counts, lines = _settle_at(sm, read, True)
    assert counts["double"] == 1 and counts["confirmed"] == 0
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "settle"]
    assert any("同店双挂" in ln for ln in lines)


def test_settle_in_dry_run_writes_nothing_and_calls_no_feishu(monkeypatch):
    """_settle 是全包写得最重的一段,dry-run 下六处写**全部**跳过。"""
    read, calls, _tx = _settle_wired(
        monkeypatch, [_OBS_COLS_CONFIRM],
        unsynced=[(9, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001")])
    counts, lines = _settle_at(sm, read, False)
    assert counts["confirmed"] == 1                      # 判决照算
    assert not [c for c in calls if isinstance(c, tuple)]  # 写一次都没有
    assert "open:tx" not in calls                        # 连写事务都没开
    assert any("将定案" in ln for ln in lines)
    assert any("一格都没写" in ln for ln in lines)


def _settle_at(mod, read_conn, execute):
    """按当轮语义调 _settle(读连接与写事务分开,见 _settle 头注)。"""
    return mod._settle(read_conn, "T1", execute)


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


def _rows_for(n=2):
    return [{"store": "T1", "old_sku": f"B0AAA0000{i}", "source_type": "amz",
             "source_key": f"B0AAA0000{i}", "product_id": f"00{i}",
             "product_id_type": "UPC"} for i in range(1, n + 1)]


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
    assert submit[1] == "MP_MAINTENANCE" and submit[2] == "sku_migrate"
    item = submit[4][0]["Orderable"]
    assert item["sku"] == rows[0]["new_sku"] != rows[0]["old_sku"]
    assert item["SkuUpdate"] == "Yes"
    assert item["productIdentifiers"] == {"productId": "001",
                                          "productIdType": "UPC"}


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


def test_feeds_per_run_cap_is_enforced(monkeypatch):
    """单店单轮最多 FEEDS_PER_STORE_PER_RUN 次提交:MP_MAINTENANCE 桶与维护链共享。"""
    monkeypatch.setattr(sm, "ITEMS_PER_FEED", 1)
    calls, _ = _migrate_wired(monkeypatch)
    sm._migrate({"name": "T1"}, _rows_for(5), True)
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "submit"]) \
        == sm.FEEDS_PER_STORE_PER_RUN


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
        ("FROM listing.sku_migrations m", (
            ["id", "old_sku", "new_sku", "source_type", "source_key", "feed_id",
             "submitted_at", "new_present", "old_gone", "fresh"],
            [(1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
              NOW - timedelta(hours=200), True, False, True),
             (2, "B0OLD00002", "BBBBBBBBBBBB", "amz", "B0OLD00002", "F1",
              datetime.now(timezone.utc) - timedelta(hours=200), False, False,
              False)])),
        ("status = 'confirmed' AND sheet_synced_at IS NULL", (
            ["id", "old_sku", "new_sku", "source_type", "source_key"],
            [(3, "B0OLD00003", "CCCCCCCCCCCC", "amz", "B0NOROW001")])),
        ("FROM listing.sku_migrations WHERE store", (["confirmed", "open"],
                                                     [(0, 2)])),
    ])
    monkeypatch.setattr(sm.feed_track, "item_results", lambda fid: {})
    monkeypatch.setattr(sm.listing_sheet, "read_rows", lambda upto=None: [])
    monkeypatch.setattr(sm.listing_sheet, "write_sku_col",
                        lambda ups, execute=True: len(ups))
    first = sm.run({"store": "T1", "execute": True}).splitlines()[0]
    assert "同店双挂 1" in first
    assert "超期未定案 1" in first
    assert "订单双行 1 组" in first
    assert "上架表 SKU 列未同步" in first


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


def test_a_blocked_preflight_still_settles_but_never_submits(monkeypatch):
    """闸拦的是"发新的",**不是"定案"**。

    反过来写会死锁:一条不相干的 executing 处置就能让整店的 pending 永远定不了案,
    而那些旧码正被缺席抑制着 —— 没有任何东西会报。
    """
    _wire(monkeypatch, executing=1)
    read = _read_conn(monkeypatch, [])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("前置闸未过不许提交"))
    monkeypatch.setattr(sm.listing_sheet, "read_rows", lambda upto=None: [])
    out = sm.run({"store": "T1", "execute": True})
    assert "前置闸未过" in out and "闸③处置" in out
    assert "本轮提交 0" in out
    # 定案面照查(账要清),候选面一条都不查(上限被压到 0)
    assert any("FROM listing.sku_migrations m" in sql for sql, _ in read.sqls)
    assert not any("FROM catalog.walmart_items w" in sql for sql, _ in read.sqls)


def test_settle_only_settles_and_never_submits(monkeypatch):
    _wire(monkeypatch)
    _read_conn(monkeypatch, [
        ("FROM listing.sku_migrations m", (
            ["id", "old_sku", "new_sku", "source_type", "source_key", "feed_id",
             "submitted_at", "new_present", "old_gone", "fresh"], [])),
    ])
    monkeypatch.setattr(sm.feeds, "submit_feed",
                        lambda *a, **k: pytest.fail("settle_only 不许提交"))
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
    _wire(monkeypatch, executing=1)
    read = _read_conn(monkeypatch, [])
    monkeypatch.setattr(sm.listing_sheet, "read_rows", lambda upto=None: [])
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
    """confirmed 的六处后果在真库上逐条核:身份 / UPC / 处置 / 节点库存 / 台账 / 飞书。"""
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
    written = []
    monkeypatch.setattr(sm.listing_sheet, "read_rows",
                        lambda upto=None: [{"store": _STORE, "asin": _ASIN,
                                            "sku": "", "rownum": 12}])
    monkeypatch.setattr(sm.listing_sheet, "write_sku_col",
                        lambda ups, execute=True: (written.extend(ups), len(ups))[1])
    counts, _lines = sm._settle(pg, _STORE, True)
    assert counts["confirmed"] == 1 and counts["sheet"] == 1
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
        cur.execute("SELECT status, settled_at IS NOT NULL, sheet_synced_at "
                    "IS NOT NULL FROM listing.sku_migrations WHERE store=%s", (_STORE,))
        assert cur.fetchone() == ("confirmed", True, True)
        # 新码的出生事件进了病历,旧码记的是 sku_replaced(不是 item_missing)
        cur.execute("SELECT sku, event FROM catalog.product_events "
                    "WHERE store=%s ORDER BY sku", (_STORE,))
        assert set(cur.fetchall()) == {(new_sku, "sku_replaced"),
                                       (_OLD, "sku_replaced")}
    assert written == [(12, new_sku)]
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


# ══════════════════════════════════════════════════════════════════════════════
#  隔离与降级(一条坏行不许拖垮整轮;飞书抖动不许让身份回滚)
# ══════════════════════════════════════════════════════════════════════════════

def test_one_bad_row_does_not_stop_the_others(monkeypatch):
    """一条定案炸了(撞唯一索引/连接抖动)⇒ 点名 + 记日志,其余照定案。

    不隔离的话一条卡住的行会让**其余全部**改码永远定不了案 —— 而它们的旧码
    正被缺席抑制着,没人会报。
    """
    rows = [(1, "B0OLD00001", "AAAAAAAAAAAA", "amz", "B0OLD00001", "F1",
             NOW - timedelta(hours=2), True, True, True),
            (2, "B0OLD00002", "BBBBBBBBBBBB", "amz", "B0OLD00002", "F1",
             NOW - timedelta(hours=2), True, True, True)]
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


def test_a_feishu_failure_does_not_roll_back_the_settled_identity(monkeypatch):
    """飞书是外部 IO:它挂了不该让已经定案的身份回滚,也不该让这一轮报失败 ——
    `sheet_synced_at` 这一列存在的意义就是"下一轮再来"。"""
    read, calls, _tx = _settle_wired(monkeypatch, [_OBS_COLS_CONFIRM])
    monkeypatch.setattr(sm.listing_sheet, "write_sku_col",
                        lambda ups, execute=True: (_ for _ in ()).throw(
                            RuntimeError("99991400 频控")))
    counts, lines = _settle_at(sm, read, True)
    assert counts["confirmed"] == 1              # 身份照样定案
    assert counts["sheet"] == 0 and counts["sheet_lag"] == 1
    assert any("回写失败" in ln and "下一轮补写" in ln for ln in lines)
    assert ("settle", "T1", "B0OLD00001", "AAAAAAAAAAAA", "confirmed") in calls


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
    monkeypatch.setattr(sm.listing_sheet, "read_rows", lambda upto=None: [])
    counts, lines = sm._settle(read, "T1", True)
    assert counts["unsent"] == 1
    assert counts["confirmed"] == counts["rolled_back"] == 0
    assert not tx.sqls                       # 一条写都没有
    assert any("落库未提交" in ln and "不自动定案" in ln for ln in lines)


def test_candidates_beyond_the_quota_headroom_are_not_minted(monkeypatch):
    """配额留量的截断在 **mint 之前**:先 mint 再截会造出"落库了但永远没发出去"
    的孤儿,而那批行会让节奏闸永远看见 pending,整店从此发不出下一批。"""
    monkeypatch.setattr(sm, "ITEMS_PER_FEED", 1)
    calls, _ = _migrate_wired(monkeypatch)
    counts, lines = sm._migrate({"name": "T1"}, _rows_for(5), True)
    minted = [c for c in calls if isinstance(c, tuple) and c[0] == "mint"]
    assert len(minted) == sm.FEEDS_PER_STORE_PER_RUN     # 只 mint 发得出去的那些
    assert any("超配额留量" in ln and "一个字都没落库" in ln for ln in lines)
