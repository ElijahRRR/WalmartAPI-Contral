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
