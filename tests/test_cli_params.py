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
