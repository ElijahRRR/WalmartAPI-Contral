"""外部资源登记处:飞书多维表格(app_token/table_id/字段名)、服务器地址、密钥读取。

规则(CLAUDE.md 铁律 3):
- 业务代码要用任何外部资源,只准 `from registry import resources` 后引用这里的常量/函数。
- 飞书字段名只准引用 Bitable.fields 里的常量,业务代码禁止字段名字符串字面量。
- 真密钥(app_secret、数据库口令)不在本文件:从环境变量读,值放 <DATA_ROOT>/.env。
- 新表建好后:在「表格清单」区补一个 Bitable 条目,并同步更新 docs/feishu_tables.md。
"""

import os
from dataclasses import dataclass
from types import SimpleNamespace

# ══════════════════════════════════════════════════════════════════════════════
#  服务器地址
# ══════════════════════════════════════════════════════════════════════════════


def walmart_base_url() -> str:
    """输入:无 → 输出:沃尔玛 Marketplace API base URL(env WALMART_BASE_URL 覆盖,用于沙箱)。"""
    return os.environ.get("WALMART_BASE_URL", "https://marketplace.walmartapis.com")


def feishu_base_url() -> str:
    """输入:无 → 输出:飞书开放平台 base URL。"""
    return os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn")


# ══════════════════════════════════════════════════════════════════════════════
#  密钥与凭据(值一律在 <DATA_ROOT>/.env,这里只登记变量名)
# ══════════════════════════════════════════════════════════════════════════════


def feishu_app_id() -> str:
    """输入:无 → 输出:飞书自建应用 App ID;未配置抛 LookupError。"""
    v = os.environ.get("FEISHU_APP_ID", "").strip()
    if not v:
        raise LookupError("FEISHU_APP_ID 未配置,请写入 <DATA_ROOT>/.env")
    return v


def feishu_app_secret() -> str:
    """输入:无 → 输出:飞书自建应用 App Secret;未配置抛 LookupError。"""
    v = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not v:
        raise LookupError("FEISHU_APP_SECRET 未配置,请写入 <DATA_ROOT>/.env")
    return v


def feishu_webhook_url() -> str | None:
    """输入:无 → 输出:运行通知群机器人 webhook URL;未配置返回 None(通知降级为仅日志)。"""
    return os.environ.get("FEISHU_WEBHOOK_URL", "").strip() or None


# ══════════════════════════════════════════════════════════════════════════════
#  沃尔玛 feed 规范(蓝图 §5.1 定稿;全项目唯一出处,旧系统同一版本号抄了 3 份)
# ══════════════════════════════════════════════════════════════════════════════

# ⚠ 官方版本表约 4-6 周滚动一版,需定期核对(2026-08-05 核验):
#   DELETE_ITEM 的 5.0.20250919 仍是官方现值;MP_MAINTENANCE 用官方当前推荐值
#   (旧系统在用的 5.0.20260304 已过时);RETIRE_ITEM 官方 guide 已消失仅存枚举,
#   版本 1.0 为旧系统实测值,迁移 daily_cleanup 前必须实测端点仍可用。
FEED_SPEC_VERSIONS = {
    "DELETE_ITEM": "5.0.20250919-16_45_47-api",
    "RETIRE_ITEM": "1.0",
    "MP_MAINTENANCE": "5.0.20260608-18_15_07-api",
    "price": "1.7",         # PriceFeed 无外层包装(加 PriceFeed 包装→ERROR,旧实证)
    "inventory": "1.4",     # InventoryFeed,Inventory 首字母大写(小写→0503009)
    "MP_ITEM_MATCH": "4.2",  # 跟卖(按匹配上架);spec enum 锁死 4.2/REPLACE
    # 上架主链(L2;旧系统实测值,官方 4-6 周滚版,上线前需实测仍被接受;
    # header version 必须完整时间戳,写 '5.0' 被拒 74597363510508 实证)
    "MP_ITEM": "5.0.20260304-22_45_32-api",
}

