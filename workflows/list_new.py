"""list_new — 上架主链(listing L2d,替代旧 auto_listing/main.py;危险,默认 dry-run)。

用法:
  python cli.py list_new                     # dry-run:闸门链判定+逐行去向
  python cli.py list_new --execute           # 真跑(领 UPC/LLM 映射/提交 feed)
  python cli.py list_new -p store=A085朱丽霖

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
  ⑤ 数据源(services/amz_source,缺席:该行本轮跳过**不写终态**,并把
    缺数据 ASIN **自动推给采集服务**补采(所有者批复 2026-08-12 接上闭环;
    日界批次名防重,当天已推不重推),采集落库后自动续上
  ⑥ 数据过滤:库存 <5 淘汰;配送超时上架但库存写 0;品牌/制造商黑名单
    (两字段都查,brand=Generic 真品牌在 manufacturer 是常态);
    定价:services/pricing(FBA/FBM 区间×倍率;出界按 300% 兜底,
    只有区间内倍率未配置才淘汰)
  ⑦ UPC 领号(catalog.upc_pool 事务)→ LLM 映射(llm_cache)→ mapper
    硬约束 → 同店打包单个 MP_ITEM feed(10/hour 硬限)

提交结局(旧三态生死语义,UPC 回收仅三类):
  submitted → K=Yes L=feedid M=日期,UPC 标已用,listing_sources 登记(amz),
              事件 list_submitted;O/P/Q 由 feed_poll 反哺器按回执四集合回填
  failed(4xx 拒)→ N=提交被拒,UPC 回收(rejected)
  unknown → K=Unknown(不重复提交),UPC **不回收**
"""

import logging
from datetime import datetime

from api import feeds, feishu, llm, scraper, settings as settings_api
from registry import db, paths, resources
from services import alloc_survey, amz_source, blacklist, brand_key, claims, kpi, \
    listing_sheet, listing_sources, llm_cache, mp_conform, mp_mapper, \
    pricing, product_events, pt_spec, risk_gate, stores as stores_svc, upc_pool

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
        # 回填前后行为一致,不会因为"还没回填"而误拦
        owned_asin = claims.load_active(conn, claims.PRODUCT)
        owned_brand = claims.load_active(conn, claims.BRAND)
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


def _push_scrape(absent: list[str], execute: bool) -> str | None:
    """输入:缺数据 ASIN 列表 + 是否真跑 → 输出:摘要行(None=无缺数据)。

    采集闭环(所有者批复 2026-08-12):数据源缺席的 ASIN 自动推给采集服务,
    替代"接线期人工推"。批次名带北京日界,天然防重——当天第二轮撞名
    (BatchExistsError)直接跳过,增量 ASIN 次日随新批次推,不追当天。
    最快形态(不切邮编不截图);采集落库(product_ingest)后主链自动续上。
    推送失败只告警不阻塞上架:缺数据行本来就是跳过不写终态。
    """
    if not absent:
        return None
    day = datetime.now(kpi.CN_TZ).strftime("%Y%m%d")
    name = f"listing_gap_{day}"
    if not execute:
        return (f"  [DRY-RUN] 缺数据 {len(absent)} 个 ASIN,"
                f"--execute 时将推采集批次 {name}")
    try:
        r = scraper.submit_batch(name, absent)
        return (f"  缺数据 {len(absent)} 个 ASIN 已推采集"
                f"(批次 {name},入库 {r.get('inserted')})")
    except scraper.BatchExistsError:
        return (f"  缺数据 {len(absent)} 个 ASIN:今日批次 {name} 已推过,"
                f"不重推(增量明日随新批次)")
    except Exception as e:
        logger.warning("推采集失败(不阻塞上架,缺数据行本轮跳过): %s", e)
        return f"  ⚠ 缺数据 {len(absent)} 个 ASIN 推采集失败:{e}"


def _map_llm(conn, pt: str, spec, product: dict) -> tuple[dict, dict]:
    """LLM 映射(缓存优先)→ (清洗后 Visible, LLM 填的 Orderable 字段)。

    2026-08-12 旧仓对照恢复两段式:Orderable 的非系统字段(条件必填等)
    交还 LLM 填,系统强制项在 build_orderable 里覆盖;Visible 照旧过
    finalize_visible 硬约束清洗。
    """
    messages = mp_mapper.build_llm_messages(pt, spec, product,
                                            ospec=pt_spec.orderable_spec())
    key = llm_cache.cache_key(messages, 0.2, 4096)
    raw = llm_cache.get(conn, key)
    if raw is None:
        raw = llm.chat_json(messages)
        llm_cache.put(conn, key, raw)
    raw_v, raw_o = mp_mapper.split_llm_output(raw)
    visible = mp_mapper.finalize_visible(pt, raw_v, spec,
                                         images=product.get("images"),
                                         product=product)
    return visible, raw_o


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


