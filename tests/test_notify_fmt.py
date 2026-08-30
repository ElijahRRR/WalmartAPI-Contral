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
    """⚠ 文案必须是 `_run_step` 的**生产真实形状** `f"{mode}{name} 成功\\n{summary}"`。

    2026-08-30 之前这里喂的是「a 完成:入库 100 行\\n明细一」—— 摘要恰好落在
    第一行,于是"折叠只取第一行"的写法在测试里怎么看都对,生产里折出来的却是
    「order_audit 成功」这句废话,**摘要一个字进不了飞书**。fixture 与生产形状
    不一致,掩盖的就是这种缺陷。
    """
    b_text = ("b 成功\nb 的摘要第一行\n第二行明细\n第三行明细" if status_b == "success"
              else "b 失败\n第二行明细\n第三行明细")
    return [("a", "success", "a 成功\na 完成:入库 100 行\n明细一\n明细二"),
            ("b", status_b, b_text),
            ("c", "success", "c 成功\nc 完成")]


def test_chain_folds_successful_steps_to_one_line():
    """七步链每步铺全文 = 二十来行没层次的文字,人第三天就不看了。"""
    out = cli._chain_text(_STEPS, _res(), "success")
    assert out.splitlines() == [
        "✅ 链 [a → b → c]",
        "✅ a:a 完成:入库 100 行",
        "✅ b:b 的摘要第一行",
        "✅ c:c 完成"]
    assert "明细一" not in out           # 全文在 ops.runs 与日志里,不在通知里


def test_chain_fold_keeps_the_summary_not_the_word_success():
    """折叠丢摘要的回归钉:链通知里出现的必须是数字,不是「x 成功」那句废话。"""
    out = cli._chain_text(["a"], [("a", "success", "a 成功\n入库 100 行 / 74 店")],
                          "success")
    assert "✅ a:入库 100 行 / 74 店" in out
    assert "a 成功" not in out


def test_chain_fold_keeps_the_dry_run_marker():
    """空跑报成功比误跑更难发现 —— [DRY-RUN] 前缀折叠后必须还在。"""
    out = cli._chain_text(["a"], [("a", "success", "[DRY-RUN] a 成功\n将提交 3 件")],
                          "success")
    assert "✅ [DRY-RUN] a:将提交 3 件" in out


def test_chain_fold_falls_back_when_text_has_no_summary_body():
    """没有换行 = 不是 `_run_step` 那个形状,原样折叠(别折出个空行)。"""
    assert cli._fold_success("a", "a 只有一行") == "a 只有一行"
    assert cli._fold_success("a", "a 成功\n") == "a 成功"


def test_chain_expands_the_failed_step_in_full():
    """真正要读的是失败那一步的明细,所以只有它铺开。"""
    out = cli._chain_text(_STEPS, _res("failed"), "failed")
    assert out.startswith("❌ 链 [a → b → c]")
    assert "❌ b" in out
    assert "   第二行明细" in out and "   第三行明细" in out
    assert "✅ a:a 完成:入库 100 行" in out    # 成功的仍然折叠
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