# 沃尔玛错误码登记(蓝图 §5.4;业务代码禁止散落字符串字面量)
WALMART_ERR_SKU_LOCKED = "ERR_EXT_DATA_0101211"     # SKU 绑死旧 UPC。解法:
# RETIRE→24h 冷却→清列→新 UPC 重上(sku_locked_heal 自愈链;旧实证:不先
# 退役直接换 UPC 重发同一 SKU 也失败。不是永久跳过,所有者纠正 2026-08-12)
WALMART_ERR_UPC_CONFLICT = "ERR_EXT_DATA_0101119"
# 异步审核假错误(旧实证:'还在合规审核中',几小时~几天自然变 SUCCESS;
# 绝不能当失败重发,否则 duplicate listing)
WALMART_ERR_ASYNC_REVIEW = ("EXT_DATA_ERROR_56026862530206",
                            "EXT_DATA_ERROR_66547201695750")


# ══════════════════════════════════════════════════════════════════════════════
#  飞书多维表格清单
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Bitable:
    """一张多维表格的登记条目:app_token + table_id + 字段名常量。

    业务代码永远写 `表.fields.client_id`,不写 "ClientId" 字面量;
    飞书改表头 = 只改本文件一行。
    """

    name: str
    app_token: str
    table_id: str
    fields: SimpleNamespace

    def require(self) -> "Bitable":
        """输入:无 → 输出:self;若尚未登记 app_token/table_id 则抛 LookupError。"""
        if not self.app_token or not self.table_id:
            raise LookupError(
                f"多维表格「{self.name}」尚未登记 app_token/table_id:"
                f"请在飞书建表后填入 registry/resources.py 并更新 docs/feishu_tables.md"
            )
        return self


def _fields(**kw: str) -> SimpleNamespace:
    return SimpleNamespace(**kw)


# ── 表格清单(随建随登记)──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Spreadsheet:
    """一张飞书电子表格(非多维表格)的登记条目:spreadsheet_token + sheet_id + 列序。

    电子表格按 range 坐标写,列序就是契约——columns 元组是唯一权威,
    业务代码禁止自行数列号。用电子表格而非 bitable 的场景:行数 >5 万(套餐上限)。
    """

    name: str
    token: str
    sheet_id: str
    columns: tuple[str, ...]
    wiki: bool = False      # True=token 是知识库节点 token(wiki/ 链接),
                            # api/feishu 会先解析成真实 spreadsheet_token

    def require(self) -> "Spreadsheet":
        if not self.token or not self.sheet_id:
            raise LookupError(
                f"电子表格「{self.name}」尚未登记:请把表格 URL 中的 token(/sheets/ 后段)"
                f"与 sheet_id(?sheet= 参数)写入 <DATA_ROOT>/.env 对应变量"
            )
        return self


# 在线产品总表(新):catalog_sync 写,PG 权威、此表是人看的投影,可随时整表重建。
# 行数约 13 万,超 bitable 5 万行套餐上限,故用电子表格。
# 列序 = catalog.walmart_items 的字段序,改列序必须两处同步。
ONLINE_PRODUCTS_SHEET = Spreadsheet(
    name="在线产品总表",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_ONLINE_SHEET_ID", ""),
    # last_seen_at/missing_since 不投影(所有者定稿 2026-08-07):
    # 追踪在 PG(walmart_items 两列 + product_events 账本),表只展示在架行
    columns=("store", "sku", "itemId", "upc", "gtin", "productName", "shelf",
             "productType", "variantGroupId", "variantGroupInfo",
             "price", "currency", "availToSellQty",
             "publishedStatus", "lifecycleStatus", "unpublishedReasons"),
)


# ── 订单中心六表(order_center_push 写;用户既有「订单中心V1」应用,表头已定)──
# 同一个多维表格应用(app_token 共用)。PG orders schema 是权威;
# 每表键列是同步对齐锚点,人工不得改动。**只登记程序拥有的字段**——
# 人工列/采集列/关联字段(主订单表、父记录、脚本审核、亚马逊单价等)
# 一律不登记,同步时不出现在载荷里即绝不会被覆盖。
# 任何表都不删行(delete_stale=False / ensure_keys):主订单表是永久枢纽,
# 行间有关联字段,删行会断链;滑出窗口的行只是停止刷新。

_ORDER_APP = os.environ.get("FEISHU_ORDER_APP_TOKEN", "")

# 主订单表 / 采购信息:人工域枢纽表,程序只补齐首列 order_line_id(ensure_keys)
ORDER_MAIN = Bitable(
    name="订单中心-主订单表",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_MAIN_TABLE_ID", ""),
    fields=_fields(key="order_line_id"),
)

