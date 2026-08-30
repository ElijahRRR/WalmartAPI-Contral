"""L1 类目锚装配的**过闸顺序**回归(2026-08-17 生产 bug)。

`load_context` 从 `walmart_category_map` 装 catmap / node_map 时,原来是
「先数 DISTINCT PT 判唯一 → 再过 pt_meta 闸」。顺序反了:

映射修正的惯例是**插新行不删旧行**(catmap_fix 头注:旧映射是历史证据)。
于是同一条 Amazon 路径长期同时挂着新 PT(有效)与旧 PT(字典里已经没有),
两个 DISTINCT 值 → 判成"两义" → **整条路径被丢弃**,连那条有效的一起没了。

生产实测:所有者的映射表里 121 条"死 PT 且置信度=高"的行,
**白白挤掉了 105 条本可直出的路径**(如
`Automotive > … > Calipers → Automotive Brakes` 被 `Disc Brake Calipers` 挤掉)。
表现是那些产品 L1 解不出 → pending,而映射表里明明写着答案。

修法:先过 pt_meta 闸再计票 —— 死 PT 永远不会被返回(原代码末尾那个
`in pt_meta` 已经保证),让它参与"是否唯一"的计票纯属逻辑错误。
"""

from services import audit_rules


class _Cur:
    def __init__(self, rows_by_key):
        self._d, self._out = rows_by_key, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        for k in sorted(self._d, key=len, reverse=True):
            if k in sql:
                self._out = self._d[k]
                return
        self._out = []

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, d):
        self._d = d

    def cursor(self):
        return _Cur(self._d)


_META = [("Automotive Brakes", "Automotive", "Brakes", "普通商品", "是", "", ""),
         ("Hammers", "Home Improvement", "Hand Tools", "普通商品", "是", "", "")]

# 同一路径两行:一行有效 PT,一行死 PT(字典里没有)——修正插新行没删旧行
_MAP_PATH = [
    ("Auto > Brake System > Calipers", "Automotive Brakes"),
    ("Auto > Brake System > Calipers", "Disc Brake Calipers"),   # 死 PT
    ("Tools > Hammers", "Hammers"),
]
_MAP_NODE = [
    ("111", "Automotive Brakes"),
    ("111", "Disc Brake Calipers"),                              # 死 PT
]


def _ctx(monkeypatch, path_rows=_MAP_PATH, node_rows=_MAP_NODE):
    d = {
        "FROM audit.walmart_pt_meta": _META,
        "SELECT amazon_category, walmart_product_type": path_rows,
        "SELECT browse_node_id, walmart_product_type": node_rows,
    }
    # 三元组:(Phase0 dict, R4 词集, R4 键→来源原文)——第三项是 TRO 接线用的
    monkeypatch.setattr(audit_rules, "_brand_map", lambda conn: ({}, set(), {}))
    monkeypatch.setattr(audit_rules, "_rows_dict",
                        lambda conn, sql, key: (
                            {r[0]: dict(zip(("walmart_product_type",
                                             "walmart_category", "walmart_ptg",
                                             "access_state", "zh_can_do",
                                             "requirements", "notes"), r))
                             for r in _META}
                            if "walmart_pt_meta" in sql else {}))
    monkeypatch.setattr(audit_rules, "_frozen",
                        lambda conn, sql: frozenset())
    monkeypatch.setattr(audit_rules.audit_l2, "load_nice_mapping",
                        lambda *a, **k: ({}, []))
    monkeypatch.setattr(audit_rules, "_build_automaton", lambda w: None)
    return audit_rules.load_context(_Conn(d))


def test_dead_pt_no_longer_kills_the_valid_sibling(monkeypatch):
    """核心:一条有效 + 一条死 ⇒ 路径仍然可用,取那条有效的。"""
    ctx = _ctx(monkeypatch)
    assert ctx.catmap["Auto > Brake System > Calipers"] == "Automotive Brakes"
    assert ctx.node_map["111"] == "Automotive Brakes"
    assert ctx.catmap["Tools > Hammers"] == "Hammers"


def test_two_valid_pts_are_still_ambiguous(monkeypatch):
    """⚠ 真两义照旧丢弃 —— 修的是"死 PT 参与计票",不是放宽唯一性。"""
    ctx = _ctx(monkeypatch,
               path_rows=[("同一路径", "Automotive Brakes"),
                          ("同一路径", "Hammers")],
               node_rows=[("222", "Automotive Brakes"), ("222", "Hammers")])
    assert "同一路径" not in ctx.catmap
    assert "222" not in ctx.node_map


def test_all_dead_means_no_anchor(monkeypatch):
    """全是死 PT 的路径依旧解不出 —— 不能凭空造出一个锚。"""
    ctx = _ctx(monkeypatch,
               path_rows=[("全死路径", "Disc Brake Calipers")],
               node_rows=[("333", "Disc Brake Calipers")])
    assert "全死路径" not in ctx.catmap and "333" not in ctx.node_map


def test_walmart_confirmed_keeps_the_opposite_order(monkeypatch):
    """⚠ 实证那一路**故意**不改:两家店报不同 PT 是真分歧,与字典无关。

    而且 walmart_items 的 PT 是沃尔玛线上现有的 —— 它不在 pt_meta 只说明
    我们那张表收得不全,不代表这个 PT 假。照着改会把真分歧当成一致。
    """
    src = __import__("inspect").getsource(audit_rules.load_context)
    assert "别照着下面" in src or "故意" in src
