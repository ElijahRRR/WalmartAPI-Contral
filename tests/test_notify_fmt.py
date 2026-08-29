"""飞书通知排版规范回归(所有者定稿 2026-08-17)。

钉的不是"好看",是三条实质规矩:主指标恒打印、例外为 0 才省、
链通知折叠成功步骤。排版跑偏顶多难看,这三条跑偏会让人读不到该读的东西 ——
而**不看的通知等于没有通知**。
"""

import cli
from services import notify_fmt as nf


# ── 数字 ─────────────────────────────────────────────────────────────────────

def test_numbers_get_thousands_separators():
    assert nf.num(29860) == "29,860"
    assert nf.num(1853.33) == "1,853.33"
    assert nf.num(0) == "0"                 # 0 是 0,不是"—"
    assert nf.num(None) == "—" and nf.num("") == "—"
    assert nf.num(37, " 单") == "37 单"
    assert nf.money(1853.33) == "$1,853.33"
    assert nf.money(None) == "—"


def test_pct_with_zero_denominator_is_not_zero_percent():
    """分母为 0 报 0% 是撒谎:"一个都没成"与"压根没有"是两件事。"""
    assert nf.pct(3, 4) == "75.0%"
    assert nf.pct(0, 4) == "0.0%"
    assert nf.pct(0, 0) == "—"
    assert nf.pct(1, None) == "—"


# ── 规矩 2:主指标恒打印,例外为 0 才省 ──────────────────────────────────────

def test_metrics_are_always_printed_even_when_zero():
    """入库 0 行**是重要信息**,必须看得见 —— 这条不许跟着例外一起被抑制。"""
    out = nf.summary("订单同步", metrics=[("入库", 0), ("店铺", 74)])
    assert "· 入库 0" in out and "· 店铺 74" in out


def test_notes_disappear_when_zero_but_one_still_shows():
    """⚠ 抑制的判据是**恰好 0**,不是"看着不重要" —— 1 也要报(最怕静默)。"""
    out = nf.summary("上架", notes=[("风控拦截", 0), ("全局去重", 1),
                                    ("黑名单", 0), ("数据过滤", 2)])
    assert "风控拦截" not in out and "黑名单" not in out
    assert "· 全局去重 1" in out and "· 数据过滤 2" in out
    assert "—— 其中 ——" in out


def test_all_zero_notes_drop_the_whole_section():
    """一条例外都没有时连小标题都不留 —— 空节本身就是噪声。"""
    out = nf.summary("上架", metrics=[("提交", 12)],
                     notes=[("风控拦截", 0), ("黑名单", 0)])
    assert "其中" not in out
    assert out.splitlines() == ["上架", "· 提交 12"]


def test_warns_take_whole_lines_and_empty_ones_vanish():
    """warns 收 ⚠ 整行,空串自动消失 —— 方便写成条件表达式,不必先攒列表。"""
    out = nf.summary("上架", warns=["⚠ UPC 池只剩 300 个(补池:cli.py upc_sync)",
                                    "", "   "])
    assert out.splitlines() == ["上架", "⚠ UPC 池只剩 300 个(补池:cli.py upc_sync)"]


def test_head_puts_the_name_first():
    """飞书列表里同一时间好几条通知:先看见是谁,再看见成没成。"""
    assert nf.head("日报", "37 单 / $1,853.33", "2026-08-17") == \
        "日报 · 2026-08-17 | 37 单 / $1,853.33"
    assert nf.head("日报") == "日报"


# ── 规矩 5:链通知折叠成功步骤 ───────────────────────────────────────────────

_STEPS = ["a", "b", "c"]


def _res(status_b="success"):
    # 文本形状 = _run_step 真实产物:首行是 cli 自造的「名 成功」横幅,
    # 摘要从第二行起(2026-08-26 审计实见:旧夹具没带横幅,正好掩盖了
    # 折叠取错行的 bug —— 生产里链通知只剩横幅,缺席点名整条被吃)
    return [("a", "success", "a 成功\na:入库 100 行;⚠ 缺席 1 店:X(代理波动)\n明细一\n明细二"),
            ("b", status_b, "b 成功\n窗口 45 天,入库 7 行\n第二行明细\n第三行明细"),
            ("c", "success", "[EXECUTE] c 成功\n提交 feed 2 个")]


def test_chain_folds_successful_steps_to_summary_first_line():
    """七步链每步铺全文 = 二十来行没层次的文字,人第三天就不看了。
    折叠取的必须是**工作流摘要的首行**(缺席点名在那里,标准③),
    不是 cli 的「名 成功」横幅;[EXECUTE] 标记从横幅继承。"""
    out = cli._chain_text(_STEPS, _res(), "success")
    assert out.splitlines() == [
        "✅ 链 [a → b → c]",
        "✅ a:入库 100 行;⚠ 缺席 1 店:X(代理波动)",   # 摘要已带名:不重复加
        "✅ b:窗口 45 天,入库 7 行",                     # 摘要没带名:补上
        "✅ [EXECUTE] c:提交 feed 2 个"]
    assert "明细一" not in out           # 全文在 ops.runs 与日志里,不在通知里
    assert "a 成功" not in out           # 横幅不进链通知(它吃掉缺席点名)


def test_chain_expands_the_failed_step_in_full():
    """真正要读的是失败那一步的明细,所以只有它铺开。"""
    out = cli._chain_text(_STEPS, _res("failed"), "failed")
    assert out.startswith("❌ 链 [a → b → c]")
    assert "❌ b" in out
    assert "   第二行明细" in out and "   第三行明细" in out
    assert "✅ a:入库 100 行" in out         # 成功的仍然折叠(摘要首行)
    assert "全文见 ops.runs" in out