ORDER_PURCHASE = Bitable(
    name="订单中心-采购信息",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_PURCHASE_TABLE_ID", ""),
    fields=_fields(key="order_line_id"),
)

ORDER_SALES = Bitable(
    name="订单中心-销售订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_SALES_TABLE_ID", ""),
    fields=_fields(
        key="order_line_id", order_date="下单时间", store="店铺",
        po_id="采购订单号", line_number="行号", sku="SKU",
        product_name="商品名称", qty="数量", sale_status="销售状态",
        audit_status="审核状态", status_date="状态更新时间",
        est_ship_date="预计发货时间", est_delivery_date="预计送达时间",
        product_amount="商品金额", shipping_amount="运费金额",
        cancel_reason="取消原因", refund_amount="行内退款金额",
        refund_comments="退款备注", carrier="承运商", tracking_no="物流单号",
        tracking_url="物流链接", ship_name="收件人姓名", phone="电话",
        address1="地址1", address2="地址2", city="城市", state="州",
        postal_code="邮编", country="国家", pulled_at="拉取时间",
    ),
)

# 销售订单表的**审核列**(order_audit 独占;与上面 ORDER_SALES 同一张表,
# 分成两个条目是为了分家所有权:sync_by_key 只覆盖 fields 里给出的列,
# order_center_push 的载荷里没有审核列 → 拉单永远冲不掉审核结论,反之亦然)。
# 「建议采购日期」属人工域,故意不登记。「产品截图」是附件字段,
# 值形如 [{"file_token": ...}],file_token 由 api/feishu.upload_media 换取。
ORDER_SALES_AUDIT = Bitable(
    name="订单中心-销售订单(审核列)",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_SALES_TABLE_ID", ""),
    fields=_fields(
        key="order_line_id", audit_status="审核状态", script_audit="脚本审核",
        amz_price="亚马逊单价", stock_qty="库存数量", ship_method="配送方式",
        ship_days="配送时长", seller="卖家店铺名", screenshot="产品截图",
        supplier="采购方", price_cap="限价", title_similarity="标题相似度",
    ),
)

ORDER_RETURNS = Bitable(
    name="订单中心-售后订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_RETURNS_TABLE_ID", ""),
    fields=_fields(
        # 唯一键 = RMA号|order_line_id(同一行可多次售后,首列 order_line_id 不唯一,
        # 需在表中新增该文本字段,与绩效表 perf_key 同理)
        key="唯一键", order_line_id="order_line_id", order_date="下单时间",
        store="店铺", rma="RMA号", customer_order_id="客户订单ID",
        po_id="采购订单号", line_number="行号", sku="SKU",
        return_status="售后状态", refund_status="退款状态",
        return_method="退货方式", refund_mode="退款方式",
        refund_total="总退款金额", return_reason="退货原因",
        return_comment="退货描述", return_by="退货截止日期",
        return_created="退货创建时间", last_modified="状态更新时间",
        customer_name="客户姓名", customer_email="客户邮箱",
        qty="数量", refunded_qty="已退款数量",
        carrier="承运商", tracking_no="物流单号", is_keep_it="keep-it单",
    ),
)

ORDER_PERF = Bitable(
    name="订单中心-绩效订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_PERF_TABLE_ID", ""),
    fields=_fields(
        key="perf_key", order_line_id="order_line_id", order_date="下单时间",
        store="店铺", po_id="采购订单号", metric="指标类型",
        accountable="计入绩效", description="问题描述", status="绩效状态",
        period_span="统计周期", detail="明细", pulled_at="拉取时间",
    ),
)

ORDER_SETTLE = Bitable(
    name="订单中心-对账明细",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_SETTLE_TABLE_ID", ""),
    fields=_fields(
        key="order_line_id", order_date="下单时间", store="店铺",
        po_id="采购订单号", line_number="行号", settle_status="入账状态",
        net_amount="结算净额USD", product_amount="商品销售额USD",
        commission_amount="实扣佣金USD", commission_rate="佣金率",
        original_commission="原始佣金USD", commission_saving="佣金优惠USD",
        incentive="优惠计划", period="账期", settle_date="结算日期",
        pulled_at="拉取时间",
    ),
)


