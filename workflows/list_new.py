"""list_new — 上架主链(listing L2d,替代旧 auto_listing/main.py;危险:缺省即真跑,空跑用 --dry-run)。

用法(⚠ 缺省即真跑 —— 会真领 UPC、真提交 feed 到沃尔玛):
  python cli.py list_new --dry-run           # 空跑:闸门链判定+逐行去向,不提交
  python cli.py list_new                     # 真跑(LLM 出参/领 UPC/提交 feed)
  python cli.py list_new -p store=A085朱丽霖
  python cli.py list_new -p gap_wait=0       # 缺数据只推采集不等(默认等 20 分钟)
  python cli.py list_new -p workers=64       # 预备期 LLM 并发(默认 128,
                                             # 实际还会按 PG 连接余量钳制)

驱动表 = 上架表(registry.LISTING_SHEET,21 列):领任务条件 E 审核结果=pass
且 K 是否上架 空/No 且 L 无 feedid;K∈{Yes,Unknown} 跳过(Unknown 也算
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
  ④ 全局 ASIN 去重(catalog.walmart_items 在架任一店即拦——旧 server
    cache 的正确替代)+ **占用闸**(catalog.claims:该 ASIN 或其品牌已被
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
    单个 MP_ITEM feed(10/hour 硬限),店间并发 ≤24

闸门链之前先**注入一次 UPC 池**(所有者定稿 2026-08-16:「运行时自动同步
一次 UPC,然后再走上架流程」)——运营刚贴进「UPC池」表的号,这一轮就要能领。
注入那段收在 `services.upc_pool.sync_from_sheet`(铁律 1:不能 import
upc_sync 工作流);失败只告警不阻断,dry-run 不注入(注入是写库)。

提交结局(旧三态生死语义,UPC 回收仅三类):
  submitted → K=Yes L=feedid M=日期,UPC 标已用,listing_sources 登记(amz),
              事件 list_submitted;O/P/Q 由 feed_poll 反哺器按回执四集合回填
  failed(4xx 拒)→ N=提交被拒,UPC 回收(rejected)
  unknown → K=Unknown(不重复提交),UPC **不回收**
  内容标准拒(回执 O=CONTENT_REJECTED)不入 FAILED 通道:走**内容拒捞回**
  (所有者定稿 2026-08-19)——冷却 7 天后用**短主标题**重上一次(一次为限,
  ops.dedupe 记账;图片类留人工;副标题未拆分的老记录先推重采),
  见 _content_retry_rows。维护链的标题比对已升级双基准(拼接标题/主标题
  谁像算谁),捞回重上的短标题行不会被 title_mismatch 反手删掉
"""

import collections
import contextlib
import logging
import threading
from datetime import datetime

from api import feeds, feishu, llm, scraper, settings as settings_api
from registry import db, paths, resources
from services import alloc_survey, amz_source, blacklist, brand_key, claims, \
    db_guard, kpi, listing_sheet, listing_sources, llm_cache, mp_conform, \
    mp_mapper, pricing, product_events, product_ingest, pt_spec, risk_gate, \
    scrape_batches, store_limits, stores as stores_svc, upc_pool, \
    variant_group, variant_remap, variant_title

