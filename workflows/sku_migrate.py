"""sku_migrate — 存量 SKU 迁到 12 位不透明码(SKU 改造批次 3;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py sku_migrate --dry-run -p store=<店>            # 空跑:打印将改哪些、载荷样例
  python cli.py sku_migrate -p store=<店> -p limit=1           # 第一级:1 个品(节奏闸会自己压到 1)
  python cli.py sku_migrate -p store=<店> -p settle_only=1     # 只定案,不发新的
  python cli.py sku_migrate -p store=<店> -p limit=10          # 第二级(前一级全部 confirmed 之后)
  python cli.py sku_migrate -p store=<店> -p skus=B0AAA,B0BBB  # 点名:只改这两个旧码
  python cli.py sku_migrate -p store=<店> -p asins=B0AAA,B0BBB # 点名:按 ASIN 说话
  python cli.py sku_migrate -p store=<店> -p exclude_skus=B0CC # 排除:从候选里剔掉(排除优先于点名)
  python cli.py sku_migrate -p store=<店> -p exclude_asins=B0C # 排除:同上,按 ASIN

**点名 / 排除**(2026-09-03 加;所有者要"挑着做",不是"按店按数量批量做"):
  · `-p skus=` 按旧 SKU 点名,`-p asins=` 按登记簿 `source_key`(= ASIN)点名,
    两个可同时给,**取并集**;`-p exclude_skus=` / `-p exclude_asins=` 从候选里排除,
    **排除优先**(同一条既被点名又被排除 ⇒ 排除)。
  · 四个参数都是同一条候选 SQL 的**参数化条件**(不另开第二条选取路径,§六 双轨禁止),
    所以点名跑的是与全量跑**逐字相同**的那套判据与闸。
  · 点名**不越闸**:前置五闸、逐候选在途闸、节奏硬闸(零 confirmed ⇒ 本轮 1 条)
    一条都不放松 —— 点了 5 个而本轮只放 1 个是常态,摘要会把这句说全。
  · 点名了却没进候选面的,**逐条点名说明为什么**(不满足哪一条判据 / 被排除 /
    在途 feed / Product ID 撞号 / 被节奏闸留到下轮 / 店下查无此行)。静默丢的表现是
    摘要看起来像"这家店没候选",而所有者以为自己点的名生效了。

**三态状态机**(身份权威在 catalog.listing_sources,过程账在 listing.sku_migrations):

    候选(存量码,在架,活码,amz 出身)
      │  mint_replacement:新码行指回旧码 + 旧行 replaced_by=新码 → **commit**
      ▼
    pending ──(新码在架 ∧ 旧码缺席 ∧ 观测新鲜)──────────────▶ confirmed
      │                                                        (旧行弃码 sku_update,
      │                                                         不烧 UPC;UPC 改标、
      │                                                         上架表 SKU 列、处置迁键、
      │                                                         节点库存清行)
      ├──(回执 failed ∨ 观测反证且超 OBSERVE_HOURS)─────────▶ rolled_back
      │                                                        (旧行复活,新码弃掉)
      ├──(超 STALE_HOURS 仍判不出)──────────────────────────▶ stalled(点名人工,不自动定案)
      └──(新码在架 ∧ 旧码**也**在架)──▶ 留 pending + ⚠ 同店双挂(只告警,不自动处置)

**六条安全约束**(每条都不是可选项):
  ① **先落库并 commit 再调接口**:mint + pending 台账的事务必须在组载荷之前退出;
     在未提交事务里 POST,进程一死就是"沃尔玛已受理、我们这边零记录"的孤儿码。
  ② **回执成功不定案**,定案只信 catalog_sync 的观测(「回执成功但后台没删」是
     所有者实证过的故障模式,见 sku_codec 模块头注②)。
  ③ **写操作永不自动兜底**:提交失败当场回滚(换个码下轮重来),不补交、不换姿势;
     POST outcome=unknown **保持 pending**(见下「与 sku_plan 的有意出入」)。
  ④ **一店一批的硬闸在代码里**(_stage_cap):零 confirmed ⇒ 上限 1;<10 ⇒ 上限 10;
     还有 pending/stalled 未清 ⇒ 本轮只定案不提交。纪律没有默认值替你挡。
  ⑤ **永不进调度**(registry/schedule.py 的手动清单里点名):改码按批、要人盯定案;
     且它与 13:00 的 product_chain 抢同一个 MP_MAINTENANCE 桶,不许并跑。
  ⑥ **dry-run 下 _settle 与 _migrate 都零写**:不 mint、不定案、不提交、不写飞书、
     不改处置、不删节点库存 —— 一行库、一行飞书、一条 feed 都不写。

**前置清单**(全部满足才允许真跑):
  · SKU 改造批次 0a / 0b / 1 / 2 已合并,且新码已在生产跑过至少一轮;
  · 所有者机器的**六件单品实测**通过(docs/sku_plan.md §4);实测前只许 --dry-run;
  · 改码期间该店**不得**有人在 Seller Center 手工改同一批 item 的 SKU / Product ID;
  · **旧仓调度已停**(CLAUDE.md 安全红线「新旧系统严禁对同一破坏性任务并跑」):
    `crontab -l | grep -Ei 'auto_listing|retire_and_relist|product_clear|daily_cleanup'`
    输出为空 —— 旧仓的清理链指着旧码,改码期间并跑 = 它对着一个即将不存在的 SKU 发删除。

**三个同名异义,别混**(sku_codec 模块头注①的复述,这里是最容易混的地方):
「码弃用(登记簿 abandoned_at)」≠「沃尔玛 lifecycle RETIRED」≠「product_clear 停用」。
本工作流只做第一种,而且只在**观测确认**之后做。

**与 docs/sku_plan.md / synthesis 的有意出入**(决策 F,写在这里免得下次复核当漏洞改回去):
POST 的 outcome=unknown **不回滚、保持 pending**,留给下一轮 _settle 与 api/feeds 的
启动对账(api/feeds.py 对 unknown 的既定处置就是保持 pending)。unknown 的语义是
「不知道到没到」——若沃尔玛其实已经改成新码而我们回滚了登记簿,新码就成了一条没有
出身的孤儿行(sources_backfill 判 unknown ⇒ 退出全部自动化),而且不报错。
synthesis 里「failed/未达/Unknown ⇒ rolled_back」说的是**回执**三态(_settle 的输入),
与 POST 的 outcome 是两件事。

**收益上限(所有者必须知道的真相)**:改码是**止血**不是清创 —— SkuUpdate feed 本身
就是「旧串 → 新码」的显式映射,沃尔玛已经掌握的旧 SKU=ASIN 关联(以及历史订单、
历史 feed 记录里的关联)收不回来。它只让**切换之后**的记录干净。
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from api import feeds
from registry import db
from services import dispositions, feed_track, listing_sheet, listing_sources, \
    mp_mapper, notify_fmt as nf, order_lines, sku_codec, store_absence, \
    stores as stores_svc, upc_pool, walmart_catalog

DANGEROUS = True
#: 接受 `-p store=X`(**必填**)。cli 的链尾缺席店重赛(cli.py:_replay_absent)
#: 够不着它:一是本工作流永不进调度、不在任何链里;二是链里只要有一步带了
#: `store=` 范围参数,重赛整段就被第一道闸跳过 —— 而本工作流 store 必填。
#: 声明它是为了让「本工作流按店限定」这件事在模块级可读,不是为了自动重赛。
SUPPORTS_STORE = True

logger = logging.getLogger("workflows.sku_migrate")

# ── 常量(**每个只有这一个出生地**;形态 A→B 的切换只改 FEED_TYPE + _build_items)──
#: 载荷形态 **A**:MP_MAINTENANCE 最小载荷 {新码, 现挂 Product ID, SkuUpdate:Yes}。
#: 依据:US 官方 Update my existing items 明写 MP_MAINTENANCE「requires only the SKU
#: and GTIN attributes」(docs/api_blueprint.md §5.4),CA manage-items 明写 SkuUpdate
#: 属性。**不提供参数覆盖**:给它一个 `-p feed_type=` 就是两条实现路径(§六 双轨禁止),
#: 而两种形态的副作用完全不同(形态 B = 重发全部内容,标题/属性会被我们再生成的覆盖)。
#: 切形态 B 的条件:所有者实测第 1 件判定最小载荷改不动码。切的时候改这一处 +
#: _build_items 的分支,并先做 mp_conform 的 SkuUpdate 放行(否则每一行都双挂)。
FEED_TYPE = "MP_MAINTENANCE"
#: ⛔ **提交通道停用**(2026-09-05,官方 spec 原件核实):US 站 MP_MAINTENANCE
#: 5.0.20260608-18_15_07-api(我们 header 里写的正是这一版)的 Orderable **没有
#: SkuUpdate 字段**(20 个属性、required=[sku, productIdentifiers]、
#: additionalProperties=false;三个版本 20260501/0608/0703 一致);`SkuUpdate`
#: 只存在于 **MP_ITEM** 的 Orderable(23 个属性)。生产实证与之吻合:两条改码
#: feed(谭总12 / A085朱丽霖,2026-09-04)回执 SUCCESS 而线上 SKU 纹丝不动 ——
#: 维护通道把 SkuUpdate 当未知字段静默丢弃,整条 feed 是一次"空维护"。
#: 形态 A 由此**作废**(§9.6 那条 schema 推断错了:它看的是 MP_ITEM 的 Orderable,
#: 不是 MP_MAINTENANCE 的)。所有者在 Seller Center 用「Match items」模板上传改码
#: **成功**,说明可行通道是 setup 类 feed(MP_ITEM 全量 + SkuUpdate=Yes,或
#: MP_ITEM_MATCH);具体走哪条,等所有者机器上 GET /v3/feeds 看那条 Seller Center
#: feed 的 feedType 再定,**定了再改这里与 _build_items**。
#: 在那之前非空 ⇒ 本工作流只定案不提交(dry-run 也不列候选,免得预览误导);
#: 定案要留着:已发出去的两条 pending 要靠观测反证走 rolled_back 把旧码复活。
SUBMIT_DISABLED = ("MP_MAINTENANCE 不支持 SkuUpdate(官方 spec 原件 2026-09-05 核实),"
                   "改码通道待切换到 setup 类 feed;见 FEED_TYPE 头注与 docs/sku_plan.md §9.10")
#: 只迁 amz 出身的存量码(决策 D 默认:**跟卖不迁**)。PHUMWMT 串本就不含 ASIN,
#: 货源隐匿收益为零;而 match 行的 source_key 是匹配 GTIN,改码后 upc_pool 的
#: (店, ASIN) 键无从对上(跟卖不用 UPC 池),实测面直接翻倍。
SOURCE_TYPES = (listing_sources.SOURCE_AMZ,)
#: 观测期:官方 15 分钟~4 小时生效,取 24h 留量。超过它且观测反证 ⇒ 回滚。
OBSERVE_HOURS = 24
#: 超期线:超过它仍判不出 ⇒ 落 stalled 点名人工,**不自动定案**(判不准就判活:
#: 回滚一个其实已经生效的改码,会让登记簿说旧码、沃尔玛说新码,而且不报错)。
STALE_HOURS = 72
#: 单店单轮最多发几个 feed。MP_MAINTENANCE 桶 8/hour(api/_client.py),与维护链
#: **共享** —— 吃光的表现是当晚维护链发不出去,而摘要只会说"配额不足"看不出是谁吃的。
FEEDS_PER_STORE_PER_RUN = 2
#: 一个 feed 装几条。远低于 MP_MAINTENANCE 的官方切片上限(1000 条 / 24MB,
#: api/feeds._SLICE_LIMITS),这样「一次 submit_feed = 一个 feed」成立,
#: FEEDS_PER_STORE_PER_RUN 才是**真闸**而不是估算。
ITEMS_PER_FEED = 500
#: -p limit 的缺省值(节奏闸只会把它压得更小,压不大)。
DEFAULT_LIMIT = 10
#: 逐候选的在途 feed 闸回看窗口。与 problem_scan._SQL_INFLIGHT 的 48h 同源:
#: 在途口径**有意不分 feed 类型** —— 一条刚发出去的 MP_ITEM/DELETE_ITEM 在途时改码,
#: 会让那条 feed 打在一个即将不存在的 SKU 上。
INFLIGHT_HOURS = 48
#: dry-run 摘要里列几行样例(人眼确认用,不是上限)。
PREVIEW_ROWS = 10

_PENDING = "pending"
_CONFIRMED = "confirmed"
_ROLLED_BACK = "rolled_back"
_STALLED = "stalled"


# ══════════════════════════════════════════════════════════════════════════════
#  SQL
# ══════════════════════════════════════════════════════════════════════════════

#: 改码候选的**九条判据,每条只在这里出生一次**(短名 / 落选人话 / SQL 布尔式)。
#: 下面两条 SQL 都由这一份拼出来:`_SQL_CANDIDATES` 把九条 AND 起来**选行**,
#: `_SQL_WHY` 把同样这九条**逐条选成布尔列**,只为给点名落选的行出理由 —— 判据
#: 文本共用一份,所以不可能"选取用一套、解释用另一套":那种漂移的表现是摘要说
#: "它满足条件",而它就是不在候选面里,谁也不报错。
#: 形态判据经 sku_codec.OPAQUE_SQL_PREDICATE 派生(**不在这里手打正则**:手打就是
#: 第二个字母表之家,两份一漂,SQL 与 is_opaque 会对"什么是新码"给出不同答案而且
#: 全程不报错)。`ls.abandoned_at IS NULL` 是白名单登记的第四处消费方过滤
#: (conventions §九②):候选只取**活码**,已弃码的行沃尔玛侧我们已经当它不存在了。
_CONDS: tuple[tuple[str, str, str], ...] = (
    ("在架", "已缺席(missing_since 非空):沃尔玛侧已经看不到它,改码也改不动",
     "w.missing_since IS NULL"),
    # ⚠ 所有者 2026-09-04:「只对 publish 的产品发就可以了吧」—— 对,而且这是
    # **必须**的一条。`missing_since IS NULL` 只说"目录里还看得见",UNPUBLISHED /
    # RETIRED / STAGE 都满足它。而 §4 六件实测的第 5 件正是「对 lifecycle=RETIRED
    # 的 item 是否可用 SkuUpdate」——官方零文档、本仓零实证。放进候选面的后果:
    #   ① 改不动 ⇒ 那条行卡到 72 小时 stalled,占着节奏闸的名额;
    #   ② 中间窗口里旧码"非 PUBLISHED 且未缺席",正好落进 problem_scan 的扫描面
    #      被建议 DELETE_ITEM —— 一次改码把商品永久删掉(§9.4 最贵的那条)。
    # 想迁非 PUBLISHED 的行,先做第 5 件实测再回来放开这一条(**不加参数开关**:
    # 加了就是两条口径,而"哪些状态能改码"是判据不是偏好,§六 双轨禁止)。
    ("已上架", "非 PUBLISHED(unpublished/retired/stage):SkuUpdate 对这些状态"
               "能不能用官方零文档、本仓零实证(§4 六件实测第 5 件),"
               "改不动会卡 stalled,中间窗口还可能被 problem_scan 判删",
     "w.published_status = 'PUBLISHED'"),
    ("活码", "码已弃用(abandoned_at 非空):这条我们已经当它不存在了,"
             "再改一次码既改不动、也会把一个死码拉回自动化",
     "ls.abandoned_at IS NULL"),
    ("未在改", "登记簿里已经指向新码(replaced_by 非空):改码只有一跳",
     "ls.replaced_by IS NULL"),
    ("出身可迁", "出身不在 SOURCE_TYPES(跟卖不迁,决策 D)",
     "ls.source_type = ANY(%(source_types)s::text[])"),
    ("是旧码", "已经是 12 位不透明码(不用再改)",
     "NOT " + sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku")),
    ("有 Product ID", "观测里 upc 与 gtin 都是空:载荷没号可匹配(**不猜**)",
     "(w.upc IS NOT NULL OR w.gtin IS NOT NULL)"),
    # ⚠ 这一条替下了原来那个整店闸(所有者 2026-09-04 复议,见 `_preflight` 闸③)。
    # 危害只发生在**同一个 SKU**、而且**只发生在破坏组**(delete/retire)上:
    #   ① executing 的 DELETE 拿的是旧码去删,而 `dispositions.settle` 的判据是
    #      catalog_sync 落的 `delete_verified`(= 这个 SKU 不见了)。改码后旧码
    #      正好消失 ⇒ **假确认**:商品还好好挂在沃尔玛上,账本记着"已确认删除",
    #      全程不报错;
    #   ② suggested 的 DELETE 马上会被 claim 成一条打在**旧码**上的 feed。
    # **维护组(title/price/inventory)不拦**(所有者 2026-09-04:13:00 三条链
    # 齐发是常态,拦它等于改码永远开不了工)。它们各有出路:
    #   · suggested → 定案时 `dispositions.rekey_suggested` 把键从旧码搬到新码;
    #   · executing → rekey **故意不碰**(搬键等于把判决对象换掉),于是滞留,
    #     由 `expire_executing` 判成 ineffective 收尾 —— 自愈,不是事故。
    #     但**不许静默**:`_confirm` 会把滞留的动作点名进摘要。
    # 已落定的行(settled_at 非空)不算数:账已经清了。
    ("无未了结破坏建议",
     "该 (店, 旧码) 还有未落定的**破坏性**建议(delete/retire):executing 那条"
     "拿的是旧码去删,而它的定案判据是「这个 SKU 不见了」—— 改码后旧码正好"
     "消失,会被读成 `delete_verified`「删除成功」的**假确认**,账本从此与"
     "沃尔玛说的不是一回事且不报错;suggested 那条马上会被 claim 成一条打在"
     "旧码上的 DELETE feed。等它落定再改",
     "NOT EXISTS (SELECT 1 FROM ops.dispositions d\n"
     "                  WHERE d.store = w.store AND d.sku = w.sku\n"
     "                    AND d.settled_at IS NULL\n"
     "                    AND d.status IN ('suggested', 'executing')\n"
     "                    AND d.action IN ("
     + ", ".join(f"'{a}'" for a in dispositions.DESTRUCTIVE_ACTIONS) + "))"),
    ("无未了结改码台账",
     "该 (店, 旧码) 已有 pending/confirmed/stalled 的改码台账(不许开第二条)",
     """NOT EXISTS (SELECT 1 FROM listing.sku_migrations m
                  WHERE m.store = w.store AND m.old_sku = w.sku
                    AND m.status IN ('pending', 'confirmed', 'stalled'))"""),
)

#: 点名(`-p skus=` / `-p asins=`)与排除(`-p exclude_skus=` / `-p exclude_asins=`):
#: **同一条候选 SQL 的参数化条件**,不为点名另开第二条选取路径(§六 双轨禁止 ——
#: 两条选取路径一漂,点名跑的就不再是全量跑的那套闸,而且不报错)。
#: 没点名时 `unnamed=true` ⇒ 整个 OR 恒真 ⇒ 与加点名之前**逐字等价**;
#: 排除拼在点名**之后**且是 NOT,所以"既点名又排除"一定是排除赢。
_PICK = """(%(unnamed)s::boolean
       OR w.sku = ANY(%(only_skus)s::text[])
       OR ls.source_key = ANY(%(only_keys)s::text[]))"""
_DROP = """NOT (w.sku = ANY(%(excl_skus)s::text[])
           OR ls.source_key = ANY(%(excl_keys)s::text[]))"""

#: 候选面的取数口径(两条 SQL 共用):目录 × 登记簿的**交集**。
#: 登记簿里没有的行不在这张面上 —— 点名点到它 ⇒ 报"店下查无此行",不是"没候选"。
_FROM = """
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku
"""

#: 改码候选(单一实现路径:点名/排除只是上面两个参数化条件,不是第二条 SQL)。
_SQL_CANDIDATES = (
    "SELECT w.store, w.sku AS old_sku, ls.source_type, ls.source_key,\n"
    "       coalesce(w.upc, w.gtin) AS product_id,\n"
    "       CASE WHEN w.upc IS NOT NULL THEN 'UPC' ELSE 'GTIN' END AS product_id_type"
    + _FROM
    + "WHERE w.store = %(store)s\n  AND "
    + "\n  AND ".join([sql for _n, _w, sql in _CONDS] + [_PICK, _DROP])
    + "\nORDER BY w.sku\nLIMIT %(limit)s\n")

#: 点名落选的理由源:**同一份 `_CONDS`**,逐条选成布尔列 c0…c6(不重写判据)。
#: 有意**不带** LIMIT、不带排除条件:被 LIMIT 截掉的与被排除的都要能说出口
#: ("它其实合格,只是本轮节奏闸没轮到"和"你自己排除了它"是两句不同的话)。
_SQL_WHY = (
    "SELECT w.sku AS old_sku, ls.source_key AS source_key,\n       "
    + ",\n       ".join(f"({sql}) AS c{i}"
                        for i, (_n, _w, sql) in enumerate(_CONDS))
    + _FROM
    + "WHERE w.store = %(store)s\n"
      "  AND (w.sku = ANY(%(only_skus)s::text[])\n"
      "       OR ls.source_key = ANY(%(only_keys)s::text[]))\n"
      "ORDER BY w.sku\n")

#: 逐候选的在途 feed 闸(W2 第⑥道)。**有意不分 feed_type**,口径与
#: workflows/problem_scan 的在途防重同源。
_SQL_INFLIGHT_OLD = """
SELECT DISTINCT sku FROM ops.feed_items
WHERE store = %(store)s AND sku = ANY(%(skus)s::text[])
  AND status = 'submitted'
  AND submitted_at > now() - make_interval(hours => %(hours)s)
