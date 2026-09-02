"""audit_why 回归:把"为什么是这个结论"的整条链摊开(只读排查)。

所有者 2026-08-16 实遇:一把普通锤子(`Home Improvement / Hand Tools /
Hammers / 普通商品 / 是`)被判拒,理由栏写着 `General-Use Products`。
结论列只留得下一句话,而结论是多层数据叠出来的 —— 要判断是"规则对数据错"
还是"数据对规则错",必须同时看见**命中哪条规则**和**那条规则读的那一格里
写着什么**。这条工作流就干这个,本文件钉住它不许少说这两样。
"""

import datetime as dt

from workflows import audit_why


class _Cur:
    """按 SQL 分流的假游标。"""

    def __init__(self, data):
        self._data, self._out = data, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        # 长键优先:_SQL_PT_VICTIMS 也 FROM catalog.products,不这样会串台
        for key in sorted(self._data, key=len, reverse=True):
            if key in sql:
                self._out = self._data[key]
                return
        self._out = []

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._data)


_AT = dt.datetime(2026, 8, 16, 20, 21)

_HAMMER = {
    "FROM catalog.products": [
        ("B00004Z4HQ", "Goldblatt G15813 Corner Clincher and Mallet", "Goldblatt",
         "Hammers", "map_direct", "rejected", "General-Use Products", _AT, "v1",
         None),   # 末列 audit_detail:老行没有(B1 之前的结论),打出来是 None
    ],
    "FROM audit.audit_runs": [
        (77, "B00004Z4HQ", "Hammers", "map_direct", "高", 0, "reject",
         "L2", "skip", None, _AT),
    ],
    "FROM audit.audit_hits": [
        (77, "L2", "cat_requires_cert_hard", -100,
         {"walmart_pt": "Hammers", "walmart_policy": "General-Use Products",
          "meta_requirements": "Comply with applicable regulations",
          "matched_hard_kws": ["UL 认证"],
          "note": "飞书维护的合规要求 (含实验室证书/官方注册号), 搬运模式做不了"}),
    ],
    "FROM audit.walmart_pt_meta": [
        ("Hammers", "Home Improvement", "Hand Tools", "普通商品", "是",
         "Comply with applicable regulations", None),
    ],
    "p.audit_status = 'rejected'": [
        ("B00004Z4HQ", "Goldblatt G15813 Corner Clincher and Mallet",
         "rejected"),
    ],
}


def test_shows_both_the_rule_and_the_cell_it_read(monkeypatch):
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_HAMMER))
    out = audit_why.run({"asins": "b00004z4hq"})      # 小写也认
    # ① 结论那一行(人看到的那句)
    assert "rejected" in out and "General-Use Products" in out
    # ② 命中的规则 + 人话
    assert "cat_requires_cert_hard" in out and "-100" in out
    assert "该类目要求认证" in out
    # ③ **规则读的是哪张表哪一列** —— 没有这个就只能干瞪眼
    assert "audit.walmart_pt_meta.requirements" in out
    # ④ 那一格里到底写着什么 + 匹配上了什么(误判一眼可见)
    assert "Comply with applicable regulations" in out
    assert "UL 认证" in out
    # ⑤ 准入两列同时亮出来:它们是过的,拒它的是第三列 —— 这个对比是关键
    assert "普通商品" in out and "'是'" in out


def test_prints_the_three_columns_verdict_category_detail(monkeypatch):
    """2026-09-02 B1:结论是**三段**(判定结果 / 类别 / 具体内容),排查要三段都见人。

    老行(B1 之前审的)`audit_detail` 是 NULL,打出来就是 None —— 那正是
    "这一行还没被新链重审过"的样子,不是查询坏了。
    """
    data = dict(_HAMMER)
    data["FROM catalog.products"] = [
        ("B00004Z4HQ", "锤子", "Goldblatt", "Hammers", "map_direct", "rejected",
         "Intellectual Property", _AT, "c.2026-09-02.2",
         "商标符号(命中:XYZ®)"),
    ]
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(data))
    out = audit_why.run({"asins": "B00004Z4HQ"})
    assert "结论 rejected  类别 'Intellectual Property'" in out
    assert "具体内容 '商标符号(命中:XYZ®)'" in out
    # 老行:具体内容打 None
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_HAMMER))
    assert "具体内容 None" in audit_why.run({"asins": "B00004Z4HQ"})


def test_says_so_when_the_asin_was_never_audited(monkeypatch):
    data = {"FROM catalog.products": _HAMMER["FROM catalog.products"]}
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(data))
    out = audit_why.run({"asins": "B00004Z4HQ"})
    assert "没审过" in out


def test_says_so_when_the_asin_is_not_in_the_catalog(monkeypatch):
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn({}))
    out = audit_why.run({"asins": "B0MISSING1"})
    assert "不在 catalog.products" in out and "product_ingest" in out


def test_by_pt_dumps_the_row_the_gates_actually_read(monkeypatch):
    """按 PT 看:一个类目被拒一片时,先确认是不是那一行数据填错了。"""
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_HAMMER))
    out = audit_why.run({"pt": "Hammers"})
    assert "access_state" in out and "普通商品" in out
    assert "zh_can_do" in out
    assert "requirements" in out and "**R3 在这一格里扫认证关键词**" in out


def test_missing_pt_row_is_called_out(monkeypatch):
    """PT 不在 meta 表 = R1/R3 静默放行(旧仓原语义)—— 静默的东西必须说出来。"""
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn({}))
    assert "没有这个 PT" in audit_why.run({"pt": "NoSuchPT"})