DANGEROUS = True

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
_SQL_LISTED_ASINS = """
SELECT DISTINCT store, sku FROM catalog.walmart_items WHERE missing_since IS NULL
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


def _load_gate_state():
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_INACTIVE)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
        cur.execute(_SQL_TODAY_LISTED)
        today_used = {s: int(n) for s, n in cur.fetchall()}
        cur.execute(_SQL_LISTED_ASINS)
        # 规划范围外的店(店名含「谭总」等,registry.alloc_excluded_stores)
        # **不参与全局去重**:所有者定稿 2026-08-15——其他店可以与它们重复
        # 上同一产品。它们既不占用、也不拦别人
        listed = {sku for store, sku in cur.fetchall()
                  if not alloc_survey.is_excluded(store)}
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
    return (inactive, today_used, listed, banned, unexplained, gate,
            owned_asin, owned_brand)


def _load_quota(default: int = 999) -> dict[str, int]:
    """限额表「上架限制」;未登记/读不到按旧语义默认 999(等于不限)。"""
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.max_daily_list])
    except LookupError:
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


def _load_multipliers() -> dict[str, dict]:
    """限额表四个区间倍率列(services/pricing 消费)。"""
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[
            f.store, f.fba_range1, f.fba_range2, f.fbm_range1, f.fbm_range2])
    except LookupError:
        return {}
    out: dict[str, dict] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        if name:
            out[name] = {k: feishu._plain_text(rec["fields"].get(getattr(f, k)))
                         for k in ("fba_range1", "fba_range2",
                                   "fbm_range1", "fbm_range2")}
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
    """输入:待提交行 + {店: partner_id} + 并发数 → 输出:(备好行, 理由, 计数)。

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
    """
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack

    llm_stats: dict = {}     # cache/reuse/reuse_miss/llm 四类取数计数

    def _one(conn, r: dict) -> tuple:
        spec = pt_spec.load_pt(r["product_type"])
        visible, llm_o = _map_llm(conn, r["product_type"], spec, r["_p"],
                                  stats=llm_stats)
        if len(visible.get("productName") or "") < 10:
            return ("title_short", r, "标题不足10字符", None)
        orderable = mp_mapper.build_orderable(
            r["asin"], _UPC_PLACEHOLDER, r["_price"], r["_qty"],
            partners[r["store"]], pt=r["product_type"], product=r["_p"],
            llm_fields=llm_o)
        visible, orderable, notes, missing = mp_conform.conform(
            spec, pt_spec.orderable_spec(), visible, orderable,
            sku=r["asin"], variant=r.get("_vplan"))
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
    ok: list[dict] = []
    reasons: list[tuple[int, str]] = []
    for kind, r, why, payload in results:
        if kind == "ok":
            v, o = payload
            ok.append({**r, "_visible": v, "_orderable": o})
        else:
            cnt[kind] += 1
            reasons.append((r["rownum"], why))
    ok.sort(key=lambda x: x["rownum"])
    reasons.sort(key=lambda t: t[0])
    cnt["llm_stats"] = llm_stats
    return ok, reasons, cnt


MAX_LIST_ATTEMPTS = 3       # 同 (店铺,SKU) 自动重上次数上限(旧 retry_state 阈值淘汰)

# psycopg3 不支持 `(a,b) IN %s` 传元组序列(psycopg2 老写法),用 unnest 配对
_SQL_ATTEMPTS = """
SELECT f.store, f.sku, count(*)
FROM ops.feed_items f
JOIN unnest(%s::text[], %s::text[]) AS t(store, sku)
  ON f.store = t.store AND f.sku = t.sku
WHERE f.feed_type = 'MP_ITEM'
GROUP BY f.store, f.sku
"""


def _retry_rows(rows: list[dict], verdicts: dict
                ) -> tuple[list[dict], list[tuple[str, str]]]:
    """输入:上架表全部行 → 输出:(可重试行, 已达上限行)。

    O=FAILED 的行要**重新排队**:失败原因多半是可修的(UPC 撞库领新号即可、
    字段问题改完 mapper 即可),旧系统靠 main 看 N=DATA_ERROR 接回重试。
    但不能无限重试——按 ops.feed_items 里同 (店铺,SKU) 的 MP_ITEM 提交次数
    卡 MAX_LIST_ATTEMPTS(旧 retry_state 永久淘汰名单的等价物)。

    ⚠ SKU_LOCKED 不进本通道:不先 RETIRE 换 UPC 重发也会失败(旧实证),
    走 sku_locked_heal 自愈链;ASYNC_PENDING 不是失败。
    """
    cand = [r for r in rows
            if (verdicts.get(r["asin"]) or (None,))[0] == AUDIT_OK
            and r["list_result"] == "FAILED"]
    if not cand:
        return [], []
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_ATTEMPTS, ([r["store"] for r in cand],
                                    [r["asin"] for r in cand]))
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


_CONTENT_COOLDOWN_DAYS = 7      # 内容拒后冷却:QARTH 审查在跑期间重发只会被
                                # 56026862530206 挡回来,白烧配额

_SQL_CONTENT_RECEIPT = """
SELECT DISTINCT ON (f.store, f.sku)
       f.store, f.sku, coalesce(f.error_desc, ''), f.resolved_at
