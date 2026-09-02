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


def alloc_excluded_stores() -> tuple[str, ...]:
    """输入:无 → 输出:**不纳入分配规划**的店铺名子串(命中即排除)。

    所有者定稿 2026-08-15:店名含「谭总」的店不在规划范围内——
    - 不给它们分配、不判它们的类目、不占品牌与产品;
    - **其他店可以与它们重复**上同一品牌/产品(所以它们的在线商品既不进
      冲突清单,也不参与 list_new 的全局去重闸);
    - 但**它们的销量照常计入**产品/品牌/类目三个全局维度——那是别的店选品
      时的有效信号,排除的是"归属",不是"数据"。

    env `ALLOC_EXCLUDE_STORES` 覆盖(逗号分隔);留空字符串 = 谁都不排除。
    """
    raw = os.environ.get("ALLOC_EXCLUDE_STORES")
    if raw is None:
        return ("谭总",)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def feishu_webhook_url() -> str | None:
    """输入:无 → 输出:运行通知群机器人 webhook URL;未配置返回 None。

    ⚠ 这是通知的**第二条路**。首选是用应用身份直接发给人
    (`feishu_notify_to()`)——旧系统一直是这么做的(legacy_survey:649/1818:
    `lark-cli im +messages-send --as bot`,收件人 open_id 硬编码在 summary.py:38),
    而群机器人 webhook 是本仓新引入的第二套身份,至今没配上。
    """
    return os.environ.get("FEISHU_WEBHOOK_URL", "").strip() or None


def feishu_notify_to() -> str | None:
    """输入:无 → 输出:运行通知的收件人标识(未配置返回 None)。

    取值可以是下面任意一种,**类型由前缀自动认**(见 api/feishu._receive_type):
      `ou_…` open_id · `oc_…` chat_id(群) · 含 `@` 邮箱 · 11 位数字手机号

    ⚠ 手机号**不能直接当 receive_id**(飞书的 receive_id_type 里没有"手机号"
    这一档),要先用 `contact/v3/users/batch_get_id` 换成 open_id ——
    api/feishu 会自动换并缓存,但那个接口要应用有 `contact:user.id:readonly`
    权限。配 open_id 或邮箱则不需要这条权限。

    前缀判型这条规矩逐字沿用旧系统(legacy_survey:1818,notify.py:137:
    `ou_` → --user-id,`oc_` → --chat-id),换个人接手不用重新学一套。
    """
    return os.environ.get("FEISHU_NOTIFY_TO", "").strip() or None


# 定制品判据键(所有者定稿 2026-08-28:「对于定制产品不上架,是否为定制产品
# 可以从产品数据中拿到」)。值随采集载荷落库(products.slow / snapshots.raw),
# 契约字段表未登记 —— rating/review_count 同款先例(allocation_plan §评分:
# 契约没登记但采集侧确实随 raw 落库,探针实测后启用)。
# 键名生产探针已核实(所有者实跑 2026-08-28):latest_snapshot.raw 带
# `is_customized` 共 1,225,423 行,值形态 Yes/No(_is_custom 的小写 truthy
# 解析天然认 "Yes")。⚠ 错键名 = 闸恒放行("明确真值才拦"方向),改名必须重探。
AMZ_CUSTOM_FLAG_KEY = "is_customized"

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
    # 分节点批量库存(多仓批次 2 启用)。⚠ **1.5 的 key 小写**
    # (inventoryHeader/inventory)与 1.4 大写恰好相反 —— 两套模板不能共用,
    # 混用的表现是整批 ERR_EXT_DATA_0503009 退回(1.4 小写时的同款错误码)
    "MP_INVENTORY": "1.5",
    "MP_ITEM_MATCH": "4.2",  # 跟卖(按匹配上架);spec enum 锁死 4.2/REPLACE
    # 上架主链(L2)。⚠ 这一个字符串同时决定**两件事**:
    #   ① feed header 的 version;② `paths.mp_item_spec_dir()` 读哪份 spec。
    # 改一处两边一起变 —— 两边错开就是拿一个版本的数据去过另一个版本的校验。
    # header version 必须完整时间戳,写 '5.0' 被拒(74597363510508 旧实证)。
    #
    # 2026-08-20 从 5.0.20260304-22_45_32-api 切到 20260608(旧版停了五个月;
    # MP_MAINTENANCE 早已在 20260608)。切换前用 `spec_split -p diff=1` 量过差集:
    #   PT 6951 → 6951(零增零减);Orderable 24 → 23(只移除 specProductType);
    #   顶层必填有变化的 PT 仅 48 个;**新增必填只有 center_bore、影响 1 个 PT**
    #   (轮毂中心孔径,汽车整顶级不做,实际影响为零);其余 5 个字段全是
    #   "不再必填"(partTerminologyID 24 / condition 20 / …),只放松不收紧。
    "MP_ITEM": "5.0.20260608-18_15_07-api",
}

# 沃尔玛错误码登记(蓝图 §5.4;业务代码禁止散落字符串字面量)
WALMART_ERR_SKU_LOCKED = "ERR_EXT_DATA_0101211"     # SKU 绑死旧 UPC。解法:
# RETIRE→24h 冷却→清列→新 UPC 重上(sku_locked_heal 自愈链;旧实证:不先
# 退役直接换 UPC 重发同一 SKU 也失败。不是永久跳过,所有者纠正 2026-08-12)
WALMART_ERR_UPC_CONFLICT = "ERR_EXT_DATA_0101119"
# 政策违禁(旧 sync_listing_state.PROHIBITED_CODES 实证,2026-08-12 抢救):
# 永远不能上架——回执标 PROHIBITED,不进重试通道(重发也永远是拒)
WALMART_ERR_PROHIBITED = frozenset({
    "EXT_DATA_ERROR_71666506605865",    # Military/Law Enforcement
    "EXT_DATA_ERROR_61696573580701",    # Firearm Accessories
    "EXT_DATA_ERROR_61020366035308",    # General Prohibited Product
})
# 异步审核假错误(旧实证:'还在合规审核中',几小时~几天自然变 SUCCESS;
# 绝不能当失败重发,否则 duplicate listing)
WALMART_ERR_ASYNC_REVIEW = ("EXT_DATA_ERROR_56026862530206",
                            "EXT_DATA_ERROR_66547201695750")
# 内容标准拒(2026-08-19 生产实证 ~30 例:标题堆词/图文不符/描述自相矛盾)。
# 文案图片全部取自亚马逊原文(系统的地盘,LLM 不碰),原样重发结果必然相同
# ——进 FAILED 通道重试三次 = 纯烧 UPC 与配额,还会触发/延长 QARTH 合规
# 审查。O 列写 CONTENT_REJECTED,list_new 不再自动领;文案人工改好后
# 清掉 O 列即可重回通道(与 PROHIBITED 的"永不"语义有别,故单列一类)。
WALMART_ERR_CONTENT = frozenset({"EXT_DATA_ERROR_07705958490105"})

