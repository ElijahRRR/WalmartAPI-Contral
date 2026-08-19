"""调度表:哪条链、什么时候、跑什么。**全项目唯一出处**。

⚠ 为什么是代码不是 10 个 XML:同一张表写两处必然漂 —— 时间表已经被所有者的
批复推翻过两次(产品线由四条散点改成一条链、`returns_sync` 从每日改每小时、
`sku_locked_heal` 移出调度)。plist 由 `services/launchd` 从这里渲染,
文档 `docs/schedule_plan.md` §九 只是它的人读投影。

批次 = 灰度上线的分组(见 schedule_plan §十),不是优先级:
  1 只读/低危 —— 起了先看 `ops.runs` 的时长与失败率
  2 订单 + 日报 —— 开之前必须先停旧 KPI 与旧订单同步(**两条同停**)
  3 破坏性 —— 每条**先手动 --dry-run 人眼确认**再 load

runner = 谁来按这个点触发(所有者定稿 2026-08-16,**推翻了 v5 的"一律不接 GPT"**):
  `launchd`  高频链(feed 轮询 + 订单链)—— 每小时/每半小时的东西交给机器,
             写死在电脑上最稳,不依赖任何智能体在不在线。
  `gpt`      其余每日/每周一次的 —— 注册成智能体的定时任务。所有者原话:
             「前期稳定,也方便我维护和调整,以后换个智能体也能用」。
             改个时间不用改代码、不用 `launchctl unload/load`,而且每次执行
             有个能读日志、能当场判断要不要重跑的东西在旁边。
⚠ runner 只决定**谁按秒表**,不决定跑什么:两边都是同一条 `python cli.py …`,
  同一把 flock 锁,同一份 ops.runs 记录。所以两边**永远不许同时挂同一条链**
  —— 撞上了后到的那条退 3 空跑一轮,而且报"成功"。

不在表里的一律**手动**:跟卖(match_listing)、分配链
(alloc_* / claim_audit / alloc_backfill)、补采(scrape_missing / brand_scrape)、
自愈(sku_locked_heal)、一次性迁移与体检
(各 *_import / catmap_* / catalog_health / variant_probe / audit_why / …)。
⚠ **审核与上架 2026-08-17 起进表**(所有者定稿):`audit_sheet` 18:10、
`list_new` 20:00。此前它们在这份"手动"清单里,是因为上架域还没做生产验收;
验收通过(变体组三条真发上去了)之后排进调度。`match_listing`(跟卖)仍手动
—— 它的对拍还没做完,头注里那条"对拍未完成前只许 --dry-run"仍有效。

**当天的次序是硬约束**(谁提前谁就是拿昨天的数据做今天的判断,而且不报错):
  product_chain 13:00 → blacklist 15:00 → audit_sheet 18:10 → list_new 20:00
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


RUNNERS = ("launchd", "gpt")


def job(label, workflows, *, batch, hour=None, minute=0, weekday=None,
        params=(), note="", runner="launchd"):
    """一条调度。hour=None ⇒ 每小时的那一分钟;minute 可给列表(每半小时)。"""
    if runner not in RUNNERS:
        raise ValueError(f"runner 只能是 {RUNNERS},给的是 {runner!r}")
    return {"label": label, "workflows": list(workflows), "batch": batch,
            "hour": hour, "minute": minute, "weekday": weekday,
            "params": list(params), "note": note, "runner": runner}


def jobs_for(runner: str) -> list[dict]:
    """输入:runner 名 → 输出:归它触发的调度。"""
    if runner not in RUNNERS:
        raise ValueError(f"runner 只能是 {RUNNERS},给的是 {runner!r}")
    return [j for j in JOBS if j["runner"] == runner]


JOBS = (
    # ── 批一:只读 / 低危 ────────────────────────────────────────────────
    job("backup", ["backup"], batch=1, hour=2, minute=0, runner="gpt",
        note="pg_dump,离峰;⚠ PATH 里要有 pg_dump"),
    job("blacklist", ["risk_sync", "blacklist_push"], batch=1, hour=15, minute=0,
        runner="gpt",
        note="黑名单双向同步。**必须排在 product_chain 之后、审核/上架之前**"
             "(所有者定稿 2026-08-17):problem_scan 当天产出的黑名单 ASIN 与"
             "品牌要立刻投影出去,而审核 Phase0 与上架闸读的是 PG,"
             "同步没跑就是拿昨天的黑名单在放行"),
    job("feed_poll", ["feed_poll"], batch=1, minute=[0, 30],
        note="每 30 分;feed 落定后越早反哺,飞书上的「处理中」越少"),

    # ── 批二:订单 + 日报 ────────────────────────────────────────────────
    # ⚠ 只挂这一条,**不要**再单挂一个 06:20:两个 plist 撞在同一分钟会各拿
    # 各的锁,后到的整链退出码 3 空跑一轮。日报依赖的那次就是每小时的 06:20 那次。
    job("order_chain", ["order_sync", "order_audit", "returns_sync"],
        batch=2, minute=20,
        note="每小时 :20;order_audit 默认 wait=1,最长阻塞 20 分钟等采集落定"),
    job("daily_report", ["daily_report"], batch=2, hour=6, minute=40,
        runner="gpt",
        note="KPI 窗口锚 06:30,必须 ≥06:35;⚠ 开它之前先停旧 KPI 调度"),
    job("order_daily", ["perf_problems", "order_asin_normalize"],
        batch=2, hour=7, minute=30, runner="gpt",
        params=["order_asin_normalize:apply=1"],
        note="⚠ apply=1 不能省:order_asin_normalize 缺省是预览,漏了就每天空转报成功"),
    # ⚠ launchd 没有"双周"。账期是双周发布,但 settlement_sync 对已入库账期
    # **永不重拉**(DISTINCT period + recon_done 台账),所以每周三跑一次是安全的:
    # 没有新账期那轮就是空转,有了才拉。用频率换掉一个 launchd 表达不了的周期。
    job("settlement", ["settlement_sync"], batch=2, weekday=3, hour=8, minute=0,
        runner="gpt",
        note="每周三;账期双周发布,没有新账期那轮自然空转(已入库账期永不重拉)"),

    # ── 批三:破坏性 ────────────────────────────────────────────────────
    # 产品维护线一条链跑完(所有者定稿:「这些我认为可以一次性做完」)。
    # ⚠ wait=1 不能省:不等采集落定就往下走,product_ingest 摄回来的是上一轮
    # 的数据,而且不报错。整条约 2 小时(采集 ~50 分钟是大头)。
    job("product_chain",
        ["catalog_sync", "sources_backfill", "product_refresh",
         "product_ingest", "maintenance_scan", "maintenance",
         "problem_scan", "problem_product_cleanup"],
        batch=3, hour=13, minute=0, runner="gpt",
        # product_ingest:lock_wait=900(2026-08-19 所有者定稿):order_chain
        # 每小时 :20 的 order_audit 会在 :25~:45 借 product_ingest 的锁做
        # 就地摄取,与本链 13:25~13:35 轮到 product_ingest 的档期天天咬合。
        # 等它 15 分钟(泵一轮一般几分钟)再跑自己的——等到再泵才补得齐
        # product_refresh 刚采回的增量;跳过 = maintenance 拿隔夜值算差异
        params=["product_refresh:wait=1", "product_ingest:lock_wait=900"],
        note="整条 ~2 小时(13:00 起,约 15:00 收);前一步不成功就不跑后面的"
             "(拿隔夜现值当判据会误伤)。sources_backfill 紧跟 catalog_sync"
             "(所有者定稿 2026-08-19):新发现的在架商品当轮补来源关联,"
             "当轮就能被维护;零缺口时零成本,摘要非零 = 有人绕过登记上架"),
    job("product_clear", ["product_clear"], batch=3, hour=15, minute=0,
        runner="gpt",
        note="消费运营填的「停用/删除表」;不定时跑 = 填了没人执行"),

    # ── 批三·上架域(所有者定稿 2026-08-17 排进调度)────────────────────
    # 当天的次序是硬的:product_chain(13:00,problem_scan 产黑名单)
    #   → blacklist(15:00,把它投影出去、并把飞书侧改动收回 PG)
    #   → audit_sheet(18:10,Phase0 与准入闸读的就是那份 PG 黑名单)
    #   → list_new(20:00,闸门读 PG 的审核结论)
    # 谁提前谁就是在拿昨天的数据做今天的判断,而且**不报错**。
    job("audit_sheet", ["product_audit"], batch=3, hour=18, minute=10,
        runner="gpt", params=["from_sheet=1"],
        note="审上架表里 E 列为空(或 E=pending)的行 + 把库里已有结论投影回 "
             "C~G。⚠ from_sheet **不是强审**:已有结论的零 LLM 直接投影,"
             "只有未审/pending 过退避的才真判;缺省 limit=500,"
             "存量大时改调度表加 -p limit=N,别在提示词里手改。"
             "库里没数据的行走**同轮补采闭环**:推采集 audit_gap_<日界>(插队)"
             "→ 轮询等采完(缺省 20 分钟)→ 就地摄取 → 采回来的这一轮就判掉;"
             "仍缺的把采集侧真实 error_type 写进 F 列(E 留空 ⇒ 下轮重领)。"
             "⚠ 所以这条链**可能跑二十几分钟**,而 20:00 的上架在等它 —— "
             "别把 gap_wait 调到吃掉那 110 分钟,不然上架拿不到锁退 3 空跑一轮"),
    job("list_new", ["list_new"], batch=3, hour=20, minute=0, runner="gpt",
        note="⚠⚠ **开这条之前必须先停旧上架栈**:com.user.autolisting.morning"
             "(06:00,链末尾就是无人值守上架)+ com.nextderboy.erp_worker×20"
             "(常驻,长轮询跑上架)+ dedup 链(每时:05 与 14:02)。"
             "不停就是新旧两套同时对上架表双写、同时消耗 UPC —— 安全铁律"
             "「新旧系统严禁对同一破坏性任务并跑」直接踩上。"
             "停旧顺序见 docs/legacy_schedules.md §D。"
             "UPC **不必单独排一条**:这条链自己开头注入一次 UPC 池、"
             "结尾把池状态回写飞书 C~F(所有者定稿 2026-08-16「放到上架里」),"
             "合起来等于跑了一次 upc_sync"),
)
