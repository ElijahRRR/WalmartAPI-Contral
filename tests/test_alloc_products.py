"""产品分体检报告回归:漏斗分类、信号覆盖率告警、硬闸与"信号全缺"分开。"""

import contextlib
import pathlib

from services import product_score as ps
from workflows import alloc_products as wf

# ⚠ 列数必须与 `product_pool._SQL_POOL` 逐字对齐(13 列)。少一列时 score_all
#   会抛 ValueError —— 这是好事:上一次同类错误被 `except Exception` 的降级分支
#   吞掉了,测试全绿而分数全错。别再给这里包 try。
POOL = [
    # asin, brand, manufacturer, pt, cat, price, shipping, stock, stock_state,
    # lead, rating, reviews, fulfillment
    ("B0AAAA0001", "Acme", None, "Socks", "Fashion", 9.9, 0.0, 50, "in_stock", 5, "4.6", "820", "FBA"),
    ("B0BBBB0002", "Beta", None, "Hats", "Fashion", 19.9, 2.0, 8, "in_stock", 6, "4.1", "35", "FBA"),
    # 配送 12 天:产品分表照收 —— 货期是**逐店**条件(限额表「配送时长限制」),
    # 不是产品自身的硬闸,筛在 alloc_plan._pool_reach,不在这里
    ("B0HHHH0008", "Theta", None, "Hats", "Fashion", 9.0, 0.0, 30, "in_stock", 12, "4.0", "8", "FBA"),
    # 只有配送时效一项:**旧实现会让它独占权重拿 100 分**,新实现判「信息不足」
    ("B0CCCC0003", "Gamma", None, "Knives", "Home", 5.0, 0.0, 100, "in_stock", 3, None, None, "FBM"),
    # 有口碑、无销量无退货:拉低加分/罚分项的覆盖率,让告警有东西可报
    ("B0GGGG0007", "Eta", None, "Socks", "Fashion", 7.5, 0.0, 40, "in_stock", 6, "4.2", "12", "FBA"),
    ("B0DDDD0004", "Delta", None, "Socks", "Fashion", None, 0.0, 20, "in_stock", 4, "4.9", "9", "FBA"),
    ("B0EEEE0005", "Eps", None, "Socks", "Fashion", 12.0, 1.0, 0, "in_stock", 4, "4.4", "60", "FBA"),
    ("B0FFFF0006", "Zeta", None, "Hats", "Fashion", 8.0, 0.0, 30, "in_stock", 4, None, None, None),
]


class _Cur:
    def __init__(self, risk_fails=False):
        self.risk_fails = risk_fails
        self._r = []

    def execute(self, sql, args=None):
        if "FROM catalog.products p" in sql:
            self._r = POOL
        elif "min(order_date)" in sql:          # 全历史(不进打分,只给报告)
            self._r = [("B0AAAA0001", 900, 8100.0, "2024-03-01", "2026-08-10")]
        elif "sum(coalesce(qty, 0))" in sql:
            self._r = [("B0AAAA0001", 120, 1080.0), ("B0BBBB0002", 3, 59.7)]
        elif "refunded_qty" in sql:
            self._r = [("B0AAAA0001", 100, 12)]
        elif "FROM catalog.walmart_items" in sql:
            # (store, sku, source_key):第三列是登记簿 amz 身份键(0a-23)
            self._r = [("A085", "B0AAAA0001", "B0AAAA0001")]
        elif "FROM catalog.claims" in sql:
            self._r = [("B0AAAA0001", "A085")]
        elif "product_risk" in sql:
            if self.risk_fails:
                raise RuntimeError("relation catalog.product_risk does not exist")
            # (asin, delete_times, unexplained_missing, missing_times, audit_reject_times)
            self._r = [("B0BBBB0002", 3, True, 4, 0)]
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
    # 落盘走 services/report_csv(2026-08-27 六处收口),报告目录在积木那侧
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    return conn


