"""事件码唯一出处的代码级约束。

此前三处清单(schema.sql 注释 / db_schema.md / product_events.py docstring)
各漂各的,maintenance_submitted、problem_categorized 发了大半个月没登记;
且全仓事件名是字符串字面量,拼错不报错——账本只追加、消费方按精确字符串
过滤,拼错的码 = 一支永远没人查的分叉。这里把约束钉死。
"""

import pytest

from services import product_events as pe


class _Conn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def executemany(self, sql, seq): conn.rows.extend(seq)
        return _Cur()


def test_record_many_rejects_unregistered_code():
    """未登记的码直接拒收,fail loud——宁可提交时炸,不要账本静默分叉。"""
    with pytest.raises(ValueError, match="未登记的事件码"):
        pe.record_many(_Conn(), [{"sku": "B0A", "event": "delete_submited",
                                  "source": "t"}])           # 少个 t,经典拼错


def test_record_many_accepts_every_registered_code():
    conn = _Conn()
    rows = [{"sku": "B0A", "event": e, "source": "t"} for e in sorted(pe.EVENTS)]
    assert pe.record_many(conn, rows) == len(pe.EVENTS)


def test_events_covers_all_receipt_codes():
    """五类 feed 的回执码必须都在集合里——feed_track 是动态拼的
    f"{kind}_feed_{status}",漏一个 kind 就是回执写不进账本。"""
    for kind in ("delete", "retire", "maintenance", "match", "list"):
        for st in ("success", "failed"):
            assert f"{kind}_feed_{st}" in pe.EVENTS


def test_constants_match_ledger_strings():
    """常量值 = 库里已落数据用的字符串。改任何一个都等于把历史事件孤儿化,
    必须让本用例红、由人确认做数据迁移。"""
    assert pe.MAINTENANCE_SUBMITTED == "maintenance_submitted"
    assert pe.PROBLEM_CATEGORIZED == "problem_categorized"
    assert pe.DELETE_SUBMITTED == "delete_submitted"
    assert pe.DELETE_VERIFIED == "delete_verified"
    assert pe.DELETE_NOT_EFFECTIVE == "delete_not_effective"
    assert pe.ITEM_MISSING == "item_missing"
    # 码级事件(SKU 改造批次 0a 登记,写入点 services/sku_codec.abandon 在批次 2)
    assert pe.SKU_ABANDONED == "sku_abandoned"
    assert pe.SKU_REPLACED == "sku_replaced"
    assert {pe.SKU_ABANDONED, pe.SKU_REPLACED} <= pe.EVENTS


def test_no_stray_event_literals_in_emitters():
    """发出点不许再出现事件名字面量(读侧 SQL 绑参同理)。

    grep 源码钉死:夹具喂什么假数据都盖不住一个新写进去的字面量。
    """
    import pathlib, re
    bad = []
    for f in list(pathlib.Path("workflows").glob("*.py")) +              [pathlib.Path("services/walmart_catalog.py")]:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\"event\":\s*\"[a-z_]+\"", line):
                bad.append(f"{f}:{i}: {line.strip()}")
    assert not bad, "事件名用 product_events 常量,别写字面量:\n" + "\n".join(bad)


def test_receipt_code_constants_match_the_derived_set():
    """回执码具名常量 == `{kind}_feed_{status}` 推导出来的那两个串(B2-32)。

    回执事件码在仓内是推导出来的、本身没有名字,于是「事件码常量唯一出处」
    这条纪律在这一处天然破功 —— 而 _FEED_KIND 一改取值,写字面量的那条 SQL
    会**静默返回空集**(list_new 的退役冷却闸、本模块的删除核验起点都读它)。
    补两个具名常量是最小修法:不新增能力,只给已经存在的字符串一个名字。
    """
    from services import product_events as pe

    assert pe.RETIRE_FEED_SUCCESS == f"{pe._FEED_KIND['RETIRE_ITEM']}_feed_success"
    assert pe.DELETE_FEED_SUCCESS == f"{pe._FEED_KIND['DELETE_ITEM']}_feed_success"
    assert pe.RETIRE_FEED_SUCCESS in pe.EVENTS
    assert pe.DELETE_FEED_SUCCESS in pe.EVENTS
    # 删除核验的 SQL 由常量拼成,三处事件码一个字面量都不剩
    assert f"event = '{pe.DELETE_FEED_SUCCESS}'" in pe._VERIFY_SQL
    assert f"'{pe.DELETE_VERIFIED}', '{pe.DELETE_NOT_EFFECTIVE}'" in pe._VERIFY_SQL
