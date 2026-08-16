"""调度表:哪条链、什么时候、跑什么。**全项目唯一出处**。

⚠ 为什么是代码不是 10 个 XML:同一张表写两处必然漂 —— 时间表已经被所有者的
批复推翻过两次(产品线由四条散点改成一条链、`returns_sync` 从每日改每小时、
`sku_locked_heal` 移出调度)。plist 由 `services/launchd` 从这里渲染,
文档 `docs/schedule_plan.md` §九 只是它的人读投影。

批次 = 灰度上线的分组(见 schedule_plan §十),不是优先级:
  1 只读/低危 —— 起了先看 `ops.runs` 的时长与失败率
  2 订单 + 日报 —— 开之前必须先停旧 KPI 与旧订单同步(**两条同停**)
  3 破坏性 —— 每条**先手动 --dry-run 人眼确认**再 load

不在表里的一律**手动**:上架链(list_new / match_listing)、分配链
(alloc_* / claim_audit / alloc_backfill)、审核(product_audit)、
补采(scrape_missing / brand_scrape)、自愈(sku_locked_heal)、
一次性迁移与体检(各 *_import / catmap_* / catalog_health / …)。
"""

_REPO = "/Users/nextderboy/Projects/WalmartAPI-Contral"

# 解释器:venv 里的那个 python,**不需要 activate**(venv 的 python 自己把
# site-packages 摆对)。launchd 不读 shell 配置,必须绝对路径。
PYTHON = f"{_REPO}/.venv/bin/python3"
REPO_DIR = _REPO
LABEL_PREFIX = "com.walmartapi."

# plist 里要显式给的环境变量。launchd **不继承任何 shell 配置**:
# `~/.zshrc` 里的 export 一概不生效。密钥走 <DATA_ROOT>/.env(cli 自己加载),
# 但 PATH 要给 —— `pg_dump`(backup 用)不在默认 PATH 里就报"命令未找到"。
ENV = {
    "WALMART_OPERATOR": "launchd",
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}


def job(label, workflows, *, batch, hour=None, minute=0, weekday=None,
        params=(), note=""):
    """一条调度。hour=None ⇒ 每小时的那一分钟;minute 可给列表(每半小时)。"""
    return {"label": label, "workflows": list(workflows), "batch": batch,
            "hour": hour, "minute": minute, "weekday": weekday,
            "params": list(params), "note": note}


JOBS = (
    # ── 批一:只读 / 低危 ────────────────────────────────────────────────
    job("backup", ["backup"], batch=1, hour=2, minute=0,
        note="pg_dump,离峰;⚠ PATH 里要有 pg_dump"),
    job("blacklist", ["risk_sync", "blacklist_push"], batch=1, hour=2, minute=30,
        note="黑名单双向同步,排在当天所有上架/审核之前"),
    job("feed_poll", ["feed_poll"], batch=1, minute=[0, 30],
        note="每 30 分;feed 落定后越早反哺,飞书上的「处理中」越少"),

    # ── 批二:订单 + 日报 ────────────────────────────────────────────────
    # ⚠ 只挂这一条,**不要**再单挂一个 06:20:两个 plist 撞在同一分钟会各拿
    # 各的锁,后到的整链退出码 3 空跑一轮。日报依赖的那次就是每小时的 06:20 那次。
    job("order_chain", ["order_sync", "order_audit", "returns_sync"],
        batch=2, minute=20,
        note="每小时 :20;order_audit 默认 wait=1,最长阻塞 20 分钟等采集落定"),
    job("daily_report", ["daily_report"], batch=2, hour=6, minute=40,
        note="KPI 窗口锚 06:30,必须 ≥06:35;⚠ 开它之前先停旧 KPI 调度"),
    job("order_daily", ["perf_problems", "order_asin_normalize"],
        batch=2, hour=7, minute=30,
        params=["order_asin_normalize:apply=1"],
        note="⚠ apply=1 不能省:order_asin_normalize 缺省是预览,漏了就每天空转报成功"),
    # ⚠ launchd 没有"双周"。账期是双周发布,但 settlement_sync 对已入库账期
    # **永不重拉**(DISTINCT period + recon_done 台账),所以每周三跑一次是安全的:
    # 没有新账期那轮就是空转,有了才拉。用频率换掉一个 launchd 表达不了的周期。
    job("settlement", ["settlement_sync"], batch=2, weekday=3, hour=8, minute=0,
        note="每周三;账期双周发布,没有新账期那轮自然空转(已入库账期永不重拉)"),

    # ── 批三:破坏性 ────────────────────────────────────────────────────
    # 产品维护线一条链跑完(所有者定稿:「这些我认为可以一次性做完」)。
    # ⚠ wait=1 不能省:不等采集落定就往下走,product_ingest 摄回来的是上一轮
    # 的数据,而且不报错。整条约 2 小时(采集 ~50 分钟是大头)。
    job("product_chain",
        ["catalog_sync", "product_refresh", "product_ingest",
         "maintenance_scan", "maintenance",
         "problem_scan", "problem_product_cleanup"],
        batch=3, hour=9, minute=0,
        params=["product_refresh:wait=1"],
        note="整条 ~2 小时;前一步不成功就不跑后面的(拿隔夜现值当判据会误伤)"),
    job("product_clear", ["product_clear"], batch=3, hour=15, minute=0,
        note="消费运营填的「停用/删除表」;不定时跑 = 填了没人执行"),
)