# ── 订单审核两张配置表(order_audit 每次运行现读,不镜像入 PG)────────────────
# 不入库的理由:配置量小、改动即时生效是运营预期;且"读不到就不出结论"比
# "拿上次的旧配置继续算钱"安全(见 services/order_audit 的 require 语义)。
# 每行实际套用的采购方/汇率/限价会写进 orders.order_lines.audit_detail,事后可追溯。

# 黑名单邮编(钓鱼检测;所有者定稿 2026-08-09:只匹配邮编,旧系统的地址/街道
# 双向 substring 匹配整套不迁)。wiki 承载电子表格,A 列邮编,无表头。
ZIP_BLACKLIST_SHEET = Spreadsheet(
    name="黑名单邮编",
    token=os.environ.get("FEISHU_ZIP_BLACKLIST_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_ZIP_BLACKLIST_SHEET_ID", ""),
    columns=("zip",),
    wiki=True,
)

# 黑名单两张收集表(所有者建 2026-08-11,与黑名单邮编同一个 wiki 承载;
# **PG 权威,这两张是数据库的投影**——写入方向只有 PG → 飞书,人不直接编辑)。
# ASIN 表来源列格式 = 「沃尔玛-〈13 类之一〉」;但**入选只限永久禁止类**
# B/C/E/F/G/K(见 services/blacklist.PERMANENT),词表≠入选范围。
ASIN_BLACKLIST_SHEET = Spreadsheet(
    name="黑名单ASIN",
    token=os.environ.get("FEISHU_BLACKLIST_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_ASIN_BLACKLIST_SHEET_ID", ""),
    columns=("asin", "source", "added_date"),
    wiki=True,
)

# 品牌黑名单(后台报错集成):方向 PG→飞书(blacklist_push),**只承接沃尔玛
# 后台问题商品拿到的品牌**(自产行 src_sku IS NOT NULL)。D 列 SKU 是溯源,
# 去重按品牌(所有者澄清 2026-08-11)。与「黑名单品牌总表」(BRAND_BAN_SHEET,
# 飞书→PG,所有者人工归拢各渠道)方向相反,这张是归拢的一条增量渠道,别混。
BRAND_ERR_SHEET = Spreadsheet(
    name="黑名单品牌(后台报错集成)",
    # token 独立成变量,不设时回落到与 ASIN 表共用的 wiki token(同文档布局)
    token=(os.environ.get("FEISHU_BRAND_ERR_WIKI_TOKEN")
           or os.environ.get("FEISHU_BLACKLIST_WIKI_TOKEN", "")),
    sheet_id=os.environ.get("FEISHU_BRAND_ERR_SHEET_ID", ""),
    columns=("brand", "source", "added_date", "sku"),
    wiki=True,
)

# 采购方表(多维表格,人工维护):按 配送方式 + 亚马逊单价区间 选采购方,
# 多个候选取汇率最低者(旧系统 采购方匹配.py:80-87 语义,逐字保留)。
SUPPLIER_TABLE = Bitable(
    name="采购方",
    app_token=os.environ.get("FEISHU_SUPPLIER_APP_TOKEN", ""),
    table_id=os.environ.get("FEISHU_SUPPLIER_TABLE_ID", ""),
    fields=_fields(
        supplier="采购方", ship_method="配送方式",
        band_from="价格区间起", band_to="价格区间止",
        rate="汇率", enabled="是否启用",
    ),
)


# 店铺KPI 电子表格(旧系统存量 workbook,两个用途,PG 权威不变):
# ① 「总览」页(sheet_id 登记)= **影刀输入投影**:一店一行,影刀 RPA 读它
#    决定抓哪些卖家页(E 列 sellerId 是其输入;空 sellerId 行会让整条 RPA
#    崩溃——A147 事故,写入前必须过滤)。daily_report yingdao=1 时只写 A:H。
# ② 每店一个 sheet(title=店铺名)= 旧系统 KPI 历史(按日期一行累积),
#    kpi_history_import 的数据源;店铺页 sheet_id 运行时经 sheet_list 发现。
# columns 前 8 列即总览 A~H 列序;历史导入按表头关键词映射,不按列位。
KPI_SHEET = Spreadsheet(
    name="店铺KPI",
    token=os.environ.get("FEISHU_KPI_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_KPI_OVERVIEW_SHEET_ID", ""),
    columns=("data_date", "store", "seller_name", "partner_id", "seller_id",
             "store_status", "payment_status", "sales_status"),
)


