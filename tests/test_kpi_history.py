"""kpi_history_import 解析积木 + 影刀新鲜度回归。"""

from datetime import datetime, timezone

from services import kpi, yingdao

# 生产真实表头(2026-08-08 预览实证:七个中文表头曾未映射,回归钉住)
_HEADER = ["日期", "店铺", "卖家名称", "partnerId", "sellerId", "店铺状态",
           "支付状态", "销售状态", "在线商品", "有库存", "无库存", "昨日出单",
           "昨日销售额($)", "准时送达(90%)", "取消率", "有效追踪(99%)",
           "卖家回复率(95%)", "退款率", "差评率", "退货率", "未收到",
           "账期销售额($)", "佣金", "退款金额", "期末余额", "迄今备用金($)",
           "回款", "回款日", "收款方", "结算周期", "无Hold", "上期回款"]


def test_header_map_full_and_traps():
    mapping, unmapped = kpi.map_history_header(_HEADER)
    assert unmapped == []
    by_field = {f: i for i, f in mapping.items()}
    # 包含关系陷阱:更具体的先占
    assert by_field["data_date"] == 0          # 「回款日」没抢走「日期」
    assert by_field["payout_date"] == 27
    assert by_field["payout"] == 26
    assert by_field["prev_payout"] == 31
    assert by_field["store"] == 1              # 「店铺状态」没抢走「店铺」
    assert by_field["store_status"] == 5
    assert by_field["refund_rate"] == 17       # 「退款金额」没抢走「退款率」
    assert by_field["refund_amount"] == 23
    assert by_field["period_sales"] == 21      # 「账期销售额」没被「销售额」抢走
    assert by_field["sales_amount"] == 12
    assert by_field["otd_rate"] == 13          # 准时送达/有效追踪/卖家回复率(中文)
    assert by_field["vtr_rate"] == 15
    assert by_field["srr_rate"] == 16          # 「卖家回复率」没抢走「卖家名称」
    assert by_field["seller_name"] == 2
    assert by_field["reserve_to_date"] == 25   # 迄今备用金
    assert by_field["payment_processor"] == 28  # 收款方
    assert by_field["no_hold"] == 30
    # 全部 32 列都有归属
    assert len(mapping) == 32


def test_parse_history_rows():
    rows = [
        ["2026/8/1", "别名店", "卖家A", "P1", "S1", "ACTIVE", "ACTIVE", "可售",
         "1,234", "1000", "234", "12", "$1,234.56", "98.5%", "0.5", "99", "100",
         "1.2", "0", "2.5", "0.8", "$5,000", "$750", "$88", "$1,000.00",
         "$0", "$900", "2026-08-05", "PAYONEER", "14", "是", "$800"],
        ["", "x"],                       # 无日期 → 跳过
        ["2026-08-02"],                  # 只有日期,其余全空 → 保留(值全 None)
    ]
    out, skipped = kpi.parse_history_rows("A085朱丽霖", _HEADER, rows)
    assert skipped == 1 and len(out) == 2
    r = out[0]
    assert r["store"] == "A085朱丽霖"          # sheet 标题权威,店铺列不采信
    assert r["data_date"] == "2026-08-01"
    assert r["items_online"] == 1234           # 千分位
    assert r["sales_amount"] == 1234.56        # $ + 千分位
    assert r["otd_rate"] == 98.5               # % 剥离
    assert r["no_hold"] is True
    assert r["payout_date"] == "2026-08-05"
    assert r["srr_rate"] == 100.0 and r["vtr_rate"] == 99.0
    assert r["reserve_to_date"] == 0.0 and r["payment_processor"] == "PAYONEER"
    assert r["prev_payout"] == 800.0
    r2 = out[1]
    assert r2["data_date"] == "2026-08-02" and r2["orders_count"] is None


def test_board_header_columns_aligned():
    from registry import resources
    from workflows import daily_report as dr
    # 看板中文表头与 registry 列序一一对应(32 列),且与导入映射对称:
    # 每个表头都能被 _HIST_HEADER_MAP 映射回同一个字段
    assert len(dr._BOARD_HEADER) == len(resources.KPI_BOARD_OVERVIEW.columns) == 32
    mapping, unmapped = kpi.map_history_header(dr._BOARD_HEADER)
    assert unmapped == []
    for i, field in mapping.items():
        assert resources.KPI_BOARD_OVERVIEW.columns[i] == field
    assert dr._board_cell(None) == ""
    assert dr._board_cell(True) == "是" and dr._board_cell(False) == "否"
    assert dr._board_cell(12.5) == "12.5"


def test_yingdao_freshness():
    trigger = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
    fresh = {"scraped_at": "2026-08-08T08:07:05+08:00"}     # 00:07 UTC > trigger
    stale = {"scraped_at": "2026-08-07T08:07:05+08:00"}
    assert yingdao.is_fresh(fresh, trigger) is True
    assert yingdao.is_fresh(stale, trigger) is False
    assert yingdao.is_fresh({}, trigger) is False           # 缺字段不炸
    assert yingdao.is_fresh({"scraped_at": "垃圾"}, trigger) is False
