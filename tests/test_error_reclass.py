"""error_reclass(存量报错按新码回填)的守门测试。

这条工作流的三个判据面各钉一遍:
  ① **原文四级优先**(全文优先于样本)—— 拿 200 字符样本判出来的码是下限,
     优先序错了会让一大批行悄悄按截断文本定案;
  ② **参数宁炸不吞** + `--dry-run` 真的不写;
  ③ **它不许改判定行为**:`category` 不动、不删行、只写 `taxonomy_*`。
"""

import pytest

from registry import resources
from services import error_taxonomy as et
from workflows import error_reclass as wf


# ── ① 原文四级优先(纯函数,拿假数据就能测)────────────────────────────────

def test_原文优先序_全文压过样本():
    """⚠ 优先序是判据的一部分:`self` 那一级是**截 200 字符的样本**,
    判据串可能被切掉(如 `appropriate product type selected` 在 200 字符外)。
    所以只要外源有全文,一律用全文。"""
    rec = {"B0AAA": "records 全文"}
    ev = {"B0AAA": "events 病历", "B0BBB": "events 病历"}
    it = {"SKU-C": "items 当前值"}
    # 1. records 压过其余全部
    assert wf.pick_source("B0AAA", "样本", "SKU-C", rec, ev, it) == \
        ("records 全文", "records")
    # 2. 没有 records 用 events
    assert wf.pick_source("B0BBB", "样本", "SKU-C", rec, ev, it) == \
        ("events 病历", "events")
    # 3. 前两级都没有,才按 src_sku 去 items
    assert wf.pick_source("B0CCC", "样本", "SKU-C", rec, ev, it) == \
        ("items 当前值", "items")
    # 4. 三级外源全空 → 退到本表样本
    assert wf.pick_source("B0DDD", "样本", None, rec, ev, it) == ("样本", "self")


def test_原文四处都没有_不猜():
    """四级全空 → `('', 'none')`;调用方据此把 `taxonomy_code` 留 NULL。
    编一个码出来会一路进报表,而没有任何东西会红。"""
    assert wf.pick_source("B0ZZZ", None, None, {}, {}, {}) == ("", "none")
    assert wf.pick_source("B0ZZZ", "   ", "SKU", {}, {}, {}) == ("", "none")


def test_原文空串不算命中():
    """外源列可能是空串(不是 NULL):空串当没有,继续往下一级找。"""
    assert wf.pick_source("B0AAA", "样本", None,
                          {"B0AAA": ""}, {"B0AAA": "病历"}, {}) == ("病历", "events")


# ── ② 归类与政策名 ──────────────────────────────────────────────────────────

def test_classify_政策名join不上就不写():
    """⚠ 与 audit_l3 的 `policy` 解析同一条纪律:猜出来的政策名会一路进报表
    与申诉口径,而没有任何东西会红。join 不上 → None。"""
    text = "Prohibited Products Policy: Alcohol."
    code, name, term = wf.classify(text, ["Alcohol"])
    assert (code, name, term) == ("POLICY", "Alcohol", None)
    code2, name2, _ = wf.classify(text, ["Animals"])   # 表里没有 Alcohol
    assert code2 == "POLICY" and name2 is None


def test_classify_带回OTHER的显式词条_拉黑判据靠它():
    """⚠ `OTHER` 是混装桶:所有者只让 business decision / trust & safety
    算永久拉黑,`currently under review` 是自愈态不算 —— 光有主码判不了,
    必须把**赢下主码那个原子**命中的词条一起带回来(落 `taxonomy_term`)。"""
    from services import error_taxonomy as et
    for text, want, permanent in (
            ("unpublished due to a Walmart business decision.",
             "business decision", True),
            ("removed by trust & safety review", "trust & safety", True),
            ("This item is currently under review.",
             "currently under review", False)):
        code, _name, term = wf.classify(text, [])
        assert code == "OTHER" and term == want, text
        assert et.is_permanent(code, term) is permanent, text


def test_classify_旧B里的那几种真的会分开():
    """所有者要的就是这个:旧 B(禁售)一个桶,新码分得开。"""
    cases = {
        "This item is a prohibited product. Prohibited Products Policy: Alcohol.":
            "POLICY",
        "may be a prohibited product. Please make sure the appropriate product "
        "type selected for this item.": "PT_WRONG",
        "This item is subject to a cpsc recall.": "RECALL",
        "not eligible for appeal": "PROHIBITED_FINAL",
    }
    for text, want in cases.items():
        assert wf.classify(text, ["Alcohol"])[0] == want, text