# ── 报错归类(第一步:引擎与对照报告用;换轨接线在第二步)────────────
# 方案定稿 docs/error_taxonomy.md(2026-09-01),判据与优先序的完整依据在那儿。
# 消费方:services/error_taxonomy.py(引擎)+ workflows/error_reclass_report.py。
ERROR_TAXONOMY_VERSION = "t.2026-09-02.1"   # 码表/判据变更时手动递增
ERROR_CATEGORY_CODES = {                     # 码 → 中文名(全大写码,与旧 A-L 单字母码同列可辨)
    "PROHIBITED_FINAL": "禁售不可申诉", "IP": "知识产权", "BRAND": "品牌未授权",
    "POLICY": "违反禁售政策", "PT_WRONG": "类目选错", "CONTENT": "内容问题",
    "PRICE": "价格规则", "GATED": "类目需预审批", "INFO": "信息缺失",
    "EXPIRED": "过期下架", "STAGE": "未上线", "FLAGGED": "内部标记",
    "RECALL": "安全召回", "SPECIAL": "特殊计划", "SYSTEM": "系统错误",
    "OTHER": "未识别",
}
# 记录级主码序:终局优先,非中性永远压过中性(J 吃 42.9%、A 盖 641 条的病根按性质钉死)
ERROR_CATEGORY_SEVERITY = (
    "PROHIBITED_FINAL", "IP", "BRAND", "RECALL", "PT_WRONG", "POLICY",
    "GATED", "FLAGGED", "CONTENT", "PRICE", "SPECIAL", "INFO",
    "SYSTEM", "OTHER", "STAGE", "EXPIRED",
)
# feed 报错的政策族锚:field 稳定、error_code 一次性(生产实证:Offensive 171 次
# 散在 35 个互不相同的码上)。QARTH/OFFER/sku 等多义 field 不入此集合。
WALMART_ERR_FIELD_POLICY = frozenset({"Defects Platform", "RNA"})


# ══════════════════════════════════════════════════════════════════════════════
#  类目树来源标记(audit.amazon_taxonomy.source)
# ══════════════════════════════════════════════════════════════════════════════

# 所有者的对账版类目树只发叶子层,中间层由 taxonomy_derive 从我们自有的
# (ID 链 × 面包屑)反推补齐。两者用 source 分辨:taxonomy_import 重灌时
# 只删文件段的行,反推行留着;同 node 文件行永远覆盖反推行。
# 两个 workflow 都要用,故登记在此(workflow 之间不互相 import)。
TAXONOMY_SOURCE_DERIVED = "derived_products"


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


# 店铺KPI 电子表格(旧系统存量 workbook,PG 权威不变):
# 每店一个 sheet(title=店铺名)= 旧系统 KPI 历史(按日期一行累积),
# kpi_history_import 的**只读**数据源;店铺页 sheet_id 运行时经 sheet_list 发现,
# 登记的 sheet_id 是总览页(require() 的存在性检查用)。
# columns 空:本表已无按列位写入的路径,历史导入按表头关键词映射不按列位。
#
# 2026-08-15 起本仓**不再写这张表**:原「总览页 = 影刀输入投影」那条路径
# (daily_report yingdao=1 写 A:H)已删,新影刀应用改读
# paths.yingdao_input_file() 的 input.json。飞书从此只是影刀改造前的存量,
# 不是任何一条链路的中继。⚠ 别再往这里加写入 —— 老应用可能还在读它,
# 新旧两个应用同时被喂数据 = 双 spawn 互抢(latest.json 新鲜度校验会反复失败)。
KPI_SHEET = Spreadsheet(
    name="店铺KPI",
    token=os.environ.get("FEISHU_KPI_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_KPI_OVERVIEW_SHEET_ID", ""),
    columns=(),
)