FROM ops.feed_items f
JOIN unnest(%s::text[], %s::text[]) AS t(store, sku)
  ON f.store = t.store AND f.sku = t.sku
WHERE f.feed_type = 'MP_ITEM' AND f.error_code = %s
ORDER BY f.store, f.sku, f.resolved_at DESC NULLS LAST
"""


def _content_retry_rows(rows: list[dict], verdicts: dict, execute: bool
                        ) -> tuple[list[dict], list[str], str | None]:
    """输入:上架表全部行 + 审核字典 + 真跑? → 输出:(捞回行, 待重采 ASIN, 摘要行)。

    内容拒捞回通道(所有者定稿 2026-08-19):O=CONTENT_REJECTED 的行用
    **短主标题**重上一次——内容审查罚的多是拼接长串(堆词/多产品类型枚举),
    主标题是干净形态。四条纪律:

      · **按理由路由**:回执理由(feed_items.error_desc)含 image 的是图片
        问题——图取自亚马逊原图,自动改不了,重发必拒,留人工;
      · **冷却 ≥7 天**:内容拒的商品多半同时进了 QARTH 审查,冷却前重发
        只会撞在审锁;
      · **一次为限**(ops.dedupe scope='content_heal',**领取即记**——宁可
        prep 失败烧掉这次机会,也不反复拿同一商品骚扰内容审查);
      · 同号复用由 upc_pool.claim 保证(重试不换号)。

    捞回行带 `_short_title=True`,主链在数据过滤段把 title 换成主标题。
    **副标题预检在本函数内做**(amz_source.main_title):拆得出主标题的行
    才领取、才记 dedupe;拆不出(改版前老记录)只进"待重采"清单——随主链
    推采集,下轮数据到位自动再来,**不烧那次唯一机会**。
    dry-run 不记 dedupe,可反复对拍。
    """
    cand = [r for r in rows
            if (verdicts.get(r["asin"]) or (None,))[0] == AUDIT_OK
            and r["list_result"] == "CONTENT_REJECTED"]
    if not cand:
        return [], [], None
    code = next(iter(resources.WALMART_ERR_CONTENT))
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_CONTENT_RECEIPT, ([r["store"] for r in cand],
                                           [r["asin"] for r in cand], code))
        info = {(s, k): (d, at) for s, k, d, at in cur.fetchall()}
        cur.execute("SELECT key FROM ops.dedupe WHERE scope = %s "
                    "AND key = ANY(%s)",
                    ("content_heal",
                     [f"{r['store']}|{r['asin']}" for r in cand]))
        used = {k for (k,) in cur.fetchall()}
    ripe, n_img, n_cool, n_used = [], 0, 0, 0
    now = datetime.now(kpi.CN_TZ)
    for r in cand:
        if f"{r['store']}|{r['asin']}" in used:
            n_used += 1                 # 已用掉唯一一次机会:终态,归人工
            continue
        desc, at = info.get((r["store"], r["asin"]), ("", None))
        if "image" in desc.lower():
            n_img += 1
            continue
        if at is not None and (now - at).days < _CONTENT_COOLDOWN_DAYS:
            n_cool += 1
            continue
        ripe.append(r)
    # 副标题预检:拆得出主标题的才领取(几十行以内,单独取一次产品数据不贵)
    prods = amz_source.fetch_products([r["asin"] for r in ripe]) if ripe else {}
    out, need_sub = [], []
    for r in ripe:
        p = prods.get(r["asin"])
        if p is None or amz_source.main_title(p) is None:
            need_sub.append(r["asin"])  # 老记录/缺数据:推重采,不烧机会
            continue
        out.append({**_with_pt(r, verdicts), "feed_id": "", "listed": "",
                    "list_result": "", "_short_title": True})
    if execute and out:
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ops.dedupe (scope, key, meta) "
                "VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                [("content_heal", f"{r['store']}|{r['asin']}", "{}")
                 for r in out])
    note = (f"  内容拒捞回:领取 {len(out)}(短主标题重上,一次为限)"
            + (f";等副标题重采 {len(need_sub)}(随本轮推采,下轮自动再试)"
               if need_sub else "")
            + (f";图片类留人工 {n_img}" if n_img else "")
            + (f";冷却中 {n_cool}(拒后满 {_CONTENT_COOLDOWN_DAYS} 天再试)"
               if n_cool else "")
            + (f";已试过不再碰 {n_used}" if n_used else ""))
    return out, sorted(need_sub), note


_FAMILY_LISTED_SQL = """
SELECT sku, variant_group_id,
       coalesce(variant_group_info->>'isPrimary', '') AS is_primary