"""

#: 自愈链在途:retire_cooldown 的 pending 行也指着旧码。
_SQL_COOLDOWN_OPEN = """
SELECT count(*) FROM listing.retire_cooldown
WHERE store = %(store)s AND status = 'pending'
"""

#: 定案证据。三个布尔都由**观测**给,回执只在 _verdict 里当第二判据。
#: fresh = 提交之后至少有一轮 catalog_sync 扫完过这家店(没有它,"旧码缺席"
#: 可能只是我们还没去看)。
_SQL_OBSERVE = """
SELECT m.id, m.old_sku, m.new_sku, m.source_type, m.source_key, m.feed_id,
       m.submitted_at,
       (nw.sku IS NOT NULL AND nw.missing_since IS NULL)          AS new_present,
       (ow.sku IS NULL OR ow.missing_since IS NOT NULL)           AS old_gone,
       EXISTS (SELECT 1 FROM catalog.walmart_items s
               WHERE s.store = m.store AND s.last_seen_at > m.submitted_at) AS fresh
FROM listing.sku_migrations m
LEFT JOIN catalog.walmart_items nw ON nw.store = m.store AND nw.sku = m.new_sku
LEFT JOIN catalog.walmart_items ow ON ow.store = m.store AND ow.sku = m.old_sku
WHERE m.store = %(store)s AND m.status = 'pending' AND m.submitted_at IS NOT NULL
ORDER BY m.id
"""

#: 飞书 SKU 列的补写集合(每轮开头先补:一次写失败之后该行已是 confirmed、
#: 不再进 pending 判决面,没有这条路径它的 SKU 列就永远停在旧码而且不报错)。
_SQL_UNSYNCED = """
SELECT id, old_sku, new_sku, source_type, source_key
FROM listing.sku_migrations
WHERE store = %(store)s AND status = 'confirmed' AND sheet_synced_at IS NULL
ORDER BY id
"""

#: 落库了但没发出去的行(submitted_at 仍空):进程死在 POST 前后、或提交当场抛异常。
#: **不自动定案** —— 死在 POST 之前是"确定没发",死在 POST 之后是"不知道到没到",
#: 从这张表上分不出来(判不准就判活)。点名让人去看 ops.feed_log 与沃尔玛后台。
_SQL_UNSENT = """
SELECT id, old_sku, new_sku, created_at FROM listing.sku_migrations
WHERE store = %(store)s AND status = 'pending' AND submitted_at IS NULL
ORDER BY id
"""

_SQL_STAGE = """
SELECT count(*) FILTER (WHERE status = 'confirmed')             AS confirmed,
       count(*) FILTER (WHERE status IN ('pending', 'stalled')) AS open
