"""list_new — 上架主链(listing L2d,替代旧 auto_listing/main.py;危险:缺省即真跑,空跑用 --dry-run)。

用法(⚠ 缺省即真跑 —— 会真领 UPC、真提交 feed 到沃尔玛):
  python cli.py list_new --dry-run           # 空跑:闸门链判定+逐行去向,不提交
  python cli.py list_new                     # 真跑(LLM 出参/领 UPC/提交 feed)
  python cli.py list_new -p store=A085朱丽霖
  python cli.py list_new -p limit=1 -p store=X   # 试点:本轮只做前 1 行
                                             # (缺省不截断;闸门与过滤之后才切)
  python cli.py list_new -p gap_wait=0       # 缺数据只推采集不等(默认等 20 分钟)
  python cli.py list_new -p workers=64       # 预备期 LLM 并发(默认 128,
                                             # 实际还会按 PG 连接余量钳制)
  python cli.py list_new -p submit_jitter_ms=0  # 提交期起跑抖动(毫秒,默认 800;
                                             # 0=关。去同步,不降并发)

驱动表 = 上架表(registry.LISTING_SHEET,21 列;**列按表头名定位**,见
services/listing_sheet.layout,列顺序随所有者调整,代码不跟着改):领任务条件
审核结果=pass 且 是否上架 空/No 且 无 feedid;是否上架∈{Yes,Unknown} 跳过(Unknown 也算
已上架——沃尔玛可能已收单,重复提交 = 双上架,旧生死规则)。
O=FAILED 走重试通道(≤3 次);O=SKU_LOCKED 本工作流不碰——由
sku_locked_heal 自愈链处理(RETIRE→24h 冷却→清列,行变新行后回到
本链领**新 UPC** 重上)。旧实证:SKU 已绑死旧 UPC,不先退役直接换
UPC 重发同一 SKU 也会失败(legacy_survey.md:1667),不是永久放弃。

闸门链(顺序即执行序,每道命中写 N=未上架理由或摘要计数):
  ① 店铺状态(ops.store_kpi_daily 非 ACTIVE 整店跳过,无记录视为 ACTIVE)
  ② 日配额**在全部过滤之后切**(所有者批复 2026-08-12:配额以成功提交为准,
    淘汰放切片前——先切片再过滤会让被淘汰行白占名额,淘汰率 40% 时实际
    只能上到配额的 60%)。额度 = 限额表「上架限制」- 今日已提交数
    (ops.feed_items MP_ITEM,北京日界);超配额行不写终态,次日自动续上
  ③ PT spec 存在(pt_spec;无 spec 淘汰)+ 风控否决闸(risk_gate:禁售 PT)
  ④ 本店 ASIN 去重(**同店**已在架才拦;所有者定稿 2026-08-28「取消全局
    去重」——跨店不再互拦,跨店分布由分配链 + 占用闸治理。改因:2026-08-28
    沃尔玛把全账号退市档案翻回 items 响应集,任何店的死档案行都会把该 ASIN
    对全船队封死)+ **占用闸**(catalog.claims:该 ASIN 或其品牌已被
    **别的店**占用即拦;占用是决策台账,商品下架也不释放,这正是快照闸
    补不上的那半边——见 docs/allocation_plan.md §二)+ ASIN 黑名单
    (catalog.asin_blacklist 永久禁止六类 + 黑名单品牌,见③)。
    **防呆=黑名单,不看删除史**(所有者口径 2026-08-12:拦"出现过侵权/
    审查等拉黑类别"的,不拦"因产品问题删过"的——可修复类删除后重上是
    正常经营,曾按删除史一刀切拦过,当日拆除);
    "不明原因消失"史=疑似平台下架,只在摘要报警不拦截(积累观察后再定)
    ⚠ 占用闸与快照闸**并存**(A1 阶段):台账回填完整前,快照闸仍是主力;
    两道都过才放行,理由分开写,谁拦的一目了然
  ⑤ 数据源(services/amz_source):**上架必须用当天最新数据**(所有者定稿
    2026-08-19)——**全部候选**先推采集刷新(日界批次名防重)+ 插队 →
    等它采完(默认 20 分钟,`-p gap_wait=` 可调)→ 就地按批摄取(批次
    端点,无锁)→ 才取数定价;超时不是失败,没刷到的行用库中现值上架
    (维护链次日纠正),库里压根没有的照旧不写终态、次日续
  ⑥ 数据过滤:库存 <5 淘汰;配送超时上架但库存写 0;品牌/制造商黑名单
    (两字段都查,brand=Generic 真品牌在 manufacturer 是常态);
    **店铺渠道闸**(限额表「配送限制」:标了 fba/fbm 就只上该渠道的货,
    **没标=不限制**;判定走 services/store_targets.channel_conflict 唯一谓词。
    产品渠道采不到的行在上一档就已被"不定价"拦下,不重复判);
    定价:services/pricing(FBA/FBM 区间×倍率;出界按 300% 兜底,
    只有区间内倍率未配置才淘汰)
  ⑦ 三段式提交(所有者定稿 2026-08-18 重排:「过闸后默认128并发打
    deepseek…能过的才能领 upc,领完直接按店批量上架」):
    预备期 = LLM 出参(取数三级:llm_cache 一级 hash 命中 → 二级同
    (asin,pt) 旧出参复用(硬条件签名 + 标题规格验证,见 _map_llm 头注)
    → 才真调;默认 128 并发,按 PG 连接余量钳制)+ mapper 硬约束 +
    spec 一致化,UPC 用占位号,缺必填本地拦下
    (不领号不烧配额)→ 领号期 = 通过的行按店批量领 UPC(catalog.upc_pool
    事务,FOR UPDATE SKIP LOCKED),真号回填占位号 → 提交期 = 同店打包
    单个 MP_ITEM feed(10/hour 硬限),店间并发 ≤24。
    提交期三条防线(所有者定稿 2026-08-26,全部只在出事时花钱):
      · **起跑抖动** 0~800ms:去掉"24 个线程同一毫秒发起",不降并发
      · **自适应降档** 24→16→12→8→4:遇 5xx/限流降一档,只降不升
        (官方 429 口径就是 resume at a lower rate);下轮回顶格
      · **延后结算**:5xx/网络不确定的片子当轮不写终态,整轮跑完再
        反查+补交 —— 内联补交打进的正是造成它失败的那片拥堵

闸门链之前先**注入一次 UPC 池**(所有者定稿 2026-08-16:「运行时自动同步
一次 UPC,然后再走上架流程」)——运营刚贴进「UPC池」表的号,这一轮就要能领。
注入那段收在 `services.upc_pool.sync_from_sheet`(铁律 1:不能 import
upc_sync 工作流);失败只告警不阻断,dry-run 不注入(注入是写库)。

提交结局(旧三态生死语义,UPC 回收仅三类):
  submitted → K=Yes L=feedid M=日期,UPC 标已用,事件 list_submitted
              (登记簿在预备期抽码时就已登记,不在这里补);SKU 列与 K/L/M 同
              一次写回;O/P/Q 由 feed_poll 反哺器按回执四集合回填
  failed(4xx 拒)→ N=提交被拒,UPC 回收(rejected)
  unknown → K=Unknown(不重复提交),UPC **不回收**
  内容标准拒(回执 O=CONTENT_REJECTED)不入 FAILED 通道,**也不自动重试**
  (所有者定稿 2026-08-23 撤除捞回通道):文案图片取自亚马逊原文,原样重发
  必然同拒,还会触发/延长 QARTH 合规审查。这类行停在 O 列等人 —— 人工改好
  文案、清掉 O 列即可重回普通通道(与 PROHIBITED 的"永不"语义有别)。
  ⚠ 撤除的只是**自动重上**;`WALMART_ERR_CONTENT` 的归类照旧留着,否则它会
  掉进 FAILED 通道被重试三次,纯烧 UPC 与配额
"""

import collections
import contextlib
import logging
import random
import threading
import time
from datetime import datetime
from typing import NamedTuple

from api import feeds, feishu, llm, scraper, settings as settings_api
from registry import db, paths, resources
from services import alloc_survey, amz_source, blacklist, brand_key, claims, \
    db_guard, kpi, listing_sheet, listing_sources, llm_cache, mp_conform, \
    mp_mapper, notify_fmt as nf, pricing, product_events, product_ingest, \
    pt_spec, risk_gate, scrape_batches, sku_codec, store_limits, \
    store_targets, stores as stores_svc, upc_pool, variant_group, \
    variant_remap, variant_title
from services import store_events, store_retry

DANGEROUS = True

# ── 提交期自适应降并发(所有者定稿 2026-08-26:「遇到限流以后动态降并发,
#    而不是直接打死」)────────────────────────────────────────────────────────
#
# 阶梯照所有者给的写:24 → 16 → 12 → 8 → 4,**不再往下**(4 路是保底通道,
# 降到 0 就等于把当轮剩下的店全废了,那正是"直接打死")。
#
# 官方口径对得上(2026-08-26 核验 developer.walmart.com/us-marketplace/docs/
# rate-limiting):429 之后「Sleep until x-next-replenish-time, then **resume
# at a lower rate**」——沃尔玛自己要求的就是降速续跑,不是停。
#
# ⚠ **只降不升**(同一轮内)。升回去要先能判断"拥堵过去了",而一轮就几分钟,
# 判据必然是猜的;猜错就是在拥堵没散时又冲一次,把刚退下来的让步白费。
# 下一轮进程重开,阶梯自然回到顶格。
#
# ⚠ **第一轮基本降不到东西**:24 家店 ≤ 24 个槽位,全都瞬间拿到许可,等第一个
# 5xx 浮上来时已经全在飞了。它真正生效的地方是 ①店数超过顶格并发时排队的那些
# ②**第二轮延后结算** —— 而第二轮恰恰最需要它:第一轮已经证明这个时段拥堵。
# 所以这套东西治的是**恢复**,不是第一轮的洪峰;洪峰要治得靠切小 feed
# (官方 feeds 页原话:「Keep your bulk files small enough to process reliably.
# Split very large submissions into multiple feeds.」),那是另一件事。
_CONCURRENCY_LADDER = (16, 12, 8, 4)

# 第一轮起跑抖动(毫秒;所有者定稿 2026-08-26:「第一轮不降并发,但是不要所有店
# 同时执行提交,可以随机抖动多少 ms 来提交」)。
#
# ⚠ **它去同步,不降峰值** —— 说清楚免得被当成万灵药:上传要几十秒,几百毫秒的
# 抖动不会让任何两家店错开成"一前一后",24 家照样同时在传。它消掉的是**同一
# 毫秒**那一下:TLS 握手、首包、PG 领号、连接池建连全撞在一个瞬间。这一下几乎
# 不要钱(整轮多花不到 1 秒),所以做;但指望它治住持续重叠是指望错了对象。
#
# 为什么三段式重排之后才需要它:重排(#51)把 LLM 出参前置到预备期,提交期只剩
# 「领号(毫秒)→ 真号回填(微秒)→ POST」,24 个线程从 pool.submit 到发出 POST
# 几乎没有耗时差。重排之前每店的 LLM 是店内串着做的,POST 天然就错开了。
SUBMIT_JITTER_MS = 800


class _AdaptiveGate:
    """并发闸:正常放行到顶格,遇 5xx/限流按阶梯降一档(线程安全,只降不升)。

    不用信号量而用 Condition + 计数:信号量的许可数发出去就收不回来,
    而降档要的正是"**已经发出去的不动、后面来的按新上限排队**"。
    """

    def __init__(self, top: int, ladder=_CONCURRENCY_LADDER):
        self._cv = threading.Condition()
        self._limit = int(top)
        self._ladder = [n for n in ladder if n < int(top)]
        self._inflight = 0
        self.steps: list[tuple[int, str]] = []   # 降档轨迹,摘要里要报

    @property
    def limit(self) -> int:
        with self._cv:
            return self._limit

    def acquire(self) -> None:
        with self._cv:
            while self._inflight >= self._limit:
                self._cv.wait()
            self._inflight += 1

    def release(self) -> None:
        with self._cv:
            self._inflight -= 1
            self._cv.notify_all()

    def step_down(self, why: str) -> None:
        """输入:降档理由 → 输出:无。降一档并记轨迹;已到底则只记不降。"""
        with self._cv:
            nxt = next((n for n in self._ladder if n < self._limit), None)
            if nxt is None:
                return
            old, self._limit = self._limit, nxt
            self.steps.append((nxt, why))
        logger.warning("提交并发降档 %d → %d(%s)——沃尔玛 429 之后的官方口径"
                       "就是 resume at a lower rate", old, nxt, why)


def _settle_round(deferred: list, stores_by_name: dict, gate, today: str,
                  cnt_by_store: dict | None = None) -> list:
    """输入:整轮攒下的不确定片 + 店表 + 并发闸(+ 计数回填)→ 输出:摘要行。

    **第二轮**(所有者定稿 2026-08-26:「重试的等到完整跑完一轮再尝试」)。
    第一轮内联结算的老毛病是:补交在失败后 30 秒发出,而那时另外二十几家店
    还在传 —— 这一次补交打进的正是造成它失败的那片拥堵。整轮跑完之后管子
    已经空了,同一次补交的成功率完全是另一回事;而且拖过这几分钟之后,
    沃尔玛侧的 feed 索引也追上了,**反查更可能 FOUND ⇒ 直接收编、连补交都
    不必发**(生产实证:2026-08-24 那晚 4 家店就是这么救回来的)。

    走 `gate`:第二轮的并发已经被第一轮的降档压过一档 —— 第一轮既然证明了
    这个时段拥堵,重试就没有理由再按顶格冲。

    `cnt_by_store` 给了就把这一轮的三个结局**加**进各店的计数(店铺事件账本
    的一轮 = 首轮 + 延后结算,不是只有首轮):这些片子在首轮是"不确定"、
    一条都没算进 submitted,不加回去的话账本上的提交数会长期偏少。
    """
    if not deferred:
        return []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lines = [f"⏸ 延后结算:{len(deferred)} 片("
             f"{sum(len(b) for _, _, b in deferred)} 条)整轮跑完后重试,"
             f"并发 {gate.limit}"]

    def _one(item):
        store_name, settle, batch = item
        updates: list = []
        try:
            gate.acquire()
            try:
                res = feeds.settle_deferred(stores_by_name[store_name], settle)
            finally:
                gate.release()
            _apply_submit_result(store_name, res, batch, updates, today)
            if updates:
                listing_sheet.write_submit_cols(updates)
            return store_name, res["outcome"], len(batch)
        except Exception as e:                                  # noqa: BLE001
            # 结算炸了**不写终态**:UPC 留在「已领」、表上还是空,
            # feed_log 保持 pending —— 启动对账是它的下一站,不是丢掉
            logger.exception("延后结算异常(保持 pending):%s: %s", store_name, e)
            return store_name, "unknown", len(batch)

    tally: dict = {}
    with ThreadPoolExecutor(max_workers=min(gate.limit, len(deferred))) as pool:
        for f in as_completed([pool.submit(_one, d) for d in deferred]):
            sn, outcome, n = f.result()
            tally.setdefault(outcome, {}).setdefault(sn, 0)
            tally[outcome][sn] += n
            if cnt_by_store is not None and outcome in ("submitted", "failed",
                                                        "unknown"):
                c = cnt_by_store.setdefault(sn, {})
                c[outcome] = c.get(outcome, 0) + n
    label = {"submitted": "✅ 补上", "failed": "❌ 判未达(UPC 已回收,次日重试)",
             "unknown": "⚠ 仍不确定(保持 pending,交启动对账)"}
    for outcome in ("submitted", "failed", "unknown"):
        by = tally.get(outcome)
        if by:
            lines.append(f"  {label[outcome]} {sum(by.values())} 条:"
                         + ",".join(f"{k}×{v}" for k, v in sorted(by.items())))
    return lines


