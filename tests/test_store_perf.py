"""店铺经营水平回归:在售天数当分母、净额、硬切 14 天、容量预支。

每条都对应一个会把货分错地方的实例。
"""

from services import store_perf as sp


def _raw(**kw):
    base = dict(rec_days=90, active_days=90, avg_online=100, orders=0,
                gross=0.0, refund=0.0, hist_rows=0, net=0.0)
    base.update(kw)
    base["net"] = base["gross"] - base["refund"]
    return base


def test_denominator_is_active_days_not_calendar_days():
    """停业期不该拉低日均 —— 这是所有者 2026-08-15 晚推翻设计稿的那一条。

    两家店同样卖 9000:A 满 90 天在营,B 只营业了 30 天。按日历天数算,
    B 的日均被摊薄到 1/3 ⇒ 缺口看起来更大 ⇒ 配额被放大 ⇒
    **停过业的店反而被灌最多货**,方向正好是反的。
    """
    m = sp.derive({"A": _raw(active_days=90, gross=9000.0),
                   "B": _raw(rec_days=30, active_days=30, gross=9000.0)}, 90)
    # 关键性质:同样卖 9000,营业天数少的那家**日均更高**。
    # 按日历天数当分母的话两家会**相等** —— 那正是要防的错。
    assert m["B"]["daily_net"] > m["A"]["daily_net"]
    assert m["B"]["cover"] == 30 / 90              # 覆盖率照报,别人才知道这是 30 天的数
    # 收缩前的原始日均确实是 9000/在售天数(两家都可信,只是互相拉了一把)
    assert m["A"]["net"] / m["A"]["active_days"] == 100.0
    assert m["B"]["net"] / m["B"]["active_days"] == 300.0


def test_net_of_refunds_not_gross():
    """退掉的钱不算产出。两家同样卖 1000,退 400 的那家实际只做了 600。"""
    m = sp.derive({"A": _raw(active_days=20, gross=1000.0, refund=400.0),
                   "B": _raw(active_days=20, gross=1000.0, refund=0.0)}, 90)
    assert m["A"]["net"] == 600.0 and m["B"]["net"] == 1000.0
    assert m["A"]["daily_net"] < m["B"]["daily_net"]


def test_slot_value_is_per_online_item_not_per_store():
    """货位值 = 给这家店一个货位值多少钱。

    500 SKU 做 500/天的店每个货位值 1;100 SKU 做 300/天的店每个货位值 3。
    只看总销售额会得出**相反**的结论,而好货该去后者。
    """
    m = sp.derive({"大店": _raw(active_days=30, avg_online=500, gross=15000.0),
                   "小店": _raw(active_days=30, avg_online=100, gross=9000.0)}, 90)
    assert m["大店"]["daily_net"] > m["小店"]["daily_net"]      # 规模:大店赢
    assert m["小店"]["slot_value"] > m["大店"]["slot_value"]    # 效率:小店赢


def test_under_14_active_days_is_a_hard_cut_to_the_median():
    """少于 14 天一律取中位数(所有者拍板)——**硬切不是收缩**。

    那个区间一单大额就能把单品日产出抬十倍;收缩公式在 active=2 时
    仍会留 12.5% 权重给它,那不叫"不信"。
    """
    m = sp.derive({
        "老店1": _raw(active_days=90, avg_online=100, gross=9000.0),
        "老店2": _raw(active_days=90, avg_online=100, gross=18000.0),
        "两天店": _raw(rec_days=2, active_days=2, avg_online=10, gross=5000.0),
    }, 90)
    med = sorted(m[s]["slot_value"] for s in ("老店1", "老店2"))
    assert m["两天店"]["slot_value"] == (med[0] + med[1]) / 2   # 就是中位数本身
    assert m["两天店"]["trusted"] is False
    assert "在售仅 2 天" in m["两天店"]["basis"]
    # 它自己那个畸高的值(5000/2/10 = 250)一点都没渗进去
    assert m["两天店"]["slot_value"] < 250


