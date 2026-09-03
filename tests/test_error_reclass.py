"""error_reclass(存量报错按新码回填)的守门测试。

这条工作流的三个判据面各钉一遍:
  ① **原文四级优先**(全文优先于样本)—— 拿 200 字符样本判出来的码是下限,
     优先序错了会让一大批行悄悄按截断文本定案;
  ② **参数宁炸不吞** + `--dry-run` 真的不写;
  ③ **它不许改判定行为**:`category` 不动、不删行、只写 `taxonomy_*`。
"""

import pytest

from registry import resources
from services import error_taxonomy as et
from workflows import error_reclass as wf


# ── ① 原文四级优先(纯函数,拿假数据就能测)────────────────────────────────

def test_原文优先序_全文压过样本():
    """⚠ 优先序是判据的一部分:`self` 那一级是**截 200 字符的样本**,
    判据串可能被切掉(如 `appropriate product type selected` 在 200 字符外)。
    所以只要外源有全文,一律用全文。"""
    rec = {"B0AAA": "records 全文"}
    ev = {"B0AAA": "events 病历", "B0BBB": "events 病历"}
    it = {"SKU-C": "items 当前值"}
    # 1. records 压过其余全部
    assert wf.pick_source("B0AAA", "样本", "SKU-C", rec, ev, it) == \
        ("records 全文", "records")
    # 2. 没有 records 用 events
    assert wf.pick_source("B0BBB", "样本", "SKU-C", rec, ev, it) == \
        ("events 病历", "events")
    # 3. 前两级都没有,才按 src_sku 去 items
    assert wf.pick_source("B0CCC", "样本", "SKU-C", rec, ev, it) == \
        ("items 当前值", "items")
    # 4. 三级外源全空 → 退到本表样本
    assert wf.pick_source("B0DDD", "样本", None, rec, ev, it) == ("样本", "self")


def test_原文四处都没有_不猜():
    """四级全空 → `('', 'none')`;调用方据此把 `taxonomy_code` 留 NULL。
    编一个码出来会一路进报表,而没有任何东西会红。"""
    assert wf.pick_source("B0ZZZ", None, None, {}, {}, {}) == ("", "none")
    assert wf.pick_source("B0ZZZ", "   ", "SKU", {}, {}, {}) == ("", "none")


def test_原文空串不算命中():
    """外源列可能是空串(不是 NULL):空串当没有,继续往下一级找。"""
    assert wf.pick_source("B0AAA", "样本", None,
                          {"B0AAA": ""}, {"B0AAA": "病历"}, {}) == ("病历", "events")


# ── ② 归类与政策名 ──────────────────────────────────────────────────────────

def test_classify_政策名join不上就不写():
    """⚠ 与 audit_l3 的 `policy` 解析同一条纪律:猜出来的政策名会一路进报表
    与申诉口径,而没有任何东西会红。join 不上 → None。"""
    text = "Prohibited Products Policy: Alcohol."
    code, name, term = wf.classify(text, ["Alcohol"])
    assert (code, name, term) == ("POLICY", "Alcohol", None)
    code2, name2, _ = wf.classify(text, ["Animals"])   # 表里没有 Alcohol
    assert code2 == "POLICY" and name2 is None


def test_classify_带回OTHER的显式词条_拉黑判据靠它():
    """⚠ `OTHER` 是混装桶:所有者只让 business decision / trust & safety
    算永久拉黑,`currently under review` 是自愈态不算 —— 光有主码判不了,
    必须把**赢下主码那个原子**命中的词条一起带回来(落 `taxonomy_term`)。"""
    from services import error_taxonomy as et
    for text, want, permanent in (
            ("unpublished due to a Walmart business decision.",
             "business decision", True),
            ("removed by trust & safety review", "trust & safety", True),
            ("This item is currently under review.",
             "currently under review", False)):
        code, _name, term = wf.classify(text, [])
        assert code == "OTHER" and term == want, text
        assert et.is_permanent(code, term) is permanent, text


def test_classify_旧B里的那几种真的会分开():
    """所有者要的就是这个:旧 B(禁售)一个桶,新码分得开。"""
    cases = {
        "This item is a prohibited product. Prohibited Products Policy: Alcohol.":
            "POLICY",
        "may be a prohibited product. Please make sure the appropriate product "
        "type selected for this item.": "PT_WRONG",
        "This item is subject to a cpsc recall.": "RECALL",
        "not eligible for appeal": "PROHIBITED_FINAL",
    }
    for text, want in cases.items():
        assert wf.classify(text, ["Alcohol"])[0] == want, text


# ── ③ 参数与安全闸 ─────────────────────────────────────────────────────────

def test_参数宁炸不吞():
    """打错的参数名被静默吞掉 = 人以为按自己说的跑完了,而实际跑的是缺省口径,
    摘要还长得一模一样(与 product_audit 同款纪律)。"""
    with pytest.raises(ValueError, match="未识别参数"):
        wf._parse({"scoope": "records"})
    with pytest.raises(ValueError, match="scope 只能是"):
        wf._parse({"scope": "everything"})
    with pytest.raises(ValueError, match="chunk 要正整数"):
        wf._parse({"chunk": 0})


def test_dry_run_必须自己认():
    """⚠ `DANGEROUS=False` 时 cli 恒给 `execute=True` —— 漏了那一句
    `--dry-run` 会直接把存量刷了,而且报成功。"""
    assert wf._parse({"execute": True})[3] is True
    assert wf._parse({"execute": True, "dry_run": True})[3] is False


def test_limit缺省不限量():
    """0 = 不限量(与 product_audit 2026-09-03 定稿同口径)。"""
    assert wf._parse({})[4] == 0
    assert wf._parse({"limit": "500"})[4] == 500


# ── ④ 它不许改判定行为 ──────────────────────────────────────────────────────

def test_口径常量不是自己抄的一份():
    """双轨禁止:与报告工作流读同一份 `services/error_taxonomy` 的常量。"""
    assert wf.NOT_A_PRODUCT_BAN is et.NOT_A_PRODUCT_BAN
    for code in wf.NOT_A_PRODUCT_BAN:
        assert code in resources.ERROR_CATEGORY_CODES, code


def test_只写taxonomy列_不碰category也不删行():
    """⚠ 这条工作流的全部承诺就在这里:老列与判定行为一个字不动。

    黑名单是「一次入选、永久禁止」,批量放行是破坏性动作 —— 顺手接上去
    就是拿 4 万多个 ASIN 的拦截行为做一次无人裁决的变更。
    """
    import inspect
    src = inspect.getsource(wf)
    assert "SET taxonomy_code" in src
    for forbidden in ("SET category", "DELETE FROM", "DROP ", "TRUNCATE"):
        assert forbidden not in src, forbidden
    # 两条 UPDATE 都必须盖版本号(不盖 = 下次重跑又是全量,永远跑不完)
    assert src.count("taxonomy_version = %(ver)s") == 2