FROM listing.sku_migrations WHERE store = %(store)s
"""

_SQL_LEDGER_NEW = """
INSERT INTO listing.sku_migrations
    (store, old_sku, new_sku, source_type, source_key, feed_type, status, detail)
VALUES (%(store)s, %(old_sku)s, %(new_sku)s, %(source_type)s, %(source_key)s,
        %(feed_type)s, 'pending', %(detail)s::jsonb)
RETURNING id
"""

_SQL_LEDGER_SUBMITTED = """
UPDATE listing.sku_migrations
   SET feed_id = %(feed_id)s, submitted_at = now()
 WHERE id = ANY(%(ids)s::bigint[])
"""

_SQL_LEDGER_SETTLE = """
UPDATE listing.sku_migrations
   SET status = %(status)s, settled_at = now(),
       error = coalesce(%(error)s::text, error)
 WHERE id = %(id)s
"""

_SQL_LEDGER_STALLED = """
UPDATE listing.sku_migrations
   SET status = 'stalled', error = %(error)s
 WHERE id = %(id)s
"""

_SQL_LEDGER_SHEET_OK = """
UPDATE listing.sku_migrations SET sheet_synced_at = now()
 WHERE id = ANY(%(ids)s::bigint[])
"""


def _rows(cur) -> list[dict]:
    """输入:执行过的游标 → 输出:[dict](按列名取值,位置解包错一位不报错)。"""
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


#: 点名/排除参数的分隔符:逗号、空白、换行任意混排(所有者是从表格/聊天里
#: 复制粘贴的,`B0A, B0B\nB0C` 必须与 `B0A,B0B,B0C` 等价)。
_NAME_SEP = re.compile(r"[,\s]+")


def _parse_names(raw) -> list[str]:
    """输入:`-p skus=` 这类原始值 → 输出:去重保序的名字列表(空串忽略)。

    大小写**一律不动**:SKU 大小写敏感,口径与 `services/order_lines.norm_sku`
    (只 strip 不 upper)同源 —— 顺手 `.upper()` 会让点名静默命中 0 个,而摘要
    看起来像"这家店没候选"。ASIN 侧同样不动:登记簿 `source_key` 存的是原样。
    去重**保序**:摘要里的"点名 N 个"要与所有者敲进去的顺序对得上。
    """
    if raw in (None, ""):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _NAME_SEP.split(str(raw)):
        name = order_lines.norm_sku(tok)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  W2 · 前置闸
# ══════════════════════════════════════════════════════════════════════════════

def _preflight(conn, store_name: str) -> tuple[bool, list[str]]:
    """输入:连接 + 店名 → 输出:(是否放行, 逐条结论)。

    五道整店闸(第⑥道「该候选的旧码上有在途 feed」是**逐候选**判的,在
    _candidates 里,不整店拦)。任一不过 ⇒ 本轮该店不迁,**不抛异常**,
    在摘要里点名"为什么不能改"。

      ① 在营 —— services/stores.enabled_names(唯一在营判据);
      ② 目录水位新鲜 —— 该店不在缺席集(判"新码在架/旧码缺席"要有当轮观测);
      ③ 处置无在途 executing —— 那意味着一条 DELETE/RETIRE feed 已在沃尔玛队列里
         指着**旧码**:改码 = 那条 feed 打空 + 建议永远等不到判决 + 部分唯一索引
         把该 SKU 的这类处置永久堵住;
      ④ 自愈链无在途 —— retire_cooldown 的 pending 行同理指着旧码;
      ⑤ 本工作流无在途 feed —— 「防重状态先落库再调接口」的启动对账要求:
         上一轮结局不确定就不许发新的。
    """
    lines: list[str] = []
    ok = True

    try:
        enabled = stores_svc.enabled_names()
    except Exception as e:                    # noqa: BLE001 —— 判不出在营就不放行
        return False, [f"  ⛔ 闸①在营:读凭证表失败({e.__class__.__name__}: {e}),"
                       f"判不出该店是否在营 —— 改码是破坏动作,判不准就不做"]
    if store_name not in enabled:
        ok = False
        lines.append(f"  ⛔ 闸①在营:{store_name} 不在**在营**店名单里"
                     f"(凭证表「启用」未勾/店名拼写不一致);停用店不许改码")
    else:
        lines.append("  ✓ 闸①在营")

    absent, note = store_absence.stale_or_note(conn, only=store_name)
    if note:
        ok = False
        lines.append(f"  ⛔ 闸②水位:{note} —— 改码的定案全靠观测,"
                     f"判不出水位新不新鲜就不许发新的")
    elif absent:
        ok = False
        lines.append(f"  ⛔ 闸②水位:{store_name} 本轮**缺席**(目录水位落后船队),"
                     f"这时改码等于把定案交给一条已经不同步的观测线;"
                     f"先把 catalog_sync 跑通再来")
    else:
        lines.append("  ✓ 闸②目录水位新鲜")

    # ⚠ **闸③只报数,不拦**(所有者 2026-09-04 复议,改前是整店 executing 非零即拦)。
    # 改前的理由写在 dispositions.open_executing_count 头注里,而那段话自己说的是
    # 「改码会把**它等的那个 (店, SKU)** 键换掉」—— 危害是逐 (店,SKU) 的,按整店
    # 计数拦是粗放的过近似。所有者的生产事实:每天 13:00 价格/库存/标题三条链
    # 齐发,任何一家店在那之后都有几百条 executing;按整店拦 = 改码永远开不了工。
    # 而改 A 商品的码,不会影响 B 商品那条改价建议的判决 —— 判据里没有任何跨 SKU
    # 依赖(settle / settle_maintenance / expire_executing 全部逐 (店,SKU) 判)。
    # **真正的危害搬到了 `_CONDS` 的「无未了结破坏建议」逐候选判据上**:那里只拦
    # 破坏组(delete/retire),但连 `suggested` 一起拦(理由见那条判据的头注)。
    # 这一行留着是因为它是有用的**上下文**:改码期间这家店有多少条在等判决,
    # 人看摘要时该知道。
    n_exec = dispositions.open_executing_count(conn, store_name)
    lines.append(f"  ✓ 闸③处置:{store_name} 有 {n_exec} 条 executing 在途"
                 f"(**不拦整店** —— 危害逐 (店,SKU),由候选判据「无未了结破坏建议」"
                 f"逐个剔;维护组不拦,破坏组连 suggested 一起拦)"
                 if n_exec else "  ✓ 闸③无在途处置(executing = 0)")

    with conn.cursor() as cur:
        cur.execute(_SQL_COOLDOWN_OPEN, {"store": store_name})
        n_cool = int((cur.fetchone() or [0])[0])
    if n_cool:
        ok = False
        lines.append(f"  ⛔ 闸④自愈链:{store_name} 有 {n_cool} 条 retire_cooldown "
                     f"pending(退役冷却中,冷却表里存的是**旧码**);"
                     f"先让 sku_locked_heal 把那批清列重上完")
    else:
        lines.append("  ✓ 闸④自愈链无在途退役")

    try:
        mine = [r for r in feeds.query_pending()
                if r.get("workflow") == "sku_migrate"
                and r.get("store") == store_name]
    except Exception as e:                    # noqa: BLE001 —— 同④:判不出就不放行
        return False, lines + [
            f"  ⛔ 闸⑤在途 feed:读 ops.feed_log 失败({e.__class__.__name__}: {e})"]
    if mine:
        ok = False
        lines.append(f"  ⛔ 闸⑤本工作流有 {len(mine)} 条 feed 结局未定"
                     f"(status={sorted({r['status'] for r in mine})});"
                     f"先跑 feed_poll 把它们落定,结局不确定时不许发新的")
    else:
        lines.append("  ✓ 闸⑤本工作流无在途 feed")

    return ok, lines


# ══════════════════════════════════════════════════════════════════════════════
#  W3 · 定案(每次调用最先跑;-p settle_only=1 时只跑这一段)
# ══════════════════════════════════════════════════════════════════════════════

def _verdict(row: dict, receipt: tuple[str, str] | None, now) -> tuple[str, str]:
    """输入:一条 pending 台账 + 该新码的回执 + 当前时刻 → 输出:(判词, 人话理由)。

    判词 ∈ pending / confirmed / rolled_back / stalled / double。**纯函数**,
    优先级固定(顺序即判据,改顺序就是改语义):

      (a) 新码在架 ∧ 旧码缺席                    ⇒ confirmed
      (b) 新码在架 ∧ 旧码**也**在架              ⇒ double(不定案,只告警)
      (c) 新码未现 ∧ 回执 failed                 ⇒ rolled_back(确认没成)
      (d) 新码未现 ∧ 观测新鲜 ∧ 超 OBSERVE_HOURS ⇒ rolled_back(观测反证)
      (e) 超 STALE_HOURS 仍判不出                ⇒ stalled(点名人工)
      (f) 其余                                    ⇒ pending

    **回执成功单独不定案**:「回执成功但后台没改」是本仓实证过的故障模式
    (delete_not_effective 同款),定案只信观测。
    """
    observe_h = row.get("_observe_hours", OBSERVE_HOURS)
    stale_h = row.get("_stale_hours", STALE_HOURS)
    age = now - row["submitted_at"]
    if row["new_present"] and row["old_gone"]:
        return _CONFIRMED, "新码在架且旧码已缺席(观测确认)"
    if row["new_present"] and not row["old_gone"]:
        return "double", ("新码与旧码**同时在架** —— 这不是改码而是多了一条 listing,"
                          "本工作流不自动处置,请人工核对沃尔玛后台")
    status = (receipt or ("", ""))[0]
    code = (receipt or ("", ""))[1]
    if status == "failed":
        return _ROLLED_BACK, f"回执 failed({code or '无错误码'}):沃尔玛拒了这次改码"
    if row["fresh"] and age > timedelta(hours=observe_h):
        return _ROLLED_BACK, (f"提交已 {age.total_seconds() / 3600:.0f}h、观测已跑过新的一轮,"
                              f"新码仍未出现(超观测期 {observe_h}h)")
    if age > timedelta(hours=stale_h):
        return _STALLED, (f"提交已 {age.total_seconds() / 3600:.0f}h 仍判不出"
                          f"(新码未现且观测{'不新鲜' if not row['fresh'] else '新鲜但未到反证条件'})"
                          f" —— 超期 {stale_h}h,交人工,**不自动定案**")
    return _PENDING, "等观测(未到判据)"


def _confirm(store_name: str, row: dict) -> list[str]:
    """输入:店 + 一条台账行 → 输出:告警行(无告警返回 [])。

    一个事务里把 confirmed 的全部后果做完(飞书回写在事务外,见 _sync_sheet):
    身份定案 → UPC 改标 → 处置迁键 → 节点库存清行 → 过程账落 confirmed。
    外部 IO 不进数据库事务:飞书失败下一轮补写,登记簿不会因此回滚。
    """
    warns: list[str] = []
    old, new = row["old_sku"], row["new_sku"]
    with db.pg_conn() as tx:
        sku_codec.settle_replacement(tx, store_name, old, new, _CONFIRMED,
                                     "观测确认(新码在架、旧码缺席)")
        if row["source_type"] == listing_sources.SOURCE_AMZ and row["source_key"]:
            upc_pool.retag_sku(tx, [(store_name, row["source_key"], new)])
        # ⚠ 先读后改:rekey 之后旧码名下的 suggested 已经搬走,再读就读不到了。
        # executing 行 rekey 故意不碰(搬键 = 换判决对象),它们滞留在旧码上,
        # 由 expire_executing 判成 ineffective 收尾 —— 自愈,但**不许静默**。
        stranded = dispositions.executing_actions_on(tx, store_name, old)
        _moved, taken = dispositions.rekey_suggested(
            tx, store_name, old, new, asin=row["source_key"])
        walmart_catalog.drop_node_rows(tx, store_name, old)
        tx.execute(_SQL_LEDGER_SETTLE,
                   {"status": _CONFIRMED, "error": None, "id": row["id"]})
    if taken:
        warns.append(f"  ⚠ {old}→{new}:新码名下已有未落定建议 {','.join(taken)},"
                     f"这些动作的旧码建议**不迁不删**,请人工处置")
    if stranded:
        bad = [a for a in stranded if a in dispositions.DESTRUCTIVE_ACTIONS]
        warns.append(
            f"  ⚠ {old}→{new}:旧码名下还有 executing 的 {','.join(stranded)},"
            f"**不迁**(搬键等于换判决对象)—— 它们滞留在旧码上,由 "
            f"expire_executing 判成 ineffective 收尾,不用管"
            + (f";⛔ 其中 {','.join(bad)} 是**破坏组**,候选判据本该把这行剔掉"
               f"(多半是中间窗口里新长出来的建议),**请人工核**" if bad else ""))
    return warns


def _roll_back(store_name: str, row: dict, why: str) -> None:
    """输入:店 + 台账行 + 理由 → 输出:无(旧行复活、新码弃掉、过程账落 rolled_back)。"""
    with db.pg_conn() as tx:
        sku_codec.settle_replacement(tx, store_name, row["old_sku"],
                                     row["new_sku"], _ROLLED_BACK, why)
        tx.execute(_SQL_LEDGER_SETTLE,
                   {"status": _ROLLED_BACK, "error": why[:900], "id": row["id"]})


def _sync_sheet(store_name: str, rows: list[dict], execute: bool) -> tuple[int, list[str]]:
    """输入:店 + 待同步的已定案行 + 是否真写 → 输出:(写入行数, 告警行)。

    上架表 SKU 列回写:按 **(店, ASIN)** 找行(source_key 就是 ASIN),写新码。
    写成功才盖 sheet_synced_at —— 盖不上的行下一轮再来(「当轮写完,攒到下一轮 =
    悄悄少写」,conventions §八)。**在数据库事务之外**:飞书是外部 IO,
    它失败不该让已经定案的身份回滚。
    """
    if not rows:
        return 0, []
    if not execute:
        return 0, []
    sheet = listing_sheet.read_rows(upto="sku")
    by_key = {(r["store"], r["asin"]): r["rownum"] for r in sheet}
    updates, done_ids, missing = [], [], []
    for r in rows:
        rownum = by_key.get((store_name, r["source_key"]))
        if rownum is None:
            missing.append(r["new_sku"])
            continue
        updates.append((rownum, r["new_sku"]))
        done_ids.append(r["id"])
    warns: list[str] = []
    n = 0
    if updates:
        n = listing_sheet.write_sku_col(updates, execute=True)
        with db.pg_conn() as tx:
            tx.execute(_SQL_LEDGER_SHEET_OK, {"ids": done_ids})
    if missing:
        warns.append(f"  ⚠ 上架表找不到行的 {len(missing)} 个新码(按 (店, ASIN) 反查):"
                     f"{missing[:5]} —— SKU 列停在旧码,回执找行与退役都会对不上;"
                     f"补一行或人工填 SKU 列,下一轮自动补写")
    return n, warns


def _settle(conn, store_name: str, execute: bool, *,
            observe_hours: float = OBSERVE_HOURS,
            stale_hours: float = STALE_HOURS) -> tuple[dict, list[str]]:
    """输入:只读连接 + 店 + 是否真写 → 输出:(判决计数, 摘要行)。

    每次调用**最先**跑这一段(-p settle_only=1 时只跑它)。判据见 _verdict;
    execute=False 时六处写全部跳过(定案 / UPC 改标 / 处置迁键 / 节点库存 /
    过程账 / 飞书),只算判决并报"将定案多少条"。

    ⚠ 写不走入参的 `conn`:那是本轮的**只读**连接,而定案必须是自己的短事务
    (每条各自成败,一条撞车不拖垮整轮),飞书回写又必须在事务提交**之后**。
    """
    counts = {_CONFIRMED: 0, _ROLLED_BACK: 0, _STALLED: 0, _PENDING: 0,
              "double": 0, "sheet": 0}
    lines: list[str] = []
    warns: list[str] = []
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(_SQL_UNSYNCED, {"store": store_name})
        backlog = _rows(cur)
        cur.execute(_SQL_OBSERVE, {"store": store_name})
        pend = _rows(cur)
        cur.execute(_SQL_UNSENT, {"store": store_name})
        unsent = _rows(cur)
    counts["unsent"] = len(unsent)
    if unsent:
        warns.append(
            f"  ⚠ 落库未提交 {len(unsent)} 条(pending 但 submitted_at 为空):"
            f"{[r['old_sku'] for r in unsent][:5]} —— 进程死在 POST 前后,或提交"
            f"当场抛了异常。**不自动定案**:从台账上分不出「确定没发」与「不知道"
            f"到没到」。人工核:先跑 feed_poll 让 ops.feed_log 那条落定,再去沃尔玛"
            f"后台看这个 Product ID 现在挂的是哪个 SKU —— 挂旧码 = 没发出去,"
            f"挂新码 = 已生效")

    receipts: dict[str, dict] = {}
    for row in pend:
        fid = row.get("feed_id")
        if fid and fid not in receipts:
            try:
                receipts[fid] = feed_track.item_results(fid)
            except Exception as e:            # noqa: BLE001 —— 读不到回执 ⇒ 当没有
                logger.warning("回执台账读取失败 feed=%s: %s", fid, e)
                receipts[fid] = {}

    confirmed_rows: list[dict] = []
    for row in pend:
        row["_observe_hours"], row["_stale_hours"] = observe_hours, stale_hours
        receipt = receipts.get(row.get("feed_id") or "", {}).get(row["new_sku"])
        verdict, why = _verdict(row, receipt, now)
        counts[verdict] = counts.get(verdict, 0) + 1
        tag = f"{row['old_sku']}→{row['new_sku']}"
        if verdict == "double":
            warns.append(f"  ⚠ 同店双挂 {tag}:{why}")
            continue
        if verdict == _PENDING:
            continue
        if not execute:
            lines.append(f"  [DRY-RUN] 将定案 {tag} = {verdict}({why})")
            continue
        # 逐条隔离:一条定案炸了(撞唯一索引 / 连接抖动)不拖垮整轮 —— 否则
        # 一条卡住的行会让**其余全部**改码永远定不了案。异常不吞:点名 + 记日志
        try:
            if verdict == _CONFIRMED:
                warns += _confirm(store_name, row)
                confirmed_rows.append(row)
                logger.info("改码定案 confirmed %s %s:%s", store_name, tag, why)
            elif verdict == _ROLLED_BACK:
                _roll_back(store_name, row, why)
                logger.warning("改码回滚 %s %s:%s(**不自动补交**,"
                               "要重来请下一轮再跑,会抽一个新码)", store_name, tag, why)
                warns.append(f"  ⚠ 回滚 {tag}:{why} —— 未自动补交(写操作永不自动兜底);"
                             f"要重来下一轮会抽新码,失败的那个码永久弃用")
            else:                              # stalled
                with db.pg_conn() as tx:
                    tx.execute(_SQL_LEDGER_STALLED,
                               {"error": why[:900], "id": row["id"]})
                warns.append(f"  ⚠ 超期未定案 {tag}:{why}")
        except Exception as e:                 # noqa: BLE001 —— 见上:隔离不是吞
            counts[verdict] -= 1
            counts["failed"] = counts.get("failed", 0) + 1
            logger.exception("改码定案失败 %s %s(判词 %s)", store_name, tag, verdict)
            warns.append(f"  ⚠ 定案失败 {tag}(判词 {verdict}):"
                         f"{e.__class__.__name__}: {e} —— 该行事务已回滚,状态没变,"
                         f"下一轮会重新判;连续失败请人工看日志")

    todo = backlog + confirmed_rows
    if todo and not execute:
        lines.append(f"  [DRY-RUN] 将回写上架表 SKU 列 {len(todo)} 行(含补写 "
                     f"{len(backlog)} 行),本次一格都没写")
    try:
        n_sheet, sheet_warns = _sync_sheet(store_name, todo, execute)
    except Exception as e:                     # noqa: BLE001 —— 飞书是外部 IO:
        # 它失败不该让已经定案的身份回滚,也不该让这一轮报失败;sheet_synced_at
        # 这一列存在的意义就是"下一轮再来"(conventions §八:当轮写完不了就点名)
        logger.exception("上架表 SKU 列回写失败(下一轮补写)")
        n_sheet, sheet_warns = 0, [
            f"  ⚠ 上架表 SKU 列回写失败({e.__class__.__name__}: {e}):"
            f"{len(todo)} 行仍停在旧码,已留 sheet_synced_at=NULL 待下一轮补写"]
    counts["sheet"] = n_sheet
    warns += sheet_warns
    if backlog:
        lines.append(f"  上架表 SKU 列补写候选 {len(backlog)} 行"
                     f"(上一轮写失败的,本轮重来)")
    lines.append(f"  定案:confirmed {counts[_CONFIRMED]}、"
                 f"rolled_back {counts[_ROLLED_BACK]}、stalled {counts[_STALLED]},"
                 f"仍 pending {counts[_PENDING]},本轮回写上架表 {n_sheet} 行")
    n_lag = len(todo) - n_sheet if execute else len(todo)
    counts["sheet_lag"] = max(n_lag, 0)
    return counts, lines + warns


# ══════════════════════════════════════════════════════════════════════════════
#  W5 · 节奏硬闸
# ══════════════════════════════════════════════════════════════════════════════

def _stage_cap(conn, store_name: str, asked_limit: int) -> tuple[int, str]:
    """输入:连接 + 店 + 请求上限 → 输出:(本轮生效上限, 一句人话)。

    所有者定的节奏是 1 → 10 → 一店 → 全店,写成代码里的闸而不是纪律
    (「缺省即真跑…这条纪律,没有默认值替你挡」——漏敲一个 limit 不该变成
    一次改一千个):

      · 该店还有 pending/stalled 未定案 ⇒ 本轮上限 **0**(只定案,不提交):
        账没清就发下一批,一旦形态选错就是成批的双挂,而双挂只能人工一条条收;
      · 零 confirmed ⇒ 1(第一级:先拿一个品把六件实测在生产上走通);
      · 0 < confirmed < 10 ⇒ 10(第二级);
      · ≥10 ⇒ 不再压,按 asked_limit。

    **`-p limit=` 只能收紧**:生效上限 = min(asked, 闸)。另外再叠一层
    FEEDS_PER_STORE_PER_RUN × ITEMS_PER_FEED 的配额留量硬顶。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_STAGE, {"store": store_name})
        row = cur.fetchone() or (0, 0)
    n_conf, n_open = int(row[0] or 0), int(row[1] or 0)
    quota_cap = FEEDS_PER_STORE_PER_RUN * ITEMS_PER_FEED
    if n_open:
        return 0, (f"节奏闸:该店还有 {n_open} 条改码未定案(pending/stalled),"
                   f"本轮**只定案不提交** —— 先把上一批的账清干净")
    if n_conf == 0:
        stage, why = 1, "该店零 confirmed(第一级:一次只许 1 个品)"
    elif n_conf < 10:
        stage, why = 10, f"该店已 confirmed {n_conf} 个(第二级:上限 10)"
    else:
        stage, why = asked_limit, f"该店已 confirmed {n_conf} 个(节奏闸放行,按 -p limit)"
    eff = max(min(asked_limit, stage, quota_cap), 0)
    return eff, (f"节奏闸:本轮上限 {eff}(请求 {asked_limit};{why};"
                 f"配额留量硬顶 {quota_cap} = {FEEDS_PER_STORE_PER_RUN} 个 feed × "
                 f"{ITEMS_PER_FEED} 条)")