def test_new_store_needs_no_special_case():
    """新店 active=0 落在同一条规则里,天然冷启动。"""
    m = sp.derive({"老店": _raw(active_days=90, avg_online=100, gross=9000.0),
                   "新店": _raw(rec_days=0, active_days=0, avg_online=None)}, 90)
    assert m["新店"]["trusted"] is False
    assert m["新店"]["daily_net"] == m["老店"]["daily_net"]     # 只有一家可信 ⇒ 中位数=它
    assert m["新店"]["cover"] == 0.0


def test_shrinkage_pulls_a_thin_sample_toward_the_median():
    """14 天以上按 w=active/(active+14) 收缩:样本越薄越靠中位数。"""
    m = sp.derive({
        "厚": _raw(active_days=90, avg_online=100, gross=9000.0),     # 100/天
        "薄": _raw(active_days=14, avg_online=100, gross=14000.0),    # 1000/天
    }, 90)
    assert m["厚"]["basis"].startswith("自身 87%")
    assert m["薄"]["basis"].startswith("自身 50%")
    # 薄样本被拉回一半:(1000+550)/2 之类,总之远低于它自己的 1000
    assert m["薄"]["daily_net"] < 1000.0


def test_history_window_share_is_reported():
    """窗口大量落在历史期的店,它的"净额"其实是毛额(历史行没有退款数据),
    与 API 期的店**不是同一个口径**,不能直接比 —— 所以要报出来。"""
    m = sp.derive({"老": _raw(active_days=90, orders=100, hist_rows=90)}, 90)
    assert m["老"]["hist_share"] == 0.9


def test_room_credits_pending_delist_but_keeps_a_conservative_number_too():
    """一边下架一边上架:不预支的话,马上要空出 300 个货位的店会被算成"满了"。

    ⚠ 但 `room_now`(当前真实)必须同时给出 —— list_new 真上架时用它,
    否则下架没做完就上架会真的超 max_online。
    """
    q = sp.quota_inputs(
        metrics={"A": {"slot_value": 3.0, "daily_net": 100.0}},
        cfg={"A": {"max_online": 1000.0, "gmv": 200.0}},
        online_now={"A": 950},
        pending_delist={"A": 300})
    assert q["A"]["room_now"] == 50            # 保守:上架闸用这个
    assert q["A"]["room"] == 350               # 乐观:配额用这个
    assert q["A"]["pending_delist"] == 300


def test_gap_is_none_when_target_missing_not_zero():
    """没填日目标 ⇒ 缺口**算不出**,返回 None。

    退化成 0 会让没填目标的店永远分不到货,而且不报错 —— 与
    store_targets 开头那条空值纪律同源。
    """
    q = sp.quota_inputs({"A": {"daily_net": 100.0}},
                        {"A": {"max_online": 500.0, "gmv": None}},
                        {"A": 100})
    assert q["A"]["gap"] is None
    q2 = sp.quota_inputs({"A": {"daily_net": 40.0}},
                         {"A": {"max_online": 500.0, "gmv": 100.0}},
                         {"A": 100})
    assert q2["A"]["gap"] == 0.6               # (100−40)/100


def test_opted_out_store_is_marked(monkeypatch):
    """「单店最大在线数」填 0 = 不参与分配(口径 #13),这里如实转述。"""
    q = sp.quota_inputs({}, {"关": {"max_online": 0.0, "gmv": 100.0},
                             "开": {"max_online": 500.0, "gmv": 100.0},
                             "未填": {"max_online": None, "gmv": 100.0}}, {})
    assert q["关"]["participates"] is False
    assert q["开"]["participates"] is True
    assert q["未填"]["participates"] is None    # 三态不压成两态


def test_refund_join_aggregates_before_joining():
    """一条订单行可能有多次售后 —— 退款要**先按 order_line_id 聚合再连**,
    裸 JOIN 会把订单行乘出多份,毛额跟着翻倍。"""
    assert "GROUP BY order_line_id" in sp._SQL_SALES
    assert "LEFT JOIN r ON r.order_line_id = o.order_line_id" in sp._SQL_SALES