def test_needs_one_of_the_two_params():
    assert "要么给" in audit_why.run({})


_BLACKLISTED = {
    "FROM catalog.products": [
        ("B00004YOG7", "Shepherd Hardware 9124 Rubber Leg Tips", "Shepherd",
         "Furniture Grippers, Pads & Sliders", "audit_llm", "rejected",
         "General-Use Products", _AT, "c.2026-08-13.1", None),
    ],
    "FROM audit.audit_runs": [
        (2395753, "B00004YOG7", "(phase0_blocked)", "skipped", "低", 0,
         "reject", "L0", "skip", None, _AT),
    ],
    "FROM audit.audit_hits": [
        (2395753, "L0", "phase0_lark_blacklist_amazon_cat", -100,
         {"amazon_category_path": "Tools & Home Improvement > Hardware",
          "normalized": "tools&homeimprovement->hardware",
          "source": "blacklist_center"}),
    ],
}


def test_blacklist_hit_says_which_entry_matched(monkeypatch):
    """⚠ 黑名单命中必须说出**命中的是哪一条**,否则等于没说。

    生产实证 2026-08-16:首版 explain_hit 只认 brand/category/keyword/matched
    四个键,而 Phase0 三表写进 detail 的是 seller_id / asin / normalized ——
    于是判拒理由打出来只有"亚马逊类目在黑名单中心",人还是不知道是哪个类目,
    没法去黑名单中心里找那一行改。
    """
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_BLACKLISTED))
    out = audit_why.run({"asins": "B00004YOG7"})
    assert "亚马逊类目在黑名单中心" in out
    assert "Tools & Home Improvement > Hardware" in out    # 命中的那一条
    assert "catalog.amazon_cat_blacklist" in out           # 去哪张表改
    assert "停在 L0" in out                                 # 短路在 Phase0


def test_meta_gap_warning_says_it_is_not_this_rejects_cause(monkeypatch):
    """PT 不在 meta 表是**另一个**问题 —— 不说清就会被当成本次判拒的原因。"""
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_BLACKLISTED))
    out = audit_why.run({"asins": "B00004YOG7"})
    assert "静默放行" in out and "与本次判拒无关" in out
    assert "missing_meta=1" in out          # 给出量化那条命令


def test_missing_meta_only_counts_runs_that_reached_the_gate(monkeypatch):
    """⚠ 前面被拦下的**没走到** R1/R3,不能算进"闸失效"。

    所有者 2026-08-17 当场指出首版输出误导:8394 个里 7872 个 PT='unknown'
    —— 那是类目没解出来、停在 L1 判 pending,与那两道闸毫无关系。
    分层停在哪的分布必须一起打出来,分母说不清的比例没有意义。
    """
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn({
        "GROUP BY 1 ORDER BY 2 DESC\n": [("L1", 7872), ("L0", 500),
                                          ("(过了闸)", 300), ("L2", 100)],
        "LEFT JOIN audit.walmart_pt_meta": [
            ("Christmas Ornaments", 47, 44, _OLD, ["l1_llm"]),
            ("coffee mugs", 84, 0, _OLD, ["l1_llm"])],
        "= ANY(%s)": [("Coffee Mugs", "coffeemugs")],
    }))
    out = audit_why.run({"missing_meta": "1"})
    # 分层分布:让人自己看见 7872 个根本没走到闸
    assert "7872" in out and "没走到 R1/R3" in out
    # 两类分开:名字对不上(补命名) vs 表里真没有(补数据)—— 修法完全不同
    assert "名字对不上" in out and "'coffee mugs'" in out and "'Coffee Mugs'" in out
    assert "表里真没有" in out and "Christmas Ornaments" in out
    assert "131 个的 PT 不在" in out          # 47+84,只数走到闸的


_OLD = dt.datetime(2026, 5, 1)
_NEW = dt.datetime(2026, 8, 16)


def test_missing_meta_separates_migrated_history_from_a_live_hole(monkeypatch):
    """⚠ 现在的引擎**产不出字典外 PT** —— 所以字典外 PT 只可能是搬进来的历史。

    是不是,看 run 的年代,别猜:`resolve_pt` 末尾有一道 `pt not in ctx.pt_meta
    → None` 的防御,L1 LLM 侧有 `_in_dictionary` 双闸(字典就是 pt_meta 本身)。
    全是老 run = 历史包袱(要不要复核由人定);有新 run = 那两道闸真漏了,是 bug。
    """
    def _data(ts):
        return {"GROUP BY 1 ORDER BY 2 DESC\n": [("(过了闸)", 300)],
                "LEFT JOIN audit.walmart_pt_meta": [
                    ("Wall Clocks", 155, 145, ts, ["l1_llm"])],
                "= ANY(%s)": []}

    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_data(_OLD)))
    old = audit_why.run({"missing_meta": "1"})
    assert "之前的 run" in old and "不是现在这套的洞" in old
    assert "force_rerun" in old              # 要复核的话怎么做

    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn(_data(_NEW)))
    new = audit_why.run({"missing_meta": "1"})
    assert "现在这套产出的" in new and "字典闸真有漏" in new
    assert "_in_dictionary" in new           # 直接点名去查哪


def test_missing_meta_all_clear(monkeypatch):
    monkeypatch.setattr(audit_why.db, "pg_conn", lambda: _Conn({}))
    assert "PT 全都在 audit.walmart_pt_meta 里" in audit_why.run(
        {"missing_meta": "1"})