# ══════════════════════════════════════════════════════════════════════════════
#  W4 · 候选 / 载荷 / 提交
# ══════════════════════════════════════════════════════════════════════════════

def _pick_report(store_name: str, only_skus, only_keys, excl_skus, excl_keys,
                 kept: list[dict], why_rows: list[dict], inflight: set,
                 dupe_skus: set, limit: int) -> list[str]:
    """输入:点名/排除四组名字 + 本轮留下的候选 + `_SQL_WHY` 的逐条判据 + 两道
    后置闸的落选集 + 本轮上限 → 输出:摘要行(点名 N 个、命中 H 个,落选的**逐条**给理由)。

    「点名了却没出现」必须有名有姓的理由。静默丢的表现是:摘要看起来像"这家店
    没候选",而所有者以为自己点的名生效了 —— 于是他等一个永远不会来的结果。
    六类理由,来源各不相同:

      · 被 `-p exclude_*` 排除(排除优先于点名)—— 参数自己说了算;
      · 不满足九条判据之一 —— 来自 `_SQL_WHY`,与候选 SQL **同一份判据文本**;
      · 旧码上有在途 feed;· 同批 Product ID 撞号 —— 两道后置闸;
      · 满足全部条件但**本轮节奏闸没轮到**(按 SKU 升序先来后到,下轮再来);
      · 店下查无此行(目录 × 登记簿的交集里没有它:拼错 / 不在册 / 从没扫到过)。

    落选名单**一个都不省略**(按理由归并成行,不截断):截断的那几个就是下一次
    "我明明点了它"的来源。
    """
    excl_sku_set, excl_key_set = set(excl_skus), set(excl_keys)
    hit_skus = {r["old_sku"] for r in kept}
    hit_keys = {r["source_key"] for r in kept}
    by_sku: dict[str, list[dict]] = {}
    by_key: dict[str, list[dict]] = {}
    for w in why_rows:
        by_sku.setdefault(w["old_sku"], []).append(w)
        by_key.setdefault(w["source_key"], []).append(w)

    def _reason(w: dict) -> str:
        if w["old_sku"] in excl_sku_set or w["source_key"] in excl_key_set:
            return "被 -p exclude_skus / -p exclude_asins 排除(排除优先于点名)"
        bad = [_CONDS[i][1] for i in range(len(_CONDS)) if not w.get(f"c{i}")]
        if bad:
            return "不满足候选条件 —— " + ";".join(bad)
        if w["old_sku"] in inflight:
            return (f"旧码上有 {INFLIGHT_HOURS}h 内的在途 feed(改了码,那条 feed "
                    f"就打在一个即将不存在的 SKU 上)")
        if w["old_sku"] in dupe_skus:
            return ("同一批里 Product ID 撞号(官方不许两个 SKU 挂同一个 "
                    "Product ID),本轮只留了先到的那条")
        return (f"**条件全都满足,只是本轮上限 {limit} 个没轮到它**(节奏闸,按 "
                f"SKU 升序先来后到)—— 下一轮它还在候选面上")

    groups: dict[str, list[str]] = {}
    n_hit = 0
    for tok in only_skus:
        if tok in hit_skus:
            n_hit += 1
            continue
        ws = by_sku.get(tok, [])
        if not ws:
            groups.setdefault(
                f"店 {store_name} 下查无此 SKU(catalog.walmart_items × "
                f"catalog.listing_sources 的交集里没有它:拼错 / 不在册 / "
                f"从没被扫到过)", []).append(tok)
        for w in ws:
            groups.setdefault(_reason(w), []).append(tok)
    for tok in only_keys:
        if tok in hit_keys:
            n_hit += 1
            continue
        ws = by_key.get(tok, [])
        if not ws:
            groups.setdefault(
                f"店 {store_name} 下查无此 source_key(登记簿里没有这个 ASIN:"
                f"拼错 / 不在册 / 是跟卖出身(source_key 存的是 GTIN))",
                []).append(f"{tok}(ASIN)")
        for w in ws:
            groups.setdefault(_reason(w), []).append(
                f"{tok}(ASIN→{w['old_sku']})")

    n_named = len(only_skus) + len(only_keys)
    head = (f"  {'⚠ ' if not n_hit else ''}点名 {n_named} 个"
            f"(-p skus {len(only_skus)} 个 + -p asins {len(only_keys)} 个),"
            f"命中 {n_hit} 个")
    if not groups:
        return [head]
    n_miss = sum(len(v) for v in groups.values())
    out = [head + f";落选 {n_miss} 个,逐条:"]
    out += [f"    · {'、'.join(names)}:{reason}" for reason, names in groups.items()]
    return out