def test_kpi_sql_counts_only_recorded_days():
    """没有 KPI 记录的天既不算 active 也不算停用 —— 那是"没抓到"不是"没营业"。

    所以 active 从 count(*) FILTER 里数(只覆盖有行的天),
    并且 rec_days 一起给出来算覆盖率。
    """
    assert "count(*) FILTER" in sp._SQL_KPI and "rec_days" in sp._SQL_KPI
    # 状态为空按 ACTIVE 算(全仓 fail-open)
    assert "coalesce(upper(store_status), 'ACTIVE') = 'ACTIVE'" in sp._SQL_KPI


# ── 对齐工具沉到 services(两个报告 workflow 共用,铁律 1)────────────────

def test_textfmt_table_aligns_by_display_width():
    from services import textfmt
    lines = textfmt.table(["店铺", "在售"], [["A085朱丽霖", 90], ["新店", 3]],
                          align="<>")
    # 三行(表头 + 两行)的第一列显示宽度必须一致
    firsts = [ln.split()[0] for ln in lines]
    assert len({textfmt.width(ln[:ln.index("  ", 2)]) for ln in lines
                if "  " in ln[2:]}) <= 2      # 允许末列 rstrip 造成的差异
    assert "A085朱丽霖" in lines[1] and "新店" in lines[2]
    # 数字列右对齐:90 与 3 的个位要对齐
    assert lines[1].rstrip().endswith("90") and lines[2].rstrip().endswith("3")


def test_textfmt_pad_never_truncates():
    """宽度不够就原样返回 —— 截断会把店名切成看不出是谁,比列歪了更糟。"""
    from services import textfmt
    assert textfmt.pad("A085朱丽霖", 4) == "A085朱丽霖"
    assert textfmt.pad("ab", 5) == "ab   "
    assert textfmt.pad("ab", 5, ">") == "   ab"


def test_online_now_matches_the_kpi_definition_not_the_occupancy_one():
    """同一张表里两个「在线数」必须同口径,否则人对不上账还找不出原因。

    「在线均值」取自 KPI 历史行(daily_report._pg_item_counts 写的:
    published_status='PUBLISHED' AND missing_since IS NULL,**不筛 lifecycle**),
    「当前在线」取自 alloc_stores._SQL_ONLINE_NOW —— 两者必须逐字一致,
    否则货位值(÷在线均值)与剩余容量(−当前在线)建在两个不同的"在线"上。

    ⚠ 它与 alloc_survey._SQL_ONLINE **有意不同**:那条管占用与冲突,
    退市商品不占货位所以筛 lifecycle;这条管容量与产出,跟的是所有者
    日报里每天看的那个数。两个口径各有其用,别去"统一"。
    """
    from services import alloc_survey as sv
    from workflows import alloc_stores as st
    q = st._SQL_ONLINE_NOW
    assert "published_status = 'PUBLISHED'" in q and "missing_since IS NULL" in q
    assert "lifecycle_status" not in q          # 跟 KPI 对齐:不筛 lifecycle
    # 占用侧那条正相反 —— 它必须筛
    assert "lifecycle_status" in sv._SQL_ONLINE


def test_slot_value_is_verifiable_from_the_same_row():
    """货位值 = 日均净额 ÷ 在线均值 —— 两个输入都在同一行上,读的人能自己验。"""
    m = sp.derive({"A": _raw(active_days=30, avg_online=200, gross=6000.0)}, 90)
    assert m["A"]["daily_net"] == 200.0                       # 6000/30
    assert m["A"]["slot_value"] == 1.0                        # 200/200
    assert m["A"]["avg_online"] == 200                        # 分母照样报出来


def test_median_substituted_values_are_marked_in_the_report():
    """在线均值是实测、日均净额是中位数顶上来的 —— 并排出现时必须标出来。

    否则同一行里"在线均值 0"与"货位值 0.104"并排,读的人会以为算错了
    (生产实测 2026-08-15:82杨乾良 在线均值 0、货位值 0.104)。
    """
    from workflows import alloc_stores as st
    own = {"取值依据": "自身 87% + 中位数 13%"}
    sub = {"取值依据": "中位数(在售仅 3 天)"}
    assert st._sub(own, "283.33") == "283.33"
    assert st._sub(sub, "283.33") == "~283.33"