# 店铺KPI看板(新表格,所有者 2026-08-08 定稿):人看的投影全在这里,
# 旧「店铺KPI」表 72 张分页停更归档。两个工作表同一 workbook:
# 「总览」= 每店最新一行(全 32 列)、「历史」= 全店合一近 N 天窗口。
# 列序 = _KPI_BOARD_COLUMNS(与 ops.store_kpi_daily 字段一一对应,
# 表头沿用旧表真实中文名,运营零学习成本)。整表重写,PG 权威可随时重建。
#
# ⚠ 首两列 = (店铺, 日期)(所有者定稿 2026-08-15:看板按店铺看,店铺列必须
# 在最左);两页均按店铺排序,历史页店内再按日期降序。
_KPI_BOARD_TOKEN = os.environ.get("FEISHU_KPI_BOARD_TOKEN", "")
_KPI_BOARD_COLUMNS = (
    "store", "data_date", "seller_name", "partner_id", "seller_id",
    "store_status", "payment_status", "sales_status", "items_online",
    "items_in_stock", "items_out_stock", "orders_count", "sales_amount",
    "otd_rate", "cancel_rate", "vtr_rate", "srr_rate", "refund_rate",
    "negative_rate", "return_rate", "inr_rate", "period_sales", "commission",
    "refund_amount", "closing_balance", "reserve_to_date", "payout",
    "payout_date", "payment_processor", "settle_cycle", "no_hold",
    # 末列 2026-08-31 由 prev_payout(上期回款)换成 total_payout(累计回款,
    # 所有者:「我需要累计回款,就沃尔玛总共已经付给我的钱」)。旧列在 PG 里
    # 保留不删(历史行的值仍是当时的真实观测),只是不再投影到看板
    "total_payout")

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
# product_clear 读「下架限制」,listing 链读「上架限制」等)
# 上架表(listing 主驱动表,L2 用;所有者建 2026-08-07,21 列 A~U,
# 较旧 26 列砍掉 状态跟踪/最近跟踪日期——产品事件账本已承接该职责):
# A=店铺 B=ASIN C=walmart上架标题 D=walmart_product_type E=审核结果 F=理由
# G=审核日期 H=amz价格 I=库存 J=walmart价格 K=是否上架 L=上架feedid
# M=上架日期 N=未上架理由 O=上架结果 P=上架失败理由 Q=feed查询日期
# R=真实walmart标题 S=真实walmart_product_type T=真实UPC U=UPC是否一致
# (U 语义=核验的 UPC 一致性,按代码实际行为登记,所有者定稿 2026-08-07)
# ⚠ **A/B 于 2026-08-16 被所有者对调**(原 A=ASIN B=店铺)。全仓只有
# `listing_sheet.read_rows()` 按位置取值(zip(columns, 单元格)),所以这条
# 元组的顺序**就是**表里的列序 —— 表头再动一次,只改这里 **+ services/listing_sheet
# 里的显式 range 字母**(写入侧按字母写,表头一动它们也得跟着挪)。
# ⚠ **2026-09-02 所有者再改表头**(第三步输出规范化):C 插入 SKU;「审核理由」
# 拆成 G=类别 + H=具体内容;尾部四列 真实标题/真实PT/真实UPC/UPC匹配 换成
# T=登记日期 U=查询编码(运营域,脚本不写)。仍 21 列 A~U。
LISTING_SHEET = Spreadsheet(
    name="上架表",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_LISTING_SHEET_ID", ""),
    columns=("store", "asin", "sku", "list_title", "product_type",
             "audit_result", "audit_category", "audit_detail", "audit_date",
             "amz_price", "stock", "walmart_price", "listed", "feed_id",
             "list_date", "not_listed_reason", "list_result",
             "list_fail_reason", "feed_check_date", "register_date",
             "query_code"),
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


# 黑名单中心两张新表(所有者定稿 2026-08-13:黑名单只维护一份,与品牌总表
# 同一个黑名单 wiki 承载;取代旧审核系统的独立三列表)。镜像语义 = 单事务
# TRUNCATE 全量重灌 + 空读/骤缩护栏(飞书删行必须跟着消失,残留即幽灵拦截)。
SELLER_BLACKLIST_SHEET = Spreadsheet(
    name="黑名单卖家店铺ID",
    token=os.environ.get("FEISHU_BLACKLIST_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_SELLER_BLACKLIST_SHEET_ID", ""),
    columns=("seller_id",),
    wiki=True,
)
# ⚠ 2026-08-20 从单列升成五列(所有者定稿:「我把 233 条整个粘贴进飞书表格,
# 你让黑名单中心按实际的读取」)。单列时代只能表达「这一条精确路径」,存不下
# 子树规则的 browse_node_id —— 233 条里 189 条子树 + 30 条顶级名会全退化成
# path_exact,拦截面从 2 万个类目塌回 233 条,等于把子树改造整个还原。
# **列序即飞书表头顺序,一个字都不许改**(飞书按表头位置索引,改名/换位会静默错位)。
AMZCAT_BLACKLIST_SHEET = Spreadsheet(
    name="黑名单亚马逊类目",
    token=os.environ.get("FEISHU_BLACKLIST_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_AMZCAT_BLACKLIST_SHEET_ID", ""),
    columns=("category", "browse_node_id", "category_zh", "match_type", "reason"),
    wiki=True,
)

# 类目映射明细(所有者 2026-08-17:「以前的审核系统是从这里拿的,我们现在
# 直接当映射查看使用」)。**投影,不是数据源** —— 权威在 audit.walmart_category_map,
# 这张表是给人看的一面镜子,由 catmap_export 整表重写。
# 列序即所有者手上那份的表头,一个字都不许改(改了他那边的筛选/公式全废)。
CATMAP_SHEET = Spreadsheet(
    name="类目映射明细",
    token=os.environ.get("FEISHU_CATMAP_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_CATMAP_SHEET_ID", ""),
    columns=("walmart_category", "walmart_ptg", "walmart_product_type",
             "amazon_leaf", "amazon_category", "browse_node_id",
             "rank_in_pt", "confidence", "match_type", "notes",
             "source_batch"),
    wiki=True,
)
# 表头文案(写进第 1 行)。与 columns 一一对应,顺序必须一致
CATMAP_SHEET_HEADER = (
    "Walmart Category", "Walmart PTG", "Walmart Product Type",
    "Amazon 叶子", "Amazon 路径", "browse_node_id",
    "排名", "置信度", "匹配方式", "备注", "来源批次")

# PT 上传模板汇总(同一个 wiki 里的另一张工作表;所有者 2026-08-17:
# 「完整的沃尔玛类目映射时可以直接映射到 PT上传模板_汇总」)。
# 它由**沃尔玛官方 MP_ITEM spec** 拆出来,是"这个 PT 到底存不存在"的凭据之一。
# ⚠ **只读**:本仓不往它写(2026-08-17 覆盖事故之后的纪律 —— 人在维护的表,
# 除非明确要求,一律只读)。
PT_TEMPLATE_SHEET = Spreadsheet(
    name="PT上传模板_汇总",
    token=os.environ.get("FEISHU_CATMAP_WIKI_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_PT_TEMPLATE_SHEET_ID", ""),
    columns=("walmart_category", "walmart_ptg", "walmart_product_type",
             "total_fields", "required_count", "required_fields",
             "core_fields"),
    wiki=True,
)

# ── LLM 计价表(2026-08-21 加;单位 USD / 每 100 万 token)──────────────
# **DeepSeek 没有任何端点能查到单价或某次调用的花费**(核过官方文档:
# `/user/balance` 只回余额;`/chat/completions` 的 usage 只回 token 数),
# 所以单价只能落在本地。放这里是铁律 3:一切配置从 registry 取。
#
# 数据来源:api-docs.deepseek.com/quick_start/pricing,**2026-08-21 核**。
# 官方会调价,对不上账时先来这里核一遍日期。
#
# ⚠ **峰谷价差整整一倍**,峰值时段(UTC)01:00–04:00 与 06:00–10:00
#   —— 换算成北京时间就是 **09:00–12:00 与 14:00–18:00**;其余时段半价。
#   所以十几万条的大重审排在**北京时间晚 18:00 到次日早 08:00**跑,直接省一半。
LLM_PRICING_SOURCE = "api-docs.deepseek.com/quick_start/pricing(2026-08-21 核)"

# 峰值时段(UTC 小时,左闭右开)。谷时段 = 其余全部
LLM_PEAK_HOURS_UTC = ((1, 4), (6, 10))

# model → {tier: (cache_hit, cache_miss, output)},USD / 1M token。
# 键是**定价页上的产品名**;请求里发的 `model` 可能是别名,先过 LLM_MODEL_ALIASES。
LLM_PRICING = {
    "deepseek-v4-flash": {"peak":    (0.014, 0.44, 1.32),
                          "offpeak": (0.007, 0.22, 0.66)},
    "deepseek-v4-pro":   {"peak":    (0.044, 1.32, 3.96),
                          "offpeak": (0.022, 0.66, 1.98)},
}

# 旧别名 → 定价页产品名(2026-08-21 核官方更新日志)。
# ⚠ `deepseek-chat` / `deepseek-reasoner` 是**官方已宣布停用的旧别名**
#   (2026-04-24 公告:三个月后即 2026-07-24 停用),当前路由到 v4-flash 的
#   非思考 / 思考模式。**停用日期已过**,还能用纯属宽限期 —— 一旦切断,
#   全仓 LLM 调用会同时失败(L1 rerank / L3 / 上架属性映射 / variant_remap)。
#   生产应在 .env 显式写 `DEEPSEEK_MODEL=deepseek-v4-flash`。
LLM_LEGACY_ALIASES = {"deepseek-chat", "deepseek-reasoner"}
LLM_MODEL_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",       # 非思考模式
    "deepseek-reasoner": "deepseek-v4-flash",   # 思考模式,同一张价表
}


