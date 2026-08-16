"""cli.py 的 -p 解析回归。

这里只有一件事要防:**粘贴事故把 dry-run 变成真跑**。所以既要抓住开关
被吞进参数值的情况,又要保证抓住之后是报错、不是"帮你"重新解释。
"""

import pytest

import cli


def test_plain_pairs():
    assert cli._build_params(["a=1", "b=x y"]) == {"a": "1", "b": "x y"}
    assert cli._build_params(["as_of=2026-08-16"]) == {"as_of": "2026-08-16"}


def test_missing_equals_is_rejected():
    with pytest.raises(SystemExit):
        cli._build_params(["justakey"])


@pytest.mark.parametrize("sep", [" ", " ", "\t", "　"])
def test_swallowed_execute_flag_is_refused(sep):
    """`-p k=v<分隔符>--execute` 整串进了 v —— 成因是分隔符不是普通空格。

    实例(2026-08-16):从聊天窗口复制 `-p from_csv=...csv --execute`,
    中间是不换行空格,shell 不分词,于是 `--execute` 成了路径的一部分。
    表现是"文件不存在:<路径> --execute",要人从错误信息里反推,而且
    **命令看起来跑了但其实还是 dry-run**。
    """
    with pytest.raises(SystemExit) as e:
        cli._build_params([f"from_csv=/tmp/x.csv{sep}--execute"])
    assert "--execute" in str(e.value) and "没有执行" in str(e.value)


def test_a_swallowed_flag_is_never_silently_promoted():
    """⚠ 抓到了也**不许**替人把它当成开关 —— 那等于让粘贴事故触发真跑。

    这条盯的是一个诱人的"顺手修好":检测到 --execute 就设 execute=True。
    危险工作流的真跑开关必须是人显式敲进去的。
    """
    with pytest.raises(SystemExit):
        cli._build_params(["k=v --execute"])          # 只能抛,不能返回 dict


def test_values_that_merely_contain_dashes_are_fine():
    """别误伤:值里带 `--` 但不是已知开关的,照常放行。"""
    assert cli._build_params(["note=a--b", "flag=--verbose"]) == {
        "note": "a--b", "flag": "--verbose"}