# ── ③ 参数与安全闸 ─────────────────────────────────────────────────────────

def test_参数宁炸不吞():
    """打错的参数名被静默吞掉 = 人以为按自己说的跑完了,而实际跑的是缺省口径,
    摘要还长得一模一样(与 product_audit 同款纪律)。"""
    with pytest.raises(ValueError, match="未识别参数"):
        wf._parse({"scoope": "records"})
    with pytest.raises(ValueError, match="scope 只能是"):
        wf._parse({"scope": "everything"})
    with pytest.raises(ValueError, match="chunk 要正整数"):
        wf._parse({"chunk": 0})


def test_dry_run_必须自己认():
    """⚠ `DANGEROUS=False` 时 cli 恒给 `execute=True` —— 漏了那一句
    `--dry-run` 会直接把存量刷了,而且报成功。"""
    assert wf._parse({"execute": True})[3] is True
    assert wf._parse({"execute": True, "dry_run": True})[3] is False


def test_limit缺省不限量():
    """0 = 不限量(与 product_audit 2026-09-03 定稿同口径)。"""
    assert wf._parse({})[4] == 0
    assert wf._parse({"limit": "500"})[4] == 500


# ── ④ 它不许改判定行为 ──────────────────────────────────────────────────────

def test_口径常量不是自己抄的一份():
    """双轨禁止:与报告工作流读同一份 `services/error_taxonomy` 的常量。"""
    assert wf.NOT_A_PRODUCT_BAN is et.NOT_A_PRODUCT_BAN
    for code in wf.NOT_A_PRODUCT_BAN:
        assert code in resources.ERROR_CATEGORY_CODES, code


def test_只写taxonomy列_不碰category也不删行():
    """⚠ 这条工作流的全部承诺就在这里:老列与判定行为一个字不动。

    黑名单是「一次入选、永久禁止」,批量放行是破坏性动作 —— 顺手接上去
    就是拿 4 万多个 ASIN 的拦截行为做一次无人裁决的变更。
    """
    import inspect
    src = inspect.getsource(wf)
    assert "SET taxonomy_code" in src
    for forbidden in ("SET category", "DELETE FROM", "DROP ", "TRUNCATE"):
        assert forbidden not in src, forbidden
    # 两条 UPDATE 都必须盖版本号(不盖 = 下次重跑又是全量,永远跑不完)
    assert src.count("taxonomy_version = %(ver)s") == 2


def test_判了0条要说清是已经判过而不是没数据():
    """⚠ 2026-09-03 实遇:版本号已盖章时增量谓词把全部行排除,摘要只剩
    "判了 0 条",看着像没数据、实际是已经判过。

    而这时若**刚加了新列**(如 taxonomy_term),那一列会全是 NULL 而没有任何
    提示 —— 下游(blacklist_route)按它做判断就会静默走错。所以必须点名
    `-p force=1`。
    """
    import inspect
    src = inspect.getsource(wf.run)
    assert "判了 0 条 = 已经按当前码表" in src
    assert "刚加了新列" in src
    assert "error_reclass -p force=1" in src
    assert "not force" in src                 # 给了 force 还判 0 条就是真没数据


# ── ⑤ 翻页:2026-09-03 实遇的死循环 ─────────────────────────────────────────

class _FakeCur:
    """只实现一件事:按键集游标 `after` 返回下一批(第 0 列是主键)。

    故意**不看 SQL 文本** —— 它验的是 `_pages` 有没有推进游标。SQL 里那句
    `key > after` 由下面的静态断言单独钉。
    """

    def __init__(self, table, log):
        self.table, self.log = table, log
        self.rows = []

    def execute(self, _sql, args):
        after, take = args["after"], args["chunk"]
        self.log.append(after)
        rows = [r for r in self.table if after is None or r[0] > after]
        self.rows = rows[:take]

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, table):
        self.table, self.log = table, []

    def cursor(self):
        return _FakeCur(self.table, self.log)