def llm_priced_model(model: str) -> str:
    """输入:请求里发的 model → 输出:LLM_PRICING 里的键(别名已折叠)。"""
    return LLM_MODEL_ALIASES.get(model, model)


# ── llm_cache 键空间锚点(2026-08-21)──────────────────────────────────────
# 缓存键里**必须**含模型(换模型 = 换答案,不含就等于"换了模型还在吃旧模型的
# 出参"且不报错),但**换个标签不算换模型**。
#
# `deepseek-chat` 与 `deepseek-v4-flash`(+ thinking disabled)是**同一个模型
# 的同一个模式**——前者只是后者的旧别名。所以它俩必须共用一个键空间,
# 否则把缺省值从别名改成正式名的那一刻,存量缓存全部作废、下一轮全额重付。
#
# ⚠ 锚点故意锚在**历史用过的那个串**上:键是哈希,存量行是按 'deepseek-chat'
#   算出来的,想让它们继续命中就只能沿用这个串。它只是哈希输入,**永远不会
#   发给接口**,别名被官方下线也不影响。
# ⚠ `deepseek-reasoner` **不在这里** —— 它是 v4-flash 的**思考模式**,
#   与非思考模式是两种输出行为,共用键空间就是拿思考模式的答案冒充非思考的。
#   计价可以合并(同一张价表),缓存身份不行。
LLM_CACHE_ANCHOR = {"deepseek-v4-flash": "deepseek-chat"}


def llm_cache_model(model: str) -> str:
    """输入:请求里发的 model → 输出:缓存键里用的模型身份串。"""
    return LLM_CACHE_ANCHOR.get(model, model)


def llm_price_tier(dt) -> str:
    """输入:带时区的 datetime → 输出:'peak' 或 'offpeak'。

    换模型/换供应商不改这里 —— 时段规则是 DeepSeek 的,新供应商加自己的表。
    """
    h = dt.astimezone(__import__("datetime").timezone.utc).hour
    return "peak" if any(a <= h < b for a, b in LLM_PEAK_HOURS_UTC) else "offpeak"