def _candidates(conn, store_name: str, limit: int, *,
                only_skus=(), only_keys=(),
                exclude_skus=(), exclude_keys=()) -> tuple[list[dict], list[str]]:
    """输入:连接 + 店 + 上限(+ 点名/排除四组名字)→ 输出:(候选行, 逐候选被跳过的点名)。

    候选 = 在架 ∧ 活码 ∧ 未在改 ∧ 出身在 SOURCE_TYPES ∧ **不是**不透明码
    ∧ 观测到的 upc/gtin 至少有一个 ∧ 该 (店, 旧码) 无未了结的改码台账
    (九条判据的唯一出处是 `_CONDS`,候选 SQL 与理由 SQL 共用同一份文本)。

    `only_skus` / `only_keys`(`-p skus=` / `-p asins=`,取并集)与
    `exclude_skus` / `exclude_keys` 是**同一条候选 SQL 的参数化条件**,
    不是第二条选取路径;**点名不放松任何一道闸**,落选的逐条给理由
    (`_pick_report`)。上限 0 时**一条 SQL 都不发**(闸未过 / settle_only /
    上一批没清):这时点名的说明由 run() 出,别在这里悄悄查库。

    Product ID 取 **catalog.walmart_items 观测到的 upc(空则 gtin)**,不取 UPC 池:
    改码按 Product ID 匹配,池里的号若与沃尔玛现挂的不一致(历史换过号),
    载荷会匹配到别的 item 或直接被拒。两列都空的行 SQL 里就排掉了(不猜)。

    再过 W2 第⑥道闸:旧码上有在途 feed 的**逐个跳过并点名**(不整店拦)——
    一条刚发出去的 feed 在途时改码,会让它打在一个即将不存在的 SKU 上。
    """
    if limit <= 0:
        return [], []
    named = bool(only_skus or only_keys)
    args = {"store": store_name, "source_types": list(SOURCE_TYPES),
            "limit": limit, "unnamed": not named,
            "only_skus": list(only_skus), "only_keys": list(only_keys),
            "excl_skus": list(exclude_skus), "excl_keys": list(exclude_keys)}
    with conn.cursor() as cur:
        cur.execute(_SQL_CANDIDATES, args)
        rows = _rows(cur)
        inflight: set = set()
        if rows:
            cur.execute(_SQL_INFLIGHT_OLD, {"store": store_name,
                                            "skus": [r["old_sku"] for r in rows],
                                            "hours": INFLIGHT_HOURS})
            inflight = {r[0] for r in cur.fetchall()}
        why: list[dict] = []
        if named:                      # 点名了就必须能解释,哪怕一条候选都没选出来
            cur.execute(_SQL_WHY, args)
            why = _rows(cur)
    notes: list[str] = []
    if inflight:
        notes.append(f"  ⚠ 跳过 {len(inflight)} 个:旧码上有 {INFLIGHT_HOURS}h 内的在途 feed"
                     f"(改了码那条 feed 就打在一个即将不存在的 SKU 上):"
                     f"{sorted(inflight)[:5]}")
    keep = [r for r in rows if r["old_sku"] not in inflight]

    # 一个 Product ID 只允许挂一个 SKU(官方:"You are not allowed to submit two
    # SKUs with the same Product Identifier")—— 同一批里撞号的只留第一条
    seen: dict[str, str] = {}
    out: list[dict] = []
    dupes: list[str] = []
    dupe_skus: set = set()
    for r in keep:
        pid = r["product_id"]
        if pid in seen:
            dupes.append(f"{r['old_sku']}(与 {seen[pid]} 同 Product ID {pid})")
            dupe_skus.add(r["old_sku"])
            continue
        seen[pid] = r["old_sku"]
        out.append(r)
    if dupes:
        notes.append(f"  ⚠ 跳过 {len(dupes)} 个:同一批里 Product ID 撞号"
                     f"(官方不许两个 SKU 挂同一个 Product ID):{dupes[:3]}")
    if named:
        notes += _pick_report(store_name, only_skus, only_keys,
                              exclude_skus, exclude_keys, out, why,
                              inflight, dupe_skus, limit)
    elif exclude_skus or exclude_keys:
        notes.append(f"  排除 -p exclude_skus {len(exclude_skus)} 个 / "
                     f"-p exclude_asins {len(exclude_keys)} 个"
                     f"(已在候选 SQL 里剔除,没点名 ⇒ 其余照常按 SKU 升序取)")
    return out, notes


