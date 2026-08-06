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
    columns=("store", "sku", "itemId", "upc", "gtin", "productName", "shelf",
             "productType", "variantGroupId", "variantGroupInfo",
             "price", "currency", "availToSellQty",
             "publishedStatus", "lifecycleStatus", "unpublishedReasons",
             "last_seen_at", "missing_since"),
)


# ── 订单中心四表(order_center_push 写,程序独占展示投影,可随时重建)────────
# 同一个多维表格应用(app_token 共用)下四张数据表;PG orders schema 是权威,
# 飞书行数受窗口参数约束(销售/售后默认 90 天),远低于 5 万行套餐上限。
# 每表第一列是去重键(sync_by_key 的对齐锚点),人工不得改动该列。

_ORDER_APP = os.environ.get("FEISHU_ORDER_APP_TOKEN", "")

ORDER_SALES = Bitable(
    name="订单中心-销售订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_SALES_TABLE_ID", ""),
    fields=_fields(
        key="行ID", store="店铺", po_id="PO号", line_number="行号",
        customer_order_id="客户单号", sku="SKU", product_name="商品名",
        qty="数量", sale_status="订单状态", status_date="状态时间",
        order_date="下单时间", product_amount="商品金额", shipping_amount="运费",
        cancel_reason="取消原因", refund_amount="退款金额",
        carrier="承运商", tracking_no="物流单号", tracking_url="物流链接",
        return_status="售后状态", refund_status="售后退款状态",
        return_total="售后金额", perf_metrics="绩效指标",
        settled_net="入账净额", settle_status="入账状态", audit_status="审核结果",
    ),
)

ORDER_RETURNS = Bitable(
    name="订单中心-售后订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_RETURNS_TABLE_ID", ""),
    fields=_fields(
        key="唯一键", rma="RMA号", order_line_id="订单行ID", store="店铺",
        po_id="PO号", sku="SKU", product_name="商品名",
        return_status="售后状态", refund_status="退款状态",
        return_method="退货方式", is_keep_it="免退回", refund_total="退款金额",
        return_reason="退货原因", qty="数量", refunded_qty="已退数量",
        carrier="承运商", tracking_no="物流单号",
        return_created="发起时间", return_by="退回截止",
    ),
)

ORDER_PERF = Bitable(
    name="订单中心-绩效订单",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_PERF_TABLE_ID", ""),
    fields=_fields(
        key="唯一键", store="店铺", po_id="PO号", metric="指标",
        sku="SKU", product_name="商品名",
        first_period="首次周期", last_period="最近周期",
        periods_seen="出现周期数", ever_accountable="计入绩效",
        still_active="仍在影响",
    ),
)

ORDER_SETTLE = Bitable(
    name="订单中心-对账明细",
    app_token=_ORDER_APP,
    table_id=os.environ.get("FEISHU_ORDER_SETTLE_TABLE_ID", ""),
    fields=_fields(
        key="行ID", store="店铺", po_id="PO号", line_number="行号",
        sku="SKU", product_name="商品名",
        net_amount="净额", gross_amount="交易额",
        product_amount="商品金额", commission_amount="佣金",
        periods="账期数", last_settle_date="最近入账日", settle_status="入账状态",
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