# 审核规则集版本(批次 B7 定稿):规则代码/seed yaml/词表任何变更时**手动递增**,
# 写入 catalog.products.audit_version;按版本批量重审走
# product_audit -p force_rerun=版本号(乱定一次 = 全量重审成本事故,勿自动化)。
# 2026-09-02 提版两次:政策表官方同步 v1(见下方 POLICY_LEGACY_NAMES 与
# docs/policy_sync.md §十.7)+ 第三步 B1 批(L3 换喂官方全文 + 输出三段化,
# docs/audit_step3_spec.md §三)。提版即触发 mode=stale 版本重审。
AUDIT_RULES_VERSION = "c.2026-09-02.2"
# c.2026-09-02.2  **第三步 B1 批:L3 换喂 + 输出规范化 + 理由映射去猜测**
#                 (规格 `docs/audit_step3_spec.md` §三,所有者八项定稿 §六):
#                 ① S4 政策块改喂**官方英文全文**(`full_policy` 经
#                    `policy_feed.render_feed_text` 渲染,ORDER BY id),六个中文
#                    人工列与 50/30/240/80 截断整体删除;S1 重写为"只认下面的
#                    官方英文原文";S2 枚举追加两条非政策类别、删 brand_misuse;
#                 ② L3 输出三段化:`verdict` / `policy`(枚举逐字)/ `detail`
#                    (中文 ≤120 字,引原文片段),外加 brand_verdicts 与
#                    confidence;政策名解析不到 → **pending**(旧版降级猜 IP,删);
#                 ③ 类别**由规则自报**(hit.detail 的 `category` 键),
#                    `compute_final_reason` 收敛为查表:查不到 = None + 计数 +
#                    warning,**没有 `General-Use Products` 兜底**;
#                 ④ `catalog.products` 新增 `audit_detail` 列(类别与具体内容
#                    分列,飞书上架表 G/H 两列同口径);
#                 ⑤ 证据通道泛化(读 L0/L1/L2 三层软 hit)、政策路由提示整体删除。
#                 ⚠ 提示词与政策表一起决定 `llm_cache` 键 ⇒ purpose=audit_l3 的
#                 存量缓存**全量未命中**,重审全额重付(谷时段减半,见 LLM_PRICING)。
#                 ⚠ **B 与 C 只切换一次**(规格 §一):生产机等 C 批合并后再 pull。
#                 影响面:reject 行的 `audit_reason` 改为类别枚举、`audit_detail`
#                 新列写具体内容。**按需重审为主**(`audit_sheet` 走 from_sheet),
#                 批量走 `mode=stale`(近 90 天有动销的那批,B2 批加 active_days)。
# c.2026-09-02.1  **政策表官方同步 v1 + 官方类别名成为全链唯一键**(所有者
#                 2026-09-02 定稿 §十.7,三件事同批,判定输入三处一起变):
#                 ① `policy_sync` 真跑:补武器族 5 行 + 42 页全文刷新 +
#                    **表内名一律改为官方拼写**(对上但拼写不同的行 UPDATE
#                    category_en,id 不变;缩写名经 POLICY_LEGACY_NAMES 认领)。
#                    政策表是 S4 政策块与 S2 候选块的唯一数据源 ⇒ L3 提示词
#                    逐字节变化,前缀缓存一次性重建属预期成本;
#                    ⚠ **成本口径说全**(与上面 LLM_CACHE_ANCHOR 那段同一个道理):
#                    system prompt 进 `llm_cache.cache_key` 的 messages ⇒ 政策表
#                    一改,`catalog.llm_cache` 里 purpose=audit_l3 的存量**全量
#                    未命中**(不是"少省一点",是一条都不命中);与本批要求的
#                    全量重审叠加 = 那批产品**全额重付**(DeepSeek 前缀缓存也
#                    要重建,只是它按 miss 价另算)。大批重审排北京时间
#                    18:00–次日 08:00 的谷时段跑,直接省一半(见 LLM_PRICING);
#                 ② `audit_l3` S1/S3 的「37 条」字面量改为按实时条数渲染
#                    (旧值早就与实际行数不符,提示词自称的数目与清单对不上);
#                 ③ `audit_reason` 的 reason_category 归一化改为**随表**
#                    (`_L3_NORMALIZE` 20 条政策名删除,改用实时 category_en
#                    集合 casefold 等值回表内原拼写)—— 表改名后旧映射会把
#                    L3 的答案改写成表里已不存在的缩写名。
#                 影响面:reject 行的 `audit_reason` 取值随表改名而变(旧结论
#                 挂的是缩写名)。**全量重审**(政策表 = L3 判定输入):
#                   python cli.py product_audit -p force_rerun=c.2026-08-24.1
# c.2026-08-24.1  R10 Made in USA 硬规则上线(漏判反哺第一条)。提版即触发
#                 mode=stale 版本重审:approved 存量(含历史导入的 1183 个
#                 "沃尔玛已下架仍 approved")按新判据全链重过,rejected 沿用。
# c.2026-08-21.1  **R3 收敛成单一判据**(所有者定稿):判类目要不要认证,从此
#                 **只看飞书类目表的「必需认证」列**。同日下线两条链:
#                 ① L2 R3 读 `audit.walmart_pt_spec` 的两条分支(硬 has_real_cert /
#                    软 has_soft_cert)—— 那张表是批次 A 从旧审核库整表搬来的
#                    **死快照**,`pt_spec_sync` 重建过但从没进调度;库里
#                    `real_cert_fields` 存的还是原始 spec 字段名,而重建写的是
#                    认证名称,两者口径相反(旧判硬、清洗判「需评估」);
#                 ② NRTL 整机/小件分类器(`_classify_nrtl_pt` + nrtl_small_parts.yaml)
#                    —— 拿 PT 名里有没有 `parts`/`accessor` 裸子串猜物理事实。
#                    生产实见:一张实木咖啡桌被判「整机电器, 必须 NRTL 认证,
#                    搬运做不了」,因为 `Coffee Tables` 的 spec 带着
#                    `has_nrtl_listing_certification`(给带 USB 口的电动桌用的字段)。
#                 所有者原话:「代码只判定确定性的,这种很明显不确定,应该交给
#                 LLM 看这个产品是不是整机电器,而不是让代码从类目看是不是整机。
#                 所以,旧的死快照不要了,死代码也不要了,以飞书源为准,以后我们
#                 只更新这个」。
#                 **先补后删,无真空期**:「整机电器」这一判定同批移入 L3 提示词
#                 判定维度 6(`audit_l3._S1`,默认放行、拿不准 pass),不是删了拉倒。
#                 删掉的 rule_code:`cat_requires_cert_small_part`(存量 hits 里
#                 仍有,理由渲染保留兼容)。
#                 影响面:曾被 spec 那条链判死的产品会翻案。**定点重审**:
#                   python cli.py product_audit -p rerule=cat_requires_cert_hard
#                   python cli.py product_audit -p rerule=cat_requires_cert_small_part
# c.2026-08-20.1  **判定面大改**(所有者定稿:先补白名单、再删黑名单,无真空期):
#                 ① 删 L2 R0(代码里 8 个 walmart_category 硬禁)、L2 R2
#                    (yaml 18 条禁售大类)、L1 excluded(yaml 13 条 3C/服饰/
#                    汽配/带电)——三份清单和 R1 类目准入白名单讲同一件事。
#                    类目能不能做**从此只有 R1 一处判据**。
#                 ② R1 两条静默放行改判 pending(PT 未知 / PT 不在 walmart_pt_meta):
#                    此前"查不到 = 没问题"直接 100 分放行,删掉黑名单后再没人兜底。
#                 ③ 修三条"看着在跑其实没跑":R3 裸子串(`ul` 命中 `regulation`)、
#                    R4 中文紧邻不算词边界(中文品牌一条都拦不住)、R7 只命中软词
#                    时整条证据丢掉;另修 _infer_walmart_policy 的 medical 分支
#                    与 L3 提示词两处(R7/R8 不进 prompt、cert 取了不存在的键)。
#                 影响面:曾被 R0/R2/excluded 拦下、而白名单放行的产品会翻成 pass;
#                 曾因 R3 裸子串误判"要 UL 认证"的会翻案。**全量重审**:
#                   python cli.py product_audit -p force_rerun=c.2026-08-18.1
# c.2026-08-18.1  理由映射:黑名单中心三码(lark_blacklist_asin/seller/
#                 amazon_cat)→ 政策 None(内部决策,不挂 [政策:General-Use
#                 Products] 兜底尾巴)。判定本身零变化(仍 reject),只影响
#                 audit_reason 与 F 列文案。历史行翻新(零 LLM,L0 短路):
#                   python cli.py product_audit -p rerule=phase0_lark_blacklist_asin
#                   python cli.py product_audit -p rerule=phase0_lark_blacklist_seller
#                   python cli.py product_audit -p rerule=phase0_lark_blacklist_amazon_cat
#                 ⚠ 别拿它跑 force_rerun —— 那是全量。
# c.2026-08-17.1  Phase0 规则 2 摘掉批次 B 新增的 4 个 Amazon 顶级大类
#                 (裁决 A,见 docs/audit_migration_plan.md 九节补批复)。
#                 ⚠ 别拿它跑 force_rerun —— 那是**全量**(库里没有一条是新版本)。
#                 只重审被摘掉的规则拒过的那批:-p rerule=phase0_forbidden_category
# c.2026-08-13.1  批次 C:L1 rerank + L3 语义 + L4 视觉接线


# ── 政策表旧名 → 官方名(一次性迁移映射,2026-09-02)────────────────────────
#
# 定稿 `docs/policy_sync.md` §十.7:**官方政策类别名 = 全链唯一键**。生产表
# `audit.walmart_prohibited_policy` 的存量行用的是旧仓搬迁时的缩写名,按
# `policy_sync.norm_category` 的词形归一化(&↔and / 逗号 / 括号后缀 / 单复数)
# **故意对不上**官方全称 —— 缩写差是**语义合并**,不许在归一化里偷偷做。
# 这张表就是那一步人工裁决的**落纸**:哪个旧名是哪个官方类别,由所有者认。
#
# 用途只有两处,都在过渡期内:
#   · `policy_sync` 真跑时凭它认领存量行,把 `category_en` 改成官方拼写;
#   · `services/error_taxonomy.POLICY_ALIASES` 从它**派生**(反向:官方名 →
#     旧名),让改名落地前的报错文本 join 照旧对得上。
#
# ⚠ **仅为一次性迁移用**:生产改名落地后,这张表与 POLICY_ALIASES 一起
#   随第三步 L3 批**整体删除**(留着 = 一份永远不会再被验证的历史映射)。
# ⚠ 键是**表内旧名的精确字面量**(不归一化匹配:旧名是历史事实,不是词形);
#   值必须与 `refdata/policy_pages/en/*.md` 的头注 H1 **逐字一致**(含
#   Tobacco 那条**没有牛津逗号**、Children’s 的弯撇号 —— 官方怎么写就怎么抄)。
# ⚠ 前 7 条来自生产存量实证(§十.6);后 4 条来自 2026-09-02 首跑 dry-run 报告的
#   「官方已不含」清单 —— 3 条由报告「疑似改名对」点名,第 4 条 Biodegradable Plastic
#   ↔ Product claims 按页面内容判定(官方 Product claims 页正文逐条列出
#   Biodegradable / Degradable / Compostable 宣称,是同一政策页改名扩写)。
#   所有者 dry-run 看到「未对上」里还有别的拼写差时,**在这里追加**,不要另起第二张表(双轨禁止)。
POLICY_LEGACY_NAMES: dict[str, str] = {
    "Auto & Motor Vehicles":      "Auto and Motor Vehicles",
    "Textiles & Apparel":         "Textiles and Apparel",
    "Drugs & Paraphernalia":      "Drugs and Drug Paraphernalia",
    "Military & Law Enforcement": "Military and Law Enforcement Products",
    "Electronics & RF":           "Electronics and Radio Frequency Devices",
    "Ride-Ons & Micromobility":   "Ride-Ons and Micromobility Devices",
    "Tobacco & Vaping":           "Tobacco, E-Cigarettes and Vaping Products",
    "Jewelry/Precious Metals":    "Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious Metals (Covered Goods)",
    "Pet Products":               "Pet Foods, Supplements, Medicines and Other Products",
    "Restricted/Illegal":         "Restricted/Illegal Products",
    "Biodegradable Plastic":      "Product claims",
}