def _build_items(rows: list[dict]) -> list[dict]:
    """输入:候选行(带 new_sku / product_id / product_id_type)→ 输出:MPItem 列表。

    **形态 A/B 的唯一分叉点**。今天走形态 A:mp_mapper.build_sku_update_item 的
    最小载荷(新码 + 现挂 Product ID + SkuUpdate=Yes),不重发内容 ⇒ 标题与属性
    不会被我们再生成的文案覆盖。形态 B(MP_ITEM 全量)要改的就是这一个函数
    + FEED_TYPE 常量,别处一行不动。
    """
    return [mp_mapper.build_sku_update_item(r["new_sku"], r["product_id"],
                                            r["product_id_type"])
            for r in rows]


def _preview(rows: list[dict]) -> list[str]:
    """输入:候选行 → 输出:dry-run 的样例行(占位码,**不 mint**)。"""
    lines = [f"  [DRY-RUN] 将改码 {len(rows)} 个(前 {min(len(rows), PREVIEW_ROWS)} 个):"]
    for r in rows[:PREVIEW_ROWS]:
        lines.append(f"    · {r['old_sku']} → <新码>(出身 {r['source_type']}/"
                     f"{r['source_key']},Product ID {r['product_id_type']}="
                     f"{r['product_id']})")
    sample = mp_mapper.build_sku_update_item(
        sku_codec.DRYRUN_PLACEHOLDER, rows[0]["product_id"],
        rows[0]["product_id_type"])
    lines.append(f"    载荷样例({FEED_TYPE};sku 位置真跑时是抽出来的 12 位码,"
                 f"这里是占位码):{sample}")
    return lines