def _apply_submit_result(store_name: str, res: dict, batch: list,
                         updates: list, today: str) -> None:
    """输入:一片的提交结果 + 该片的 (行, UPC) → 输出:无(落库并攒表更新)。

    **两轮共用一条落地路径**(第一轮直接提交、第二轮延后结算)。分开写的话,
    延后结算那条迟早漏掉 mark_used 或事件 —— 漏了不报错,只是那批货在维护链
    眼里成了"来源不明"的孤儿(sources_backfill 才捞得回来)。

    ⚠ **这里不再调 listing_sources.register**(批次 2):登记已经在预备期
    `_prep_rows` 抽码那一刻由 `sku_codec.mint` 在同一事务里做完(抽码即登记,
    单一实现)。留着 register 是同一能力两条路径,而且语义已经不同 ——
    register 是「首次登记 ON CONFLICT DO NOTHING」,对 mint 出来的行永远是空
    操作,留着只会让下一个人以为登记发生在提交之后,进而把 mint 挪到提交后去
    (挪过去 = 串行补试二次抽码 = 双上架,见 `_prep_rows` 头注)。
    """
    with db.pg_conn() as conn:
        if res["outcome"] == "submitted" and res["feed_id"]:
            # 写进 upc_pool.sku 的是**这一轮真发出去的那个码**(预备期 mint 挂
            # 在 r["_sku"] 上)——列名叫 sku 就该存 sku。
            # upc_pool.asin 列(claim 时写)仍是 ASIN,不动:(store, asin) 是
            # 原号复用的契约键,动它等于每次重试白烧一个号
            upc_pool.mark_used(conn, [(u, r["_sku"]) for r, u in batch])
            product_events.record_many(conn, [
                {"sku": r["_sku"], "store": store_name,
                 "event": product_events.LIST_SUBMITTED, "source": "list_new",
                 # detail 显式带 asin:不透明码在 product_events.asin 列里提不
                 # 出来,而这里 ASIN 本来就在手边,是最省的一份补充证据
                 "detail": {"feed_id": res["feed_id"], "price": r["_price"],
                            "asin": r["asin"]}}
                for r, _ in batch])
            for r, u in batch:
                updates.append((r["rownum"], [
                    (r["_p"].get("title") or "")[:190],
                    r["_p"].get("price") or "",
                    r["_qty"],      # 实际提交的库存(0 也照写)
                    r["_price"], "Yes", res["feed_id"], today, "",
                    r["_sku"]]))
        elif res["outcome"] == "failed":
            upc_pool.release(conn, [u for _, u in batch], "rejected")
            for r, _ in batch:
                updates.append((r["rownum"], [
                    "", "", "", "", "No", "", "", "提交被拒", r["_sku"]]))
        else:   # unknown:UPC 不回收,K=Unknown 防重复提交
            for r, _ in batch:
                updates.append((r["rownum"], [
                    "", "", "", "", "Unknown", "", today,
                    "提交结局不确定,待对账", r["_sku"]]))



logger = logging.getLogger("workflows.list_new")

_SQL_INACTIVE = """
SELECT DISTINCT ON (store) store, store_status FROM ops.store_kpi_daily
ORDER BY store, data_date DESC
"""
_SQL_TODAY_LISTED = """
SELECT store, count(DISTINCT sku) FROM ops.feed_items
WHERE feed_type = 'MP_ITEM'
  AND submitted_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')
      AT TIME ZONE 'Asia/Shanghai'
GROUP BY store
"""
# 本店去重的数据面:(店铺, **身份键**) 对。只拦"同一家店重复上同一 ASIN",
# 跨店不互拦(2026-08-28 取消全局去重,见文件头 ④)。
# ① 第二列是**身份键**(coalesce(ls.source_key, w.sku)),不是 SKU 串:切码后
#    拿裸 SKU 去比闸就恒不命中 ⇒ 同店同 ASIN 反复上架,烧 UPC 烧 MP_ITEM 配额,
#    而且不报错。
# ② 本闸**只按 amz 身份键去重**(LEFT JOIN 带 source_type='amz'):match 行的
#    码寿命由 match_listing 自己的通道管。这是与 synthesis 规则 4 字面写法
#    (不带 source_type)的一处**有意偏差** —— 后果是「已弃码的 match 僵尸行
#    仍会挡新码」,对只处理 amz 行的 list_new 无实害(正向测试钉住)。
# ③ **必须 LEFT JOIN**:未登记的在架行也要拦,否则两次回填之间新出现的行会
#    静默漏闸。
# ④ **不加 lifecycle 条件**(别照抄 alloc_push 的排 RETIRED):RETIRED 行只要
#    码未弃就拦,退市档案不由 list_new 复活(2026-08-28 定稿;plan.md 的
#    7,342 行批量复活事故)。
# ⑤ `ls.abandoned_at IS NULL` 在批次 2 之前恒真(全库该列 NULL + LEFT JOIN),
#    提前落地是为了让写侧切换只改一处。
_SQL_LISTED_ASINS = """
SELECT DISTINCT w.store, coalesce(ls.source_key, w.sku)
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
WHERE w.missing_since IS NULL AND ls.abandoned_at IS NULL
"""
_SQL_UNEXPLAINED = """
SELECT asin FROM catalog.product_risk WHERE unexplained_missing
"""
# 审核结论与 PT 的**权威在 PG**(所有者定稿 2026-08-16:「上架链应该以数据库
# 的数据为准,因为我把审核接进来了,要上架就肯定要过审核,读取速度也更快」)。
# 上架表 E/D 两列自 2026-08-16 起是这两个字段的**投影**
# (product_audit -p from_sheet=1 回填),给人看的;闸门读库,不读投影。
_SQL_VERDICT = """
SELECT asin, audit_status, walmart_pt
FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
"""

# 退役冷却的数据面:最近 RETIRE_COOLDOWN_HOURS 小时内**退役回执成功**过的
# (店, ASIN)。
# ① 键是 (店, **ASIN**) 不是 (店, SKU):退役发生在旧码上、重上用的可能是新码,
#    按 SKU 建键会在换码那一刻静默失效(闸还在,永不命中)。
# ② **LEFT JOIN 且带 source_type='amz'**:未登记的存量行也要能算出身份键
#    (coalesce 回落 e.sku,存量 sku=asin 时结果相同);不带 source_type 的话
#    跟卖行的 source_key 是 GTIN,冷却键按 GTIN 建、与闸判用的 r["asin"] 永远
#    对不上 ⇒ 跟卖品的冷却恒不生效且不报错。身份表达式与 _SQL_LISTED_ASINS /
#    _SQL_ATTEMPTS 逐字同款。
# ③ 事件码走 product_events 常量,**不写字面量**:回执码是 {kind}_feed_{status}
#    派生的,_FEED_KIND 一改取值,写字面量的这条 SQL 会静默返回空集。
_SQL_RETIRE_COOLDOWN = """
SELECT e.store, coalesce(ls.source_key, e.sku) AS asin, max(e.occurred_at)
FROM catalog.product_events e
LEFT JOIN catalog.listing_sources ls
  ON ls.store = e.store AND ls.sku = e.sku AND ls.source_type = 'amz'
WHERE e.event = %(event)s AND e.store IS NOT NULL
  AND e.occurred_at >= now() - make_interval(hours => %(hours)s)
GROUP BY 1, 2
"""
# 代际上限的数据面:同 (店, 来源, 源头键) 已弃码行数达 MAX_SKU_GENERATIONS 的品。
# 数的是**已弃码行数**(一个产品换过几代码),所以按 source_key 分组而不是 sku
# —— 按 sku 分组每行恒 1,闸永不命中。
_SQL_ABANDONED_GEN = """
SELECT store, source_key, count(*)
FROM catalog.listing_sources
WHERE abandoned_at IS NOT NULL AND source_type = %(source_type)s
  AND source_key IS NOT NULL
GROUP BY 1, 2
HAVING count(*) >= %(cap)s
"""


def load_verdicts(asins: list[str]) -> dict[str, tuple]:
    """输入:ASIN 列表 → 输出:{asin: (audit_status, walmart_pt)}。库里没有的不在字典里。"""
    if not asins:
        return {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_VERDICT, (sorted(set(asins)),))
        return {a: (st, pt) for a, st, pt in cur.fetchall()}


AUDIT_OK = "approved"       # catalog.products.audit_status 的过审值


def _with_pt(row: dict, verdicts: dict) -> dict:
    """输入:上架表一行 + 审核字典 → 输出:类目以库为准的同一行。

    「以数据库的数据为准」是同一条口径的两半:结论读库,**类目也读库**。
    只读结论不读类目的话,表 D 列被手改成另一个 PT,上架会按手改的那个走
    ——而审核是按库里那个 PT 过的,等于绕过审核换了类目。
    库里没有 PT(老数据/未审)才退回表里的值。
    """
    pt = (verdicts.get(row["asin"]) or (None, None))[1]
    return {**row, "product_type": pt} if pt else row


class _GateState(NamedTuple):
    """闸门链的库侧快照。**字段名即闸门名**:原来是无名 8 元组,按位置解包,
    中间插一个字段就会让后面全部错位 —— 而错位不报错(集合/字典长得都一样)。"""
    inactive: set               # ops.store_kpi_daily 里非 ACTIVE 的店名
    today_used: dict            # 店 → 今日已提交 MP_ITEM 条数(北京日界)
    listed_pairs: set           # 在架 (店铺, **身份键**) 对(本店去重,2026-08-28 起)
    banned: dict                # ASIN → (拉黑类别, 说明)
    unexplained: set            # 有"不明原因消失"史的 ASIN(只报警不拦)
    gate: dict                  # risk_gate 否决表(禁售 PT / 黑名单品牌)
    owned_asin: dict            # ASIN → 持有店(占用台账 A1)
    owned_brand: dict           # 品牌键 → 持有店(占用台账 A1)
    # ⚠ 下面两个是**追加在末尾**的(批次 2):本类按位置构造,往中间插字段会
    # 让后面全部错位,而错位不报错(集合与字典长得都一样,见类头注)
    cooling: dict               # (店, ASIN) → 最近一次退役回执成功时刻
    over_gen: set               # (店, ASIN) 已弃码代数达 MAX_SKU_GENERATIONS


def _load_gate_state() -> _GateState:
    """输入:无(读 PG)→ 输出:`_GateState`,闸门链要的十份库侧快照。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_INACTIVE)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
        cur.execute(_SQL_TODAY_LISTED)
        today_used = {s: int(n) for s, n in cur.fetchall()}
        cur.execute(_SQL_LISTED_ASINS)
        # 全部店都进(含规划外店):去重改成**本店**语义(2026-08-28 取消全局
        # 去重)后,这个集合只回答"这家店自己有没有这个 ASIN"——自己拦自己
        # 防重复上架,与 2026-08-15「规划外店既不占用、也不拦别人」不冲突
        # (那条定稿针对的是跨店互拦,现在跨店根本不拦了)。
        # 第二列是**身份键**(见 _SQL_LISTED_ASINS 头注),闸判那头拿的是
        # r["asin"],两边同一个口径。
        listed_pairs = {(store, key) for store, key in cur.fetchall()}
        cur.execute(_SQL_UNEXPLAINED)
        unexplained = {r[0] for r in cur.fetchall()}
        banned = blacklist.load_banned_asins(conn)
        gate = risk_gate.load_gate(conn)
        # 占用台账(A1):台账为空时两个 dict 都是空的,闸门恒放行——
        # 回填前后行为一致,不会因为"还没回填"而误拦。
        # 规划外店(谭总系)**双向豁免**(所有者定稿 2026-08-15,2026-08-19
        # 生产实证补全:此前只豁免了快照闸一个方向,占用/品牌闸仍在拦):
        # 持有人是规划外店的占用行剔除(它们不拦别人);行侧闸门另按
        # 上架店豁免(它们上架也不被别人拦、不做品牌归属)
        owned_asin = {a: s for a, s in
                      claims.load_active(conn, claims.PRODUCT).items()
                      if not alloc_survey.is_excluded(s)}
        owned_brand = {b: s for b, s in
                       claims.load_active(conn, claims.BRAND).items()
                       if not alloc_survey.is_excluded(s)}
        # 两道码闸的数据面(批次 2)与上面八份**同一次读完**:逐行查库会在几百
        # 行的轮次里打出几百条 SQL,而 _load_gate_state 是闸门链唯一的库侧取数点
        cur.execute(_SQL_RETIRE_COOLDOWN,
                    {"event": product_events.RETIRE_FEED_SUCCESS,
                     "hours": sku_codec.RETIRE_COOLDOWN_HOURS})
        cooling = {(store, key): at for store, key, at in cur.fetchall()}
        cur.execute(_SQL_ABANDONED_GEN,
                    {"source_type": listing_sources.SOURCE_AMZ,
                     "cap": sku_codec.MAX_SKU_GENERATIONS})
        over_gen = {(store, key) for store, key, _n in cur.fetchall()}
    return _GateState(inactive, today_used, listed_pairs, banned, unexplained,
                      gate, owned_asin, owned_brand, cooling, over_gen)


def _load_quota(default: int = 999) -> dict[str, int]:
    """限额表「上架限制」;未登记/读不到按旧语义默认 999(等于不限)。

    ⚠ **同一张限额表,本链与分配链的降级方向相反** —— 这是有意的,但必须说破:
      · 这里读不到 ⇒ 默认不限,**照常上架**(旧语义,不改:改成硬拒会在飞书抖动
        时停掉生产上架线);
      · 分配链(`alloc_plan`/`alloc_stores`/`alloc_backfill`/`claim_audit`)读不到
        ⇒ **硬拒**(没有类目/渠道/容量就没法分配);
      · `alloc_audit` 是第三种:记 `cfg_err` 后降级继续,报告末尾点破。
    最坏组合是**上架侧放开、分配侧关停**:货照上,却没人在决定该上什么。
    所以这条降级**必须留痕** —— 原来是静默 `return {}`,运行摘要上完全看不出
    今天的上架是"按限额跑的"还是"限额没读到、全店不限"。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.max_daily_list])
    except LookupError:
        logger.warning(
            "限额表未登记/读不到 —— 本轮**所有店按不限量上架**(旧语义 default=%d)。"
            "⚠ 同一张表分配链是硬拒:此刻上架侧放开、分配侧很可能已经停摆,"
            "两边同时发生时先去看限额表的 app_token/table_id 有没有配", default)
        return {}
    out: dict[str, int] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        try:
            v = int(float(feishu._plain_text(
                rec["fields"].get(f.max_daily_list)) or 0))
        except ValueError:
            v = 0
        if name and v > 0:
            out[name] = v
    return out