# 店铺KPI看板(新表格,所有者 2026-08-08 定稿):人看的投影全在这里,
# 旧「店铺KPI」表 72 张分页停更归档。两个工作表同一 workbook:
# 「总览」= 每店最新一行(全 32 列)、「历史」= 全店合一近 N 天窗口。
# 列序 = _KPI_BOARD_COLUMNS(与 ops.store_kpi_daily 字段一一对应,
# 表头沿用旧表真实中文名,运营零学习成本)。整表重写,PG 权威可随时重建。
_KPI_BOARD_TOKEN = os.environ.get("FEISHU_KPI_BOARD_TOKEN", "")
_KPI_BOARD_COLUMNS = (
    "data_date", "store", "seller_name", "partner_id", "seller_id",
    "store_status", "payment_status", "sales_status", "items_online",
    "items_in_stock", "items_out_stock", "orders_count", "sales_amount",
    "otd_rate", "cancel_rate", "vtr_rate", "srr_rate", "refund_rate",
    "negative_rate", "return_rate", "inr_rate", "period_sales", "commission",
    "refund_amount", "closing_balance", "reserve_to_date", "payout",
    "payout_date", "payment_processor", "settle_cycle", "no_hold",
    "prev_payout")

KPI_BOARD_OVERVIEW = Spreadsheet(
    name="KPI看板-总览",
    token=_KPI_BOARD_TOKEN,
    sheet_id=os.environ.get("FEISHU_KPI_BOARD_OVERVIEW_ID", ""),
    columns=_KPI_BOARD_COLUMNS,
)

KPI_BOARD_HISTORY = Spreadsheet(
    name="KPI看板-历史",
    token=_KPI_BOARD_TOKEN,
    sheet_id=os.environ.get("FEISHU_KPI_BOARD_HISTORY_ID", ""),
    columns=_KPI_BOARD_COLUMNS,
)


# 商品停用/删除表(product_clear 驱动表):电子表格,运营填 A~D,程序写 E~H。
# 列序即契约(A=store B=sku C=停用/删除 D=操作原因
#             E=feedid F=操作日期 G=结果 H=报错)
RETIRE_SHEET = Spreadsheet(
    name="商品停用删除表",
    token=os.environ.get("FEISHU_RETIRE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_RETIRE_SHEET_ID", ""),
    columns=("store", "sku", "action", "reason",
             "feed_id", "op_date", "result", "error"),
)

# 上下架限额表(多维表格,**按店铺分行**,2026-08-06 所有者更正列名;
# daily_retire 读「下架限制」,未来 listing 读「上架限制」等)
# 上架表(listing 主驱动表,L2 用;所有者建 2026-08-07,21 列 A~U,
# 较旧 26 列砍掉 状态跟踪/最近跟踪日期——产品事件账本已承接该职责):
# A=ASIN B=店铺 C=walmart上架标题 D=walmart_product_type E=审核结果 F=理由
# G=审核日期 H=amz价格 I=库存 J=walmart价格 K=是否上架 L=上架feedid
# M=上架日期 N=未上架理由 O=上架结果 P=上架失败理由 Q=feed查询日期
# R=真实walmart标题 S=真实walmart_product_type T=真实UPC U=UPC是否一致
# (U 语义=核验的 UPC 一致性,按代码实际行为登记,所有者定稿 2026-08-07)
LISTING_SHEET = Spreadsheet(
    name="上架表",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_LISTING_SHEET_ID", ""),
    columns=("asin", "store", "list_title", "product_type", "audit_result",
             "audit_reason", "audit_date", "amz_price", "stock",
             "walmart_price", "listed", "feed_id", "list_date",
             "not_listed_reason", "list_result", "list_fail_reason",
             "feed_check_date", "real_title", "real_pt", "real_upc",
             "upc_match"),
)

# 跟卖表(match_listing 驱动表,替代旧 xlsx 输入,所有者定稿 2026-08-07
# 单路飞书读;11 列 A~K):运营填 A=UPC C=售价 D=重量 E=店铺,
# 脚本填 B=SKU F=跟卖状态 G=匹配GTIN H=上架时间 I=feedId J=feed结果
# K=feed查询时间(J/K 由 feed_poll 反哺器回填)
MATCH_SHEET = Spreadsheet(
    name="跟卖表",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_MATCH_SHEET_ID", ""),
    columns=("upc", "sku", "price", "weight", "store", "match_status",
             "matched_gtin", "list_time", "feed_id", "feed_result",
             "feed_check_time"),
)