def _retry_rows(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """输入:上架表全部行 → 输出:(可重试行, 已达上限行)。

    O=FAILED 的行要**重新排队**:失败原因多半是可修的(UPC 撞库领新号即可、
    字段问题改完 mapper 即可),旧系统靠 main 看 N=DATA_ERROR 接回重试。
    但不能无限重试——按 ops.feed_items 里同 (店铺,SKU) 的 MP_ITEM 提交次数
    卡 MAX_LIST_ATTEMPTS(旧 retry_state 永久淘汰名单的等价物)。

    ⚠ SKU_LOCKED 不进本通道:不先 RETIRE 换 UPC 重发也会失败(旧实证),
    走 sku_locked_heal 自愈链;ASYNC_PENDING 不是失败。
    """
    cand = [r for r in rows
            if r["audit_result"].lower() == "pass"
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
        retry.append({**r, "feed_id": "", "listed": "", "list_result": ""})
    return retry, exhausted


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
                r["asin"], "000000000000", r["_price"], r["_qty"], "0",
                pt=r["product_type"], product=r["_p"], llm_fields=llm_o)
            _v, _o, notes, missing = mp_conform.conform(
                spec, pt_spec.orderable_spec(), visible, orderable,
                sku=r["asin"])
            if missing:
                lines.append(f"    ✗ {r['asin']} 必填缺失 {len(missing)}:"
                             f"{','.join(missing[:8])}")
            else:
                ok += 1
                lines.append(f"    ✓ {r['asin']} 通过(一致化 {len(notes)} 处)")
    lines.append(f"  预检结论:{ok}/{min(len(ready), 20)} 行可提交")
    return "\n".join(lines)


def run(params: dict) -> str:
    """输入:params(execute/store/check_spec)→ 输出:闸门链与提交摘要。"""
    execute = bool(params.get("execute"))
    rows = listing_sheet.read_rows()
    if params.get("store"):
        rows = [r for r in rows if r["store"] == params["store"]]
    fresh = [r for r in rows
             if r["audit_result"].lower() == "pass"
             and r["listed"].lower() in ("", "no")
             and not r["feed_id"]
             # SKU_LOCKED 归自愈链;PROHIBITED 政策违禁永不重试(旧 O 列
             # 第五类,2026-08-12 接线——重发也永远是拒,白烧 UPC 与配额)
             and r["list_result"] not in ("SKU_LOCKED", "PROHIBITED")]
    retry, exhausted = _retry_rows(rows)
    pending = fresh + retry
    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}上架表 {len(rows)} 行:待上架 {len(pending)}"
             + (f"(其中重试 {len(retry)})" if retry else "")]
    if exhausted:
        lines.append(f"  ⚠ 重试已达上限({MAX_LIST_ATTEMPTS} 次)不再自动重试:"
                     + ",".join(a for _, a in exhausted[:10]))
    if not pending:
        return "\n".join(lines)

    (inactive, today_used, listed, banned, unexplained, gate,
     owned_asin, owned_brand) = _load_gate_state()
    quota = _load_quota()
    mults = _load_multipliers()
    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    n = {"inactive": 0, "quota": 0, "no_spec": 0, "risk": 0, "dedup": 0,
         "blacklist": 0, "claimed": 0, "no_data": 0, "filtered": 0,
         "no_upc": 0, "stock_assumed": 0, "invalid": 0, "no_weight": 0}
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
            if r["asin"] in listed:
                n["dedup"] += 1
                reasons.append((r["rownum"], "全局去重:该ASIN已在售"))
                continue
            holder = owned_asin.get(r["asin"])
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

    products = amz_source.fetch_products([r["asin"] for r in candidates])
    # 缺数据 ASIN 自动推采集(所有者批复 2026-08-12 接上闭环,替代"接线期
    # 人工推")。日界批次名天然防重:当天已推过撞名即跳过,增量次日再推
    absent = sorted({r["asin"] for r in candidates
                     if products.get(r["asin"]) is None})
    scrape_note = _push_scrape(absent, execute)
    survivors: list[dict] = []
    data_echo: list[tuple[int, list]] = []   # 淘汰行也回显 C/H/I/J(旧行为)
    for r in candidates:
        p = products.get(r["asin"])
        if p is None:
            n["no_data"] += 1        # 数据源缺席:不写终态,恢复后自动续上
            continue
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
        bholder = owned_brand.get(bkey) if bkey else None
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
        # 配送时长同样三态:采到且 >12 天 → 上架但库存写 0(旧规则);
        # **没采到(None)不算超时**——or 0 会把"未知"读成"当天达",方向反了
        lead = p.get("lead_days")
        qty = 0 if (lead is not None and lead > amz_source.MAX_LEAD_DAYS) \
            else int(stock)
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

    gate_line = (f"闸门:非ACTIVE店 {n['inactive']},超配额 {n['quota']},"
                 f"PT无spec {n['no_spec']},风控拦截 {n['risk']},"
                 f"去重 {n['dedup']},黑名单 {n['blacklist']},"
                 f"待数据源 {n['no_data']},数据过滤 {n['filtered']}")
    if n["stock_assumed"]:
        # 亮出来:这些行的库存不是真值,是保守常量(高库存页面不显示具体数)
        gate_line += (f";库存数未采到按 {amz_source.IN_STOCK_QTY} 铺货"
                      f" {n['stock_assumed']} 行")
    if n["no_weight"]:
        # 采集侧没给 attrs.weight → ShippingWeight 兜 1.0 磅。持续大面积
        # 出现 = 采集契约的 weight 形态可能对不上(backlog P1 核实项)
        gate_line += f";无重量数据按 1.0 磅 {n['no_weight']} 行"
    lines.append(gate_line)
    if scrape_note:
        lines.append(scrape_note)
    if missing_warn:
        lines.append(f"  ⚠ {len(missing_warn)} 行有\"不明原因消失\"史"
                     f"(疑似平台下架)仍放行:{','.join(missing_warn[:8])}"
                     f"——暂只提示不拦截(2026-08-12 口径)")

    if not execute:
        for rownum, why in reasons[:15]:
            lines.append(f"  第{rownum}行:{why}")
        for r in ready[:10]:
            lines.append(f"  [DRY-RUN] {r['store']} {r['asin']} "
                         f"定价 {r['_price']} 库存 {r['_qty']} 待提交")
        if ready:
            lines.append(f"[DRY-RUN] 共 {len(ready)} 行将进入 领UPC→LLM→提交")
            if str(params.get("check_spec", "")) in ("1", "true", "yes"):
                lines.append(_spec_precheck(ready))
            else:
                lines.append("  (加 -p check_spec=1 可在提交前跑 spec 一致化"
                             "预检:会真调 LLM,但不领 UPC 不提交)")
        return "\n".join(lines)

    for rownum, why in reasons:
        listing_sheet.write_reason(rownum, why)
    n_reasons_written = len(reasons)
    # 淘汰行数据回显(待提交行随后由 write_submit_cols 写全套,不重复写)
    ready_rownums = {r["rownum"] for r in ready}
    listing_sheet.write_data_cols(
        [(rn, v) for rn, v in data_echo if rn not in ready_rownums])

    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    by_store2: dict[str, list[dict]] = {}
    for r in ready:
        by_store2.setdefault(r["store"], []).append(r)

    for store_name, srows in sorted(by_store2.items()):
        store = stores_by_name[store_name]
        try:
            partner = settings_api.get_partner_id(store)
            items, claimed = [], []
            with db.pg_conn() as conn:
                upcs = upc_pool.claim(conn, [{"store": store_name,
                                              "asin": r["asin"]}
                                             for r in srows])
                for r, upc in zip(srows, upcs):
                    if upc is None:
                        n["no_upc"] += 1
                        reasons.append((r["rownum"], "UPC池余量不足"))
                        continue
                    visible, llm_o = _map_llm(
                        conn, r["product_type"],
                        pt_spec.load_pt(r["product_type"]), r["_p"])
                    if len(visible.get("productName") or "") < 10:
                        upc_pool.release(conn, [upc], "prep_failed")
                        reasons.append((r["rownum"], "标题不足10字符"))
                        continue
                    orderable = mp_mapper.build_orderable(
                        r["asin"], upc, r["_price"], r["_qty"], partner,
                        pt=r["product_type"], product=r["_p"],
                        llm_fields=llm_o)
                    # spec 一致化流水线(类型/条件必填/枚举/未知字段/minItems…):
                    # 缺必填就**不提交**——本地拦下比让沃尔玛拒省 UPC 也省配额
                    visible, orderable, notes, missing = mp_conform.conform(
                        pt_spec.load_pt(r["product_type"]),
                        pt_spec.orderable_spec(), visible, orderable,
                        sku=r["asin"])
                    if notes:
                        logger.info("%s spec 一致化 %d 处:%s", r["asin"],
                                    len(notes), "; ".join(notes[:6]))
                    if missing:
                        upc_pool.release(conn, [upc], "prep_failed")
                        n["invalid"] += 1
                        reasons.append((r["rownum"],
                                        f"必填缺失:{','.join(missing[:6])}"))
                        _dump_llm_debug(r["asin"], visible, orderable,
                                        missing, notes)
                        continue
                    items.append(mp_mapper.assemble_mp_item(
                        orderable, r["product_type"], visible))
                    claimed.append((r, upc))
            if not items:
                continue
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
            lines.append(f"  {store_name}:提交 {sum(1 for _, v in updates if v[4] == 'Yes')} 条")
        except Exception as e:
            logger.exception("店铺 %s 上架异常,跳过继续其它店: %s", store_name, e)
            lines.append(f"  ⚠ {store_name}:上架异常已跳过({e}),下轮重试")

    for rownum, why in reasons[n_reasons_written:]:   # 提交期新增的理由(UPC/标题)
        listing_sheet.write_reason(rownum, why)
    lines.append("回执 O/P/Q 由 feed_poll 反哺器回填;结果轮询走 feed_poll")
    return "\n".join(lines)
