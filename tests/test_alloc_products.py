"""产品分体检报告回归:漏斗分类、信号覆盖率告警、硬闸与"信号全缺"分开。"""

import contextlib
import pathlib

from services import product_score as ps
from workflows import alloc_products as wf

POOL = [
    # asin, brand, pt, cat, price, shipping, stock, stock_state, lead, rating, reviews
    ("B0AAAA0001", "Acme", "Socks", "Fashion", 9.9, 0.0, 50, "in_stock", 5, "4.6", "820"),
    ("B0BBBB0002", "Beta", "Hats", "Fashion", 19.9, 2.0, 8, "in_stock", 12, "4.1", "35"),
    # 只有配送时效一项:**旧实现会让它独占权重拿 100 分**,新实现判「信息不足」
    ("B0CCCC0003", "Gamma", "Knives", "Home", 5.0, 0.0, 100, "in_stock", 3, None, None),
    # 有口碑、无销量无退货:拉低加分/罚分项的覆盖率,让告警有东西可报
    ("B0GGGG0007", "Eta", "Socks", "Fashion", 7.5, 0.0, 40, "in_stock", 6, "4.2", "12"),
    ("B0DDDD0004", "Delta", "Socks", "Fashion", None, 0.0, 20, "in_stock", 4, "4.9", "9"),
    ("B0EEEE0005", "Eps", "Socks", "Fashion", 12.0, 1.0, 0, "in_stock", 4, "4.4", "60"),
    ("B0FFFF0006", "Zeta", "Hats", "Fashion", 8.0, 0.0, 30, "in_stock", None, None, None),
]


class _Cur:
    def __init__(self, risk_fails=False):
        self.risk_fails = risk_fails
        self._r = []

    def execute(self, sql, args=None):
        if "FROM catalog.products p" in sql:
            self._r = POOL
        elif "sum(coalesce(qty, 0))" in sql:
            self._r = [("B0AAAA0001", 120), ("B0BBBB0002", 3)]
        elif "refunded_qty" in sql:
            self._r = [("B0AAAA0001", 100, 12)]
        elif "product_risk" in sql:
            if self.risk_fails:
                raise RuntimeError("relation catalog.product_risk does not exist")
            self._r = [("B0BBBB0002", 3, True, 0)]
        else:
            self._r = []

    def fetchall(self):
        return self._r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, tmp_path, cur=None):
    conn = _Conn(cur or _Cur())

    def _gen():
        yield conn
    monkeypatch.setattr(wf.db, "pg_conn", contextlib.contextmanager(_gen))
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    return conn


def test_hard_gates_and_all_signals_missing_are_counted_apart(monkeypatch, tmp_path):
    """「信号全缺」**不是硬闸** —— 它是"我们对这个品一无所知",
    归进"淘汰"会让人以为这个品有毛病,其实是我们的数据缺口。"""
    _wire(monkeypatch, tmp_path)
    out = wf.run({})
    assert "未进入打分" in out
    assert "落地价算不出 1" in out          # price NULL
    assert "库存不足 1" in out              # stock=0,保守量不许救它
    # Gamma(只有配送时效)与 Zeta(什么都没有)都进「信息不足」——
    # 旧实现里 Gamma 会独占权重拿 **100 分**,那正是要防的
    assert "没有评分/评论(信息不足,不判分) 2" in out


def test_low_coverage_signal_is_called_out(monkeypatch, tmp_path):
    """权重再合理,信号采不到就是空的 —— 覆盖率低于一半要点名。

    否则设计稿写着"退货率 10%",实际只有三成产品算得出,分数其实由别的
    信号决定,而报告上看不出来。
    """
    _wire(monkeypatch, tmp_path)
    out = wf.run({})
    line = next(ln for ln in out.splitlines() if "退货率" in ln)
    assert "⚠ 只有 API 期算得出" in line
    # 配送时效 100% 覆盖,不该被点名
    assert "⚠" not in next(ln for ln in out.splitlines() if "配送时效" in ln)
    # 覆盖率分母是「有分可判」,不许超过 100%
    for ln in out.splitlines():
        if "%" in ln and ("评分" in ln or "配送" in ln or "销量" in ln):
            assert "150" not in ln


def test_risk_view_failure_degrades_instead_of_crashing(monkeypatch, tmp_path):
    """product_risk 是罚分项不是主线:视图缺了要降级并明说,不能拖垮整份体检。"""
    conn = _wire(monkeypatch, tmp_path, _Cur(risk_fails=True))
    out = wf.run({})
    assert "product_risk 读不到" in out and "黑历史罚分本轮全为 0" in out
    assert conn.rolled_back is True          # 事务 aborted 必须先回滚
    assert "▍分数分布" in out                # 主线照常出


def test_csv_exposes_which_signals_were_missing(monkeypatch, tmp_path):
    """每行要写明这个分是靠哪几项算出来的 —— 说不清来源就没法推翻它。"""
    _wire(monkeypatch, tmp_path)
    wf.run({})
    txt = (tmp_path / "alloc_产品分.csv").read_text(encoding="utf-8-sig")
    head, *body = txt.splitlines()
    assert "缺失信号" in head and "罚分原因" in head
    assert "口碑分" in head and "销量加分" in head   # 三段各自可查
    beta = next(ln for ln in body if ln.startswith("B0BBBB0002"))
    assert "不明原因消失过" in beta          # 罚分理由写进行里
    assert "配送12天" in beta                # 配送慢的罚分理由也写进去


def test_sales_sql_only_reads_rows_with_asin(monkeypatch):
    """A1.5 之后按 asin 聚合;asin IS NULL 的行进不了这个维度(0.79%),
    但它们照常在店×SKU 维度起作用 —— 不许拿 sku 原文冒充 asin。"""
    assert "asin IS NOT NULL" in wf._SQL_SALES
    assert "GROUP BY asin" in wf._SQL_SALES


def test_refund_denominator_excludes_history_rows():
    """退货率的分母只能是**同期 API 行**:历史导入行没有退款数据,
    拿它们当分母会把退货率系统性稀释成接近 0(§7.4e)。"""
    assert "source IS NULL" in wf._SQL_REFUND