# ── 审核类别词表:非政策类别与代码写死的政策名(2026-09-02 §二 定稿)───────
#
# 「类别」= 判定落在哪一类,**只许两种来源、零推断**(`docs/audit_step3_spec.md` §二):
#   ① 官方政策类别名 —— `audit.walmart_prohibited_policy.category_en` 实时集合
#      (44 条:42 类禁售 + 内容族两页),不在本文件里写死;
#   ② 下面这两条**非政策类别** —— 它们不对应任何一条沃尔玛政策,政策表怎么改
#      都影响不到,所以只能是常量。别再往这个元组里加第三条:每加一条就是给
#      "判不清就新造一个类别"开一道门(旧链的 `General-Use Products` 兜底
#      就是这么长出来的,所有者 2026-08-16 实遇「这是什么意思」)。
AUDIT_CAT_INTERNAL_BLACKLIST = "内部黑名单"   # 卖家/ASIN/亚马逊类目黑名单命中(内部决策)
AUDIT_CAT_ACCESS = "类目准入"                 # 类目白名单拦下 / 出版物硬禁 / 需证而无
AUDIT_NONPOLICY_CATEGORIES = (AUDIT_CAT_INTERNAL_BLACKLIST, AUDIT_CAT_ACCESS)

# 规则代码里**唯一写死的政策类别名**:品牌黑名单 / 商标符号 / 专利自述三条硬拒
# 与 L3 的品牌翻拒都判它(§二 表「现状不变」那三行)。它是政策表里的一行,
# 拼写必须与表内一致 —— `services/audit_rules.load_context` 装配时对表解析一次,
# 解析不到或拼写不同**启动即 RuntimeError**(表改名了而代码没跟上,不许静默)。
AUDIT_IP_POLICY = "Intellectual Property"


# LLM 用途→模型 env 映射(批复 #1,2026-08-13:DeepSeek 分用途选模型;
# 未配置的用途回落 DEEPSEEK_MODEL 默认。api/llm.py 批次 C 接线 purpose
# 参数;视觉走豆包 api/llm_vision.py,不在此表)
LLM_PURPOSE_ENV = {
    "default": "DEEPSEEK_MODEL",
    "audit_l1": "DEEPSEEK_MODEL_AUDIT_L1",
    "audit_l3": "DEEPSEEK_MODEL_AUDIT_L3",
    # 变体维度错位重映射(旧仓 Phase 0.8 补迁,2026-08-17):亚马逊维度名不在
    # PT 枚举内时问一次"它实际表达什么"。调用极少(命中即缓存,键按
    # (PT, 维度名) 定案),未配置专用模型时回落 DEEPSEEK_MODEL
    "variant_remap": "DEEPSEEK_MODEL_VARIANT_REMAP",
    # 上架出参(mp_mapper:把亚马逊产品映成沃尔玛 PT 的属性)。2026-08-21 建:
    # 此前这条链**不传 purpose**,于是全落进 "default" 桶 —— 记是记了,但摘要里
    # 和别的默认调用混成一坨,换模型时看不出"上架这一段到底花了多少"
    "listing_attrs": "DEEPSEEK_MODEL_LISTING_ATTRS",
}

# ── 沃尔玛五大品类(Walmart Category 之上的一层)────────────────────────
#
# 来源:所有者 2026-08-21 提供的沃尔玛官方招商材料「五大品类多元商品」。
# 这是沃尔玛自己的商品部门划分,坐在 `Walmart Category`(库里 26 个值)**之上**。
#
# ⚠ **两层都在用,别混**:附 A.1(2026-08-07)拍的「大类目 = Walmart Category」
# 是**下层**(库里 26 个值,产品侧的事实);所有者心智里的"大类"是**上层**。
# 2026-08-21 实测这个差异不是术语出入 —— 按下层判,品牌组内的少数派件
# 156,188 件(全池 24.2%)会被锁死在做不了那个大类的店里;折到上层判是
# 105,571 件(16.3%)。
# **Q1 已拍板(2026-08-21):类目闸判上层。** 见 `store_targets.allowed`。
# 下层不作废 —— 它仍是产品侧的事实来源,报告要拿它当佐证(说"缺 Home 品类"
# 而不说是哪几个 26 类,所有者没法照着去开类目)。
#
# 归类由所有者 2026-08-21 逐条拍板,其中四条是他当天点名回的:
#   Musical Instruments   → ETS
#   Business & Industrial → Hardlines
#   Safety & Emergency    → **不归**(None)
#   Everything Else       → **不归**(None)
# 「不归」不是漏填,是一条口径:这两类**只能分给没有确定类目的店**。它正好
# 落在 `store_targets.allowed` 已有的两条规则上(「三列全空 = 不限制」+
# 「归不到大类的,受限店拒收」),所以映成 None 就够了,不需要任何新逻辑。
WALMART_SUPER_CATEGORIES = ("Fashion", "ETS", "Home", "FCHW", "Hardlines")

# 「不归五品类」那一桶的**显示名与可填名**(所有者 2026-08-22:建议列要
# 「填写 5 大类和其他」)。它是限额表「类目1/2/3」里的**一等值** —— 填了
# 「其他」的店就是"专收归不到五品类的货"那种店。
# ⚠ 有它之前,「其他」填进表里会把店废掉:折完是空集 ⇒ 按「填了就只准入填的
# 那几个」判 ⇒ 谁也接不了。所以出建议之前必须先让它可填,否则那份建议
# 是照着做就出事的。
SUPER_OTHER = "其他"

# 限额表「类目1/2/3」三列的**可填值全集**(建议列只从这里出)。
SUPER_BUCKETS = (*WALMART_SUPER_CATEGORIES, SUPER_OTHER)