def _dump_llm_debug(asin: str, visible: dict, orderable: dict,
                    missing: list, notes: list) -> None:
    """必填缺失的载荷落盘(旧 llm_raw_*.json 语义,2026-08-12 接线):
    排障直接看文件,不必重跑 LLM;失败只告警不影响主链。"""
    try:
        import json
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(kpi.CN_TZ).strftime("%Y%m%d_%H%M%S")
        f = d / f"llm_raw_{asin}_{ts}.json"
        f.write_text(json.dumps(
            {"asin": asin, "missing": missing, "notes": notes,
             "visible": visible, "orderable": orderable},
            ensure_ascii=False, indent=2))
        logger.info("必填缺失载荷已落盘:%s", f)
    except Exception as e:
        logger.warning("载荷落盘失败(不影响主链): %s", e)


def _push_scrape(want: list[str], execute: bool
                 ) -> tuple[str | None, list[str]]:
    """输入:本轮要刷新的 ASIN 列表 + 是否真跑 → 输出:(摘要行, 可等待的批次名)。

    2026-08-19 所有者定稿「上架必须用最新数据」之后,这里推的是**全部候选**
    的刷新(此前只推缺数据的)。批次名带北京日界,天然防重——当天第二轮
    撞名(BatchExistsError)沿用既有批次:那轮的新增候选刷不到,用库中现值
    上架,次日随新批次刷。最快形态(不切邮编不截图);按批摄取后主链续走。
    推送失败只告警不阻塞上架:有数据的行用现值,缺数据的照旧跳过不写终态。
    """
    if not want:
        return None, []
    day = datetime.now(kpi.CN_TZ).strftime("%Y%m%d")
    name = f"listing_gap_{day}"
    if not execute:
        return (f"  [DRY-RUN] 候选 {len(want)} 个 ASIN,"
                f"真跑时将推采集批次 {name} 刷新"), []
    # 2026-08-18 所有者定稿同轮闭环之后,这批采集**本侧在等**(下游 20 分钟
    # 窗口),所以:①落 ops.scrape_batches 台账(check_open/监控能圈到它);
    # ②插队(与 audit_gap 同一条时间账:不插队几乎注定等不到)。此前
    # "list_new 补采不插队"的口径随"本轮跳过"语义一并作废。
    try:
        r = scraper.submit_batch(name, want)
        bid = r.get("batch_id")
        scrape_batches.record(name, bid, len(want), "pushed",
                              f"list_new 同轮闭环 inserted={r.get('inserted')}")
        note = (f"  候选 {len(want)} 个 ASIN 已推采集刷新"
                f"(批次 {name},入库 {r.get('inserted')})"
                + ("" if scrape_batches.prioritize(name, bid)
                   else ",⚠ 插队没成功(按常规优先级采,可能等不到)"))
        return note, [name]
    except scraper.BatchExistsError as e:
        scrape_batches.record(name, e.batch_id, len(want), "pushed",
                              "同日已推,沿用既有批次")
        scrape_batches.prioritize(name, e.batch_id)
        return (f"  候选 {len(want)} 个 ASIN:今日批次 {name} 已推过,"
                f"沿用既有批次接着等(本轮新增候选刷不到,用库中现值)"), [name]
    except Exception as e:
        logger.warning("推采集失败(不阻塞上架,有数据的行用现值): %s", e)
        scrape_batches.record(name, None, len(want), "failed", str(e)[:200])
        return f"  ⚠ 候选 {len(want)} 个 ASIN 推采集刷新失败:{e}", []


def _ingest_batches(names: list[str]) -> str:
    """输入:本轮推的批次名 → 输出:按批摄取摘要。

    2026-08-19 起走采集侧批次端点(`export_batch_records`,契约 §4.11):
    只拉**自己这批**的事件,批内游标每次从 0 拉到底,不碰全局游标、
    **不需要 product_ingest 的锁**——此前是借锁抽全库到当刻头部,13:00 的
    product_chain 与整点 order_chain 一撞就是一场 15 分钟等锁。幂等靠
    snapshots.source_id,与全局泵(product_chain 每天的 product_ingest)
    重复摄取无害。没拉到底的批次摘要里点名,这批照旧不写终态、次日续上。
    """
    _, note = product_ingest.pump_batches(scraper, db, names)
    return note


_STATS_LOCK = threading.Lock()


def _bump(stats: dict | None, key: str) -> None:
    """预备期并发下的取数计数(dict 的 += 非原子,统一走这把锁)。"""
    if stats is not None:
        with _STATS_LOCK:
            stats[key] = stats.get(key, 0) + 1


def _map_llm(conn, pt: str, spec, product: dict,
             stats: dict | None = None) -> tuple[dict, dict]:
    """LLM 映射(缓存优先)→ (清洗后 Visible, LLM 填的 Orderable 字段)。

    2026-08-12 旧仓对照恢复两段式:Orderable 的非系统字段(条件必填等)
    交还 LLM 填,系统强制项在 build_orderable 里覆盖;Visible 照旧过
    finalize_visible 硬约束清洗。

    取数三级(2026-08-18 所有者定稿加二级):
      ① input_hash 精确命中(catalog.llm_cache);
      ② miss 时按 (asin, pt) 反查最近出参:reuse_sig 相等(spec 字段面/
        brand/category/变体属性都没变)且新旧标题过规格 token 验证
        (mp_mapper.title_spec_compatible)才复用——文案图片本就由系统
        每轮从最新采集数据覆盖、不靠 LLM,复用只赌"结构化字段没变"。
        命中回写新 hash,下轮直接走 ①;
      ③ 都不中才真打 LLM。
    stats 计四类(cache/reuse/reuse_miss/llm),预备期摘要必须亮出来——
    二级是兜底式优化,静默常态化 = 它在替 LLM 说话而没人知道。
    """
    messages = mp_mapper.build_llm_messages(pt, spec, product,
                                            ospec=pt_spec.orderable_spec())
    key = llm_cache.cache_key(messages, 0.2, 4096)
    raw = llm_cache.get(conn, key)
    meta = {"asin": product.get("asin"), "pt": pt,
            "src_title": product.get("title"),
            "reuse_sig": mp_mapper.reuse_sig(pt, spec, product,
                                             ospec=pt_spec.orderable_spec())}
    if raw is not None:
        _bump(stats, "cache")
    else:
        got = (llm_cache.find_reusable(conn, meta["asin"], pt,
                                       meta["reuse_sig"])
               if meta["asin"] else None)
        if got and mp_mapper.title_spec_compatible(
                got[1], product.get("title") or "", got[0]):
            raw = got[0]
            _bump(stats, "reuse")
            logger.info("%s 二级复用旧出参(硬条件同 + 标题规格验证通过,"
                        "零 LLM)", meta["asin"])
        else:
            if got:
                _bump(stats, "reuse_miss")
                logger.info("%s 有旧出参但标题规格验证不过(规格疑似变了),"
                            "重打 LLM", meta["asin"])
            raw = llm.chat_json(messages, purpose="listing_attrs")
            _bump(stats, "llm")
        llm_cache.put(conn, key, raw, **meta)
    raw_v, raw_o = mp_mapper.split_llm_output(raw)
    visible = mp_mapper.finalize_visible(pt, raw_v, spec,
                                         images=product.get("images"),
                                         product=product)
    return visible, raw_o


# 预备期占位 UPC(_spec_precheck 的生产验证方案):出参与 spec 一致化都不依赖
# 真号 —— UPC 只进 orderable 的 productIdentifiers,通过后领到真号原位回填。
_UPC_PLACEHOLDER = "000000000000"


def _prep_rows(ready: list[dict], partners: dict[str, str], workers: int
               ) -> tuple[list[dict], list[tuple[int, str]], dict]:
    """输入:待提交行 + {店: 上架仓 FC ID} + 并发数 → 输出:(备好行, 理由, 计数)。

    预备期(所有者定稿 2026-08-18 新流程):LLM 出参 + spec 一致化**前置到
    领号之前**、全行跨店并发 —— 缓存优先,miss 才打 DeepSeek,高并发把墙钟
    大头(LLM)压下来;UPC 用占位号,**过了一致化的行才有资格领号**,预备
    失败不再走"领了再 release":池紧张时那会把号先占给注定失败的行,而且
    release 回收让水位来回抖、摘要里 no_upc 忽多忽少没法对拍。

    worker 各领一条 autocommit 连接(product_audit 判定并发同款连接池);
    并发数由调用方先过 db_guard.cap_workers 钳制。输出按 rownum 定序 ——
    完成序是随机的,不定序的话同一批重跑,摘要行序与随后的领号顺序都会漂。

    备好行挂 `_visible` / `_orderable`(占位号在里面),提交期领到真号后
    只回填 productIdentifiers,不重算。

    **抽码也在这里**(批次 2):每行一次 `sku_codec.mint`,结果挂 `r["_sku"]`,
    载荷里的 SKU 从此是它。三条硬理由,一条都不许绕:
      ① **绝不许挪进 `_one_store`**:店级失败时 `store_retry.serial_second_pass`
         重跑的就是 `_one_store`,抽码若在里面,补试会抽出新码 ⇒ 载荷不再一字
         不差 ⇒ `api/feeds.payload_key` 的指纹变了 ⇒ 在途防重不命中 ⇒ 首轮已
         发出去的那片被真的再发一次 = **双上架,而且全程不报错**。
      ② **单事务顺序做一遍**,不放进 128 路 worker:worker 用的是 autocommit
         连接,并发抢同一个 (店, 来源, 源头键) 的部分唯一索引会制造大量唯一
         冲突重试;顺序几百次单行 INSERT,相对 LLM 那段墙钟可以忽略。
      ③ **排在任何外部调用之前**(防重状态先落库再调接口):这段随 with 退出
         就 commit,进程半路死掉重跑拿到的是同一个码。
    dry-run 走不到这里(`run()` 的 `if not execute:` 早已 return),所以本函数
    里没有、也不许有 dry-run 分支;空跑要看码走 `sku_codec.DRYRUN_PLACEHOLDER`。
    **`r["_sku"]` 不许有任何 `or r["asin"]` 兜底** —— 那正是"静默把 ASIN 当
    SKU 发出去"的制造机;缺了就该 KeyError 炸在测试里。
    """
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack

    llm_stats: dict = {}     # cache/reuse/reuse_miss/llm 四类取数计数

    # 抽码 + 登记(同一事务,随 with 退出 commit),然后才开并发做 LLM
    with db.pg_conn() as conn:
        for r in sorted(ready, key=lambda x: x["rownum"]):
            r["_sku"] = sku_codec.mint(conn, r["store"],
                                       listing_sources.SOURCE_AMZ, r["asin"],
                                       workflow="list_new")
    n_codes = len({r["_sku"] for r in ready})
    if n_codes < len(ready):
        # 同 (店, ASIN) 贴重了两行 ⇒ mint 复用同一个码。两个随机码相同不像两个
        # ASIN 相同那样扎眼,不数出来没人看得见
        logger.info("本轮 %d 行只用了 %d 个码:同 (店,ASIN) 在上架表贴重了",
                    len(ready), n_codes)

    def _one(conn, r: dict) -> tuple:
        spec = pt_spec.load_pt(r["product_type"])
        visible, llm_o = _map_llm(conn, r["product_type"], spec, r["_p"],
                                  stats=llm_stats)
        if len(visible.get("productName") or "") < 10:
            return ("title_short", r, "标题不足10字符", None)
        # 两处都用 r["_sku"]:第一参进 Orderable.sku(发给沃尔玛的 SKU),
        # conform 的 sku= 会被 mp_conform 当作单品占位 variantGroupId 写进
        # Visible —— 只改一处会出现「Orderable.sku 是新码、variantGroupId 还是
        # ASIN」的半身像,而 variantGroupId 也是发出去的,等于把 ASIN 从后门递
        # 出去。⚠ 这只修好**单品**口径:变体品的 variantGroupId 仍由
        # services/variant_group 从 parent ASIN 派生(sku_plan §8 待决项)。
        # 下面 logger / _dump_llm_debug 仍打 r["asin"]:那是给人看的定位键
        orderable = mp_mapper.build_orderable(
            r["_sku"], _UPC_PLACEHOLDER, r["_price"], r["_qty"],
            partners[r["store"]], pt=r["product_type"], product=r["_p"],
            llm_fields=llm_o)
        visible, orderable, notes, missing = mp_conform.conform(
            spec, pt_spec.orderable_spec(), visible, orderable,
            sku=r["_sku"], variant=r.get("_vplan"))
        if notes:
            logger.info("%s spec 一致化 %d 处:%s", r["asin"],
                        len(notes), "; ".join(notes[:6]))
        if missing:
            _dump_llm_debug(r["asin"], visible, orderable, missing, notes)
            return ("invalid", r, f"必填缺失:{','.join(missing[:6])}", None)
        return ("ok", r, None, (visible, orderable))

    with ExitStack() as stack:
        conns: _queue.SimpleQueue = _queue.SimpleQueue()
        for _ in range(workers):
            conns.put(stack.enter_context(db.pg_conn(autocommit=True)))

        def _judge(r: dict) -> tuple:
            c = conns.get()
            try:
                return _one(c, r)
            except Exception as e:                              # noqa: BLE001
                # 单行失败不许拖垮整批:该行不提交、理由写表,下轮重试
                logger.warning("%s 预备期失败(该行本轮不提交): %s",
                               r["asin"], e)
                return ("llm_failed", r, f"出参失败:{e}", None)
            finally:
                conns.put(c)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_judge, ready))

    cnt = {"invalid": 0, "title_short": 0, "llm_failed": 0}
    # 必填缺失**按店**再留一份(店铺事件账本要每店一条):全局那个数回答
    # "这一轮坏了多少行",答不了"是哪家店的货一直组不出合规载荷"
    by_store: dict[str, int] = {}
    ok: list[dict] = []
    reasons: list[tuple[int, str]] = []
    for kind, r, why, payload in results:
        if kind == "ok":
            v, o = payload
            ok.append({**r, "_visible": v, "_orderable": o})
        else:
            cnt[kind] += 1
            if kind == "invalid":
                by_store[r["store"]] = by_store.get(r["store"], 0) + 1
            reasons.append((r["rownum"], why))
    ok.sort(key=lambda x: x["rownum"])
    reasons.sort(key=lambda t: t[0])
    cnt["llm_stats"] = llm_stats
    cnt["invalid_by_store"] = by_store
    return ok, reasons, cnt


MAX_LIST_ATTEMPTS = 3       # 同 (店铺,身份键) 自动重上次数上限(旧 retry_state 阈值淘汰)