def test_pages_每行只发一次且会终止():
    """⚠ 2026-09-03 实遇:`-p force=1` 时候选谓词被 `true OR …` **短路**,
    `ORDER BY key LIMIT n` 每轮返回**同一批**,UPDATE 盖了章也排不掉 ——
    循环永不终止,进度打到 **5,574 万**(表里只有 97,002 行),而第一批之后的
    数据**一条都没碰过**。看着像在干活,其实原地转圈。

    所以断点续跑(版本号)与翻页(键集游标)是**两件事**,`force` 只许关掉
    前者。这条测试钉住后者:每行恰好发一次,且循环会自己停。
    """
    table = [(i, f"raw-{i}", None) for i in range(1, 251)]
    conn = _FakeConn(table)
    got = [r for batch in wf._pages(conn, "sql", "t.x", 100, 0, True)
           for r in batch]
    assert [r[0] for r in got] == list(range(1, 251))     # 每行恰好一次、按序
    assert conn.log == [None, 100, 200, 250]              # 游标真的在推进


def test_pages_空跑只取一批_limit真截断():
    """空跑不盖版本号 ⇒ 再取一批还是同一批,所以只取一批看形态(老行为保留)。"""
    table = [(i, "", None) for i in range(1, 251)]
    assert sum(len(b) for b in
               wf._pages(_FakeConn(table), "s", "v", 100, 0, False)) == 100
    assert sum(len(b) for b in
               wf._pages(_FakeConn(table), "s", "v", 100, 120, True)) == 120


def test_两条PICK的SQL里都有键集游标():
    """⚠ 光有 `_pages` 不够:SQL 少了 `key > after` 那一句,游标推进也没用,
    每轮照样返回同一批。两张表都钉,主键类型也钉(NULL 起点要能比较)。"""
    assert "%(after)s::bigint IS NULL OR id > %(after)s::bigint" in wf._SQL_REC_PICK
    assert "%(after)s::text IS NULL OR asin > %(after)s::text" in wf._SQL_BL_PICK
    for sql in (wf._SQL_REC_PICK, wf._SQL_BL_PICK):
        assert "ORDER BY" in sql          # 键集分页的前提:按主键定序
    # 两条 PICK 都不许再自己写 while 翻页(双轨禁止:翻页只有 `_pages` 一份)
    import inspect
    for fn in (wf._records_pass, wf._blacklist_pass):
        src = inspect.getsource(fn)
        assert "_pages(" in src, fn.__name__
        assert "while True" not in src, fn.__name__


def test_统一之后不许再报对角线矩阵():
    """⚠ 2026-09-04:`category` 统一到新码之后,「入选旧码 → 新码」大部分是
    对角线(`FLAGGED → FLAGGED`),信息量归零 —— 留着全量对角线只会让人以为
    "还在迁移中"。只报**真的换了码**的,其余给个总数。"""
    import inspect
    src = inspect.getsource(wf._blacklist_pass)
    assert "k[0] != k[1]" in src                    # 只留换了码的
    assert "入选码**全部没变**" in src               # 全对角线时说清楚


def test_站不住要按会不会被放行分两栏():
    """⚠ 「站不住」(`NOT_A_PRODUCT_BAN`:病根不是产品本身违禁)与**去留**
    (`is_permanent`:所有者裁决的七码)是两个正交问题,**两张表都含 `GATED`** ——
    品类准入拿不到,产品本身不违禁(所以"站不住"),但我们照样卖不了
    (所以裁决"继续禁")。

    统一之前那段写「旧码算它们永久禁售,新码认出病根另在别处」,统一之后左右
    同码(`GATED → GATED`)那句话自相矛盾;而 2,006 条 GATED 被叫"站不住"更是
    误导 —— **它们是留下的**。所以按「会不会被 blacklist_route 删」分两栏报。
    """
    import inspect
    from services import error_taxonomy as et
    src = inspect.getsource(wf._blacklist_pass)
    assert "会被放行的" in src and "按裁决仍留" in src
    assert "下次 blacklist_route 会删" in src
    # 判据取自引擎,不在这儿重新写一份
    assert "error_taxonomy.is_permanent(c, None)" in src
    # GATED 正是那个两边都在的码 —— 它必须落到"仍留"那一栏
    assert "GATED" in et.NOT_A_PRODUCT_BAN and "GATED" in et.PERMANENT_CODES


# ── ⑥ 事件回填的棘轮:2026-09-04「回填把判对的行改错了」那 2,595 条 ──────────

#: 生产原文的形状(判据串在**句尾**),拉长到 200 字符以外 —— 截断后判据串没了。
_EV_FULL = ("This item has been unpublished for violating Walmart's Marketplace "
            "*Prohibited Product Policy*. Please review the policy documentation in "
            "the Seller Help Center for the complete list of restricted categories. "
            "To republish this item please make sure you have the appropriate "
            "product type selected for this item.")