# Walmart Category → 五大品类;**不在表里 = 归不到**(与映成 None 同义)。
_SUPER_CATEGORY_OF = {
    "Fashion": "Fashion",
    "Electronics": "ETS", "Toys": "ETS", "Occasion & Seasonal": "ETS",
    "Media": "ETS", "Photography": "ETS", "Musical Instruments": "ETS",
    "Home": "Home", "Furniture": "Home", "Arts & Crafts": "Home",
    "Beauty": "FCHW", "Health & Personal Care": "FCHW", "Animals": "FCHW",
    "Baby": "FCHW", "Household": "FCHW", "Food & Beverage": "FCHW",
    "Office": "Hardlines", "Sporting Goods": "Hardlines",
    "Sports & Outdoors": "Hardlines", "Vehicles": "Hardlines",
    "Automotive": "Hardlines", "Home Improvement": "Hardlines",
    "Garden & Patio": "Hardlines", "Business & Industrial": "Hardlines",
    # Safety & Emergency / Everything Else 刻意不列 —— 见上面「不归」那条
}

# 那两个**是**合法的 Walmart Category,只是不归五品类。单列出来是因为
# 「认不认得这个填写值」与「归不归得到品类」是两个问题:把它们判成"认不出"
# 会让 alloc_audit 去点名两个其实填得对的值,人就学会忽略那一栏了。
_UNMAPPED_CATEGORIES = ("Safety & Emergency", "Everything Else")


def _fold(v) -> str:
    """输入:任意填写值 → 输出:比对用的归一键(小写 + 内部空白压单空格)。

    限额表「类目1/2/3」是**人手填的**,而同一张表的「配送限制」列早就做了
    `fba/FBA/Fba` 都认(`store_targets._channel` 的 `.upper()`)—— 类目这三列
    漏了。2026-08-22 实测:所有者填「hardlines」,查不到规范名 `Hardlines`,
    被静默兜进「其他」⇒ 这家店的准入从 Hardlines 变成了「只收归不到的货」,
    **没有任何报错**。
    ⚠ 只归一大小写与空白,**不做别的猜测**:不去标点、不认中文译名、不做
    近似匹配。猜错一次就是一家店收错一批货,而占用撤不回。真填错了就让
    `known_category_literal` 报出来,人改一格比代码猜一辈子强。
    """
    return " ".join(str(v or "").lower().split())


# 归一键 → 规范值。26 类映到它的品类,五品类与「其他」映到自己。
_BUCKET_INDEX = {
    **{_fold(c): b for c, b in _SUPER_CATEGORY_OF.items()},
    **{_fold(c): SUPER_OTHER for c in _UNMAPPED_CATEGORIES},
    **{_fold(b): b for b in SUPER_BUCKETS},
}

# 认得的填写值(归一键)。与 `_BUCKET_INDEX` 同源 —— 两处各列一遍必然漂。
_KNOWN_LITERALS = frozenset(_BUCKET_INDEX)


def super_category(category: str | None) -> str | None:
    """输入:Walmart Category → 输出:五大品类之一,或 None(**归不到**)。

    ⚠ **闸门与报告都不要用这个,用 `super_bucket`。** 这一支只回答一个
    很窄的问题:"这个 26 类映得到五品类吗" —— 它给 `Everything Else` 和
    一个拼错的字符串**同样的 None**,分不清"业务上归不到"与"根本不认得"。
    2026-08-22 之前闸门用的就是它,后果是填「其他」的店被折成空集而废掉。
    现存的正当用途只有一处:`tests/test_alloc_registry` 拿它盘点**哪些 26 类
    还没映射**(换成 `super_bucket` 那条测试就永远绿了,盘不出漏项)。
    """
    return _SUPER_CATEGORY_OF.get((category or "").strip())


def super_bucket(category: str | None) -> str | None:
    """输入:Walmart Category(或已经是品类名/「其他」)→ 输出:
    五大品类之一 / `SUPER_OTHER`;**真·未知(空值)仍返回 None**。

    与 `super_category` 的分工 —— 这是**总函数**版本,给闸门与报告用:
    把"归不到"从 `None` 折成一个能显示、能填表、能进集合的值。

    ⚠ **空 ≠ 其他,这条不许合并。** 「其他」是"我们知道它属于 Safety &
    Emergency / Everything Else 这类"(一条业务归类);空是"我们不知道它
    属于哪类"(一条数据缺口)。合并的后果:填了「其他」的店会开始收**大类
    采不到**的货 —— 而那批货的处置是补采集,不是分给谁。`category_offenders`
    也正是靠这条区分才没把"不知道"当成"违规"。

    ⚠ **大小写与多余空白不算认不出**(`hardlines` = `Hardlines`)—— 这三列
    是人手填的,与同表「配送限制」列的 `fba/FBA` 一视同仁。

    ⚠ **认不出的字面量也归「其他」**,不再像原来那样被静默丢掉。丢掉时
    表里一个拼写错误会让店变成"填了但空集 = 谁也接不了";归「其他」则是
    "只收归不到的货"。两种都不对,但后者不会静默把一家店废掉,而且
    `alloc_audit` 会把认不出的值单独点名(见 `known_category_literal`)。
    """
    key = _fold(category)
    if not key:
        return None
    return _BUCKET_INDEX.get(key, SUPER_OTHER)


# 报表里「大类采不到」那一格的显示值。**不是** SUPER_OTHER:「其他」是业务
# 归类(Safety & Emergency / Everything Else),这个是数据缺口 —— 两者处置
# 不同(找一家收「其他」的店 vs 补一次采集),在表上必须一眼分得开。
UNKNOWN_SUPER = "(大类未知)"


def super_label(category: str | None) -> str:
    """输入:Walmart Category → 输出:**报表列**里那个品类值(总是有字)。

    所有 csv 的「品类」列都走这一个函数 —— 每处各写一遍
    `super_bucket(x) or "…"` 的话,兜底文案迟早分叉,而这一列是所有者用来
    跟飞书限额表对照的,两张表写法不一样就对不上了。
    """
    return super_bucket(category) or UNKNOWN_SUPER


def known_category_literal(value: str | None) -> bool:
    """输入:限额表「类目1/2/3」里的一个填写值 → 输出:这个值认不认得。

    认得的三种:26 个 `Walmart Category` 之一、五大品类之一、或「其他」;
    **大小写与多余空白不算错**(`hardlines` = `Hardlines`,见 `_fold`)。
    认不出的会被 `super_bucket` 折进「其他」——**不报出来就是静默改变准入**,
    所以 `alloc_audit` 必须逐店点名(拼写错、旧类目名、随手写的中文都在此列)。
    """
    return _fold(value) in _KNOWN_LITERALS


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