def test_only_hard_gates_keep_a_product_out_of_scoring(monkeypatch, tmp_path):
    """「未进入打分」只剩硬闸:落地价算不出、库存不足。

    没评分没评论的品**照常打分**(口碑段记 0),它们靠淘汰线被筛掉 ——
    走到打分这步的品一定有快照,「没有评分」是真实观测不是数据缺口。
    """
    _wire(monkeypatch, tmp_path)
    out = wf.run({})
    assert "未进入打分" in out
    assert "落地价算不出 1" in out          # price NULL
    assert "库存不足 1" in out              # stock=0,保守量不许救它
    # 「未进入打分」现在只剩硬闸两类 —— 没评分没评论的品照常打分(记 0 分),
    # 不再单列「信息不足」桶(所有者定稿 2026-08-15 晚)
    assert "信息不足" not in out


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
    assert "不明原因消失过4次" in beta        # 罚分理由带次数写进行里


def test_sales_sql_only_reads_rows_with_asin(monkeypatch):
    """A1.5 之后按 asin 聚合;asin IS NULL 的行进不了这个维度(0.79%),
    但它们照常在店×SKU 维度起作用 —— 不许拿 sku 原文冒充 asin。"""
    assert "asin IS NOT NULL" in wf._SQL_SALES
    assert "GROUP BY asin" in wf._SQL_SALES


def test_refund_denominator_excludes_history_rows():
    """退货率的分母只能是**同期 API 行**:历史导入行没有退款数据,
    拿它们当分母会把退货率系统性稀释成接近 0(§7.4e)。"""
    assert "source IS NULL" in wf._SQL_REFUND


def test_csv_carries_price_sales_revenue_and_both_owner_columns(monkeypatch, tmp_path):
    """所有者 2026-08-16 要的四样:售价、历史销量、销售额、当前所属店铺。

    ⚠ 「所属店铺」拆成**两列**,合成一列会把最有用的信息抹掉:
      占用店 = 台账里的**决策**(claims,无自动释放)
      在线店 = 货此刻**实际**挂在谁那儿(walmart_items 观测)
    两者不一致本身就是信息 —— 占用在 A、货在 B ⇒ B 该下架;有货无占用 ⇒
    规划外店或回填时被闸挡下的行。这正是本项目"占用是决策不是观测"那条纪律。
    """
    _wire(monkeypatch, tmp_path)
    wf.run({})
    txt = (tmp_path / "alloc_产品分.csv").read_text(encoding="utf-8-sig")
    head, *body = txt.splitlines()
    for col in ("售价", "运费", "落地价", "历史总销量(件)", "历史总销售额(毛额)",
                "占用店", "在线店", "首次售出", "最近售出"):
        assert col in head, col
    acme = next(ln for ln in body if ln.startswith("B0AAAA0001")).split(",")
    cols = head.split(",")
    val = dict(zip(cols, acme))
    assert val["售价"] == "9.9" and val["落地价"] == "9.9"
    w = ps.SALES_WINDOW_DAYS      # 产品侧窗口默认近一年,不是 90 天
    assert val[f"近{w}天销量(件)"] == "120"
    assert val[f"近{w}天销售额(毛额)"] == "1080.0"
    assert val["历史总销量(件)"] == "900" and val["历史总销售额(毛额)"] == "8100.0"
    assert val["占用店"] == "A085" and val["在线店"] == "A085"


def test_online_stores_read_the_registry_key(monkeypatch, tmp_path):
    """「在线店」按**身份键**归集:登记簿优先,模式提取兜存量(0a-23)。

    裸提取在切码后会让这一列恒空 —— 「占用在 A、货在 B」这类信息全被抹掉,
    而报告照样出得来,一个字的报错都没有。
    """
    cur = _Cur()
    real_execute = cur.execute

    def _execute(sql, args=None):
        real_execute(sql, args)
        if "FROM catalog.walmart_items" in sql:
            # 不透明码 + 登记簿键 ⇒ 仍归到 B0AAAA0001 这个 ASIN 上
            cur._r = [("A107", "AZZZZ234567", "B0AAAA0001")]

    cur.execute = _execute
    _wire(monkeypatch, tmp_path, cur=cur)
    wf.run({})
    txt = (tmp_path / "alloc_产品分.csv").read_text(encoding="utf-8-sig")
    head, *body = txt.splitlines()
    val = dict(zip(head.split(","),
                   next(ln for ln in body
                        if ln.startswith("B0AAAA0001")).split(",")))
    assert val["在线店"] == "A107"