# psycopg3 不支持 `(a,b) IN %s` 传元组序列(psycopg2 老写法),用 unnest 配对。
# 计数键是 (店铺, **身份键**):切码后按裸 SKU 数每次新码 count 恒 0 ⇒ FAILED
# 无限重试(烧 UPC、烧 MP_ITEM 配额,不报错)。
# **代际口径**(LATERAL 那段):
#   · 无弃码事件 ⇒ g.since IS NULL ⇒ 谓词恒真 ⇒ 退化成今天的**跨码累计**;
#   · 有弃码事件 ⇒ 只数最近一次弃码之后的提交(换了码就重新给三次)。
# 认弃码事件读的是 abandon 自己写进 detail 的 source_key,**不是
# product_events.asin 列**(那一列要到批次 0b 才经登记簿反查,在「0a 已合、
# 0b 未合」的窗口里恒为 NULL ⇒ 代际过滤永不命中)。
# 代际**上限**(同 (store, source_type, source_key) 弃码行数 ≥ 阈值即拦)属批次 2。
# ⚠ 参数全部具名:psycopg3 不许位置占位符与具名占位符混用。
_SQL_ATTEMPTS = """
SELECT t.store, t.asin, count(*)
FROM ops.feed_items f
LEFT JOIN catalog.listing_sources ls
  ON ls.store = f.store AND ls.sku = f.sku AND ls.source_type = 'amz'
JOIN unnest(%(stores)s::text[], %(asins)s::text[]) AS t(store, asin)
  ON f.store = t.store AND coalesce(ls.source_key, f.sku) = t.asin
LEFT JOIN LATERAL (
    SELECT max(occurred_at) AS since
    FROM catalog.product_events e
    WHERE e.store = t.store
      AND e.event = %(abandoned)s
      AND e.detail ->> 'source_key' = t.asin
) g ON true
WHERE f.feed_type = 'MP_ITEM'
  AND (g.since IS NULL OR f.submitted_at > g.since)
GROUP BY t.store, t.asin
"""


def _retry_rows(rows: list[dict], verdicts: dict
                ) -> tuple[list[dict], list[tuple[str, str]]]:
    """输入:上架表全部行 → 输出:(可重试行, 已达上限行)。

    O=FAILED 的行要**重新排队**:失败原因多半是可修的(UPC 撞库领新号即可、
    字段问题改完 mapper 即可),旧系统靠 main 看 N=DATA_ERROR 接回重试。
    但不能无限重试——按 ops.feed_items 里同 (店铺,身份键) 的 MP_ITEM 提交次数
    卡 MAX_LIST_ATTEMPTS(旧 retry_state 永久淘汰名单的等价物);换过码的品
    只数最近一次弃码之后的提交(代际口径见 _SQL_ATTEMPTS 头注)。

    ⚠ SKU_LOCKED 不进本通道:不先 RETIRE 换 UPC 重发也会失败(旧实证),
    走 sku_locked_heal 自愈链;ASYNC_PENDING 不是失败。
    """
    cand = [r for r in rows
            if (verdicts.get(r["asin"]) or (None,))[0] == AUDIT_OK
            and r["list_result"] == "FAILED"]
    if not cand:
        return [], []
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_ATTEMPTS,
                    {"stores": [r["store"] for r in cand],
                     "asins": [r["asin"] for r in cand],
                     "abandoned": product_events.SKU_ABANDONED})
        tried = {(s, k): int(n) for s, k, n in cur.fetchall()}
    retry, exhausted = [], []
    for r in cand:
        if tried.get((r["store"], r["asin"]), 0) >= MAX_LIST_ATTEMPTS:
            exhausted.append((r["store"], r["asin"]))
            continue
        # 重新排队:清掉上一轮的 feedid/结果,让主链当新行处理
        retry.append({**_with_pt(r, verdicts),
                      "feed_id": "", "listed": "", "list_result": ""})
    return retry, exhausted


# 同族已在架成员(按**身份键**匹配,传进来的一直是同族 ASIN)。
# ⚠ 本条**有意不加 abandoned_at 谓词**:变体同族查的是「这家店此刻还挂着哪些
# 同族成员」这个在架事实,与码是否已弃用无关。abandoned_at 只出现在 mint 的
# 复用查询、list_new 去重闸、alloc_push._SQL_ONLINE 三处(消费方契约,
# conventions §九)。
_FAMILY_LISTED_SQL = """
SELECT w.sku, w.variant_group_id,
       coalesce(w.variant_group_info->>'isPrimary', '') AS is_primary
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
WHERE w.store = %(store)s AND w.missing_since IS NULL
  AND coalesce(ls.source_key, w.sku) = ANY(%(asins)s::text[])
"""


def _variant_plan(conn, store: str, r: dict, spec) -> dict:
    """输入:连接 + 店铺 + 待上架行 + PT spec → 输出:variant_group.plan() 决策。

    ③ 所有者定稿:同族已有成员在架 ⇒ 新成员沿用它的 variantGroupId。
    ② 分配侧保证「一组变体只分配一个店」⇒ **只查本店**,不做跨店重定向。

    ⚠ 查库失败不许把整行拖下水:变体只是锦上添花,拿不到在架信息就按"本店还没有
    同族"处理 —— 派生 ID 照样能让以后的兄弟归到一起(group_id 由 parent_asin
    决定,不依赖这次查询)。
    """
    p = r.get("_p") or {}
    fam = variant_group.parse_family(p.get("variation_asins"), r["asin"])
    gid, has_primary = "", False
    if store and len(fam) > 1:
        try:
            with conn.cursor() as cur:
                cur.execute(_FAMILY_LISTED_SQL,
                            {"store": store, "asins": [a for a in fam
                                                       if a != r["asin"]]})
                for _sku, g, prim in cur.fetchall():
                    gid = gid or (str(g) if g else "")
                    has_primary = has_primary or str(prim).lower() in ("yes", "true")
        except Exception as e:      # noqa: BLE001
            logger.warning("%s 查同族在架信息失败(按本店无同族处理): %s",
                           r["asin"], e)
    props = (spec or {}).get("properties") or {}
    enum = mp_conform.variant_attr_enum(props)
    return variant_group.plan(
        r["asin"], p.get("variant_attributes"), p.get("variation_asins"),
        p.get("parent_asin"), enum,
        existing_group_id=gid, family_has_primary=has_primary)


def _plan_variants(ready: list[dict], n_var: dict) -> None:
    """输入:待提交行 + 计数字典 → 输出:无(把决策挂到 r["_vplan"],顺便计数)。

    连接**按需惰性打开**:只有真需要查"本店同族在架成员"的行才要连接
    (`_variant_plan` 在家族只有自己时压根不查)。一行都不需要就一次连接也不开
    —— 闸门段本来不持有连接,为了变体硬开一个会让"没有同族"的场景也依赖库。

    ⚠ **惰性开连接必须用 ExitStack,不能 `db.pg_conn().__enter__()`**
    (2026-08-17 事故):`registry.db.pg_conn` 是 `@contextmanager`,
    `db.pg_conn()` 造出来的临时 CM 对象在那一行之后**立刻被回收**,生成器被
    `close()`,`finally: conn.close()` 当场执行 —— 拿到手的连接已经是关闭的
    (实测 `conn.closed is True`)。后果:`_variant_plan` 里那条 SELECT 每行抛
    "connection is closed",被它自己的 except 吞成一行 warning,于是
    **所有者定稿③「同族已在架成员 ⇒ 新成员沿用它的 variantGroupId」与
    `family_has_primary` 在生产里从未真正生效过**,全程不报错。
    """
    with contextlib.ExitStack() as stack:
        conn = None
        for r in ready:
            fam = variant_group.parse_family(
                (r.get("_p") or {}).get("variation_asins"), r["asin"])
            if conn is None and r.get("store") and len(fam) > 1:
                conn = stack.enter_context(db.pg_conn())
            try:
                r["_vplan"] = _variant_plan(
                    conn, r["store"], r, pt_spec.load_pt(r["product_type"]))
            except Exception as e:                              # noqa: BLE001
                # 变体是锦上添花,算不出不许拖垮整行(同 _variant_plan 内的纪律)
                logger.warning("%s 变体决策失败(按单品处理): %s", r["asin"], e)
                r["_vplan"] = None
    _drop_degenerate_dims(ready)
    _remap_unmapped_dims(ready)
    _dedupe_primary(ready)
    for r in ready:
        vp = r.get("_vplan")
        if not vp:
            continue
        n_var[vp["code"]] += 1
        if len(vp.get("attr_pairs") or ()) > 1:
            # 多维发出的行数:这一栏为 0 而库里明明有 color+size 的家族,
            # 就是多维那条链没生效(2026-08-17 补齐后的验收点)
            n_var["其中多维"] += 1
        if vp.get("unmapped_dims"):
            # 映不上的维度只剔它、不退单品 —— 但组内差异若恰好只在被剔的
            # 那个维度上,发出去就是几条看不出区别的变体
            n_var["有维度映不上"] += 1


def _drop_degenerate_dims(ready: list[dict]) -> None:
    """输入:带 _vplan 的待提交行 → 输出:无(就地剔掉组内**取值完全相同**的维度)。

    ⚠ **声明了一个维度却在组内没有差异值,等于告诉沃尔玛"这几个按尺寸不同"
    然后给出三个一样的尺寸。** 2026-08-17 生产实见(所有者的礼品袋组):
    亚马逊给的 `size_name` 三个成员**全是** `1 Count (Pack of 100)` —— 那不是
    尺寸,是包装数量,而且对分组毫无信息量。真正区分它们的是标题里的
    Small/Medium/Large,亚马逊自己没把它放进 twister 维度。

    判据只用**本轮看得见的成员**:同 (店铺, 组 ID) 至少 2 条时,某维度的取值集合
    只有 1 个 ⇒ 剔掉它。只看见 1 条时**什么都不做** —— 一条数据判不出"组内有没有
    差异",按"可能有"处理(所有者的第一次验收就是只放了 2 个成员进表)。

    剔到一个维度都不剩 ⇒ 这几条压根不该是一个变体组(沃尔玛看不出区别),
    整组退回单品口径并计一笔 `no_diff_dim`。宁可各自独立上架,也不要发一个
    成员之间毫无区别的变体组 —— 后者要用 MP_MAINTENANCE 才能改回来。
    """
    by_group: dict[tuple, list[dict]] = {}
    for r in ready:
        vp = r.get("_vplan")
        if vp and vp.get("mode") == "variant" and vp.get("attr_pairs"):
            by_group.setdefault((r.get("store"), vp.get("group_id")),
                                []).append(r)
    for (store, gid), rows in sorted(by_group.items(),
                                     key=lambda kv: (str(kv[0][0]),
                                                     str(kv[0][1]))):
        if len(rows) < 2:
            continue                     # 一条判不出组内差异,不动
        names = [n for n, _ in rows[0]["_vplan"]["attr_pairs"]]
        for name in names:
            vals = {str(dict(r["_vplan"]["attr_pairs"]).get(name))
                    for r in rows if name in dict(r["_vplan"]["attr_pairs"])}
            if len(vals) > 1:
                continue
            logger.warning("变体组 %s(%s)的维度 %s 在本轮 %d 个成员上取值全同"
                           "(%s),剔掉 —— 声明了没有差异的维度等于没声明",
                           gid, store, name, len(rows), vals)
            for r in rows:
                vp = r["_vplan"]
                r["_vplan"] = {**vp, "attr_pairs": [
                    (n, v) for n, v in vp["attr_pairs"] if n != name],
                    "degenerate_dims": sorted(
                        set(vp.get("degenerate_dims") or ()) | {name})}
        for r in rows:
            vp = r["_vplan"]
            if vp["attr_pairs"]:
                continue
            # 所有维度都没差异 ⇒ 这不是一个变体组
            r["_vplan"] = {**vp, "mode": "single", "code": "no_diff_dim",
                           "reason": f"组内 {len(rows)} 个成员在 "
                                     f"{','.join(vp.get('degenerate_dims') or ())} "
                                     f"上取值全同,没有可分变体的差异维度"}


