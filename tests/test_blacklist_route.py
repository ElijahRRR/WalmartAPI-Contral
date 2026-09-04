"""blacklist_route(存量黑名单按新码路由)的守门测试。

这条工作流是**不可逆**的(DELETE 数万行),所以钉的全是"别删错":
  ① 留/删的判据与 `error_taxonomy.is_permanent` 同义,SQL 与 Python 不许漂;
  ② 所有者三条裁决逐条钉:七个永久码 / OTHER 只认两个词条 / 判不出的**留着**;
  ③ 没回填过的行一条都不动;`--dry-run` 一行都不删。
"""

import inspect

import pytest

from services import error_taxonomy as et
from workflows import blacklist_route as wf


# ── ① 判据同义:SQL 那段 `_KEEP` 与 Python 的 `keeps` ────────────────────────

def test_keep判据与is_permanent同义():
    """⚠ 两处必须同义:漂了就是"报告说要删 A、实际删了 B",而两边看着都正常。"""
    for code in et.PERMANENT_CODES:
        assert wf.keeps(code, None) is True, code
    for code in ("PT_WRONG", "CONTENT", "INFO", "PRICE",
                 "SYSTEM", "STAGE", "EXPIRED", "SPECIAL"):
        assert wf.keeps(code, None) is False, code


def test_SQL那段KEEP三段齐_少一段就是删错一大片():
    """`_KEEP` 是判据搬进 SQL 的那一份:三段(永久码 / OTHER 词条 / NULL)
    少写一段就会多删一大片,而且不报错。"""
    keep = wf._KEEP
    assert "taxonomy_code = ANY(%(perm)s)" in keep
    assert "taxonomy_code = 'OTHER'" in keep and "%(terms)s" in keep
    assert "taxonomy_code IS NULL" in keep
    # 删的那条 SQL 必须是 `NOT <KEEP>`,不许另写一份反条件
    assert "NOT " + keep in wf._SQL_DOOMED


# ── ② 所有者三条裁决 ────────────────────────────────────────────────────────

def test_裁决一_七个永久码留下_PT_WRONG不在其中():
    """所有者 2026-09-03 逐码定的七个(裁决表 §十二)。
    ⚠ `PT_WRONG` 绝不许混进来 —— 那是"把 product type 选对",修法不是禁令,
    存量里 40,825 条就是被它误拉黑的。"""
    assert set(et.PERMANENT_CODES) == {
        "PROHIBITED_FINAL", "POLICY", "IP", "BRAND", "RECALL", "FLAGGED", "GATED"}
    assert "PT_WRONG" not in et.PERMANENT_CODES


def test_裁决二_OTHER只认两个词条_审查中不算():
    """`OTHER` 是混装桶(显式杂项 + 兜底),整码留下会把"未识别"也永久禁掉。
    ⚠ `currently under review` 是**自愈态**(24 小时内自动复架),不算。"""
    assert wf.keeps("OTHER", "business decision") is True
    assert wf.keeps("OTHER", "trust & safety") is True
    assert wf.keeps("OTHER", "currently under review") is False
    assert wf.keeps("OTHER", None) is False
    assert wf.keeps("OTHER", "  Business Decision  ") is True   # 大小写/空白容错


def test_裁决三_判不出来的留着():
    """⚠ 所有者定的是**拉黑**:四处都找不到原文(`taxonomy_code` 为 NULL)时
    继续禁,不因为"查不出理由"而放行。反过来写就是把一万条无据的行全放出去。"""
    assert wf.keeps(None, None) is True
    assert wf.keeps(None, "business decision") is True


# ── ③ 安全闸 ───────────────────────────────────────────────────────────────

def test_没回填过的行一条都不动():
    """`taxonomy_version` 对不上 = 拿不出新码,按陈旧信息删就是瞎删。
    它们只进摘要点名,提示先跑 error_reclass。"""
    src = inspect.getsource(wf)
    assert "taxonomy_version = %(ver)s" in wf._SQL_DOOMED
    assert "taxonomy_version = %(ver)s" in wf._SQL_PLAN
    out, _d, _k = wf.plan_lines([("POLICY", None, 3)], "t.x", stale=42)
    assert "另有 42 条还没按当前码表回填" in "\n".join(out)
    assert "本轮**一条都不动**" in "\n".join(out)
    assert "error_reclass" in src


