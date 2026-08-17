"""catmap_export 回归:类目映射投影到飞书(镜子,不是数据源)。

所有者定稿 2026-08-17:「以前的审核系统是从这里拿的,我们现在直接当映射
查看使用」。他手上那份的表头列序是硬契约 —— 筛选/公式按列位置写死,
列一挪就全废而且不报错。

本文件钉三样:列序与表头逐字不动、**死映射必须点名**(映射指向字典外 PT =
这行白填,而表面上"映射是有的")、dry-run 一格不写。
"""

import pytest

from registry import resources
from workflows import catmap_export as ce


@pytest.fixture(autouse=True)
def _sheet_size(monkeypatch):
    """默认让表看起来是空的 —— 骤缩护栏只在"表里本来有东西"时才该说话。"""
    monkeypatch.setattr(ce.feishu, "sheet_row_count", lambda s: 0)


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.args = args

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._rows)


def _row(pt, cat, ptg, leaf, path, node, rank, conf, mt, note, batch, alive):
    return (pt, cat, ptg, leaf, path, node, rank, conf, mt, note, batch, alive)


_ROWS = [
    _row("Hammers", "Home Improvement", "Hand Tools", "Hammers",
         "Tools > Hand Tools > Hammers", "123", 1, "高", "a2w_agent",
         "", "v5.5", True),
    _row("Wall Clocks", "Home", "Decor", "Wall Clocks",
         "Home > Decor > Clocks", None, 2, "中", "人工", "", "v5.4", False),
]


def test_header_and_column_order_are_a_hard_contract(monkeypatch):
    """列序 = 所有者手上那份的表头。挪一列,他那边的筛选/公式全废且不报错。"""
    assert resources.CATMAP_SHEET_HEADER == (
        "Walmart Category", "Walmart PTG", "Walmart Product Type",
        "Amazon 叶子", "Amazon 路径", "browse_node_id",
        "排名", "置信度", "匹配方式", "备注", "来源批次")
    # columns 与表头一一对应,长度必须一致
    assert len(resources.CATMAP_SHEET.columns) == len(
        resources.CATMAP_SHEET_HEADER)

    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    sent = []
    monkeypatch.setattr(ce.feishu, "sheet_overwrite",
                        lambda s, rows: (sent.append(rows), len(rows))[1])
    ce.run({"execute": True})
    head, first, second = sent[0]
    assert tuple(head) == resources.CATMAP_SHEET_HEADER
    # SQL 取的是 PT 打头,写出去必须换回 Category/PTG/PT 的序
    assert first[:3] == ["Home Improvement", "Hand Tools", "Hammers"]
    assert first[3:6] == ["Hammers", "Tools > Hand Tools > Hammers", "123"]
    assert first[6:] == ["1", "高", "a2w_agent", "", "v5.5"]
    assert second[2] == "Wall Clocks" and second[5] == ""   # node 为空 → 空串


def test_dead_mappings_are_called_out(monkeypatch):
    """⚠ 映射指向 walmart_pt_meta 里没有的 PT = 这行映射是死的。

    审核 `resolve_pt` 末尾那道 `pt not in ctx.pt_meta → None` 会把它作废判
    pending,而表面上"映射是有的" —— 不点名就永远找不到。
    (所有者 2026-08-17 查到 88 个字典外 PT 全是亚马逊类目名,根子就在这里。)
    """
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    out = ce.run({"execute": True, "dry_run": True})
    assert "1 行映射指向 walmart_pt_meta 里没有的 PT" in out
    assert "Wall Clocks" in out
    assert "等于没映射" in out


def test_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    monkeypatch.setattr(ce.feishu, "sheet_overwrite",
                        lambda s, rows: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许写飞书")))
    out = ce.run({"execute": True, "dry_run": True})
    assert "DRY-RUN" in out and "一格未写" in out


def test_says_the_sheet_is_a_mirror_not_a_source(monkeypatch):
    """在表里改格子不影响判定 —— 不说清就会有人改了表等着生效。"""
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    monkeypatch.setattr(ce.feishu, "sheet_overwrite", lambda s, rows: len(rows))
    out = ce.run({"execute": True})
    assert "镜子" in out and "下次导出即被覆盖" in out
    assert "catmap_fix" in out          # 要改映射走哪条链


def test_node_coverage_is_reported(monkeypatch):
    """没有 browse_node_id 的行只能按路径字符串匹配,类目改名就失效。"""
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    out = ce.run({"execute": False})
    assert "带 browse_node_id 的 1 行" in out and "类目改名就失效" in out


def test_empty_result_is_not_an_empty_overwrite(monkeypatch):
    """⚠ 空结果绝不能整表重写 —— 那会把飞书表清空(整表重写还会删尾部行)。"""
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn([]))
    monkeypatch.setattr(ce.feishu, "sheet_overwrite",
                        lambda s, rows: (_ for _ in ()).throw(
                            AssertionError("空结果不许写")))
    assert "没有匹配的行" in ce.run({"execute": True, "pt": "NoSuchPT"})


def test_shrink_guard_stops_before_deleting_rows(monkeypatch):
    """⚠ 2026-08-17 生产事故:整表重写把飞书 17592 行覆盖成库里的 15770 行,

    净删 1847 行映射(1695 行指向有效 PT)。库里那份是 v5.5 xlsx 的一次性
    导入快照,飞书侧此后一直在被维护 —— **"库更全"不是既定事实**。
    risk_sync 家族的镜像早有空读/骤缩护栏,这里当初没照做。
    """
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))     # 库 2 行
    monkeypatch.setattr(ce.feishu, "sheet_row_count", lambda s: 101)  # 表 100 行
    monkeypatch.setattr(ce.feishu, "sheet_overwrite",
                        lambda s, rows: (_ for _ in ()).throw(
                            AssertionError("骤缩时不许写")))
    out = ce.run({"execute": True})
    assert "已停手,一格未写" in out
    assert "要少 98 行" in out
    assert "catmap_import" in out          # 指向正确的补救路径


def test_shrink_guard_can_be_overridden_when_intended(monkeypatch):
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    monkeypatch.setattr(ce.feishu, "sheet_row_count", lambda s: 101)
    monkeypatch.setattr(ce.feishu, "sheet_overwrite", lambda s, rows: len(rows))
    assert "已整表重写" in ce.run({"execute": True, "allow_shrink": "1"})


def test_shrink_guard_does_not_fire_on_a_filtered_export(monkeypatch):
    """只导一个 PT 时本来就该少 —— 护栏不该在这里挡路。"""
    monkeypatch.setattr(ce.db, "pg_conn", lambda: _Conn(_ROWS))
    monkeypatch.setattr(ce.feishu, "sheet_row_count", lambda s: 9999)
    monkeypatch.setattr(ce.feishu, "sheet_overwrite", lambda s, rows: len(rows))
    assert "已整表重写" in ce.run({"execute": True, "pt": "Hammers"})
