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
        elif sql.startswith("TRUNCATE"):
            self.store["op"].append("TRUNCATE")

    def fetchone(self):
        return (self.store["before"],)

    def executemany(self, sql, rows):
        self.store["op"].append("INSERT")
        self.store["rows"] = list(rows)


class _MetaConn:
    def __init__(self, before):
        self.store = {"before": before, "op": [], "rows": []}

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
    n, dropped = risk_gate.sync_pt_meta(conn, _pt_rows(90))
    assert conn.store["op"] == ["COUNT", "TRUNCATE", "INSERT"]
    assert n == 90 and dropped == 10          # 飞书删的 10 行同步生效
    # 数字列要转 int,不能把 '86' 塞进 integer 列
    assert conn.store["rows"][0][7] == 86 and conn.store["rows"][0][8] == 15


def test_empty_read_never_wipes_the_gate_dictionary():
    """⚠ 读到 0 行就清空 = 审核两道闸对**全部**类目静默放行。宁炸不吞。"""
    with pytest.raises(ValueError, match="拒绝重灌"):
        risk_gate.sync_pt_meta(_MetaConn(before=7000), [])


def test_sudden_shrink_is_refused():
    """删几行是常态,删掉一半必是读漏/读错(与 _mirror 同款护栏)。"""
    with pytest.raises(ValueError, match="拒绝重灌"):
        risk_gate.sync_pt_meta(_MetaConn(before=7000), _pt_rows(3000))


def test_normal_shrink_passes():
    conn = _MetaConn(before=7008)
    n, dropped = risk_gate.sync_pt_meta(conn, _pt_rows(6942))
    assert n == 6942 and dropped == 66        # 所有者删掉的那 66 个
    assert "TRUNCATE" in conn.store["op"]


def test_first_load_into_an_empty_table_is_allowed():
    """库里本来是空的(before=0)时护栏不该挡路。"""
    conn = _MetaConn(before=0)
    assert risk_gate.sync_pt_meta(conn, _pt_rows(10))[0] == 10
