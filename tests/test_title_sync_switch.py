"""标题维护整路停闸(所有者 2026-09-05)—— 开关唯一出处 `maintenance_intents.TITLE_SYNC`。

根因不在 feed 在沃尔玛的内容规则(Seller Center 截图实证):我们 MP_MAINTENANCE 发的
新标题进了「卖家提交值」(回执 SUCCESS、重发报 stale 0101198),但过不了内容质量闸
(>150 字符 / 不合 Style Guide 公式 / 大小写),**没成为「在 Walmart.com 上生效的值」**。
这条链于是每天烧配额零效果。停闸要停两处:生成侧不产、执行件不领 —— 只停一处等于
只停一半;而且两处都要**见人**,静默关闭 = 没人记得它关着。

⚠ tests/conftest.py 的 autouse 夹具把 TITLE_SYNC 打成 True 给存量用例用;本文件
每条都自己把它关回去。
"""

import re
from pathlib import Path

from services import maintenance_intents as mi
from tests.test_maintenance import _row, _wire
from workflows import maintenance as mw


def test_default_is_off_until_titles_meet_walmart_rules():
    """缺省必须是 False:改 True 的前提是 TITLE_SYNC 头注里的两件事都做完
    (标题生成按沃尔玛口径 ≤150 + 公式;「沃尔玛未采纳」记账)。"""
    src = Path(mi.__file__).read_text(encoding="utf-8")
    assert re.search(r"^TITLE_SYNC = False$", src, re.M), "TITLE_SYNC 缺省被改了 —— 两个恢复条件做完了吗?"
    head = src[src.index("TITLE_SYNC = False") - 2500:src.index("TITLE_SYNC = False")]
    for must in ("150", "Style Guide", "生效的值", "恢复条件"):
        assert must in head, must


def test_generation_side_emits_nothing_when_off(monkeypatch, caplog):
    monkeypatch.setattr(mi, "TITLE_SYNC", False)
    rows = [_row(sku="B0NEW", name="Steel Cup 500ml",
                 slow={"title": "ACME Steel Cup", "brand": "ACME"})]
    with caplog.at_level("WARNING"):
        assert mi.title_intents(rows) == []
    assert any("标题维护已整路停闸" in r.getMessage() for r in caplog.records)   # 不许静默
    monkeypatch.setattr(mi, "TITLE_SYNC", True)
    assert [i["sku"] for i in mi.title_intents(rows)] == ["B0NEW"]        # 开关一翻就回来


def test_executor_neither_claims_titles_nor_hides_the_held_count(monkeypatch):
    """只停生成侧不够:库里已有的 suggested title 行下一轮照样会被领走提交。
    执行件领取集要剔掉 title,留在 suggested 的条数要进摘要;改价/改库存照常。"""
    monkeypatch.setattr(mi, "TITLE_SYNC", False)
    seen: dict = {}
    calls = _wire(monkeypatch, [])
    monkeypatch.setattr(mw.dispositions, "claim",
                        lambda conn, actions=None: seen.setdefault("actions", actions) and [])
    monkeypatch.setattr(mw.dispositions, "count_open_action",
                        lambda conn, action, status="suggested": 37 if action == "title" else 0)
    out = mw.run({"execute": True})
    assert "title" not in seen["actions"]
    assert set(seen["actions"]) == {"price", "inventory"}
    assert "标题维护已整路停闸" in out and "37 条 title 建议留在 suggested" in out


def test_executor_claims_titles_again_when_on(monkeypatch):
    monkeypatch.setattr(mi, "TITLE_SYNC", True)
    seen: dict = {}
    _wire(monkeypatch, [])
    monkeypatch.setattr(mw.dispositions, "claim",
                        lambda conn, actions=None: seen.setdefault("actions", actions) and [])
    out = mw.run({"execute": True})
    assert set(seen["actions"]) == set(mw.dispositions.MAINT_ACTIONS)
    assert "标题维护已整路停闸" not in out