def _remap_unmapped_dims(ready: list[dict]) -> None:
    """输入:带 _vplan 的待提交行 → 输出:无(给"映不上"的维度找归宿,就地补进 attr_pairs)。

    旧仓 Phase 0.8 的等价物。**三层的路由在这里,不在 services**(2026-08-27
    定案):第①层"哪些维度还没归宿"由本函数判(`unmapped_dims` +
    `if not enum: continue` + 全同/单成员两道过滤),`services.variant_remap`
    只提供第②③层的 `hardcoded` / `llm_remap` 两个显式函数,由本函数显式 if
    分流(conventions §六「能力不同的两个端点由调用方显式路由」)。
    要解决的是**名字对不上而语义有归宿**那一类:
    旧仓原始案例是文具类目 `color_name=48 Color` 其实是件数,该映 `pieceCount`。

    与旧仓的差异(逐条见 services/variant_remap 头注)里,**接线侧要守的两条**:

    · **只补映不上的,不动已映上的** —— 旧仓 remap 一触发整组改用一个 key,
      会把已经映上的 color 一起丢掉;
    · **本轮取值全同的维度不问 LLM** —— 生产实见(礼品袋组三个成员的
      `size_name` 全是 `1 Count (Pack of 100)`):送去问只会得到一个"看着对"的
      key,然后三个成员带着同一个值发出去,声明了没有差异的维度等于没声明。
      这一层在这里过滤,`variant_remap` 只接"值确有差异"的组。
    · 本轮**只看见 1 个成员**时判不了有没有差异,所以只允许走**内置表**
      (确定性、零成本、人工策展),不问 LLM。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in ready:
        vp = r.get("_vplan")
        # ⚠ **`no_dim` 那批也要进来**(2026-08-17 修):`code='no_dim'` 是
        # "一个维度都没映上"⇒ `mode='single'`。而错位重映射存在的**唯一理由**
        # 正是这一类(旧仓原始案例:Art Sets 的 color_name=48 Color 其实是件数,
        # 该映 pieceCount)。首版只收 `mode=='variant'` 的组,等于刚补迁的
        # variant_remap 对它自己的主场景一次都不会被调用。
        if not vp or not vp.get("unmapped_dims"):
            continue
        if vp.get("mode") == "variant" or vp.get("code") == "no_dim":
            groups.setdefault((r.get("store"), vp.get("group_id")),
                              []).append(r)
    if not groups:
        return
    with contextlib.ExitStack() as stack:
        conn = None
        for (store, gid), rows in sorted(groups.items(),
                                         key=lambda kv: (str(kv[0][0]),
                                                         str(kv[0][1]))):
            pt = rows[0].get("product_type") or ""
            spec = pt_spec.load_pt(pt) or {}
            enum = mp_conform.variant_attr_enum(spec.get("properties") or {})
            if not enum:
                continue
            used = {n for n, _ in (rows[0]["_vplan"].get("attr_pairs") or ())}
            raw = {r["asin"]: variant_group.parse_attrs(
                (r.get("_p") or {}).get("variant_attributes")) for r in rows}
            for dim in sorted(set(rows[0]["_vplan"]["unmapped_dims"])):
                values = {a: v[dim] for a, v in raw.items() if dim in v}
                if len(values) < len(rows):
                    continue            # 有成员没这个维度,不是整组的共同维度
                if len(rows) > 1 and len({str(v) for v in values.values()}) < 2:
                    _mark(rows, "degenerate_dims", dim)
                    logger.info("变体组 %s 的 %s 取值全同,不送重映射", gid, dim)
                    continue
                # 先试内置表(零成本、确定性),表不中且本轮看得见 ≥2 个成员
                # 才开连接问 LLM —— 连接**在表命中时一次都不开**。
                # ⚠ 用 ExitStack,不能 `db.pg_conn().__enter__()`:那样拿到的
                # 连接立刻就被 contextmanager 的 finally 关掉了(见 _plan_variants
                # 头注的事故说明),而这里的异常**没有 except 兜着**,会一路
                # 冒到 run() 让整条 list_new 失败(dry-run 也一样)
                got = variant_remap.hardcoded(pt, dim, values, enum)
                if not got and len(rows) > 1:
                    if conn is None:
                        conn = stack.enter_context(db.pg_conn())
                    got = variant_remap.llm_remap(conn, pt, dim, values, enum)
                if not got:
                    continue
                name, vals = got
                if name in used:
                    # 已被别的维度占了:两个维度写同一个属性名 = 载荷自相矛盾
                    logger.info("变体组 %s 的 %s 重映到 %s,但该属性已被占用,跳过",
                                gid, dim, name)
                    continue
                used.add(name)
                for r in rows:
                    vp = r["_vplan"]
                    r["_vplan"] = {
                        **vp,
                        "attr_pairs": list(vp["attr_pairs"])
                        + [(name, vals[r["asin"]])],
                        "unmapped_dims": [d for d in vp["unmapped_dims"]
                                          if d != dim],
                        "remapped_dims": sorted(
                            set(vp.get("remapped_dims") or ())
                            | {f"{dim}→{name}"}),
                    }
        # 重映成功把 `no_dim` 救回来的,升回变体口径(否则决策说"有维度"、
        # 载荷层却按单品发,两边打架)。`is_primary` 在这里补:no_dim 时它恒 False
        for rows in groups.values():
            for r in rows:
                vp = r["_vplan"]
                if vp.get("code") == "no_dim" and vp.get("attr_pairs"):
                    r["_vplan"] = {**vp, "mode": "variant", "code": "variant",
                                   "reason": "",
                                   "is_primary": not vp.get("family_has_primary")}


def _differentiate_titles(prepped: list[dict]) -> int:
    """输入:本店已备好的载荷 [{r, upc, visible, orderable}] → 输出:改了几条标题。

    旧仓 Feature B(`auto_listing/mapper.py:1633`)的接线:同一个变体组内
    `productName` 一字不差时,给每条追加 ` - <维度取值>`。判定在
    `services.variant_title`(纯函数),这里只负责**按组切分**。

    分组键 = 载荷里的 `variantGroupId`(不是 `_vplan` 的):走到这一步,
    `mp_conform` 可能已经把变体三件套整套剔掉(属性名不在枚举、值不合类型),
    那种行不该再被当成同组成员参与"标题是不是全同"的判定。
    """
    by_group: dict[str, list[dict]] = {}
    for p in prepped:
        gid = str((p.get("visible") or {}).get("variantGroupId") or "")
        if gid:
            by_group.setdefault(gid, []).append(p)
    return sum(variant_title.differentiate(rows)
               for _gid, rows in sorted(by_group.items()))


def _mark(rows: list[dict], field: str, value: str) -> None:
    """把一个标记并进这一组每行的 _vplan(列表字段,去重有序)。"""
    for r in rows:
        vp = r["_vplan"]
        r["_vplan"] = {**vp, field: sorted(set(vp.get(field) or ()) | {value})}


def _dedupe_primary(ready: list[dict]) -> None:
    """输入:带 _vplan 的待提交行 → 输出:无(就地把同组多余的主变体降成非主)。

    ⚠ **同一轮里同族多个新成员会各自都判自己是主变体**(2026-08-17 拿所有者的
    真实场景推演时发现):`_variant_plan` 的 `family_has_primary` 查的是
    `catalog.walmart_items` 里**已在架**的同族成员,而同一批新上的两个兄弟此刻
    都还没在架,两边都查到"本店没有主变体" ⇒ 两条都发 isPrimaryVariant=Yes,
    而且**在同一个 feed 里**。旧仓正是为了躲这个才干脆不发这个字段
    (`auto_listing/docs/variant_groups_design.md` §3.4:"会出现两个 primary,
    Walmart 行为未定义")。

    我们保留这个字段,但必须自己收口:每个 (店铺, 组 ID) 本轮**最多一个** Yes,
    按 ASIN 字母序取第一个 —— 定序而非按行序,这样同一批重跑选出的主变体不变
    (行序会随表格增删漂,主变体跟着漂就等于每轮都在改沃尔玛端的首图)。
    """
    by_group: dict[tuple, list[dict]] = {}
    for r in ready:
        vp = r.get("_vplan")
        if vp and vp.get("mode") == "variant" and vp.get("is_primary"):
            by_group.setdefault((r.get("store"), vp.get("group_id")),
                                []).append(r)
    for (store, gid), rows in sorted(by_group.items(),
                                     key=lambda kv: (str(kv[0][0]),
                                                     str(kv[0][1]))):
        if len(rows) < 2:
            continue
        keep = min(rows, key=lambda x: x["asin"])
        for r in rows:
            if r is not keep:
                r["_vplan"] = {**r["_vplan"], "is_primary": False}
        logger.info("变体组 %s(%s)本轮 %d 个新成员都判了主变体,"
                    "保留 %s 一个", gid, store, len(rows), keep["asin"])


def _variant_echo(vp: dict | None) -> str:
    """输入:变体决策(可为 None)→ 输出:挂在 dry-run 逐行后面的一段中文。

    **逐行报,不只报总数**:所有者的验收问题是"这两个会不会成一组、那一个会不会
    单独上"(2026-08-17)—— 只给"variant 4"这种总数答不了他的问题,得看到每行
    各自的组 ID、按哪几个维度分、是不是主变体。
    """
    if not vp:
        return " |变体:决策失败(按单品)"
    if vp.get("mode") != "variant":
        return f" |单品口径({vp.get('reason') or vp.get('code')})"
    pairs = ",".join(f"{n}={v}" for n, v in (vp.get("attr_pairs") or ()))
    out = (f" |变体组 {vp.get('group_id')} 按 {pairs}"
           f",家族 {vp.get('family_size')} 个"
           f",{'主' if vp.get('is_primary') else '非主'}变体")
    if vp.get("unmapped_dims"):
        out += f",⚠ 维度 {','.join(vp['unmapped_dims'])} 映不上未发"
    if vp.get("degenerate_dims"):
        out += (f",⚠ 维度 {','.join(vp['degenerate_dims'])} 组内取值全同已剔"
                f"(声明没有差异的维度等于没声明)")
    if vp.get("remapped_dims"):
        out += f",重映射 {','.join(vp['remapped_dims'])}"
    return out


def _spec_precheck(ready: list[dict]) -> str:
    """输入:待提交行 → 输出:spec 一致化预检报告(不领 UPC、不提交)。

    dry-run 里就能看到"哪些行会因为哪些必填过不了",不必靠回执试错烧 UPC。
    LLM 走缓存,同一批重复预检不重复计费。

    SKU 用 `sku_codec.DRYRUN_PLACEHOLDER` 而不是 mint:本函数只在 dry-run 分支
    被调到,而空跑绝不许写库(mint 是写库函数,同事务登记)。占位码含 `0`,
    不在字母表里 ⇒ `is_opaque` 恒 False,形态上就不可能被当成真码。
    抬头行说破"这是占位的",免得它看起来像真发出去的那个串。
    """
    ph = sku_codec.DRYRUN_PLACEHOLDER
    lines = [f"  spec 预检(不领 UPC/不提交;sku 用占位码 {ph},"
             f"真跑时由登记簿给真码):"]
    ok = 0
    with db.pg_conn() as conn:
        for r in ready[:20]:
            spec = pt_spec.load_pt(r["product_type"])
            try:
                visible, llm_o = _map_llm(conn, r["product_type"], spec,
                                          r["_p"])
            except Exception as e:
                lines.append(f"    {r['asin']}:LLM 映射失败 {e}")
                continue
            orderable = mp_mapper.build_orderable(
                ph, _UPC_PLACEHOLDER, r["_price"], r["_qty"], "0",
                pt=r["product_type"], product=r["_p"], llm_fields=llm_o)
            _v, _o, notes, missing = mp_conform.conform(
                spec, pt_spec.orderable_spec(), visible, orderable,
                sku=ph,
                variant=_variant_plan(conn, r.get("store") or "", r, spec))
            if missing:
                lines.append(f"    ✗ {r['asin']} 必填缺失 {len(missing)}:"
                             f"{','.join(missing[:8])}")
            else:
                ok += 1
                lines.append(f"    ✓ {r['asin']} 通过(一致化 {len(notes)} 处)")
    lines.append(f"  预检结论:{ok}/{min(len(ready), 20)} 行可提交")
    return "\n".join(lines)


def _sync_upc(execute: bool, lines: list[str]) -> None:
    """先注入一次 UPC 池(所有者定稿 2026-08-16:上架运行时自动同步一次 UPC)。

    ⚠ **不能 import upc_sync 工作流**(铁律 1)——注入那段收在
    `services.upc_pool.sync_from_sheet`,两个调用方共用同一份代码。

    失败只告警不阻断:飞书挂了不该把整条上架链拖下水,池里已有的号照样能领
    (最坏情况是本轮 no_upc 多几行,下轮补上)。**但必须说出来** —— 静默跳过
    会表现为"明明贴了号还是 no_upc",而日志里一个字都没有。

    dry-run 不注入:注入是写库。代价是 dry-run 的 `no_upc` 可能**偏多**
    (运营刚贴进表格、还没入库的号看不见),这个方向是保守的,摘要里点明。
    """
    if not execute:
        lines.append("  🧪 dry-run 跳过 UPC 注入:真跑会先注入一次,"
                     "本轮 no_upc 可能比真跑偏多(刚贴进表格的号还没入库)")
        return
    try:
        with db.pg_conn() as conn:
            got = upc_pool.sync_from_sheet(conn)
        lines.append(f"  UPC 注入:表内 {len(got['rows'])} 行,新入库 {got['new']}"
                     + (f",⚠ 非法前缀 {got['bad']}(标注永不分配)"
                        if got["bad"] else ""))
    except LookupError as e:
        lines.append(f"  ⚠ UPC池表未登记,跳过注入({e});池里已有的号照常领")
    except Exception as e:                                      # noqa: BLE001
        logger.warning("UPC 注入失败(不阻断上架,池里已有的号照常领): %s", e)
        lines.append(f"  ⚠ UPC 注入失败({e}),本轮用池里已有的号;"
                     f"刚贴进表格的号要等下轮或手动 `python cli.py upc_sync`")


def _writeback_upc(execute: bool, lines: list[str]) -> None:
    """上架**之后**把 UPC 池状态回写飞书 C~F(所有者定稿 2026-08-16)。

    与开头那次注入是同一个工作流的两头,合起来等于跑了一次 `upc_sync`,
    所以 `upc_sync` 不必再单独挂调度(所有者:「放到上架里」)。

    ⚠ **必须放在上架之后**:回写的是 PG 现状(哪些号已领、已用、给了谁)。
    放在注入旁边一起做,回写出来的是**上一轮**的状态 —— 表面看也在动,
    实际上你永远看不到刚刚这一轮消耗了哪些号。

    失败只告警不阻断:feed 已经提交出去了,回写只是展示面板
    (与 maintenance 写维护记录同款纪律)。
    """
    if not execute:
        return
    try:
        with db.pg_conn() as conn:
            got = upc_pool.sync_from_sheet(conn)     # 先取行(顺带把新号补进来)
            n = upc_pool.project_to_sheet(conn, got["rows"]) if got["rows"] else 0
        lines.append(f"UPC池状态回写 {n} 行(仅差异行)")
    except LookupError as e:
        lines.append(f"⚠ UPC池表未登记,状态未回写({e})")
    except Exception as e:                                      # noqa: BLE001
        logger.warning("UPC池状态回写失败(不影响本轮上架): %s", e)
        lines.append(f"⚠ UPC池状态回写失败:{e}"
                     f"(feed 已提交;补写跑 `python cli.py upc_sync`)")


def _llm_cost_lines(items: int = 0) -> list[str]:
    """输入:本轮进过预备期的行数 → 输出:LLM 用量/花费摘要行(没调过就返回空)。

    所有者 2026-08-21:「上架我也希望可以输出花了多少钱」。数据本来就在记 ——
    `api.llm.chat_json` 每次成功都调 `record_usage`,只是从没人打印。
    `items` 给的是**进预备期的行数**(那一段才烧 LLM),不是提交成功数:
    出参失败/必填缺失的行钱照花,拿成功数当分母会把单价算低。

    ⚠ 放在**每一个 return 之前**,dry-run 也不例外 —— `-p check_spec=1` 的
    预检是**真调 LLM** 的(那行提示自己写着),不报就等于白花钱不留痕。
    没调过 LLM 时 `summarize` 返回空列表,所以到处放不会制造噪声。
    """
    from api import llm as _llm
    from services import llm_cost as _cost
    return _cost.summarize(_llm.USAGE_STATS, items=items)


class _GateCtx(NamedTuple):
    """两道闸共用的只读上下文:库侧快照 + 三张按店配置表 + 在册凭证。"""
    state: _GateState           # _load_gate_state() 的库侧快照
    stores_by_name: dict        # 店名 → 凭证(不在里面 = 凭证缺失,整店跳过)
    quota: dict                 # 店 → 限额表「上架限制」(读不到按 999 不限)
    mults: dict                 # 店 → 四区间倍率(services/store_limits)
    lead_caps: dict             # 店 → 「配送时长限制」上限天数
    store_chs: dict             # 店 → 「配送限制」渠道(没标=不限)


class _StoreGate(NamedTuple):
    """_gate_by_store 的产物。计数**返回**给调用方合并,不就地改共享计数器。"""
    survivors: list             # 过了按店闸的候选行
    reasons: list               # [(rownum, N 理由)]
    counts: dict                # 闸门计数增量,调用方按键累加进 n
    allow_by_store: dict        # 店 → 本轮剩余配额(只有过了店闸的店才有)
    missing_warn: list          # 不明消失史 ASIN(放行但摘要报警)
    lines: list                 # 整店级摘要行(凭证缺失)


class _RowGate(NamedTuple):
    """_gate_by_row 的产物。同上:计数返回,不就地改。"""
    survivors: list             # 过了按行闸、已带 _p/_price/_qty 的行
    reasons: list               # [(rownum, N 理由)]
    counts: dict                # 闸门计数增量,调用方按键累加进 n
    data_echo: list             # [(rownum, [C,H,I,J])] 淘汰行也回显


def _gate_by_store(rows: list[dict], ctx: _GateCtx) -> _StoreGate:
    """输入:待上架行 + 闸门上下文 → 输出:`_StoreGate`(候选行/理由/计数/配额/报警/摘要行)。

    闸门链 ①③④ 的按店那半边:凭证 → 非 ACTIVE 店 → PT spec → 风控 →
    本店去重 → 产品占用 → ASIN 黑名单 →(不明消失史只报警)。
    **判据顺序即业务语义,逐条不许挪**(顺序改了 N 列理由就换一个,
    运营看到的"为什么没上"跟着变)。

    计数与理由**返回**给调用方合并 —— 就地改 run() 的共享计数器是这个函数
    当年长在 run() 里的原因,拆出来就不能再留那条尾巴。
    """
    st = ctx.state
    counts: dict = collections.defaultdict(int)
    reasons: list[tuple[int, str]] = []
    lines: list[str] = []
    missing_warn: list[str] = []             # 不明消失史,放行但报警
    candidates: list[dict] = []

    by_store: dict[str, list[dict]] = {}
    for r in rows:
        by_store.setdefault(r["store"], []).append(r)

    # 配额**不在这里切**(所有者批复 2026-08-12):先过全部闸门与数据过滤,
    # 幸存者再按店切片——被淘汰行不占名额,配额以能成功提交的行计
    allow_by_store: dict[str, int] = {}
    for store_name, srows in sorted(by_store.items()):
        if store_name not in ctx.stores_by_name:
            lines.append(f"  {store_name}:凭证缺失,整店跳过")
            continue
        if store_name in st.inactive:
            counts["inactive"] += len(srows)
            # 所有者定稿 2026-08-28:整店跳过也**逐行**写明原因——此前静默,
            # 表现是"行挂着好多天、理由空白"。只写理由不写终态,店铺回
            # ACTIVE 下一轮自动续上
            reasons.extend((r["rownum"], "店铺非ACTIVE,整店暂停上架")
                           for r in srows)
            continue
        allow_by_store[store_name] = max(0, ctx.quota.get(store_name, 999)
                                         - st.today_used.get(store_name, 0))
        # 规划外店(谭总系)**不受产品/品牌占用管**(所有者定稿 2026-08-15
        # 「既不占用、也不拦别人」,2026-08-19 生产实证补全行侧方向)。
        # 去重闸对它们照常生效:2026-08-28 起去重是**本店**语义(自己拦自己
        # 防重复上架),不存在"被别人拦"的问题
        unplanned = alloc_survey.is_excluded(store_name)
        for r in srows:
            if pt_spec.load_pt(r["product_type"]) is None:
                counts["no_spec"] += 1
                reasons.append((r["rownum"], f"PT无spec:{r['product_type']}"))
                continue
            why = risk_gate.check(st.gate, r["product_type"], None)
            if why:
                counts["risk"] += 1
                reasons.append((r["rownum"], why))
                continue
            if (store_name, r["asin"]) in st.listed_pairs:
                # 本店已在架(2026-08-28 取消全局去重:只拦同店重复,
                # 跨店同 ASIN 交给分配链 + 占用闸决定)
                counts["dedup"] += 1
                reasons.append((r["rownum"], "本店已在架:同店重复上架拦截"))
                continue
            # ── 两道码闸(批次 2):位置就是语义 ────────────────────────────
            # 在去重闸**之后**:已在架的行压根不是"再上架",不该走到这儿;
            # 在占用/黑名单闸**之前**:那两道问"这个产品该不该由这家店上",
            # 这两道问"这个 (店, 产品) 现在能不能上"——后者是更硬的时序事实。
            # 两道之间:代际上限在前,它是要人介入的终局判断,冷却只是等一等;
            # 一行同时命中两者时,N 列该显示要人做的那条。
            # 命中只写 N 理由**不写终态**(与既有闸门同语义:冷却期满/人工处置
            # 之后下一轮自动续上)。
            if (store_name, r["asin"]) in st.over_gen:
                # 判据:同 (店, amz, ASIN) 已弃码行数 ≥ sku_codec
                # .MAX_SKU_GENERATIONS(数据面 _SQL_ABANDONED_GEN)。堵的是
                # 「弃码→新码→再弃码」的循环,每转一圈白烧一个 UPC 与一个
                # MP_ITEM 配额名额,而且三条护栏跟着码重新计数
                counts["gen_cap"] += 1
                reasons.append((r["rownum"], "换码次数达上限,待人工"))
                continue
            if st.cooling.get((store_name, r["asin"])):
                # 判据:该 (店, ASIN) 在 sku_codec.RETIRE_COOLDOWN_HOURS 小时内
                # 退役回执成功过(数据面 _SQL_RETIRE_COOLDOWN)。旧实证:退役
                # 后不等冷却就重上,沃尔玛侧那条记录还在,必然再失败一次
                counts["cooldown"] += 1
                reasons.append((r["rownum"], "退役冷却中"))
                continue
            holder = None if unplanned else st.owned_asin.get(r["asin"])
            if holder and holder != store_name:
                # 占用闸:与快照闸的区别是**下架也不释放**——"店没了产品还
                # 被占着"正是所有者要的语义(§二);同店占用放行(本来就是它的)
                counts["claimed"] += 1
                reasons.append((r["rownum"], f"产品占用:已属于 {holder}"))
                continue
            bl = st.banned.get(r["asin"])
            if bl:
                # 黑名单是永久产品级禁止(PERMANENT 六类),命中即拦。
                # 这就是防呆的全部:按拉黑类别拦,不按删除史拦(所有者口径
                # 2026-08-12:因产品问题删过的修好重上是正常经营)
                counts["blacklist"] += 1
                reasons.append((r["rownum"],
                                f"ASIN黑名单:{bl[1]}({bl[0]}类)"))
                continue
            if r["asin"] in st.unexplained:
                # 只提示不拦截(所有者口径 2026-08-12):从目录消失过且我们
                # 没提交过删/停 = 疑似平台强制下架,放行但必须在摘要里亮出来
                missing_warn.append(r["asin"])
            candidates.append(r)
    return _StoreGate(candidates, reasons, dict(counts), allow_by_store,
                      missing_warn, lines)


def _gate_by_row(cands: list[dict], products: dict, ctx: _GateCtx) -> _RowGate:
    """输入:按店闸的候选行 + 采集数据 + 闸门上下文 → 输出:`_RowGate`(幸存行/理由/计数/回显)。

    闸门链 ⑤⑥ 的按行那半边:数据源 → 定制品 → 库存三态 → 库存下限 →
    品牌风控 → 品牌占用 → 产品渠道 → 店铺渠道 → 运费 → 落地价倍率 →
    配送时长 → 素材。
    **判据顺序即业务语义,逐条不许挪**(每道闸都假设前面那道已经拦掉了它
    处理不了的形状,例如店铺渠道闸靠"产品渠道未采到"上一行先拦)。

    计数与理由**返回**给调用方合并,不就地改 run() 的共享计数器。
    """
    st = ctx.state
    counts: dict = collections.defaultdict(int)
    reasons: list[tuple[int, str]] = []
    survivors: list[dict] = []
    data_echo: list[tuple[int, list]] = []   # 淘汰行也回显 C/H/I/J(旧行为)
    for r in cands:
        # ⚠ **必须在这里取本行的店名**(2026-08-25 修):此前 `lead_cap` 那行
        # 直接用了 `store_name`,而这个名字是上面那个按店循环
        # (`for store_name, srows in sorted(by_store.items())`)**留下的残值**
        # —— Python 的循环变量出了循环还活着,于是每一行读到的都是
        # **店名排序最末那家店**的「配送时长限制」。多店同轮时:那家店填了 3 天
        # ⇒ 全部店的货按 3 天拦;那家店没填 ⇒ 全部店回落全局 7 天,
        # 逐店那一列**对其余每家店都静默失效**。d4bcaab(2026-08-17 走进生产,
        # 把全局常量换成逐店列)引入,单店夹具照不出来。
        store_name = r["store"]
        p = products.get(r["asin"])
        if p is None:
            counts["no_data"] += 1   # 数据源缺席:不写终态,恢复后自动续上
            continue
        # 拉到数据的行,无论最终去向都回显标题与价库——运营在表上直接
        # 看到"为什么这行没上"的数字(旧系统对 prep_fails 同款)
        echo = [(p.get("title") or "")[:190], p.get("price") or "",
                p.get("stock") if p.get("stock") is not None else "", ""]
        data_echo.append((r["rownum"], echo))
        # 定制品闸(所有者定稿 2026-08-28:定制产品不上架)。放数据闸最前:
        # 它是产品属性,与库存/价格无关。判据在 amz_source._is_custom
        # (键 = registry.AMZ_CUSTOM_FLAG_KEY;明确真值才拦,未采到放行)
        if p.get("is_custom"):
            counts["custom"] += 1
            reasons.append((r["rownum"], "定制品不上架"))
            continue
        # ⚠ 库存三态,**绝不能 or 0 兜底**(契约 3b:None=没采到,0=确实缺货):
        #   有真值 → 走 MIN_INVENTORY 闸(防亚马逊只剩三两件时上架超卖)
        #   无真值 + in_stock → 亚马逊高库存不显示具体数,按保守常量铺货
        #   无真值 + 其余状态 → 不知道有没有货,不上架
        stock = p.get("stock")
        if stock is None:
            if p.get("stock_state") == "in_stock":
                stock = amz_source.IN_STOCK_QTY
                counts["stock_assumed"] += 1
            else:
                counts["filtered"] += 1
                reasons.append((r["rownum"],
                                f"库存未知(状态 {p.get('stock_state') or '缺失'})"))
                continue
        if stock < amz_source.MIN_INVENTORY:
            counts["filtered"] += 1
            reasons.append((r["rownum"], f"库存不足:{stock}"))
            continue
        # 品牌与制造商两个字段都查(所有者批复 2026-08-12):brand=Generic
        # 而真品牌在 manufacturer 是亚马逊常态,只查 brand 黑名单必漏
        why = risk_gate.check(st.gate, None, p.get("brand"),
                              p.get("manufacturer"))
        if why:
            counts["risk"] += 1
            reasons.append((r["rownum"], why))
            continue
        # 品牌占用闸(A1):品牌排他 ⇒ 同品牌只能在一家店。放在这里而不是
        # 前面那批闸,是因为品牌要等 amz_source 取回产品数据才知道。
        # 键与占用侧同一套归一算法(brand_key 唯一出处),否则大小写差一点
        # 就漏拦;真·无品牌(两字段皆占位符)不参与品牌排他,只受产品占用管
        bkey = brand_key.brand_key(p.get("brand"), p.get("manufacturer"))
        # 规划外店(谭总系)不做品牌归属:上架不受品牌占用管(也不占,
        # load 侧已剔;所有者定稿 2026-08-15,2026-08-19 补全行侧)
        bholder = (st.owned_brand.get(bkey)
                   if bkey and not alloc_survey.is_excluded(r["store"])
                   else None)
        if bholder and bholder != r["store"]:
            counts["claimed"] += 1
            reasons.append((r["rownum"], f"品牌占用:{bkey} 已属于 {bholder}"))
            continue
        # 配送方式决定用哪套区间(FBA 0-30/30-1000 vs FBM 15-80/80-1000)。
        # **未知不猜**(所有者 2026-08-09:这是必须要获取的信息)——猜错一档
        # 就是拿错倍率定价;宁可这行等下一轮采到 is_fba 再上。
        channel = p.get("channel")
        if channel not in pricing.PRICE_BANDS:
            counts["filtered"] += 1
            reasons.append((r["rownum"], "配送方式(FBA/FBM)未采到,不定价"))
            continue
        # 店铺渠道闸(所有者定稿 2026-08-25:「读取配送限制列,我会在其中标记
        # fba/fbm,没标就都能上」)。放在这里是因为**产品渠道上一行才刚判明**,
        # 而判定本身走 store_targets 唯一谓词(分配/上架/维护/对账共用一处)。
        # 两条空值口径都在谓词里,别在这儿另写:店没标 → 放行;产品渠道不是
        # 另一个已知值 → 放行(采不到不算不符,那一档上一行已经拦掉了)。
        # ⚠ 与分配侧的**未填口径相反**且都对:分配未填=不接自由流(没渠道就
        # 过不了硬闸),上架未填=不限制(照搬分配会把没配置的店整店废掉)。
        want_ch = ctx.store_chs.get(store_name)
        if store_targets.channel_conflict(want_ch, channel):
            counts["channel"] += 1
            reasons.append((r["rownum"],
                            f"本店只做 {want_ch},该品是 {channel}"))
            continue
        # 定价输入是**落地价 = 单价 + 运费**(所有者定稿 2026-08-10):采购真正
        # 付的是单价加运费。运费没采到(采集侧 N/A)一律不上架——与"配送方式
        # 未知不定价"同一个道理,当 0 定出来的价偏低,越贵的运费亏得越多
        if p.get("shipping") is None:
            counts["filtered"] += 1
            reasons.append((r["rownum"], "运费未采到,落地价算不出来,不定价"))
            continue
        w_price = pricing.walmart_price(channel, p.get("price"),
                                        ctx.mults.get(r["store"], {}),
                                        p.get("shipping"))
        if w_price is None:
            counts["filtered"] += 1
            reasons.append((r["rownum"],
                            f"该区间倍率未配置:落地价 "
                            f"{pricing.landed_price(p.get('price'), p.get('shipping'))}"))
            continue
        # 配送时长超限 ⇒ **不上架**(所有者定稿 2026-08-16 走进生产;
        # 此前是"上架但库存写 0")。不上架就不占 UPC、不占配额,比上一个
        # 卖不动的更省。上限按店读限额表「配送时长限制」,查不到回落 8 天。
        # **没采到(None)不算超时**——or 0 会把"未知"读成"当天达",方向反了。
        lead_cap = store_limits.cap_for(ctx.lead_caps, store_name,
                                        amz_source.MAX_LEAD_DAYS)
        if store_limits.over_lead_cap(p.get("lead_days"), lead_cap):
            counts["lead_days"] += 1
            reasons.append((r["rownum"],
                            f"配送 {p.get('lead_days')} 天 > 本店上限 {lead_cap} 天"))
            continue
        # 素材闸(2026-08-22):卖点/副图这两个**必填数组**由系统从采集数据
        # 生成,LLM 插不上手,所以此刻就能定论。不在这儿拦的话它们要走到
        # 预备期才被 validate 拦下 —— 白打一次 LLM、白占一个当天配额名额
        # (切片在预备期之前),而且素材是产品的固定属性,天天重来天天白烧。
        gap = mp_mapper.material_gap(pt_spec.load_pt(r["product_type"]), p)
        if gap:
            counts["no_material"] += 1
            reasons.append((r["rownum"], gap))
            continue
        qty = int(stock)
        echo[3] = w_price               # 算出定价的行回显 J 列
        if not ((p.get("attrs") or {}).get("weight")):
            counts["no_weight"] += 1    # ShippingWeight 将按 1.0 磅兜底,亮出来
        survivors.append({**r, "_p": p, "_price": w_price, "_qty": qty})
    return _RowGate(survivors, reasons, dict(counts), data_echo)


def run(params: dict) -> str:
    """输入:params(execute/store/check_spec/limit)→ 输出:闸门链与提交摘要。"""
    execute = bool(params.get("execute"))
    # 人工上限(试点闸,所有者节奏「一店一品 → 10 个 → 全店」):缺省 None =
    # 与今天逐字一致,只有显式传值才截断。缺省即真跑、一跑就是全店 ready 行
    # 全部抽码 + 提交 MP_ITEM,码已发、UPC 已 used,不可逆 —— 纪律没有默认值
    # 替你挡,所以把节奏做成代码闸而不是口头约定(形态照 alloc_push 的既有写法)
    limit = int(params.get("limit", 0)) or None
    # 同轮闭环等待窗(分钟,0=只推不等)与预备期 LLM 并发(所有者定稿
    # 2026-08-18:「过闸后默认128并发打deepseek」;实际并发还要过
    # db_guard.cap_workers 按 PG 连接余量钳一道)
    gap_wait = int(params.get("gap_wait", 20))
    prep_workers = int(params.get("workers", 128))
    # 第一轮起跑抖动(毫秒;0=关)。去同步,不降峰值 —— 见 SUBMIT_JITTER_MS 头注
    jitter_ms = int(params.get("submit_jitter_ms", SUBMIT_JITTER_MS))
    rows = listing_sheet.read_rows()
    if params.get("store"):
        rows = [r for r in rows if r["store"] == params["store"]]
    # 审核闸**读库不读表**(所有者定稿 2026-08-16)。表里「审核结果」是投影,
    # 可能被人手改、可能滞后;PG 是权威,而且快。
    verdicts = load_verdicts([r["asin"] for r in rows if r.get("asin")])
    open_rows = [r for r in rows
                 if r["listed"].lower() in ("", "no") and not r["feed_id"]
                 # SKU_LOCKED 归自愈链;PROHIBITED 政策违禁永不重试(旧 O 列
                 # 第五类,2026-08-12 接线——重发也永远是拒,白烧 UPC 与配额);
                 # CONTENT_REJECTED 内容标准拒(2026-08-19):文案是亚马逊
                 # 原文,原样重发必然同拒——人工改文案清 O 列后才重回通道
                 and r["list_result"] not in ("SKU_LOCKED", "PROHIBITED",
                                              "CONTENT_REJECTED")]
    fresh, n_unaudited, n_rejected = [], 0, 0
    # 审核闸逐行写 N 列理由(所有者定稿 2026-08-28:除「配额排队」外的静默桶
    # 都要写明原因——配额不写是因为那是"计划上架"还在队里,写了反而像终态)。
    # 只写理由**不写终态**:审核翻案/补审后下一轮自动续上,与闸门链同语义
    audit_reasons: list[tuple[int, str]] = []
    for r in open_rows:
        st = (verdicts.get(r["asin"]) or (None, None))[0]
        if st == AUDIT_OK:
            fresh.append(_with_pt(r, verdicts))
        elif st in ("rejected",):
            n_rejected += 1
            audit_reasons.append((r["rownum"], "审核判拒,不上架"))
        else:                       # 没结论 / pending
            n_unaudited += 1
            audit_reasons.append(
                (r["rownum"], f"审核未过:{st or '未审'}(过审后自动续上)"))
    retry, exhausted = _retry_rows(rows, verdicts)
    pending = fresh + retry
    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}上架表 {len(rows)} 行:待上架 {len(pending)}"
             + (f"(其中重试 {len(retry)})" if retry else "")]
    if n_unaudited or n_rejected:
        # ⚠ 必须点名:审核闸从"读表「审核结果」"改成"读库"之后,**没审过的行会静默
        # 消失在待上架里**。不说的话表现是"表里明明有几百行却一行也不上"
        lines.append(
            f"  审核闸(读 catalog.products,不读表「审核结果」):"
            + (f"**未审核 {n_unaudited} 行**"
               f"(先跑 `python cli.py product_audit -p from_sheet=1`)"
               if n_unaudited else "")
            + (f"{';' if n_unaudited else ''}审核判拒 {n_rejected} 行(不上)"
               if n_rejected else ""))
    if exhausted:
        lines.append(f"  ⚠ 重试已达上限({MAX_LIST_ATTEMPTS} 次)不再自动重试:"
                     + ",".join(a for _, a in exhausted[:10]))
    if not pending:
        lines += _llm_cost_lines()
        return "\n".join(lines)

    # UPC 注入排在闸门链之前:领号是第 ⑦ 道闸,运营刚贴进表格的号必须这一轮就能用
    _sync_upc(execute, lines)

    # 读取次序与拆函数之前一字不差(闸门状态 → 配额 → 三张按店表 → 凭证)
    ctx = _GateCtx(
        state=_load_gate_state(),
        quota=_load_quota(),
        # 四个区间倍率列与维护链**同一份读取**(services/store_limits):上架价与
        # 维护价两套口径会自己跟自己打架 —— 本链此前另抄了一份 _load_multipliers
        mults=store_limits.price_multipliers(),
        lead_caps=store_limits.lead_day_caps(),     # 按店「配送时长限制」
        store_chs=store_targets.store_channels(),   # 按店「配送限制」(没标=不限)
        stores_by_name={s["name"]: s for s in stores_svc.load_stores()})
    stores_by_name = ctx.stores_by_name
    n = {"inactive": 0, "quota": 0, "no_spec": 0, "risk": 0, "dedup": 0,
         "gen_cap": 0, "cooldown": 0,
         "blacklist": 0, "claimed": 0, "no_data": 0, "filtered": 0,
         "no_upc": 0, "stock_assumed": 0, "invalid": 0, "no_weight": 0,
         "lead_days": 0, "no_material": 0, "channel": 0, "custom": 0}
    # 变体口径分布(所有者定稿 2026-08-15):键 = 'variant' 或退回单品的原因首词。
    # 四类退回必须逐类见人 —— 静默降级 = 变体功能悄悄没生效而没人知道。
    n_var: dict[str, int] = collections.defaultdict(int)
    reasons: list[tuple[int, str]] = []      # (rownum, N 理由)
    reasons.extend(audit_reasons)            # 审核闸的理由同渠道落 N 列

    sg = _gate_by_store(pending, ctx)
    candidates, allow_by_store = sg.survivors, sg.allow_by_store
    missing_warn = sg.missing_warn           # 不明消失史,放行但报警
    reasons.extend(sg.reasons)
    lines.extend(sg.lines)
    for k, v in sg.counts.items():
        n[k] += v

    # 上架必须用当天最新数据(所有者定稿 2026-08-19):**全部候选**先推采集
    # 刷新(不再只推缺数据的),等窗口 + 按批摄取之后才取数定价。日界批次名
    # 天然防重:当天第二轮撞名沿用(新增候选那轮刷不到,用库中现值,次日续)。
    all_want = sorted({r["asin"] for r in candidates})
    scrape_note, gap_names = _push_scrape(all_want, execute)
    gap_line = None
    if execute and gap_names and gap_wait > 0:
        # 同轮闭环(所有者定稿 2026-08-18,与审核链同款):推完**等它采完**,
        # 按批摄取后本轮续走。超时不是失败:已采到的照常用上,没刷到的行
        # 用库中现值上架(维护链次日按最新采集纠正),库里压根没有的照旧
        # 不写终态、次日续。list_new 是当天最后一棒(20:00),不挤下游。
        wnote, _n_open = scrape_batches.wait_settled(gap_names, gap_wait)
        gap_line = f"  同轮闭环:{wnote};{_ingest_batches(gap_names)}"
    products = amz_source.fetch_products([r["asin"] for r in candidates])
    absent = sorted({r["asin"] for r in candidates
                     if products.get(r["asin"]) is None})
    if absent:
        gap_line = ((gap_line + ";") if gap_line else "  ") + \
            f"仍缺 {len(absent)} 个 ASIN 无数据(不写终态,次日续)"
    rg = _gate_by_row(candidates, products, ctx)
    survivors, data_echo = rg.survivors, rg.data_echo
    reasons.extend(rg.reasons)
    for k, v in rg.counts.items():
        n[k] += v

    # 配额切片(在全部过滤**之后**):幸存者按店取额度内前 N 行;
    # 超额行不写终态、不算失败,次日配额刷新自动续上
    ready: list[dict] = []
    sv_by_store: dict[str, list[dict]] = {}
    for r in survivors:
        sv_by_store.setdefault(r["store"], []).append(r)
    for store_name, srows in sorted(sv_by_store.items()):
        allow = allow_by_store.get(store_name, 0)
        ready.extend(srows[:allow])
        n["quota"] += max(0, len(srows) - allow)

    # 人工上限**必须在全部闸门与数据过滤之后**切(与配额切片同一条纪律:
    # 被淘汰行不占名额,否则 -p limit=1 可能一行都上不了,人会以为功能坏了)。
    # 不写 N 理由、不写终态:没轮到的行下一轮照常续上,不是"被拦"
    if limit and len(ready) > limit:
        n_held = len(ready) - limit
        ready = ready[:limit]
        lines.append(f"  ⚠ 人工上限 -p limit={limit}:本轮只做前 {limit} 行,"
                     f"其余 {n_held} 行留到下一轮(试点闸,不写 N 理由不写终态)")

    # ── 变体决策提前到这里算(2026-08-17 修两个问题)────────────────────────
    # ① **摘要里的"变体"一栏从来没出现过**:n_var 原来在下面的提交循环里才填,
    #    而 gate_line 是**字符串**、在那之前就拼好了 —— 后填的计数永远进不去。
    #    "四类退回逐类见人"这条纪律写在注释里,实际是死代码,dry-run 与真跑都没数。
    # ② dry-run 当时压根走不到变体决策,所以**验收不了**分组 —— 而这正是
    #    所有者要拿 dry-run 做的事(2026-08-17 实遇:摘要里连"变体"两个字
    #    都没有)。
    # 放这里是安全的:_variant_plan 只读一条 SELECT + 本地 spec 文件,
    # **零 LLM、不领 UPC、不写库**;提交循环改成复用这份决策,
    # 于是 dry-run 说的就是真跑要发的(同一份对象,不可能漂)。
    _plan_variants(ready, n_var)

    # 闸门行:**只报真的拦到了的**(排版规范 services/notify_fmt 规矩 2)。
    # 2026-08-17 之前是九个计数不管零不零全打印,于是每天那一行都长这样:
    #   闸门:非ACTIVE店 0,超配额 0,PT无spec 0,风控拦截 0,去重 1,黑名单 0,
    #   待数据源 0,数据过滤 1,配送超时 0
    # 真正发生的只有两项,剩下七个 0 是噪声 —— 而噪声多了人就不看了。
    # ⚠ 抑制的判据是**恰好 0**,不是"看着不重要":1 也要报(本仓最怕静默)。
    blocked = [(label, n[key]) for key, label in (
        ("inactive", "非 ACTIVE 店"), ("quota", "超配额"),
        ("no_spec", "PT 无 spec"), ("risk", "风控拦截"),
        ("dedup", "本店已在架"), ("gen_cap", "换码达上限"),
        ("cooldown", "退役冷却中"), ("blacklist", "黑名单"),
        ("no_data", "待数据源"), ("filtered", "数据过滤"),
        ("lead_days", "配送超时"), ("no_material", "素材不足"),
        ("channel", "渠道不符本店"), ("custom", "定制品")) if n[key]]
    gate_line = ("闸门:" + ",".join(f"{lab} {v}" for lab, v in blocked)
                 if blocked else "闸门:一条都没拦")
    if n["stock_assumed"]:
        # 亮出来:这些行的库存不是真值,是保守常量(高库存页面不显示具体数)
        gate_line += (f";库存数未采到按 {amz_source.IN_STOCK_QTY} 铺货"
                      f" {n['stock_assumed']} 行")
    if n_var:
        gate_line += (";变体:" + ",".join(f"{k} {v}" for k, v in
                                          sorted(n_var.items())))
    if n["no_weight"]:
        # 采集侧没给 attrs.weight → ShippingWeight 兜 1.0 磅。持续大面积
        # 出现 = 采集契约的 weight 形态可能对不上(backlog P1 核实项)
        gate_line += f";无重量数据按 1.0 磅 {n['no_weight']} 行"
    lines.append(gate_line)
    if scrape_note:
        lines.append(scrape_note)
    if gap_line:
        lines.append(gap_line)
    if missing_warn:
        lines.append(f"  ⚠ {len(missing_warn)} 行有\"不明原因消失\"史"
                     f"(疑似平台下架)仍放行:{','.join(missing_warn[:8])}"
                     f"——暂只提示不拦截(2026-08-12 口径)")

    if not execute:
        for rownum, why in reasons[:15]:
            lines.append(f"  第{rownum}行:{why}")
        for r in ready[:10]:
            lines.append(f"  [DRY-RUN] {r['store']} {r['asin']} "
                         f"定价 {r['_price']} 库存 {r['_qty']} 待提交"
                         + _variant_echo(r.get("_vplan")))
        if ready:
            lines.append(f"[DRY-RUN] 共 {len(ready)} 行将进入 领UPC→LLM→提交")
            if str(params.get("check_spec", "")) in ("1", "true", "yes"):
                lines.append(_spec_precheck(ready))
            else:
                lines.append("  (加 -p check_spec=1 可在提交前跑 spec 一致化"
                             "预检:会真调 LLM,但不领 UPC 不提交)")
        lines += _llm_cost_lines(len(ready))
        return "\n".join(lines)

    # 批量写理由(2026-08-19 所有者实遇修复):此前逐行调 write_reason,
    # 一行一个飞书请求 ~0.7s,几百行淘汰理由 = 提交前先白耗几分钟
    listing_sheet.write_reasons(reasons)
    n_reasons_written = len(reasons)
    # 淘汰行数据回显(待提交行随后由 write_submit_cols 写全套,不重复写)
    ready_rownums = {r["rownum"] for r in ready}
    listing_sheet.write_data_cols(
        [(rn, v) for rn, v in data_echo if rn not in ready_rownums])

    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")

    # ── 预备期(所有者定稿 2026-08-18 新流程)──────────────────────────────
    # LLM 出参 + spec 一致化前置到领号之前、跨店高并发(缓存优先,miss 才打
    # DeepSeek);占位号跑 conform,**通过的行才有资格领号**。FC ID 预取:
    # build_orderable 要它,而取不到凭证的店整店提交不了 —— 提前拦,
    # 别让它的行进预备期白烧 LLM。
    # 多仓批次 3:上架仓 = 配置了「维护仓库」的店填那个 FC ID,其余店仍是
    # Partner ID(Virtual Node)。**校验失败的店整店跳过、不回落 Partner ID**
    # —— 回落等于把本该进新仓的货上到旧节点,而且全程不报错
    managed_ok, managed_bad = store_limits.managed_nodes(
        list(stores_by_name.values()))
    node_note = store_limits.managed_note(managed_ok, managed_bad)
    if node_note:
        lines.append("  " + node_note)
    partners: dict[str, str] = {}
    prep_in: list[dict] = []
    by_store_pre: dict[str, list[dict]] = {}
    for r in ready:
        by_store_pre.setdefault(r["store"], []).append(r)
    for store_name, srows in sorted(by_store_pre.items()):
        if store_name in managed_bad:
            lines.append(f"  ⚠ {store_name}:「维护仓库」校验失败整店跳过"
                         f"(不回落 Virtual Node),修好配置后下轮自动恢复")
            continue
        try:
            partners[store_name] = store_limits.listing_fc(
                stores_by_name[store_name], managed_ok)
            prep_in.extend(srows)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("店铺 %s 取上架仓 FC ID 失败,整店本轮跳过: %s",
                           store_name, e)
            lines.append(f"  ⚠ {store_name}:取上架仓 FC ID 失败整店跳过"
                         f"({e}),下轮重试")
    prep_ok: list[dict] = []
    invalid_by_store: dict[str, int] = {}
    if prep_in:
        workers, clamp_note = db_guard.cap_workers(
            min(prep_workers, max(1, len(prep_in))))
        if clamp_note:
            lines.append(f"  {clamp_note}")
        prep_ok, prep_reasons, pc = _prep_rows(prep_in, partners, workers)
        reasons.extend(prep_reasons)
        n["invalid"] += pc["invalid"]
        invalid_by_store = pc.get("invalid_by_store") or {}
        prep_post = [(lab, v) for lab, v in (
            ("必填缺失", pc["invalid"]),
            ("标题不足10字符", pc["title_short"]),
            ("出参失败", pc["llm_failed"])) if v]
        lines.append(f"预备期(出参+一致化,并发 {workers}):"
                     f"通过 {len(prep_ok)}/{len(prep_in)}"
                     + (";" + ",".join(f"{lab} {v}" for lab, v in prep_post)
                        if prep_post else ""))
        # 取数四类只报非零(规矩 2)。二级复用/拒绝必须见人——它是兜底式
        # 优化,静默常态化 = 旧出参在替 LLM 说话而没人知道
        st = pc.get("llm_stats") or {}
        llm_line = ",".join(f"{lab} {st[k]}" for k, lab in (
            ("cache", "一级缓存命中"), ("reuse", "二级复用(零LLM)"),
            ("reuse_miss", "二级验证不过重打"), ("llm", "真调 LLM"))
            if st.get(k))
        if llm_line:
            lines.append(f"  LLM 取数:{llm_line}")

    by_store2: dict[str, list[dict]] = {}
    for r in prep_ok:
        by_store2.setdefault(r["store"], []).append(r)
    gate = _AdaptiveGate(stores_svc.STORE_WORKERS)
    # 店级失败时 _one_store 交出来的**半成品**(product_clear 同款):领号阶段
    # 已经攒好的 no_upc 计数、N 列理由、已被 defer 的片子都是这一轮真发生过的
    # 事,不能随异常一起蒸发 —— 补试仍失败时调用方照原样并进摘要与 N 列。
    partial: dict[str, tuple] = {}

    def _one_store(store_name: str, srows: list[dict]) -> tuple:
        """输入:店铺 + 该店**已备好**的行 → 输出:(店铺名, 计数增量, reasons, lines, 待结算片)。

        店级失败**往外抛**(不再就地吞成一行摘要):调用方收进 to_retry,
        跑完别人后走 `store_retry.serial_second_pass` 串行补试一遍,补试跑的
        就是本函数(单一落地路径)。抛之前把半成品存进 `partial`。

        提交期只剩三步:批量领号 → 真号回填占位号 → 同店打包提交。
        LLM 出参与 spec 一致化已在预备期做完(挂在 _visible/_orderable 上),
        这里**不重算** —— 重算等于预备期说的与真跑发的各算一次,
        中间任何差异都会变成"预备期说没事"。

        **各店各自的局部状态**,主线程按店名排序合并 —— 跨店并发之后不能再往
        共享 dict/list 上写:`n["no_upc"] += 1` 是"读-加-写"三步,两个线程交错
        会**丢计数**(丢得随机、不报错);`reasons`/`lines` 直接 append 则会按
        完成先后乱序交织,同一轮跑两次输出不一样,dry-run 与真跑没法对拍。

        领号并发安全(upc_pool.claim 用 FOR UPDATE SKIP LOCKED,「并发领号
        互不阻塞且绝不双领」),每店各开各的 pg 连接,不共享游标。

        飞书回写 write_submit_cols **留在本函数内**、不挪到合并之后:UPC 已
        mark_used、product_events 已落库,而表上 K 列还是空 —— 下一轮读表看到
        「listed=No 且无 feed_id」就会重发一遍。让每个店的表写紧跟自己的提交,
        别的店炸了也带不走它。(并发下的写节流由 api.feishu 的 _sheet_locks 兜。)
        """
        # submitted/failed/unknown 也进 cnt(2026-08-30 接店铺事件账本):此前
        # 「提交 N 条」是就地 sum 出来拼进摘要字符串的,数字一出这一行就没了
        cnt = {"no_upc": 0, "title_diff": 0,
               "submitted": 0, "failed": 0, "unknown": 0}
        reasons_s: list[tuple[int, str]] = []
        lines_s: list[str] = []
        # 不确定待结算的片子:**本店局部**(跨店并发之后不能往共享 list 上
        # append,与 reasons_s/lines_s 同一条纪律),主线程按店名排序合并
        deferred_s: list[tuple] = []
        store = stores_by_name[store_name]
        if jitter_ms > 0:
            # 放在领号之前:ms 级,把「领号→提交」的窗口撑宽不了多少,
            # 而这样连 PG 领号那一下的同时性也一并去掉了
            time.sleep(random.random() * jitter_ms / 1000.0)
        try:
            prepped: list[dict] = []
            with db.pg_conn() as conn:
                upcs = upc_pool.claim(conn, [{"store": store_name,
                                              "asin": r["asin"]}
                                             for r in srows])
            for r, upc in zip(srows, upcs):
                if upc is None:
                    cnt["no_upc"] += 1
                    reasons_s.append((r["rownum"], "UPC池余量不足"))
                    continue
                # 真号回填占位号:UPC 在载荷里只有这一处(build_orderable
                # 实证:productIdentifiers 单对象非数组),其余字段不重算
                r["_orderable"]["productIdentifiers"] = {
                    "productId": str(upc), "productIdType": "UPC"}
                prepped.append({"r": r, "upc": upc,
                                "visible": r["_visible"],
                                "orderable": r["_orderable"]})
            # 同变体组标题差异化(旧仓 Feature B)**必须在 assemble 之前**:
            # 组装成 MP_ITEM 之后 productName 已经埋进载荷,再改就是改两份。
            # 按 (组 ID) 分组:同一个 store 循环内,不会跨店混
            cnt["title_diff"] = _differentiate_titles(prepped)
            items = [mp_mapper.assemble_mp_item(
                p["orderable"], p["r"]["product_type"], p["visible"])
                for p in prepped]
            claimed = [(p["r"], p["upc"]) for p in prepped]
            if not items:
                return store_name, cnt, reasons_s, lines_s, deferred_s
            updates = []
            gate.acquire()
            try:
                results = feeds.submit_feed(store, "MP_ITEM", items,
                                            workflow="list_new",
                                            defer_settle=True)
            finally:
                gate.release()
            # 逐切片结果与 (行, UPC) 对位:游标走法是 submit_feed 返回契约的
            # 机械后果,收在 api/feeds.iter_result_slices(错一位 = 整批结局
            # 落到别人行上,而且不报错)
            for res, batch in feeds.iter_result_slices(results, claimed):
                if res["outcome"] == "deferred":
                    # 不确定态**当轮不写终态**:UPC 不回收、表不动、事件不落。
                    # 整轮跑完之后统一结算(所有者定稿 2026-08-26)——那时管子
                    # 已经空了,反查更可能 FOUND、补交也更可能成
                    gate.step_down(f"{store_name} 提交遇 5xx/网络不确定")
                    deferred_s.append((store_name, res["_settle"], batch))
                    continue
                _apply_submit_result(store_name, res, batch, updates, today)
            if updates:
                listing_sheet.write_submit_cols(updates)
            # K 列三态就是这一轮的三个结局(Yes=提交 / No=被拒 / Unknown=不确定)
            for _rn, v in updates:
                cnt[{"Yes": "submitted", "No": "failed"}.get(v[4], "unknown")] += 1
            n_defer = sum(len(b) for _, _, b in deferred_s)
            lines_s.append(
                f"  {store_name}:提交 {cnt['submitted']} 条"
                + (f",⏸ {n_defer} 条不确定待整轮后结算" if n_defer else ""))
        except Exception as e:
            # 店级隔离 → **不当场判生死**:跑完别人再串行补试一遍(标准①,
            # 所有者定稿 2026-08-26)。半成品先交出去再抛,仍失败时不丢账
            logger.exception("店铺 %s 上架异常(待串行补试): %s", store_name, e)
            partial[store_name] = (cnt, reasons_s, lines_s, deferred_s)
            raise
        return store_name, cnt, reasons_s, lines_s, deferred_s

    todo2 = sorted(by_store2.items())
    deferred_all: list[tuple] = []
    if todo2:
        lines.append(
            f"提交期:并发 {stores_svc.STORE_WORKERS} 起步(遇 5xx 按 "
            f"{'→'.join(str(n) for n in _CONCURRENCY_LADDER)} 降档)"
            + (f",起跑抖动 0~{jitter_ms}ms" if jitter_ms > 0
               else ",⚠ 起跑抖动**已关**(全部店同一毫秒发起)")
            + ";不确定的片子留到整轮跑完再结算")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        per_store: dict[str, tuple] = {}
        to_retry: list[tuple] = []
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(todo2))) as pool:
            futs = {pool.submit(_one_store, sn, sr): (sn, sr)
                    for sn, sr in todo2}
            for f in as_completed(futs):
                sn, sr = futs[f]
                try:
                    _sn, cnt, reasons_s, lines_s, deferred_s = f.result()
                    per_store[sn] = (cnt, reasons_s, lines_s)
                    deferred_all.extend(deferred_s)
                except Exception as e:                      # noqa: BLE001
                    to_retry.append(((sn, sr), e))
        # 按店名定序再补试:收集序是 as_completed 的完成序(随机),而补试次序
        # 与首行缺席点名的次序都不许跟着随机(同 todo2 合并那条纪律)
        to_retry.sort(key=lambda t: t[0][0])
        # 标准①(所有者定稿 2026-08-26):跑完别人再串行补试一遍,补试跑的是
        # **同一个** _one_store(单一落地路径,不另写简化版);StoreDeadError
        # 不补试。二次提交安全性由 feeds 的 payload_key 在途防重看护
        # (同 (店,ASIN) 领号还是原号 —— upc_pool.claim「先复用后新领」,
        # 载荷一字不差 ⇒ 真重了会被防重挡回 dedup,不会双上架)。
        absent_stores: list[tuple[str, str]] = []
        gate_note = ""
        if to_retry:
            recovered, still, gate_note = store_retry.serial_second_pass(
                [({"name": sn, "_srows": sr}, e) for (sn, sr), e in to_retry],
                lambda st: _one_store(st["name"], st["_srows"]),
                total_stores=len(todo2))
            for _st, (sn, cnt, reasons_s, lines_s, deferred_s) in recovered:
                per_store[sn] = (cnt, reasons_s, lines_s)
                deferred_all.extend(deferred_s)
            for st, e in still:
                sn = st["name"]
                cls = store_retry.diagnose(e)
                absent_stores.append((sn, cls))
                # 半成品照原样入账:领到号/没领到号的理由都要落 N 列,
                # 已 defer 的片子照旧进第二轮结算
                cnt, reasons_s, lines_s, deferred_s = partial.get(
                    sn, ({"no_upc": 0, "title_diff": 0, "submitted": 0,
                          "failed": 0, "unknown": 0}, [], [], []))
                per_store[sn] = (
                    cnt, reasons_s,
                    lines_s + [f"  ⚠ {sn}:上架异常已跳过({cls}:{e}),下轮重试"])
                deferred_all.extend(deferred_s)
        # 标准②:缺席不炸整轮,但必须点名在摘要**首行** —— 链通知对成功步骤
        # 只发首行(cli first_line_of),写在后面等于只写进日志
        lines[0] += nf.absent_tail(absent_stores, gate_note,
                                   tail="未上架行下轮重试")
        if gate_note:
            lines.append(gate_note)
        # 店铺事件账本(运营类)的本轮计数:首轮的三个结局在 per_store 的 cnt
        # 里,延后结算那批**加**在这里(一轮 = 首轮 + 延后结算)
        round_cnt: dict[str, dict] = {}
        # 按店名排序合并:完成先后是随机的,摘要行序与 N 列理由的写入顺序不能跟着随机
        lines.extend(_settle_round(deferred_all, stores_by_name, gate, today,
                                   round_cnt))
        if gate.steps:
            lines.append(
                "  提交并发降档:" + " → ".join(
                    str(n) for n, _ in [(stores_svc.STORE_WORKERS, "")] + gate.steps)
                + f"(首因:{gate.steps[0][1]};只降不升,下轮回顶格)")
        for sn, _ in todo2:
            cnt, reasons_s, lines_s = per_store[sn]
            n["no_upc"] += cnt["no_upc"]
            n_var["标题加维度后缀"] += cnt["title_diff"]
            reasons.extend(reasons_s)
            lines.extend(lines_s)
            c = round_cnt.setdefault(sn, {})
            for k in ("submitted", "failed", "unknown"):
                c[k] = c.get(k, 0) + cnt[k]
            c["no_upc"] = cnt["no_upc"]
            c["title_diff"] = cnt["title_diff"]
        # 预备期的必填缺失也按店记:一家店的货老是组不出合规载荷,只有把它
        # 按店摆出来才看得见(全局那个数只说"这一轮坏了多少行")
        for sn, v in (invalid_by_store or {}).items():
            round_cnt.setdefault(sn, {})["invalid"] = v
        # 每店每轮一条(全 0 的店不落行);记账失败只告警,不拖垮上架链
        store_events.record_round_safe("list_new", store_events.LIST_ROUND,
                                       round_cnt, lines)

    # 提交期计数曾经**只加不看**:gate_line 是字符串,在提交循环之前就拼死了
    # (2026-08-17 修 n_var 那一处时同一个坑没扫干净)。逐行理由确实写进了 N 列,
    # 但摘要里一个字没有 —— 于是"这轮 300 行只上了 40 条"在通知里看不出原因。
    # 必填缺失自 2026-08-18 起在预备期报(那一行紧跟 _prep_rows),这里不再重复。
    post = [(lab, v) for lab, v in (("UPC池不足", n["no_upc"]),
                                    ("标题加维度后缀", n_var["标题加维度后缀"]))
            if v]
    if post:
        lines.append("提交期:" + ",".join(f"{lab} {v}" for lab, v in post))

    # 预备/提交期新增的理由(出参失败/必填缺失/UPC 不足),同样批量写
    listing_sheet.write_reasons(reasons[n_reasons_written:])
    _writeback_upc(execute, lines)
    lines += _llm_cost_lines(len(prep_in))
    lines.append("回执 O/P/Q 由 feed_poll 反哺器回填;结果轮询走 feed_poll")
    return "\n".join(lines)