def test_chain_expands_skipped_steps_so_the_stop_point_is_obvious():
    """跳过的步骤也铺开:它写的是"上游谁没成功",那正是要看的那句。"""
    res = [("a", "success", "a 完成"),
           ("b", "failed", "b 炸了"),
           ("c", "skipped", "c:上游 b 未成功,未执行")]
    out = cli._chain_text(_STEPS, res, "failed")
    assert "⏭ c" in out and "上游 b 未成功" in out


def test_single_run_notification_shape_is_untouched():
    """⚠ 单跑的通知形态**逐字不动** —— 人和告警规则都认那个格式。

    这条用例守的是"改链通知时别顺手把单跑也改了":单跑走的是 `_notify(f"{icon}
    {摘要全文}")`,压根不经过 `_chain_text`。
    """
    import inspect
    src = inspect.getsource(cli._run_chain if hasattr(cli, "_run_chain")
                            else cli.main)
    assert "_ICON[results[0][1]]" in src        # 单跑那一行的原样写法还在
    assert "逐字一致" in src


def test_first_line_of_skips_leading_blanks():
    assert nf.first_line_of("\n\n  真正的第一行  \n后面") == "真正的第一行"
    assert nf.first_line_of("") == "" and nf.first_line_of(None) == ""


# ── 收口的两段尾巴:feed 四档计数 / 缺席点名 ────────────────────────────────

def test_feed_outcome_tail_prints_submitted_and_drops_zero_exceptions():
    """规矩 2 原样落地:提交 0 也要看见,例外恰好 0 才省。"""
    t = nf.feed_outcome_tail
    assert t(0, 0, 0, 0, failed_word="提交被拒") == "提交 0"
    assert t(12, 3, 0, 0, failed_word="提交被拒") == "提交 12,在途防重跳过 3"
    assert t(12, 0, 1, 2, failed_word="提交被拒") == (
        "提交 12,⚠ 提交被拒 1(查日志),"
        "⚠ 结局不确定留 pending 2(待对账)")


def test_feed_outcome_tail_keeps_each_owners_failed_word():
    """「提交被拒」/「提交失败」是三个执行件各自的现行字样。进飞书通知的
    字样收口时逐字保留,不由积木替所有者统一措辞。"""
    assert "⚠ 提交被拒 1(查日志)" in nf.feed_outcome_tail(
        0, 0, 1, 0, failed_word="提交被拒")
    assert "⚠ 提交失败 1(查日志)" in nf.feed_outcome_tail(
        0, 0, 1, 0, failed_word="提交失败")


def test_feed_outcome_tail_reproduces_the_three_live_lines():
    """与三个执行件现行成品逐字对拍(接线后摘要不许变形)。"""
    n = {"submitted": 12, "dedup": 3, "failed": 1, "unknown": 2}
    rest = ",在途防重跳过 3,⚠ {} 1(查日志),⚠ 结局不确定留 pending 2(待对账)"

    maint = nf.feed_outcome_tail(n["submitted"], n["dedup"], n["failed"],
                                 n["unknown"], failed_word="提交被拒")
    assert f"  店A:标题 feed {maint}" == \
        "  店A:标题 feed 提交 12" + rest.format("提交被拒")     # maintenance
    assert f"  店A:跟卖{maint}" == \
        "  店A:跟卖提交 12" + rest.format("提交被拒")           # match_listing

    ppc = nf.feed_outcome_tail(n["submitted"], n["dedup"], n["failed"],
                               n["unknown"], failed_word="提交失败")
    assert f"  店A:删除{ppc}" == \
        "  店A:删除提交 12" + rest.format("提交失败")   # problem_product_cleanup


def test_absent_tail_matches_the_live_first_line():
    """与 catalog_sync 现行首行逐字一致;一家都不缺席时整段消失。"""
    assert nf.absent_tail([], "", tail="下游按水位避让,链尾重赛") == ""
    assert nf.absent_tail([("A085", "代理波动"), ("81张三", "网络未达")], "",
                          tail="下游按水位避让,链尾重赛") == (
        ";⚠ 缺席 2 店:A085(代理波动),81张三(网络未达)"
        "——已串行补试仍失败,本轮不炸链(下游按水位避让,链尾重赛)")


def test_absent_tail_says_gate_held_the_batch_instead_of_retried():
    """规模闸拦下整批时不许还说"已串行补试仍失败"——那是两种不同的处境。"""
    got = nf.absent_tail([("A085", "代理波动")], "⚠ 补试规模闸:失败店过半",
                         tail="下游按水位避让,链尾重赛")
    assert "——超补试规模闸未补试(疑似系统性故障)" in got
    assert "已串行补试仍失败" not in got


def test_absent_tail_lets_each_chain_bring_its_own_next_step():
    """规矩 3「每条 ⚠ 自带处置」:目录链按水位避让、订单链下轮自然重拉,
    两句是各链自己的真实处置,不许收口时统一成一句。"""
    a = [("A085", "代理波动")]
    assert nf.absent_tail(a, "", tail="下轮整点自然重拉").endswith(
        ",本轮不炸链(下轮整点自然重拉)")
    assert nf.absent_tail(a, "", tail="下游按水位避让,链尾重赛").endswith(
        ",本轮不炸链(下游按水位避让,链尾重赛)")
