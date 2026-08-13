"""init_data_root — 初始化 <DATA_ROOT> 目录结构与 .env 模板(幂等)。

用法:python cli.py init_data_root
DATA_ROOT 默认 ~/walmart_data,可用环境变量 WALMART_DATA_ROOT 覆盖
(launchd 不读 shell 配置,生产机若用非默认路径需在 plist 里设该变量)。
"""

import os

from registry import paths

DANGEROUS = False

_ENV_TEMPLATE = """\
# WalmartAPI-Contral 密钥与环境配置(本文件 chmod 600,永不进 git)
# 值为空的条目按需填写;变量名清单与含义见 registry/resources.py

# PostgreSQL(默认本机 socket 连 walmart_data,通常无需改)
#WALMART_PG_DSN=dbname=walmart_data

# 飞书自建应用凭据(注意:旧系统的 APP_SECRET 已进 git 历史,必须在飞书后台轮换后再填)
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# 运行通知群机器人 webhook(不填则通知降级为仅日志)
FEISHU_WEBHOOK_URL=

# 店铺凭证多维表格(在飞书建表后,从表格 URL 取 app_token 与 table_id 填入)
FEISHU_STORE_TABLE_APP_TOKEN=
FEISHU_STORE_TABLE_ID=

# 订单审核两张配置表
# 黑名单邮编(wiki 电子表格:token=/wiki/ 后段,sheet_id=?sheet= 参数)
FEISHU_ZIP_BLACKLIST_WIKI_TOKEN=
FEISHU_ZIP_BLACKLIST_SHEET_ID=
# 采购方(多维表格)
FEISHU_SUPPLIER_APP_TOKEN=
FEISHU_SUPPLIER_TABLE_ID=

# 黑名单两张投影表(PG→飞书,blacklist_push 写;所有者建 2026-08-11)
# ASIN 表=库的全量映射;BRAND_ERR=只承接沃尔玛后台问题商品拿到的品牌
# (归拢总表的增量渠道)。BRAND_ERR 若在另一份 wiki 文档,
# 填 FEISHU_BRAND_ERR_WIKI_TOKEN;留空回落到 FEISHU_BLACKLIST_WIKI_TOKEN
FEISHU_BLACKLIST_WIKI_TOKEN=
FEISHU_ASIN_BLACKLIST_SHEET_ID=
FEISHU_BRAND_ERR_WIKI_TOKEN=
FEISHU_BRAND_ERR_SHEET_ID=
# 黑名单中心两张只读源(飞书→PG,risk_sync TRUNCATE 全量重灌;
# 同一份 wiki 文档,复用 FEISHU_BLACKLIST_WIKI_TOKEN;定稿 2026-08-13)
FEISHU_SELLER_BLACKLIST_SHEET_ID=
FEISHU_AMZCAT_BLACKLIST_SHEET_ID=

# listing 风控两张只读源(飞书→PG,risk_sync 镜像入库)
# FEISHU_BRAND_* = 黑名单品牌总表(各渠道人工归拢的总清单,方向与
# BRAND_ERR 相反,别混;2026-08-11 换新表)
FEISHU_RISK_PT_WIKI_TOKEN=
FEISHU_RISK_PT_SHEET_ID=
FEISHU_BRAND_WIKI_TOKEN=
FEISHU_BRAND_SHEET_ID=

# 沃尔玛 API(默认 production;沙箱测试才需要设)
#WALMART_BASE_URL=https://sandbox.walmartapis.com

# db_init 创建只读角色 readonly 用(不填则跳过角色创建)
#READONLY_DB_PASSWORD=
"""


def run(params: dict) -> str:
    """输入:params(无参数)→ 输出:初始化结果摘要(建了哪些目录、.env 是否新建)。"""
    root = paths.data_root()
    created = paths.ensure_data_root()

    env = paths.env_file()
    if env.exists():
        env_note = ".env 已存在(未动)"
    else:
        env.write_text(_ENV_TEMPLATE, encoding="utf-8")
        os.chmod(env, 0o600)
        env_note = ".env 模板已创建(chmod 600),请填写密钥"
    # 无论新旧,确保权限收紧
    os.chmod(env, 0o600)

    made = "、".join(created) if created else "无(全部已存在)"
    return f"DATA_ROOT={root};新建目录:{made};{env_note}"