def test_online_sql_reads_the_registry_key():
    """SQL 文本钉住身份键那一跳:LEFT JOIN 登记簿 amz 行、取 source_key。"""
    assert "ls.source_key" in wf._SQL_ONLINE_SKU
    assert "ls.source_type = 'amz'" in wf._SQL_ONLINE_SKU
    assert "LEFT JOIN catalog.listing_sources" in wf._SQL_ONLINE_SKU


def test_landed_price_is_price_plus_shipping(monkeypatch, tmp_path):
    """落地价才是硬闸真正用的那个数(§7.6)。

    只看售价会让「9.9 美元 + 12 美元运费」看起来很便宜 —— 而下架/定价链路
    从来用的是落地价。
    """
    _wire(monkeypatch, tmp_path)
    wf.run({})
    txt = (tmp_path / "alloc_产品分.csv").read_text(encoding="utf-8-sig")
    head, *body = txt.splitlines()
    cols = head.split(",")
    beta = dict(zip(cols, next(ln for ln in body
                               if ln.startswith("B0BBBB0002")).split(",")))
    assert beta["售价"] == "19.9" and beta["运费"] == "2.0"
    assert beta["落地价"] == "21.9"


def test_lifetime_sales_never_feeds_the_score(monkeypatch, tmp_path):
    """⚠ 历史销量**不进打分**:分数只看窗口内(§7.6)。

    拿全历史当信号会让一个三年前卖爆、现在没人要的品一直挂高分。
    这条盯着"顺手把 life 塞进 score() 的 sales"这个诱惑。
    """
    import inspect
    from services import product_pool
    assert "lifetime" not in inspect.getsource(product_pool.score_all)
    _wire(monkeypatch, tmp_path)
    out = wf.run({})
    assert "历史销量**不进打分**" in out


def test_csv_carries_the_fulfillment_channel(monkeypatch, tmp_path):
    """配送方式(FBA/FBM)是渠道闸的产品侧 —— 决定这个品能去哪些店。

    所有者 2026-08-16 追加。⚠ **认不出的写「(未知)」而不是留空**:留空跟
    "这一列没填"长得一样,而两者处置不同 —— 未知渠道的品在建组时会被整组
    剔除(不猜),那是要去查采集侧的,不是配置问题。
    """
    _wire(monkeypatch, tmp_path)
    wf.run({})
    txt = (tmp_path / "alloc_产品分.csv").read_text(encoding="utf-8-sig")
    head, *body = txt.splitlines()
    cols = head.split(",")
    assert "配送方式" in cols
    val = lambda a: dict(zip(cols, next(  # noqa: E731
        ln for ln in body if ln.startswith(a)).split(",")))
    assert val("B0AAAA0001")["配送方式"] == "FBA"
    assert val("B0CCCC0003")["配送方式"] == "FBM"
    assert val("B0FFFF0006")["配送方式"] == "(未知)"   # 采不到,不留空


def test_the_window_param_is_sales_days_and_days_is_refused(monkeypatch):
    """⚠ `days` 在这条工作流里曾经装着 365(产品销量窗口),而在
    `alloc_stores`/`alloc_plan` 里装的是 90(店铺经营水平窗口)。

    同名异义的实测后果:照 §12.4「两个窗口」的心智模型给例行一跑统一加
    `-p days=90`,alloc_stores 与 alloc_plan 都对,**alloc_products 静默把
    产品销量窗口从 365 砍到 90** —— 而销量信号覆盖率本来就只有几个百分点,
    再砍会把唯一有正面证据的那批品抹平,报告上只写"近 90 天销量",看不出配错了。

    处置与 cli 吞 flag 同一路数:**报错,绝不自动改口**。静默接受等于替
    所有者做了他没做的决定。
    """
    import pytest
    with pytest.raises(ValueError, match="sales_days"):
        wf.run({"days": 90})
