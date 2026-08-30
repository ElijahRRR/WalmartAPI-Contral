"""returns_sync 的店级重试标准落地 + 摘要首行字样(2026-08-27 收口后补钉)。

端到端与零店闸在 tests/test_order_workflows.py(mock 沃尔玛 + 假 PG),这里只钉
**收口之后唯一没有测试看着的那部分**:骨架搬进 `services/store_retry.fan_out`
之后,本件与 catalog_sync/order_sync 的差别只剩摘要首行的**字样**——
「该店本轮售后缺口由下轮窗口覆盖」是 `notify_fmt.absent_tail` 模板拼不出来的
一句(模板固定拼「,本轮不炸链(处置)」),收口时据此**不接线**。
没有这几条,下一个做"统一措辞"的人改掉它不会有任何东西报警。
"""

import time

import pytest

from api import _client
from services import notify_fmt as nf


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """串行补试每店前有 `_client.backoff(0)` 抖动等待(1~2s),单测不等。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _wire(monkeypatch, names, behave):
    """输入:店名表 + 单店行为函数 → 输出:(returns_sync 模块, 调用流水)。"""
    from workflows import returns_sync
    calls: list = []

    def one(store, created_start):
        name = store["name"]
        calls.append(name)
        got = behave(name, calls.count(name))
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(returns_sync.stores_svc, "load_stores",
                        lambda filter_names=None: [
                            {"name": n, "client_id": f"c{n}",
                             "client_secret": "s", "proxy": "http://p:1"}
                            for n in names])
    monkeypatch.setattr(returns_sync, "_sync_one_store", one)
    monkeypatch.setattr(returns_sync.order_center, "push_after",
                        lambda spec, days=90: "订单中心投影:桩")
    return returns_sync, calls


def _ok(name):
    return {"store": name, "returns": 1, "lines": 3, "dropped": 0}


def _socks(name):
    from socksio.exceptions import ProtocolError
    return ProtocolError("Malformed reply")      # 08-26 事故同款


def test_absent_store_named_in_first_line_with_this_file_s_own_wording(monkeypatch):
    """标准②③:补试仍失败 ⇒ **不炸整轮**,缺席店点名进摘要**首行**。

    ⚠ 尾巴字样**逐字**钉死:本件说「该店本轮售后缺口由下轮窗口覆盖」——
    售后是按创建时间窗口全量重拉 + 幂等 upsert,缺口下一轮自然补上,
    没有 catalog_sync 那种"下游按水位避让、链尾重赛"的处置。
    进飞书通知的字不许在收口/统一措辞时被改掉(规矩 3「每条 ⚠ 自带处置」,
    各链的处置本来就不同)。
    """
    returns_sync, calls = _wire(
        monkeypatch, ["好店", "断店"],
        lambda n, c: _socks(n) if n == "断店" else _ok(n))

    out = returns_sync.run({})                    # 不抛 = 不炸链
    first = out.splitlines()[0]
    assert "1/2 店完成" in first
    assert (";⚠ 缺席 1 店:断店(代理波动)"
            "——已串行补试仍失败,该店本轮售后缺口由下轮窗口覆盖") in first
    assert calls.count("断店") == 2               # 首轮 + 串行补试各一次


def test_first_line_tail_is_not_the_absent_tail_template(monkeypatch):
    """P2a 收口的 refused 理由,写成可执行证据(2026-08-27)。

    `notify_fmt.absent_tail` 的模板恒拼「,本轮不炸链({处置})」,任何 tail
    取值都带这两段字面量,拼不出本件现行尾巴 —— 所以本件**不接**那个积木。
    哪天模板改成能容下这句(比如把中段也参数化),这条会红,那时才是接线的时候。
    """
    absent = [("断店", "代理波动")]
    tpl = nf.absent_tail(absent, "", tail="下轮窗口覆盖")
    assert ",本轮不炸链(" in tpl                  # 模板的硬拼段
    assert "该店本轮售后缺口由下轮窗口覆盖" not in tpl

    returns_sync, _ = _wire(monkeypatch, ["好店", "断店"],
                            lambda n, c: _socks(n) if n == "断店" else _ok(n))
    first = returns_sync.run({}).splitlines()[0]
    assert ",本轮不炸链(" not in first


def test_second_pass_reruns_the_same_function_and_recovers(monkeypatch):
    """标准①:失败店跑完别人后**串行补试一遍**,救回的照常入账。

    补试跑的是第一轮**同一个** `_sync_one_store`(单一落地路径纪律)——
    重试轮另写简化版迟早漏掉一半落地动作。
    """
    returns_sync, calls = _wire(
        monkeypatch, ["好店", "抖店"],
        lambda n, c: _socks(n) if (n == "抖店" and c == 1) else _ok(n))

    out = returns_sync.run({})
    assert "2/2 店完成" in out and "售后行入库 6" in out
    assert "⚠ 缺席" not in out                    # 救回了就不点名
    assert calls.count("抖店") == 2               # 不多试第三次


def test_dead_store_is_skipped_not_retried(monkeypatch):
    """凭证死是确定性的:跳过全店、**不补试**,以「凭证失效跳过」进摘要。

    一家店的凭证坏掉不拖垮整轮(41/42 照常报成功),但它每轮可见,不会
    烂在那没人管。
    """
    returns_sync, calls = _wire(
        monkeypatch, ["好店", "坏店"],
        lambda n, c: _client.StoreDeadError(n, 400) if n == "坏店" else _ok(n))

    out = returns_sync.run({})
    assert "1/2 店完成" in out
    assert "凭证失效跳过:坏店" in out
    assert calls.count("坏店") == 1               # 补试不碰死店
    assert "⚠ 缺席" not in out                    # 凭证死走 dead 口径,不算缺席


def test_scale_gate_stops_the_second_pass_and_says_so_in_the_summary(monkeypatch):
    """规模闸:失败店数超 max(3, 总数//5) 判系统性故障,一家都不补,止损说明进摘要。

    ⚠ 已知不一致,留给所有者拍板(P2a 未授权改字样):闸命中时本件首行仍说
    「已串行补试仍失败」,而 catalog_sync/order_sync 会改说「超补试规模闸未
    补试(疑似系统性故障)」;止损原文在本件是**第二行**,而链通知对成功步骤
    只发首行(cli._fold_success)—— 于是闸命中这件事到不了飞书。
    本条只钉「不炸轮 + 止损原文确实进了摘要 + 一家都没补试」,故意不钉那半句
    字样,免得把这个不一致当成"本来就该这样"焊死。
    """
    names = ["好店"] + [f"断{i}" for i in range(5)]
    returns_sync, calls = _wire(
        monkeypatch, names,
        lambda n, c: _socks(n) if n.startswith("断") else _ok(n))

    out = returns_sync.run({})                    # 不抛
    assert "1/6 店完成" in out
    assert "超过补试规模闸" in out and "疑似系统性故障" in out
    assert all(calls.count(f"断{i}") == 1 for i in range(5))   # 一家都不补试