def test_dry_run一行都不删_且报的就是真跑会删什么():
    """DANGEROUS=True ⇒ cli 把 `--dry-run` 翻成 execute=False。"""
    assert wf._parse({"execute": True})[2] is True
    assert wf._parse({"execute": False})[2] is False
    assert "🧪 --dry-run" in inspect.getsource(wf.run)


def test_删前必须落备份():
    """所有者选的是"直接删行",备份不改变这一点,只是把溯源留在库外 ——
    数万行的不可逆操作,值这一个文件。⚠ 顺序也钉:**先 dump 再 DELETE**。"""
    src = inspect.getsource(wf.run)
    assert "_dump(rows, _COLS" in src
    i_dump, i_del = src.index("_dump(rows"), src.index("_SQL_DELETE")
    assert i_dump < i_del, "备份必须写在 DELETE 之前"
    # 备份要带全行(含新码四列),只存 asin 等于没存
    for col in ("reason", "created_at", "taxonomy_code", "taxonomy_src"):
        assert col in wf._COLS, col


def test_摘要提醒飞书与拦截行为的后果():
    """删完飞书还是旧的(投影是 blacklist_push 整表重写);拦截行为从此变了。
    这两句漏掉,人会以为"删完就完事了"。"""
    src = inspect.getsource(wf.run)
    assert "blacklist_push" in src
    assert "audit_replay" in src


def test_参数宁炸不吞_且limit缺省不限量():
    with pytest.raises(ValueError, match="未识别参数"):
        wf._parse({"limitt": 5})
    with pytest.raises(ValueError, match="limit 要正整数"):
        wf._parse({"limit": "0"})
    assert wf._parse({})[0] == 0            # 不给 = 不限量
    assert wf._parse({"limit": "500"})[0] == 500


def test_plan_lines分将删与将留():
    rows = [("POLICY", None, 100), ("PT_WRONG", None, 700),
            ("OTHER", "business decision", 3), ("OTHER", "trust & safety", 2),
            ("OTHER", "currently under review", 1), (None, None, 50)]
    out, doomed, kept = wf.plan_lines(rows, "t.x", stale=0)
    assert doomed == {"PT_WRONG": 700, "OTHER": 1}
    assert kept == {"POLICY": 100, "OTHER": 5, "(判不出)": 50}
    text = "\n".join(out)
    assert "**将删** 701 条" in text and "**留下** 155 条" in text


def test_缺列时给人话不甩traceback():
    """⚠ 2026-09-03 实遇:`taxonomy_term` 没建就跑,人看到的是一屏
    `psycopg.UndefinedColumn`。缺的是**前置步骤**,说清做什么比说清哪一行
    炸了有用得多 —— 而且必须点出 `-p force=1`(版本号已盖章时 error_reclass
    会判 0 条,不加 force 回填不了新列)。"""
    src = inspect.getsource(wf.run)
    assert "does not exist" in src            # 只认缺列这一类,别的异常照抛
    assert "db_init" in src
    assert "error_reclass -p force=1" in src
    assert "conn.rollback()" in src           # PG 报错后不回滚会连累后续每一查


def test_一轮只落一个备份文件():
    """⚠ 2026-09-04 生产实遇:42,113 条分 9 批、1 秒多跑完,而时间戳戳到秒
    且在 `_dump` 里逐批算 —— 备份**裂成两个文件**(前 30,000 条一个、后
    12,113 条一个),摘要只报最后那一个。真要回滚的人照摘要去找,会少掉
    三万行**而且不会发现**。

    删数万行不可逆,备份是唯一的网:路径必须整轮算一次。
    """
    src = inspect.getsource(wf.run)
    # 路径在进循环之前定死,循环里只往同一个文件追加
    assert "backup = str(_backup_path())" in src
    i_path, i_loop = src.index("_backup_path()"), src.index("while True:")
    assert i_path < i_loop, "备份路径必须在批循环之外算"
    # `_dump` 自己不许再算时间戳(算了就又每批一个文件)
    assert "strftime" not in inspect.getsource(wf._dump)
    assert "strftime" in inspect.getsource(wf._backup_path)
    # 追加模式:同一轮多批要落进同一个文件,不是互相覆盖
    assert '"a"' in inspect.getsource(wf._dump)
