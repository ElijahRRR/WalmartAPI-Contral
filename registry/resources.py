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
    columns=("store", "sku", "wpid", "upc", "gtin", "productName", "shelf",
             "productType", "price", "currency", "availToSellQty",
             "publishedStatus", "lifecycleStatus", "unpublishedReasons",
             "last_seen_at", "missing_since"),
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