# TRO 品牌的判据(2026-08-30 建,product_audit 的 TRO 命中接线用)。
# 上面这张总表的「来源」列是**自由文本**,由所有者手填、risk_sync 原样镜像进
# catalog.brand_blacklist.source。生产实证取值三种:「TRO品牌」(22,527 行)/
# 「TRO」/「tro」——所以判据是 `source.strip().lower().startswith(前缀)`,
# 前缀匹配三种全中,且没有别的来源词以 tro 开头,零误伤。
# ⚠ 常量登记在此、判定写在 services(services/audit_store.tro_hits 收前缀参数):
# registry 只登记外部资源的取值口径,不替业务判。改这个前缀 = 改「谁算 TRO」,
# 改之前先看 product_audit 日志里那行「R4 品牌来源:TRO 前缀 N 词」还非不非零。
TRO_BRAND_SOURCE_PREFIX = "tro"


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
# 维护记录(流水账,只追加):A~K 十一列。
# 2026-08-16 所有者在飞书加了「建议」「原因」两列(9 → 11),配合 maintenance
# 拆成 scan(决策,写 建议/原因)+ 执行件(写 动作/feedid/结果/报错)。
# ⚠ 「原因」不是装饰:四条清零判据(Currently unavailable / No Featured Offer /
#   out_of_stock / 配送超时)在表里长得一模一样(库存 12 → 0),没有原因列
#   分不出是哪一条触发的;删除那类更要紧(not_found 与 标题相似度 42% 都是删除,
#   但正确性判断完全不同)。
# ⚠ services/maint_sheet 按**本元组的下标**算列字母,不再硬编码 A/H/I ——
#   下次再加列只改这里一行。
MAINT_SHEET = Spreadsheet(
    name="维护记录",
    token=os.environ.get("FEISHU_ONLINE_SHEET_TOKEN", ""),
    sheet_id=os.environ.get("FEISHU_MAINT_SHEET_ID", ""),
    columns=("store", "sku", "suggestion", "reason", "action",
             "old_value", "new_value", "feed_id", "op_date", "result", "error"),
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
        # 店铺目标三列(所有者建列 2026-08-12,Q4 拍板;分配引擎 A2 消费):
        # 销售额与订单均为**日目标**;最大在线数是总容量上限(≠上架限制的日配额)
        target_gmv_daily="目标销售额",
        target_orders_daily="目标订单",
        max_online="单店最大在线数",
        # 一店一配送方式的权威列(所有者建列 2026-08-13):填 fba/fbm,
        # 填什么就只给该店**分配 / 上架 / 维护**该渠道的产品。
        # ⚠ **一列,三个消费方**(2026-08-25 从一个扩到三个;取数唯一口
        # `services.store_targets.store_channels`,判定唯一谓词
        # `store_targets.channel_conflict` —— 别在消费方那边另写):
        #   · 分配 alloc_engine._blocker      —— 只把该渠道的组发给它
        #   · 上架 list_new                    —— 只上该渠道的货
        #   · 维护 maintenance_intents         —— 货翻成另一个渠道 ⇒ 库存写 0,
        #     连续 N 天卖不了 ⇒ 走删除链「渠道不符 N 天」下架(与缺货同一阶梯)
        # ⚠ 三者对"这一格没填"的解释**不同,而且都是对的**(与 lead_limit 同款):
        #   分配 = 不接自由流(没渠道就过不了硬闸,宁可不分);
        #   上架/维护 = **不限制**(所有者定稿 2026-08-25「没标就都能上」——
        #   照搬分配那条会把没配置的店整店废掉:一件也上不了、在架的还全被清零)。
        # ⚠ 对"产品渠道没采到"三者口径一致:**不算不符**。第三种值恒高说明
        #   采集侧 is_fba 解析坏了,那是要修采集,不是要动商品。
        # ⚠ **规划外店(谭总系)照判**(所有者定稿 2026-08-25「维护链对规划外店
        #   不豁免」;上架链同):规划外排除的是「归属」——不给它们分货、不占
        #   品牌与产品、不拦别人上架——**不是**"这家店能不能卖这个渠道的货",
        #   后者是店铺自己的经营配置,填了就该生效。
        #   由此**分配/审计两条链与上架/维护两条链在这一点上口径不同,且都对**:
        #   `alloc_survey.claimable` 在判渠道之前就剔掉规划外店,所以这些店的
        #   渠道不符行**永远不会**进 alloc_audit 的下架清单,而维护链照样清零、
        #   照样走「渠道不符 N 天」下架。两份报告在这批行上对不上是预期的
        #   (maintenance_scan 的 ⚑ 旗标就是为了让人别把它当成漏报)。
        channel_limit="配送限制",
        # 店铺准入大类目(所有者建列 2026-08-15):**只准入表里填的大类**,
        # 三列都空 = 该店不限制类目。这三列是类目档案的**唯一权威**——
        # 因此不建 catalog.store_categories 表(与配送限制同一治理方式:
        # 人工配置直读,改类目 = 改表格一格,引擎永不自行开类目)
        category1="类目1",
        category2="类目2",
        category3="类目3",
        # 逐店配送时长上限(所有者建列 2026-08-16)。
        # ⚠ **一列,三个消费方,不是三个常量**(2026-08-16 合并时撞车:
        # 分配链与上下架链各建了一个常量指同一列 —— 表头一改名只坏一半,
        # 另一半静默读空)。谁在读:
        #   · 分配   services.store_targets.lead_ok   —— 只分配 ≤ 该值的产品
        #   · 上架   list_new                          —— 超限**不上架**
        #   · 维护   maintenance                       —— 超限**库存写 0**
        # ⚠ 三者对"这一格没填"的解释**不同,而且都是对的**:
        #   分配 = 不限(该店不挑货期);上架/维护 = 回落全局
        #   `amz_source.MAX_LEAD_DAYS`(=7,既有链路的基线)。
        #   这一列是在全局基线之上**再收紧**,不是替代它。
        # ⚠ 对"产品没采到配送天数"的解释也相反,同样都是对的 ——
        #   分配侧拒收(宁可不分也不错分),上架/维护侧放行
        #   (不拿"未知"当"超限"去删/清零)。方向不同是因为动作不同。
        lead_limit="配送时长限制",
        # 受管发货节点(所有者建列 2026-08-24,多仓改造)。填 Seller Center →
        # Shipping Profile → Seller Fulfillment 里的 **FC ID**(即官方 shipNode,
        # 17-18 位数字)。**留空 = 该店走 Virtual Node**,行为与改造前逐字节一致。
        # 谁在读:上架(list_new 的 fulfillmentCenterID)、维护(库存读写的节点)。
        # ⚠ 填错不回落:认不出的 FC ID 会让该店**整店跳过并告警**——静默回落
        # Virtual Node 等于把新仓的货写到旧节点,比不动更坏(见
        # docs/multi_node_plan.md §3)。
        maint_node="维护仓库",
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
