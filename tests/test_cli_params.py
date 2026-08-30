"""cli.py 的 -p 解析与输出回归。

两件事:
· **粘贴事故不许把 dry-run 变成真跑** —— 既要抓住开关被吞进参数值的情况,
  又要保证抓住之后是报错、不是"帮你"重新解释;
· **摘要在终端上只出现一次,但日志文件里留全文** —— 两个需求都要满足。
"""

import logging

import pytest

import cli

# 串联之后 _build_params 收 (pairs, steps) 并返回 {工作流名: params}。
# 这些用例只关心单跑那一档,包一层保持原来的读法。
_W = "zz"


def _params(pairs):
    return cli._build_params(pairs, [_W])[_W]


def test_plain_pairs():
    assert _params(["a=1", "b=x y"]) == {"a": "1", "b": "x y"}
    assert _params(["as_of=2026-08-16"]) == {"as_of": "2026-08-16"}


def test_missing_equals_is_rejected():
    with pytest.raises(SystemExit):
        _params(["justakey"])


@pytest.mark.parametrize("sep", [" ", " ", "\t", "　"])
def test_swallowed_execute_flag_is_refused(sep):
    """`-p k=v<分隔符>--execute` 整串进了 v —— 成因是分隔符不是普通空格。

    实例(2026-08-16):从聊天窗口复制 `-p from_csv=...csv --execute`,
    中间是不换行空格,shell 不分词,于是 `--execute` 成了路径的一部分。
    表现是"文件不存在:<路径> --execute",要人从错误信息里反推,而且
    **命令看起来跑了但其实还是 dry-run**。
    """
    with pytest.raises(SystemExit) as e:
        _params([f"from_csv=/tmp/x.csv{sep}--execute"])
    assert "--execute" in str(e.value) and "没有执行" in str(e.value)


def test_a_swallowed_flag_is_never_silently_promoted():
    """⚠ 抓到了也**不许**替人把它当成开关 —— 那等于让粘贴事故触发真跑。

    这条盯的是一个诱人的"顺手修好":检测到 --execute 就设 execute=True。
    危险工作流的真跑开关必须是人显式敲进去的。
    """
    with pytest.raises(SystemExit):
        _params(["k=v --execute"])          # 只能抛,不能返回 dict


def test_values_that_merely_contain_dashes_are_fine():
    """别误伤:值里带 `--` 但不是已知开关的,照常放行。"""
    assert _params(["note=a--b", "flag=--verbose"]) == {
        "note": "a--b", "flag": "--verbose"}


# ── 输出:摘要只上屏一次,但日志文件留全文 ──────────────────────────────

def _rec(**extra):
    r = logging.LogRecord("cli", logging.INFO, __file__, 1, "x", None, None)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def test_file_only_records_are_kept_off_the_screen():
    f = cli._NotOnScreen()
    assert f.filter(_rec()) is True                    # 普通记录照常上屏
    assert f.filter(_rec(file_only=True)) is False     # 摘要那条不上屏


def test_summary_still_reaches_the_log_file(tmp_path, capsys):
    """⚠ 只是"别刷屏"的话,把 logger.info 删掉最省事 —— 但那样日志文件里
    就没有摘要了,事后再也答不出"那次到底输出了什么"。这条盯住那个偷懒解法。
    """
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    for h in root.handlers[:]:
        root.removeHandler(h)
    try:
        # 串联之后:根 logger 只挂屏幕 handler,每步的文件 handler 由
        # _log_to 挂/摘 —— 两段都要在,少一段就是"日志文件是空的"或"屏幕刷两遍"
        cli._setup_logging(tmp_path)
        with cli._log_to("t", tmp_path):
            cli.logger.info("workflow %s 成功:\n%s", "t", "第一行\n第二行",
                            extra={"file_only": True})
            for h in root.handlers:
                h.flush()
            text = (tmp_path / "t.log").read_text(encoding="utf-8")
        assert "第一行" in text and "第二行" in text        # 文件里有全文
        assert "第二行" not in capsys.readouterr().err     # 屏幕上没有
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.handlers[:], root.level = saved


def test_unconfigured_webhook_logs_only_the_first_line(monkeypatch, caplog):
    """通知降级成"仅记日志"时**不许把整段摘要再抄一遍**。

    cli 传进来的是完整摘要,而它此刻已经打到终端了 —— 再吐一份就是同一屏
    文字出现两次(2026-08-16 所有者:"所有的命令都是这样子的")。
    全文进不进日志由 cli 决定,通知这一层只负责说"没发出去"。
    """
    from api import feishu
    monkeypatch.setattr(feishu.resources, "feishu_webhook_url", lambda: None)
    with caplog.at_level(logging.INFO, logger="api.feishu"):
        assert feishu.notify("✅ claim_audit 成功\n第二行\n第三行") is False
    text = caplog.text
    assert "claim_audit 成功" in text                  # 说清楚是哪条通知没发出去
    assert "第二行" not in text and "第三行" not in text


# ── .env 重复键守卫(2026-08-17 生产实证)────────────────────────────────────