_EV_SAMPLE = _EV_FULL[:200]          # problem_scan 当初写进事件的那一份


class _EvCur:
    """事件遍的假游标:按键集游标发行,并把 executemany 的 (SQL, 参数) 记下来。"""

    def __init__(self, rows, log):
        self.rows_all, self.log, self.rows = rows, log, []

    def execute(self, _sql, args=None):
        after, take = args["after"], args["chunk"]
        self.rows = [r for r in self.rows_all
                     if after is None or r[0] > after][:take]

    def fetchall(self):
        return self.rows

    def executemany(self, sql, seq):
        self.log.append((sql, list(seq)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _EvConn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return _EvCur(self.rows, self.log)

    def commit(self):
        pass


def test_事件回填不许把判对的行改错(monkeypatch):
    """⚠ 2026-09-04 生产事故(docs/error_taxonomy.md §17)。

    `problem_scan` 到当天为止是**判用全文、存留残文**:归类吃
    `it["reasons"]` 全文,写事件时却 `(it["reasons"] or "")[:200]`。
    回填第一版拿事件自己那份残文重判,于是 **2,595 条**当初判对的
    `PT_WRONG`(可放)被改成 `POLICY`(永久禁),摘要还显示一切正常。

    **回填的风险不是判得糙,是把判对的行改错。** 三种行为各钉一遍。
    """
    rows = [
        # ① 已是新码 + 原文还原得到 → 拿全文重判(**这正是修 2,595 条的路径**)
        (1, _EV_SAMPLE, "PT_WRONG", "SKU-1", "B0A"),
        # ② 已是新码 + 还原不了 → **一个字不动**(棘轮)
        (2, _EV_SAMPLE, "PT_WRONG", "SKU-2", "B0B"),
        # ③ 还是旧 A-L 码 + 还原不了 → 判残文写进去,这才是真正的"下限"
        (3, _EV_SAMPLE, "K", "SKU-3", "B0C"),
    ]
    conn = _EvConn(rows)
    monkeypatch.setattr(wf, "_sources",
                        lambda c, a, s: ({}, {}, {"SKU-1": _EV_FULL}))
    lines = wf._events_pass(conn, "t.x", 100, True, 0, True, [], {})

    sets = {u["id"]: u for sql, ups in conn.log if "'category'" in sql
            for u in ups}
    keeps = [u["id"] for sql, ups in conn.log
             if "'taxonomy_src', 'keep'" in sql for u in ups]
    assert keeps == [2]                      # 判对的那条没被碰
    assert 2 not in sets
    assert sets[1]["code"] == "PT_WRONG" and sets[1]["src"] == "items"
    assert sets[3]["code"] == "POLICY"       # 只有残文时的下限
    # 不动的那条也要盖版本号 —— 否则每轮重新排队,永远跑不完
    assert all(u["ver"] == "t.x" for sql, ups in conn.log for u in ups)
    assert "原样不动** 1 条" in lines[0]


def test_事件遍不许拿别的时间点的文本判这一格(monkeypatch):
    """⚠ 事件是**时间线上的一格**,自带那一刻的原文。

    所以它走 `restore`(候选须以自己那份为前缀,只接回被切掉的那段),
    不走 `pick`(那是给「没有自己原文」的黑名单行用的四级优先)。
    events 那一级对本遍更是没有意义 —— 拿同一 asin **另一条**事件的文本判
    这一条就是串账。
    """
    import inspect
    src = inspect.getsource(wf._events_pass)
    assert "error_source.restore(" in src
    assert "error_source.pick(" not in src and "pick_source(" not in src
    assert "_ev" in src                       # events 那一级取回来就丢掉
    # 候选**不是**这一条的延长 → 不能用,判的还是自己那份残文
    conn = _EvConn([(1, _EV_SAMPLE, "K", "SKU-1", "B0A")])
    monkeypatch.setattr(wf, "_sources",
                        lambda c, a, s: ({}, {}, {"SKU-1": "另一次报错的更长的全文" * 30}))
    wf._events_pass(conn, "t.x", 100, True, 0, True, [], {})
    u = [u for sql, ups in conn.log if "'category'" in sql for u in ups][0]
    assert u["src"] == "self" and u["code"] == "POLICY"
