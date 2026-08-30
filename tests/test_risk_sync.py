"""risk_sync 回归:飞书风控三源 → PG 的同步语义。

本文件目前只钉住 2026-08-17 补上的那条链:**审核准入字典的全量重灌**。
其余同步(类目表 upsert / 品牌表 / 黑名单两张镜像)的回归在
tests/test_blacklist_push.py 与 tests/test_audit_rules_wiring.py。
"""

import pytest

from services import risk_gate



# ── 审核准入字典同步(2026-08-17 生产实证)────────────────────────────────────

class _MetaCur:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SELECT count(*)"):
            self.store["op"].append("COUNT")
        elif sql.startswith("SELECT walmart_product_type"):
            self.store["op"].append("DIFF")      # 变更比对,必须在 TRUNCATE 之前
        elif sql.startswith("TRUNCATE"):
            self.store["op"].append("TRUNCATE")

    def fetchone(self):
        return (self.store["before"],)

    def fetchall(self):
        # 库里现有的 pt_meta 三判据列(变更比对读的就是它)
        return self.store["existing"]

    def executemany(self, sql, rows):
        self.store["op"].append(
            "CHANGELOG" if "pt_meta_change_log" in sql else "INSERT")
        self.store["rows" if "pt_meta_change_log" not in sql
                   else "changes"] = list(rows)


class _MetaConn:
    def __init__(self, before, existing=()):
        self.store = {"before": before, "op": [], "rows": [], "changes": [],
                      "existing": list(existing)}

    def cursor(self):
        return _MetaCur(self.store)


def _pt_rows(n, start=0):
    return [{"product_type": f"PT{i}", "category": "Home", "ptg": "G",
             "admit_status": "普通商品", "cn_seller": "是",
             "cert_required": "", "note": "", "field_total": "86",
             "field_required": "15", "field_list": "a | b"}
            for i in range(start, start + n)]


def test_pt_meta_is_truncated_not_upserted():
    """⚠ upsert 只增改不删 —— 飞书删掉的行在库里永远留着。

    所有者 2026-08-17:「飞书表格里面的已废弃我已经删掉了。但是重新拉以后
    还是存在」。根因:audit.walmart_pt_meta 是批次 A 的死快照,**没有任何
    同步链**,而同一张飞书表早就被 risk_sync 读着灌 risk_product_types 了。
    同一份数据两个消费方,只同步了一个。
    """
    conn = _MetaConn(before=100)
    n, dropped, _ = risk_gate.sync_pt_meta(conn, _pt_rows(90))
    assert conn.store["op"][:4] == ["COUNT", "DIFF", "TRUNCATE", "INSERT"]
    assert n == 90 and dropped == 10          # 飞书删的 10 行同步生效
    # 数字列要转 int,不能把 '86' 塞进 integer 列
    assert conn.store["rows"][0][7] == 86 and conn.store["rows"][0][8] == 15


def test_empty_read_never_wipes_the_gate_dictionary():
    """⚠ 读到 0 行就清空 = 审核两道闸对**全部**类目静默放行。宁炸不吞。"""
    with pytest.raises(ValueError, match="拒绝重灌"):
        risk_gate.sync_pt_meta(_MetaConn(before=7000), [])


def test_sudden_shrink_is_refused():
    """删几行是常态,删掉一半必是读漏/读错(与 _guard_shrink 同款护栏)。"""
    with pytest.raises(ValueError, match="拒绝重灌"):
        risk_gate.sync_pt_meta(_MetaConn(before=7000), _pt_rows(3000))


def test_normal_shrink_passes():
    conn = _MetaConn(before=7008)
    n, dropped, _ = risk_gate.sync_pt_meta(conn, _pt_rows(6942))
    assert n == 6942 and dropped == 66        # 所有者删掉的那 66 个
    assert "TRUNCATE" in conn.store["op"]


def test_first_load_into_an_empty_table_is_allowed():
    """库里本来是空的(before=0)时护栏不该挡路。"""
    conn = _MetaConn(before=0)
    assert risk_gate.sync_pt_meta(conn, _pt_rows(10))[0] == 10


# ── 判据变更台账(2026-08-21:飞书数据变了,失效信号第一次接上)────────────

def _existing(*triples):
    """库里现有的 pt_meta 三判据列(变更比对读的就是它)。"""
    return [(pt, acc, zh, req) for pt, acc, zh, req in triples]


def test_only_the_three_judged_columns_count_as_a_change():
    """⚠ 只比 R1/R3 真正读的三列 —— 别的列改了不影响任何判定。

    类目名、字段数、备注变了也记一笔的话,"要重判 M 条"那个数会虚高,
    而那是所有者决定跑不跑重判的唯一依据,虚一次他下次就不看了。
    """
    conn = _MetaConn(before=1,
                     existing=_existing(("PT0", "普通商品", "是", "")))
    rows = _pt_rows(1)
    rows[0]["category"] = "Household"      # 判据之外的列改了
    rows[0]["note"] = "改过备注"
    rows[0]["field_total"] = "999"
    _, _, changes = risk_gate.sync_pt_meta(conn, rows)
    assert changes == []
    assert "CHANGELOG" not in conn.store["op"]     # 一行都不写


def test_a_real_judgement_change_is_logged_with_before_and_after():
    """判据真变了要落台账,**前后值都记** —— 那是"谁把这个类目改成什么样"的轨迹。"""
    conn = _MetaConn(before=1,
                     existing=_existing(("PT0", "禁售", "否(Walmart 禁售)", "")))
    _, _, changes = risk_gate.sync_pt_meta(conn, rows := _pt_rows(1))
    assert len(changes) == 1
    pt, kind, b_acc, a_acc, b_zh, a_zh, b_req, a_req = changes[0]
    assert (pt, kind) == ("PT0", "changed")
    assert (b_acc, a_acc) == ("禁售", "普通商品")       # 正是所有者手改的那一列
    assert (b_zh, a_zh) == ("否(Walmart 禁售)", "是")
    assert conn.store["op"].index("DIFF") < conn.store["op"].index("TRUNCATE")
    assert conn.store["changes"] == changes


def test_added_and_removed_pts_are_changes_too():
    """新增 PT 与**删除** PT 同样是判据变更。

    删除尤其要记:R1 的两条 pending 分支正是「PT 不在 walmart_pt_meta」,
    删掉一个 PT 会让它名下的产品从"有结论"变成"判不了" —— 那也得重判。
    """
    conn = _MetaConn(before=2,
                     existing=_existing(("PT0", "普通商品", "是", ""),
                                        ("PT_GONE", "普通商品", "是", "")))
    _, _, changes = risk_gate.sync_pt_meta(conn, _pt_rows(2))
    kinds = {c[0]: c[1] for c in changes}
    assert kinds == {"PT1": "added", "PT_GONE": "removed"}   # PT0 原样,不记


def test_blank_and_none_are_the_same_value_not_a_change():
    """飞书空单元格读出来有时 None 有时空串 —— 不归一的话每次同步都刷一屏假阳性。"""
    conn = _MetaConn(before=1,
                     existing=_existing(("PT0", "普通商品", "是", None)))
    rows = _pt_rows(1)
    rows[0]["cert_required"] = "   "
    assert risk_gate.sync_pt_meta(conn, rows)[2] == []