def _migrate(store: dict, rows: list[dict], execute: bool) -> tuple[dict, list[str]]:
    """输入:店铺(凭证 dict)+ 候选行 + 是否真跑 → 输出:(计数, 摘要行)。

    事务边界是**安全铁律**,不是风格:

      ① `with db.pg_conn()` 里对全部候选 mint_replacement + 写 pending 台账,
         **该 with 在组载荷之前退出**(registry/db.pg_conn 正常退出才 commit)。
         照"一个大事务包到底"写的话,进程死在 POST 之后、with 退出之前,
         新码行与 pending 台账全部 rollback,而沃尔玛已经受理 —— 新码成了
         一条没有出身的孤儿行,而且不报错。
      ② 组载荷、③ 提交(每批 ≤ ITEMS_PER_FEED 条 = 一个 feed,最多
         FEEDS_PER_STORE_PER_RUN 批)、④ 另开短事务按 outcome 落账:
           submitted/dedup 且有 feed_id ⇒ 落 feed_id + submitted_at(等观测定案);
           failed(4xx 或 token/代理阶段失败,api/feeds 已判**确认未达**)⇒ 当场回滚;
           unknown ⇒ **保持 pending 不回滚**(见模块头注「有意出入」)。
    """
    counts = {"submitted": 0, "unknown": 0, "rolled_back": 0}
    lines: list[str] = []
    if not rows:
        return counts, ["  本轮无候选(该店存量码已迁完,或全被闸拦下)"]
    if not execute:
        return counts, _preview(rows)

    store_name = store["name"]
    # 配额留量的截断必须在 **mint 之前**:先 mint 再截,多出来的行就成了
    # "落库了但永远没发出去"的孤儿 —— 它们不进 _SQL_OBSERVE(submitted_at 为空),
    # 却让节奏闸永远看见 pending,整店从此发不出下一批。
    # 正常路径上 _stage_cap 已经压过一次,这里是同一条闸的第二道保险。
    cap = FEEDS_PER_STORE_PER_RUN * ITEMS_PER_FEED
    if len(rows) > cap:
        lines.append(f"  ⚠ 候选 {len(rows)} 个超配额留量 {cap},本轮只发前 {cap} 个"
                     f"(其余**一个字都没落库**,下轮再来)")
        rows = rows[:cap]
    # ① 先落库并 commit(防重状态先落库再调接口)
    with db.pg_conn() as conn:
        for r in rows:
            r["new_sku"] = sku_codec.mint_replacement(
                conn, store_name, r["old_sku"], r["source_type"], r["source_key"],
                workflow="sku_migrate")
            with conn.cursor() as cur:
                cur.execute(_SQL_LEDGER_NEW, {
                    "store": store_name, "old_sku": r["old_sku"],
                    "new_sku": r["new_sku"], "source_type": r["source_type"],
                    "source_key": r["source_key"], "feed_type": FEED_TYPE,
                    "detail": json.dumps({"product_id": r["product_id"],
                                          "product_id_type": r["product_id_type"]})})
                r["id"] = cur.fetchone()[0]
    logger.info("改码台账已落库并提交:%s %d 条 pending(此刻才允许调接口)",
                store_name, len(rows))

    # ②③④ 分批提交,逐片对位落账
    batches = [rows[i:i + ITEMS_PER_FEED]
               for i in range(0, len(rows), ITEMS_PER_FEED)]
    for batch in batches:
        results = feeds.submit_feed(store, FEED_TYPE, _build_items(batch),
                                    workflow="sku_migrate")
        for res, slice_rows in feeds.iter_result_slices(results, batch):
            if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                with db.pg_conn() as tx:
                    tx.execute(_SQL_LEDGER_SUBMITTED,
                               {"feed_id": res["feed_id"],
                                "ids": [r["id"] for r in slice_rows]})
                counts["submitted"] += len(slice_rows)
                lines.append(f"  提交 {len(slice_rows)} 条(feed={res['feed_id']},"
                             f"{res['outcome']}) —— **回执成功不定案**,等观测")
            elif res["outcome"] == "failed":
                why = "POST 被拒或确认未达(api/feeds 判定 failed)"
                for r in slice_rows:
                    _roll_back(store_name, r, why)
                counts["rolled_back"] += len(slice_rows)
                lines.append(f"  ⚠ 提交失败 {len(slice_rows)} 条,已**当场回滚**"
                             f"(旧码复活、新码弃用);**不自动补交** —— "
                             f"人核对原因后下一轮重来,会抽新码")
            else:                              # unknown / deferred
                counts["unknown"] += len(slice_rows)
                lines.append(f"  ⚠ 提交结局不确定 {len(slice_rows)} 条,"
                             f"**保持 pending 不回滚**(不知道到没到;回滚会造出"
                             f"没有出身的孤儿码),留给启动对账与下一轮定案")
    return counts, lines


# ══════════════════════════════════════════════════════════════════════════════
#  W6 · 入口
# ══════════════════════════════════════════════════════════════════════════════