FROM catalog.walmart_items
WHERE store = %(store)s AND sku = ANY(%(skus)s::text[])
  AND missing_since IS NULL
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
                            {"store": store, "skus": [a for a in fam
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

    旧仓 Phase 0.8 的等价物,判定在 `services.variant_remap`(三层:枚举内检查 →
    内置错位表 → LLM 兜底)。要解决的是**名字对不上而语义有归宿**那一类:
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
    """
    lines = ["  spec 预检(不领 UPC/不提交):"]
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
                r["asin"], _UPC_PLACEHOLDER, r["_price"], r["_qty"], "0",
                pt=r["product_type"], product=r["_p"], llm_fields=llm_o)
            _v, _o, notes, missing = mp_conform.conform(
                spec, pt_spec.orderable_spec(), visible, orderable,
                sku=r["asin"],
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


def run(params: dict) -> str:
    """输入:params(execute/store/check_spec)→ 输出:闸门链与提交摘要。"""
    execute = bool(params.get("execute"))
    # 同轮闭环等待窗(分钟,0=只推不等)与预备期 LLM 并发(所有者定稿
    # 2026-08-18:「过闸后默认128并发打deepseek」;实际并发还要过
    # db_guard.cap_workers 按 PG 连接余量钳一道)
    gap_wait = int(params.get("gap_wait", 20))
    prep_workers = int(params.get("workers", 128))
    rows = listing_sheet.read_rows()
    if params.get("store"):
        rows = [r for r in rows if r["store"] == params["store"]]
    # 审核闸**读库不读表**(所有者定稿 2026-08-16)。表里 E 列是投影,
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
    for r in open_rows:
        st = (verdicts.get(r["asin"]) or (None, None))[0]
        if st == AUDIT_OK:
            fresh.append(_with_pt(r, verdicts))
        elif st in ("rejected",):
            n_rejected += 1
        else:                       # 没结论 / pending
            n_unaudited += 1
    retry, exhausted = _retry_rows(rows, verdicts)
    c_rows, c_need_sub, c_note = _content_retry_rows(rows, verdicts, execute)
    pending = fresh + retry + c_rows
    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}上架表 {len(rows)} 行:待上架 {len(pending)}"
             + (f"(其中重试 {len(retry)})" if retry else "")]
    if n_unaudited or n_rejected:
        # ⚠ 必须点名:审核闸从"读表 E 列"改成"读库"之后,**没审过的行会静默
        # 消失在待上架里**。不说的话表现是"表里明明有几百行却一行也不上"
        lines.append(
            f"  审核闸(读 catalog.products,不读表 E 列):"
            + (f"**未审核 {n_unaudited} 行**"
               f"(先跑 `python cli.py product_audit -p from_sheet=1`)"
               if n_unaudited else "")
            + (f"{';' if n_unaudited else ''}审核判拒 {n_rejected} 行(不上)"
               if n_rejected else ""))
    if exhausted:
        lines.append(f"  ⚠ 重试已达上限({MAX_LIST_ATTEMPTS} 次)不再自动重试:"
                     + ",".join(a for _, a in exhausted[:10]))
    if c_note:
        lines.append(c_note)
    if not pending:
        lines += _llm_cost_lines()
        return "\n".join(lines)

    # UPC 注入排在闸门链之前:领号是第 ⑦ 道闸,运营刚贴进表格的号必须这一轮就能用
    _sync_upc(execute, lines)

    (inactive, today_used, listed, banned, unexplained, gate,
     owned_asin, owned_brand) = _load_gate_state()
    quota = _load_quota()
    mults = _load_multipliers()
    lead_caps = store_limits.lead_day_caps()      # 按店「配送时长限制」
    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    n = {"inactive": 0, "quota": 0, "no_spec": 0, "risk": 0, "dedup": 0,
         "blacklist": 0, "claimed": 0, "no_data": 0, "filtered": 0,
         "no_upc": 0, "stock_assumed": 0, "invalid": 0, "no_weight": 0,
         "lead_days": 0}
    # 变体口径分布(所有者定稿 2026-08-15):键 = 'variant' 或退回单品的原因首词。
    # 四类退回必须逐类见人 —— 静默降级 = 变体功能悄悄没生效而没人知道。
    n_var: dict[str, int] = collections.defaultdict(int)
    reasons: list[tuple[int, str]] = []      # (rownum, N 理由)
    missing_warn: list[str] = []             # 不明消失史,放行但报警
    candidates: list[dict] = []

    by_store: dict[str, list[dict]] = {}
    for r in pending:
        by_store.setdefault(r["store"], []).append(r)

    # 配额**不在这里切**(所有者批复 2026-08-12):先过全部闸门与数据过滤,
    # 幸存者再按店切片——被淘汰行不占名额,配额以能成功提交的行计
    allow_by_store: dict[str, int] = {}
    for store_name, srows in sorted(by_store.items()):
        if store_name not in stores_by_name:
            lines.append(f"  {store_name}:凭证缺失,整店跳过")
            continue
        if store_name in inactive:
            n["inactive"] += len(srows)
            continue
        allow_by_store[store_name] = max(0, quota.get(store_name, 999)
                                         - today_used.get(store_name, 0))
        # 规划外店(谭总系)上架**不进全局去重、不受产品/品牌占用管**
        # (所有者定稿 2026-08-15「既不占用、也不拦别人」,2026-08-19 生产
        # 实证补全行侧方向:此前它们上架仍被别店的在架/占用拦下)
        unplanned = alloc_survey.is_excluded(store_name)
        for r in srows:
            if pt_spec.load_pt(r["product_type"]) is None:
                n["no_spec"] += 1
                reasons.append((r["rownum"], f"PT无spec:{r['product_type']}"))
                continue
            why = risk_gate.check(gate, r["product_type"], None)
            if why:
                n["risk"] += 1
                reasons.append((r["rownum"], why))
                continue
            if not unplanned and r["asin"] in listed:
                n["dedup"] += 1
                reasons.append((r["rownum"], "全局去重:该ASIN已在售"))
                continue
            holder = None if unplanned else owned_asin.get(r["asin"])
            if holder and holder != store_name:
                # 占用闸:与快照闸的区别是**下架也不释放**——"店没了产品还
                # 被占着"正是所有者要的语义(§二);同店占用放行(本来就是它的)
                n["claimed"] += 1
                reasons.append((r["rownum"], f"产品占用:已属于 {holder}"))
                continue
            bl = banned.get(r["asin"])
            if bl:
                # 黑名单是永久产品级禁止(PERMANENT 六类),命中即拦。
                # 这就是防呆的全部:按拉黑类别拦,不按删除史拦(所有者口径
                # 2026-08-12:因产品问题删过的修好重上是正常经营)
                n["blacklist"] += 1
                reasons.append((r["rownum"],
                                f"ASIN黑名单:{bl[1]}({bl[0]}类)"))
                continue
            if r["asin"] in unexplained:
                # 只提示不拦截(所有者口径 2026-08-12):从目录消失过且我们
                # 没提交过删/停 = 疑似平台强制下架,放行但必须在摘要里亮出来
                missing_warn.append(r["asin"])
            candidates.append(r)

    # 上架必须用当天最新数据(所有者定稿 2026-08-19):**全部候选**先推采集
    # 刷新(不再只推缺数据的),等窗口 + 按批摄取之后才取数定价。日界批次名
    # 天然防重:当天第二轮撞名沿用(新增候选那轮刷不到,用库中现值,次日续)。
    # 内容拒捞回里"副标题未拆分"的老记录一并搭车重采。
    all_want = sorted({r["asin"] for r in candidates} | set(c_need_sub))
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
    survivors: list[dict] = []
    data_echo: list[tuple[int, list]] = []   # 淘汰行也回显 C/H/I/J(旧行为)
    for r in candidates:
        p = products.get(r["asin"])
        if p is None:
            n["no_data"] += 1        # 数据源缺席:不写终态,恢复后自动续上
            continue
        if r.get("_short_title"):
            # 内容拒捞回行:标题换成主标题(领取时已预检拆得出;这里再兜一道
            # ——refetch 合并后数据可能变形,拆不出就退下轮,机会已烧认账)
            mt = amz_source.main_title(p)
            if mt is None:
                n["filtered"] += 1
                reasons.append((r["rownum"],
                                "内容拒捞回:主标题拆不出(数据变形),留人工"))
                continue
            p = {**p, "title": mt}
        # 拉到数据的行,无论最终去向都回显标题与价库——运营在表上直接
        # 看到"为什么这行没上"的数字(旧系统对 prep_fails 同款)
        echo = [(p.get("title") or "")[:190], p.get("price") or "",
                p.get("stock") if p.get("stock") is not None else "", ""]
        data_echo.append((r["rownum"], echo))
        # ⚠ 库存三态,**绝不能 or 0 兜底**(契约 3b:None=没采到,0=确实缺货):
        #   有真值 → 走 MIN_INVENTORY 闸(防亚马逊只剩三两件时上架超卖)
        #   无真值 + in_stock → 亚马逊高库存不显示具体数,按保守常量铺货
        #   无真值 + 其余状态 → 不知道有没有货,不上架
        stock = p.get("stock")
        if stock is None:
            if p.get("stock_state") == "in_stock":
                stock = amz_source.IN_STOCK_QTY
                n["stock_assumed"] += 1
            else:
                n["filtered"] += 1
                reasons.append((r["rownum"],
                                f"库存未知(状态 {p.get('stock_state') or '缺失'})"))
                continue
        if stock < amz_source.MIN_INVENTORY:
            n["filtered"] += 1
            reasons.append((r["rownum"], f"库存不足:{stock}"))
            continue
        # 品牌与制造商两个字段都查(所有者批复 2026-08-12):brand=Generic
        # 而真品牌在 manufacturer 是亚马逊常态,只查 brand 黑名单必漏
        why = risk_gate.check(gate, None, p.get("brand"), p.get("manufacturer"))
        if why:
            n["risk"] += 1
            reasons.append((r["rownum"], why))
            continue
        # 品牌占用闸(A1):品牌排他 ⇒ 同品牌只能在一家店。放在这里而不是
        # 前面那批闸,是因为品牌要等 amz_source 取回产品数据才知道。
        # 键与占用侧同一套归一算法(brand_key 唯一出处),否则大小写差一点
        # 就漏拦;真·无品牌(两字段皆占位符)不参与品牌排他,只受产品占用管
        bkey = brand_key.brand_key(p.get("brand"), p.get("manufacturer"))
        # 规划外店(谭总系)不做品牌归属:上架不受品牌占用管(也不占,
        # load 侧已剔;所有者定稿 2026-08-15,2026-08-19 补全行侧)
        bholder = (owned_brand.get(bkey)
                   if bkey and not alloc_survey.is_excluded(r["store"])
                   else None)
        if bholder and bholder != r["store"]:
            n["claimed"] += 1
            reasons.append((r["rownum"], f"品牌占用:{bkey} 已属于 {bholder}"))
            continue
        # 配送方式决定用哪套区间(FBA 0-30/30-75 vs FBM 15-80/80-1000)。
        # **未知不猜**(所有者 2026-08-09:这是必须要获取的信息)——猜错一档
        # 就是拿错倍率定价;宁可这行等下一轮采到 is_fba 再上。
        channel = p.get("channel")
        if channel not in pricing.PRICE_BANDS:
            n["filtered"] += 1
            reasons.append((r["rownum"], "配送方式(FBA/FBM)未采到,不定价"))
            continue
        # 定价输入是**落地价 = 单价 + 运费**(所有者定稿 2026-08-10):采购真正
        # 付的是单价加运费。运费没采到(采集侧 N/A)一律不上架——与"配送方式
        # 未知不定价"同一个道理,当 0 定出来的价偏低,越贵的运费亏得越多
        if p.get("shipping") is None:
            n["filtered"] += 1
            reasons.append((r["rownum"], "运费未采到,落地价算不出来,不定价"))
            continue
        w_price = pricing.walmart_price(channel, p.get("price"),
                                        mults.get(r["store"], {}),
                                        p.get("shipping"))
        if w_price is None:
            n["filtered"] += 1
            reasons.append((r["rownum"],
                            f"该区间倍率未配置:落地价 "
                            f"{pricing.landed_price(p.get('price'), p.get('shipping'))}"))
            continue
        # 配送时长超限 ⇒ **不上架**(所有者定稿 2026-08-16 走进生产;
        # 此前是"上架但库存写 0")。不上架就不占 UPC、不占配额,比上一个
        # 卖不动的更省。上限按店读限额表「配送时长限制」,查不到回落 8 天。
        # **没采到(None)不算超时**——or 0 会把"未知"读成"当天达",方向反了。
        lead_cap = store_limits.cap_for(lead_caps, store_name,
                                        amz_source.MAX_LEAD_DAYS)
        if store_limits.over_lead_cap(p.get("lead_days"), lead_cap):
            n["lead_days"] += 1
            reasons.append((r["rownum"],
                            f"配送 {p.get('lead_days')} 天 > 本店上限 {lead_cap} 天"))
            continue
        qty = int(stock)
        echo[3] = w_price               # 算出定价的行回显 J 列
        if not ((p.get("attrs") or {}).get("weight")):
            n["no_weight"] += 1         # ShippingWeight 将按 1.0 磅兜底,亮出来
        survivors.append({**r, "_p": p, "_price": w_price, "_qty": qty})

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

    # ── 变体决策提前到这里算(2026-08-17 修两个问题)────────────────────────
    # ① **摘要里的"变体"一栏从来没出现过**:n_var 原来在下面的提交循环里才填,
    #    而 gate_line 是**字符串**、在那之前就拼好了 —— 后填的计数永远进不去。
    #    "四类退回逐类见人"这条纪律写在注释里,实际是死代码,dry-run 与真跑都没数。
    # ② dry-run 压根不算变体决策(它在 664 行就 return 了),所以**验收不了**
    #    分组 —— 而这正是所有者要拿 dry-run 做的事(2026-08-17 实遇:摘要里
    #    连"变体"两个字都没有)。
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
        ("dedup", "全局去重"), ("blacklist", "黑名单"),
        ("no_data", "待数据源"), ("filtered", "数据过滤"),
        ("lead_days", "配送超时")) if n[key]]
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
    # DeepSeek);占位号跑 conform,**通过的行才有资格领号**。Partner ID 预取:
    # build_orderable 要它,而取不到凭证的店整店提交不了 —— 提前拦,
    # 别让它的行进预备期白烧 LLM。
    partners: dict[str, str] = {}
    prep_in: list[dict] = []
    by_store_pre: dict[str, list[dict]] = {}
    for r in ready:
        by_store_pre.setdefault(r["store"], []).append(r)
    for store_name, srows in sorted(by_store_pre.items()):
        try:
            partners[store_name] = settings_api.get_partner_id(
                stores_by_name[store_name])
            prep_in.extend(srows)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("店铺 %s 取 Partner ID 失败,整店本轮跳过: %s",
                           store_name, e)
            lines.append(f"  ⚠ {store_name}:取 Partner ID 失败整店跳过"
                         f"({e}),下轮重试")
    prep_ok: list[dict] = []
    if prep_in:
        workers, clamp_note = db_guard.cap_workers(
            min(prep_workers, max(1, len(prep_in))))
        if clamp_note:
            lines.append(f"  {clamp_note}")
        prep_ok, prep_reasons, pc = _prep_rows(prep_in, partners, workers)
        reasons.extend(prep_reasons)
        n["invalid"] += pc["invalid"]
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

    def _one_store(store_name: str, srows: list[dict]) -> tuple:
        """输入:店铺 + 该店**已备好**的行 → 输出:(店铺名, 计数增量, reasons, lines)。

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
        cnt = {"no_upc": 0, "title_diff": 0}
        reasons_s: list[tuple[int, str]] = []
        lines_s: list[str] = []
        store = stores_by_name[store_name]
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
                return store_name, cnt, reasons_s, lines_s
            updates = []
            i = 0
            for res in feeds.submit_feed(store, "MP_ITEM", items,
                                         workflow="list_new"):
                batch = claimed[i:i + res["count"]]
                i += res["count"]
                with db.pg_conn() as conn:
                    if res["outcome"] == "submitted" and res["feed_id"]:
                        upc_pool.mark_used(conn, [(u, r["asin"])
                                                  for r, u in batch])
                        listing_sources.register(conn, [
                            {"store": store_name, "sku": r["asin"],
                             "source_type": listing_sources.SOURCE_AMZ,
                             "source_key": r["asin"], "workflow": "list_new"}
                            for r, _ in batch])
                        product_events.record_many(conn, [
                            {"sku": r["asin"], "store": store_name,
                             "event": product_events.LIST_SUBMITTED, "source": "list_new",
                             "detail": {"feed_id": res["feed_id"],
                                        "price": r["_price"]}}
                            for r, _ in batch])
                        for r, u in batch:
                            updates.append((r["rownum"], [
                                (r["_p"].get("title") or "")[:190],
                                r["_p"].get("price") or "",
                                r["_qty"],      # 实际提交的库存(0 也照写)
                                r["_price"], "Yes", res["feed_id"], today, ""]))
                    elif res["outcome"] == "failed":
                        upc_pool.release(conn, [u for _, u in batch], "rejected")
                        for r, _ in batch:
                            updates.append((r["rownum"], [
                                "", "", "", "", "No", "", "", "提交被拒"]))
                    else:   # unknown:UPC 不回收,K=Unknown 防重复提交
                        for r, _ in batch:
                            updates.append((r["rownum"], [
                                "", "", "", "", "Unknown", "", today,
                                "提交结局不确定,待对账"]))
            listing_sheet.write_submit_cols(updates)
            lines_s.append(f"  {store_name}:提交 {sum(1 for _, v in updates if v[4] == 'Yes')} 条")
        except Exception as e:
            logger.exception("店铺 %s 上架异常,跳过继续其它店: %s", store_name, e)
            lines_s.append(f"  ⚠ {store_name}:上架异常已跳过({e}),下轮重试")
        return store_name, cnt, reasons_s, lines_s

    todo2 = sorted(by_store2.items())
    if todo2:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        per_store: dict[str, tuple] = {}
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(todo2))) as pool:
            futs = [pool.submit(_one_store, sn, sr) for sn, sr in todo2]
            for f in as_completed(futs):
                sn, cnt, reasons_s, lines_s = f.result()
                per_store[sn] = (cnt, reasons_s, lines_s)
        # 按店名排序合并:完成先后是随机的,摘要行序与 N 列理由的写入顺序不能跟着随机
        for sn, _ in todo2:
            cnt, reasons_s, lines_s = per_store[sn]
            n["no_upc"] += cnt["no_upc"]
            n_var["标题加维度后缀"] += cnt["title_diff"]
            reasons.extend(reasons_s)
            lines.extend(lines_s)

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
