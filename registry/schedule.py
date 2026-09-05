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
自愈(sku_locked_heal)、**存量改码(sku_migrate)**、
**来源码人工归类(sources_reclassify)**、一次性迁移与体检
(各 *_import / catmap_* / catalog_health / variant_probe / audit_why / …)。
⚠ `sources_reclassify`(所有者 2026-09-03)**永不进调度**:它导出清单等人逐行
认出"这一串里的源头码是哪一段"再读回,机器提议里「标准 ASIN + 尾巴」那一档
只够猜(guess),没有人认就没有输入。而它写下的每一行都把一个商品**交还自动
链**(此后被改价/清库存/删除管到)—— 这种判断不许由秒表触发。
与它同源的 `sources_backfill`(纯格式回填,判得准的那一半)常驻 product_chain,
两者分工:回填补的是"有没有登记行",归类补的是"登记行认不认得出出身"。
⚠ `sku_migrate`(SKU 改造批次 3)**永不进调度**,两条理由各自独立成立:
  ① 它是 DANGEROUS 的一次性迁移,按批发、按观测定案,每一批之间要人看摘要
     (节奏 1 → 10 → 一店 → 全店,闸在 `workflows/sku_migrate._stage_cap`);
     排进调度 = 每天自动改一批码,而"同店双挂"这类后果只有人能收。
  ② 它与 13:00 的 `product_chain` 抢**同一个 MP_MAINTENANCE 桶**(8/hour,
     维护链的反补也吃它)—— 两边并跑的表现是当天维护/反补发不出去,而摘要
     只会说"配额不足",看不出是谁吃的。手动跑请避开 13:00 那一轮。
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
    # 店铺事件账本的唯一推送出口(2026-08-30)。写入方(日报/审核/订单审计/
    # 治理快照/五条运营链)一律只落行不发通知 —— 谁发谁就得各自实现去重与
    # 限流,而同一次封店会从三个地方各响一次。
    # ⚠ 分钟选 :45 是为了**避开每小时的三个既有峰**(:00/:30 feed 轮询、
    # :20 订单链、:50 product_ingest):四条都在 launchd 上,撞在同一分钟不会
    # 互相拿锁(锁是按工作流名的),但会一起挤代理与 PG 连接。
    # 它同时是**治理快照 diff 的属主**(每小时敲两张飞书人工表,是本仓最便宜的
    # 飞书调用之一);高危事件本轮落、本轮推,最长延迟一小时。
    job("store_watch", ["store_watch"], batch=1, minute=45,
        note="每小时 :45;店铺高危事件扫描 → 飞书 → 标已推,顺带比对治理配置"
             "快照。⚠ **首次上线先手动 `-p seed=1` 吞掉历史存量**,不然第一条"
             "消息是几个月的历史高危(真正今天那条埋在里面);缺省窗口 48h、"
             "单轮上限 50 条,超出的下一轮接着推"),

    # ── 批二:订单 + 日报 ────────────────────────────────────────────────
    # ⚠ 只挂这一条,**不要**再单挂一个 06:20:两个 plist 撞在同一分钟会各拿
    # 各的锁,后到的整链退出码 3 空跑一轮。日报依赖的那次就是每小时的 06:20 那次。
    job("order_chain", ["order_sync", "order_audit", "returns_sync"],
        batch=2, minute=20,
        note="每小时 :20;order_audit 默认 wait=1,最长阻塞 20 分钟等采集落定"),
    # product_ingest 单独长驻(所有者定稿 2026-08-19:「product_ingest 现在的
    # 主要功能是让本地产品库与采集器数据库对齐,单独配长驻定时任务」):
    # 四条链(order_audit/product_audit/list_new/product_refresh)的同轮闭环
    # 全部按批次自取(谁推的批谁拉,无锁),这条管的是**其余一切增量**
    # (超时批次的尾巴、零散采集)——保中心库小时级对齐。全局游标从此只有
    # 这一个属主;lock_wait 只防手动跑 product_ingest 撞上它(等而不空转)
    job("product_ingest", ["product_ingest"], batch=2, minute=50,
        params=["lock_wait=900"],
        note="每小时 :50;全局增量泵:本地产品中心 ↔ 采集器数据库对齐"
             "(各链按批自取之外的全部增量走这条)"),
    # catalog_sync 打头(所有者定稿 2026-08-24):日报的「在线商品/有库存/
    # 无库存」三列直接读 catalog.walmart_items,而 catalog_sync 此前只在 13:00
    # 的 product_chain 里跑 —— 06:40 的日报拿到的是昨天 13 点的快照,产品数
    # 永远差一天。前置一次同步,统计的就是今早的在架现状。
    # 链式而不是在 daily_report 里调:workflow 不互相调用(铁律 1),
    # 且链的语义正好是要的 —— 同步失败就不出日报(拿旧数出报不如不出)。
    job("daily_report", ["catalog_sync", "daily_report"],
        batch=2, hour=6, minute=40, runner="gpt",
        params=["catalog_sync:strict=1"],
        note="KPI 窗口锚 06:30,必须 ≥06:35;catalog_sync 打头让产品三列是"
             "今早现状而非昨日 13 点快照;⚠ 开它之前先停旧 KPI 调度;"
             "strict=1 保住本链「同步不全就不出日报」的闸(店级重试标准②"
             "让缺席不再炸链,唯独此链宁可不出产物,2026-08-26)"),
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
    # 链里不再单摆 product_ingest 一步(所有者定稿 2026-08-19:「product_chain
    # 链也应该使用按批次拿」):product_refresh wait=1 等采完后**就地按批摄取**
    # 自己推的批(批次端点,无锁),维护判据当轮就是刚采回的值;全局对齐归
    # 单独长驻的 product_ingest(launchd 每小时)
    # product_audit 排在 problem_scan **紧前面**(所有者定稿 2026-08-22):
    # 在架 pass 重过 L0(纯查库零 LLM)→ 今天新拉黑/新禁售的东西当天翻成
    # rejected → 紧接着 problem_scan 按 audit_listing_conflicts 建删除建议
    # → problem_product_cleanup 执行。三步同一轮闭环,不用等第二天。
    # ⚠ **建议期与执行期分开编排**(所有者定稿 2026-08-24):
    #   两个扫描件(maintenance_scan / problem_scan)先跑完,再跑两个执行件
    #   (maintenance / problem_product_cleanup)。理由是破坏类建议要能压制
    #   同 SKU 的维护类建议 —— 旧序里 maintenance 排在 problem_scan 之前,
    #   审核链上午刚判拒的东西,维护链已经先花配额去改标题/改价了。
    #   **但压制不靠这个顺序**:压制在 dispositions.claim() 里按库里所有未落定
    #   的破坏类建议判,与写入先后无关。顺序改了结果也不变 —— 这是有意的,
    #   本仓吃过"顺序即语义"的亏,不再让调度表承载判据。
    #   product_audit 跟着 problem_scan 一起前移,紧邻关系不变。
    # ⚠ 两个参数一个都不能少:
    #   mode=online  只扫在架行(不在架的翻案下游产不出动作,白扫)
    #   stages=L0    纯查库零 LLM(run() 里钉死,少了会被拒绝启动)
    # ⚠ 而 limit **一个都不许给**:这条**没有天然分页**(未命中不落结论不盖
    #   版本、不退出候选),给了小 limit 就每天从头扫同一批前缀,尾巴永远轮不到
    #   而且不报错。2026-09-03 起缺省即不限量(此前这里写 `limit=1000000`
    #   凑效果,那个魔数已随缺省口径删掉)
    job("product_chain",
        ["catalog_sync", "sources_backfill", "product_refresh",
         "product_audit", "maintenance_scan", "problem_scan",
         "maintenance", "problem_product_cleanup"],
        batch=3, hour=13, minute=0, runner="gpt",
        params=["product_refresh:wait=1", "product_audit:mode=online",
                "product_audit:stages=L0"],
        note="整条 ~2 小时(13:00 起,约 15:00 收);前一步不成功就不跑后面的"
             "(拿隔夜现值当判据会误伤)。sources_backfill 紧跟 catalog_sync"
             "(所有者定稿 2026-08-19):新发现的在架商品当轮补来源关联,"
             "当轮就能被维护;零缺口时零成本,摘要非零 = 有人绕过登记上架"),
    job("product_clear", ["product_clear"], batch=3, hour=15, minute=0,
        runner="gpt",
        note="消费运营填的「停用/删除表」;不定时跑 = 填了没人执行"),
    # 版本重审**不进调度**(所有者定稿 2026-08-24:「规则存在,上架时对要上架
    # 的品起作用就够了,平常直接审核某一批产品也够」)。两条消化路径:
    #   · from_sheet(audit_sheet 18:10 已有):_DEFAULT_CANDIDATE 含
    #     「approved×旧版本」⇒ 要上架的品自动被新判据重过;
    #   · 手动批量:python cli.py product_audit -p mode=stale [-p limit=N]

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
             "只有未审/pending 过退避的才真判;**缺省不限量**(2026-09-03 定稿:"
             "要限量才手动带参数),要分批就改调度表加 -p limit=N,别在提示词里手改。"
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