def run(params: dict) -> str:
    """输入:params(store 必填 / limit / settle_only / observe_hours / stale_hours /
    skus / asins / exclude_skus / exclude_asins)→ 输出:定案 + 提交摘要(首行点名四类告警)。

    执行序:读参 → _preflight → _settle(先定案,再考虑发新的)→ _stage_cap →
    _candidates → _migrate → 订单双算体检 → 组摘要。

    **点名(`-p skus=` / `-p asins=`)与排除(`-p exclude_skus=` / `-p exclude_asins=`)
    只挑范围,不放松任何一道闸**:前置五闸、逐候选在途闸、节奏硬闸照旧,点名 5 个而
    本轮只放 1 个是常态(零 confirmed ⇒ 上限 1),摘要会把这句说全;点名了却没进
    候选面的**逐条给理由**(_pick_report),不静默丢。

    `store` **必填**:一店一批不是口头节奏,是接口约束。缺省返回 ⛔ 开头的提示
    并且**什么都不做**(cli 认这个前缀,记 refused 而不是 success)。
    """
    store_name = str(params.get("store") or "").strip()
    if not store_name:
        return ("⛔ sku_migrate 必须指定店铺:-p store=<店铺名>\n"
                "   改码是一店一批、人盯定案的破坏动作,没有"
                "「对全船跑一遍」这种用法(节奏:1 → 10 → 一店 → 全店)。\n"
                "   先空跑:python cli.py sku_migrate --dry-run -p store=<店铺名>")
    execute = bool(params.get("execute", True))
    settle_only = str(params.get("settle_only", "")).strip() in ("1", "true", "True")
    raw_limit = params.get("limit")
    try:
        asked = DEFAULT_LIMIT if raw_limit in (None, "") else int(raw_limit)
    except (TypeError, ValueError):
        return f"⛔ -p limit= 必须是整数,收到 {raw_limit!r}"
    observe_h = float(params.get("observe_hours") or OBSERVE_HOURS)
    stale_h = float(params.get("stale_hours") or STALE_HOURS)
    # 点名与排除:两个点名参数**取并集**(skus 按旧码、asins 按登记簿 source_key),
    # 排除拼在候选 SQL 的点名条件之后且是 NOT ⇒ **排除优先**。
    only_skus = _parse_names(params.get("skus"))
    only_keys = _parse_names(params.get("asins"))
    excl_skus = _parse_names(params.get("exclude_skus"))
    excl_keys = _parse_names(params.get("exclude_asins"))
    n_named = len(only_skus) + len(only_keys)

    lines: list[str] = []
    warns: list[str] = []
    with db.pg_conn() as conn:
        ok, pre_lines = _preflight(conn, store_name)
        lines += pre_lines
        # ⚠ **前置闸拦的是"发新的",不是"定案"**:闸不过照样把上一批的账清干净。
        # 反过来写会死锁 —— 一条不相干的 executing 处置就能让整店的 pending 永远
        # 定不了案,而那些旧码正被缺席抑制着,没有任何东西会报。
        # 定案本身不怕闸不过:confirmed 要新码在架 ∧ 旧码缺席,rolled_back 要观测
        # 跑过新的一轮(fresh),两条都由**观测数据**说了算,水位陈旧只会让它们
        # 一直停在 pending。
        settle_counts, settle_lines = _settle(
            conn, store_name, execute,
            observe_hours=observe_h, stale_hours=stale_h)
        lines += settle_lines
        cap, cap_note = (0, "前置闸未过,本轮不提交") if not ok \
            else _stage_cap(conn, store_name, asked)
        if settle_only:
            cap, cap_note = 0, "-p settle_only=1:本轮只定案,不提交新的"
        if SUBMIT_DISABLED:
            # 硬闸,不看 execute:dry-run 列出"将改码 N 个"同样是误导
            cap, cap_note = 0, f"⛔ 提交通道停用(本轮只定案):{SUBMIT_DISABLED}"
        lines.append(f"  {cap_note}")
        cands, cand_notes = _candidates(conn, store_name, cap,
                                        only_skus=only_skus, only_keys=only_keys,
                                        exclude_skus=excl_skus,
                                        exclude_keys=excl_keys)
        lines += cand_notes
        # 上限 0 时 _candidates 一条 SQL 都不发(闸未过 / settle_only / 上一批没清)——
        # 点名的说明只能在这里出,否则摘要看起来像"这家店没候选"。
        if n_named and cap <= 0:
            lines.append(f"  ⚠ 点名 {n_named} 个,但本轮上限 0(理由见上一行):"
                         f"一条都没查、一条都没发 —— **点名不越闸**,先按上一行"
                         f"把账清干净,下轮再点同样这几个")
        dupes = order_lines.duplicate_po_lines(conn)

    mig_counts: dict = {}
    if ok and not settle_only:
        store = None
        if cands:
            matched = stores_svc.load_stores(filter_names=[store_name])
            store = matched[0] if matched else None
        if cands and store is None:
            warns.append(f"  ⚠ {store_name} 不在可调用列表里(未启用/没配代理/没凭证),"
                         f"本轮 {len(cands)} 个候选一个都没发")
        elif cands or not execute:
            mig_counts, mig_lines = _migrate(store or {"name": store_name},
                                             cands, execute)
            lines += mig_lines

    # 订单双算体检(X1):改码后若沃尔玛对改码**之前**的 PO 返回新码,会插出第二行
    # 而旧行不删 ⇒ 同一笔销售算两次且不报错。本体检只能**发现**不能阻止。
    if dupes:
        warns.append(f"  ⚠ 订单双行体检非零:{len(dupes)} 组 (店,PO,行号) 有多个 "
                     f"order_line_id,样本 {[(d['store'], d['po_id']) for d in dupes[:3]]}"
                     f" —— 改码前后各跑一次,数字变了就是销量双算")

    n_conf = settle_counts.get(_CONFIRMED, 0)
    n_roll = settle_counts.get(_ROLLED_BACK, 0) + mig_counts.get("rolled_back", 0)
    n_stall = settle_counts.get(_STALLED, 0)
    n_pend = settle_counts.get(_PENDING, 0)
    n_double = settle_counts.get("double", 0)
    n_lag = settle_counts.get("sheet_lag", 0)
    n_unsent = settle_counts.get("unsent", 0)
    n_failed = settle_counts.get("failed", 0)
    n_sub = mig_counts.get("submitted", 0)
    n_unk = mig_counts.get("unknown", 0)
    n_skip = len(cands) - n_sub - n_unk - mig_counts.get("rolled_back", 0) \
        if execute else len(cands)

    gist = (f"定案 confirmed {n_conf} / rolled_back {n_roll} / stalled {n_stall}"
            f"(仍 pending {n_pend}),本轮提交 {n_sub},跳过 {max(n_skip, 0)}")
    if n_named:
        # 首行必须说清"点了几个、中了几个、为什么只中这么几个":cli 的链通知只取
        # 首行,而"命中 0 个"与"这家店没候选"是两件事(后者不该让人去查参数)。
        gist += f";点名 {n_named} 个,命中 {len(cands)} 个"
        if cap <= 0:
            gist += "(本轮上限 0,一个都没发)"
        elif cap < n_named:
            gist += f"(节奏闸本轮只放 {cap} 个,其余下轮;逐条理由见明细)"
        elif len(cands) < n_named:
            gist += "(落选的逐条理由见明细)"
    if not ok:
        # ⚠ 这个 ⛔ **有意不放在首行行首**:cli 认行首的 ⛔ 记 refused,而 refused 的
        # 语义是「前提不成立、**什么都没做**」—— 闸不过时定案照跑,可能真定了案,
        # 报 refused 就把"清了账"说成"什么都没干"。闸的结论仍在首行里,人看得见。
        gist = "⛔ 前置闸未过,本轮只定案、不发新的;" + gist
    # 四类必须见人的告警**拼在首行**:cli 的链通知只取首行,落在下面就只进了日志
    if n_double:
        gist += f";⚠ 同店双挂 {n_double}"
    if n_stall:
        gist += f";⚠ 超期未定案 {n_stall}"
    if dupes:
        gist += f";⚠ 订单双行 {len(dupes)} 组"
    if n_lag:
        gist += f";⚠ 上架表 SKU 列未同步 {n_lag} 行"
    if n_unk:
        gist += f";⚠ 提交结局不确定 {n_unk}(保持 pending)"
    if n_unsent:
        gist += f";⚠ 落库未提交 {n_unsent}"
    if n_failed:
        gist += f";⚠ 定案失败 {n_failed}"
    # dry-run 前缀**拼在首行行首**:用 insert(0) 会让告警顶到抬头行之前,
    # 一条空跑摘要就以真跑的面目进了飞书(cli 只取首行)
    first = ("" if execute else "🧪 [DRY-RUN] ") + nf.head(
        "sku_migrate", gist, store_name)
    return nf.summary(first, sections=[("本轮明细", lines + warns)],
                      tail=(f"下一步:catalog_sync -p store={store_name} 跑一轮观测,"
                            f"再 sku_migrate -p store={store_name} -p settle_only=1 定案"
                            if n_sub else ""))