def test_duplicate_env_keys_are_detected():
    """⚠ dotenv 顺序读取、后者覆盖前者,**且不报重复**。

    所有者的 .env 里 FEISHU_RISK_PT_WIKI_TOKEN 等四个键各出现两次,
    第二次是模板留下的空值,于是那四张表全读成"未登记" —— 值明明就在
    文件里、肉眼看过去也在,报错却说"尚未登记"。这种错自己找不到。
    """
    dup = cli.dup_env_keys("""
# 沃尔玛类目
FEISHU_RISK_PT_WIKI_TOKEN=UKdZwr7JqiO6UckjSnPcwTCgnvd
FEISHU_BRAND_SHEET_ID=jF8dOw
OTHER=1

# listing 风控两张只读源(模板留下的空壳)
FEISHU_RISK_PT_WIKI_TOKEN=
FEISHU_BRAND_SHEET_ID=
""")
    assert set(dup) == {"FEISHU_RISK_PT_WIKI_TOKEN", "FEISHU_BRAND_SHEET_ID"}
    # 顺序必须保留 —— 判"最后一个是不是空值"全靠它
    assert dup["FEISHU_RISK_PT_WIKI_TOKEN"] == \
        ["UKdZwr7JqiO6UckjSnPcwTCgnvd", ""]
    assert "OTHER" not in dup


def test_comments_and_blank_lines_do_not_count():
    assert cli.dup_env_keys("# A=1\n# A=2\n\nA=3\n") == {}


def test_export_prefix_is_normalized():
    """`export FOO=1` 与 `FOO=2` 是同一个键 —— 不归一就漏报。"""
    assert list(cli.dup_env_keys("export FOO=1\nFOO=2\n")) == ["FOO"]


def test_warn_says_which_direction_it_broke(capsys, tmp_path):
    """空值覆盖有值 = 静默失效,必须与"重复但都有值"区分开说。"""
    p = tmp_path / ".env"
    p.write_text("A=有值\nB=1\nA=\n", encoding="utf-8")
    cli._warn_dup_env(p)
    err = capsys.readouterr().err
    assert "最后一次是空值" in err and "删掉那行空的" in err
    assert "B" not in err


def test_warn_is_silent_when_env_is_missing(capsys, tmp_path):
    cli._warn_dup_env(tmp_path / "nope.env")
    assert capsys.readouterr().err == ""

def test_failure_notification_carries_whole_message_not_last_line():
    """失败通知必须带整条消息,**不是** traceback 的最后一行。

    2026-08-17 生产实见(catalog_sync 一家店 400),飞书收到的整条通知是:

        ❌ catalog_sync 失败
        For more information check: https://developer.mozilla.org/…/400)

    —— 整条消息里最没用的那一行。取"最后一行"只对**单行**异常成立;而本项目
    的失败摘要恰恰是多行的(catalog_sync 明写「整体判失败,飞书通知会带明细」),
    那份明细全长在前面几行,被切光了;httpx 的错误自身又是两行、第二行是 MDN
    链接,于是越是需要细节的失败,通知越是只剩一句废话。
    """
    msg = ("catalog_sync:41/42 店完成,入库 30611 行,本轮缺席标记 2149 行\n"
           "删除核验:生效 2241\n"
           "失败:谭总10(Client error '400 Bad Request' for url '…/v3/token'\n"
           "For more information check: https://developer.mozilla.org/…/400)")
    brief = cli._err_brief(RuntimeError(msg))
    assert brief.startswith("catalog_sync:41/42 店完成")   # 明细在,而且打头
    assert "谭总10" in brief                                # 哪家店坏了看得见
    assert brief != "For more information check: https://developer.mozilla.org/…/400)"


def test_err_brief_truncates_but_says_so():
    brief = cli._err_brief(RuntimeError("x" * (cli._ERR_MAX + 500)))
    assert len(brief) < cli._ERR_MAX + 200
    assert "已截断" in brief and "RuntimeError" in brief


def test_err_brief_empty_message_falls_back_to_type():
    """消息为空时不能发一条空通知——至少要说清是什么异常。"""
    assert cli._err_brief(KeyError()) == "KeyError"
    assert cli._err_brief(RuntimeError("")) == "RuntimeError"


def test_non_dangerous_workflows_must_honor_dry_run():
    """DANGEROUS=False 的工作流读 execute 时**必须同时认 dry_run**。

    cli._run_step 对 DANGEROUS=False 恒给 `execute=True`(缺省即真跑,
    2026-08-16 定稿),`--dry-run` 只体现在 `params["dry_run"]`。只判 execute
    的工作流,敲了 `--dry-run` 照样写库、而且**报成功** —— 这正是
    audit_reason_backfill / sources_backfill / category_blacklist_import
    三个一次性刷全表的工作流最不能出的错(没有第二次机会)。
    """
    import pathlib
    import re

    bad = []
    for f in sorted(pathlib.Path("workflows").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "DANGEROUS = False" not in src:
            continue
        for m in re.finditer(r'^\s*execute\s*=\s*(.+)$', src, re.M):
            expr = m.group(1)
            if 'params.get("execute")' in expr and "dry_run" not in expr:
                bad.append(f"{f.name}: {expr.strip()}")
    assert not bad, ("这些 DANGEROUS=False 工作流的 --dry-run 是坏的(会照写不误):"
                     + "; ".join(bad))