# 风控·沃尔玛类目表(wiki 承载;拦截条件沿旧实证:准入状态='禁售' 或
# 中国卖家可做 以'否'开头;risk_sync 同步入 PG,闸门读库不读表——
# 所有者 2026-08-07:表格随时会停用)
RISK_PT_SHEET = Spreadsheet(
    name="沃尔玛类目表",
    token=os.environ.get("FEISHU_RISK_PT_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_RISK_PT_SHEET_ID", ""),
    columns=("category", "ptg", "product_type", "admit_status", "cn_seller",
             "cert_required", "note", "field_total", "field_required",
             "field_list"),
    wiki=True,
)

# 黑名单品牌总表(wiki 承载):各渠道黑名单品牌由**所有者人工归拢**的总清单
# (2026-08-11 换成新表 jF8dOw,旧「禁止品牌收集」退役),方向飞书→PG
# (risk_sync),名单语义=黑名单品牌,casefold 精确匹配。
# 归拢的增量渠道之一是 BRAND_ERR_SHEET(方向相反,PG→飞书),别混。
BRAND_BAN_SHEET = Spreadsheet(
    name="黑名单品牌总表",
    token=os.environ.get("FEISHU_BRAND_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_BRAND_SHEET_ID", ""),
    # D 列 sku 为溯源列(旧表 4 列,legacy_survey:1360;新总表若只有 3 列,
    # 第 4 列读空无害——risk_gate.sync_brands 本就不消费 sku)
    columns=("brand", "source", "added_date", "sku"),
    wiki=True,
)


# UPC 池(L2a,所有者建表 2026-08-07,6 列 A~F):PG(catalog.upc_pool)权威,
# 此表 = 运营注入口 + 投影。运营填 A=UPC B=放入日期;脚本填 C=状态
# (已领/已用/冲突/非法前缀/空=未用)D=店铺 E=SKU F=上架日期。
UPC_SHEET = Spreadsheet(
    name="UPC池",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_UPC_SHEET_ID", ""),
    columns=("upc", "put_date", "status", "store", "sku", "list_date"),
)


# 维护记录(maintenance 流水账):与「在线产品总表」同一 spreadsheet 的
# 另一工作表(所有者已建,2026-08-07;多维表格 5 万行上限装不下故用电子表格)。
# 列序即契约:A=店铺 B=SKU C=动作 D=旧值 E=新值 F=feedid G=日期 H=结果 I=报错
# feed 路径 F 写真 feedid、H 由 feed_poll 反哺器回填;PUT 同步路径 F 写
# "sync"、H 当场写 成功/失败。
MAINT_SHEET = Spreadsheet(
    name="维护记录",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_MAINT_SHEET_ID", ""),
    columns=("store", "sku", "action", "old_value", "new_value",
             "feed_id", "op_date", "result", "error"),
)


RETIRE_LIMITS = Bitable(
    name="上下架限额表",
    app_token=os.environ.get("FEISHU_LIMITS_APP_TOKEN", ""),
    table_id=os.environ.get("FEISHU_LIMITS_TABLE_ID", ""),
    fields=_fields(
        store="店铺",
        fba_range1="fba区间1", fba_range2="fba区间2",
        fbm_range1="FBM区间1", fbm_range2="FBM区间2",
        max_daily_list="上架限制",
        max_daily_retire="下架限制",
        inventory_note="库存特殊要求",
    ),
)


# 店铺凭证表:飞书人工维护 → 程序读 + 本地快照兜底。
# 密钥类字段(ClientSecret/代理密码)只在此表,访问权限收紧到最小人群。
STORE_CREDENTIALS = Bitable(
    name="店铺凭证表",
    app_token=os.environ.get("FEISHU_STORE_TABLE_APP_TOKEN", ""),
    table_id=os.environ.get("FEISHU_STORE_TABLE_ID", ""),
    fields=_fields(
        store="店铺",
        client_id="ClientId",
        client_secret="ClientSecret",
        proxy_type="代理类型",
        proxy_host="IP地址或域名",
        proxy_port="端口",
        proxy_user="IP登录账号",
        proxy_pass="IP登录密码",
        enabled="启用",
    ),
)
