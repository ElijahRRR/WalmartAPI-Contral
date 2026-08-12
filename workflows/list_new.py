"""list_new — 上架主链(listing L2d,替代旧 auto_listing/main.py;危险,默认 dry-run)。

用法:
  python cli.py list_new                     # dry-run:闸门链判定+逐行去向
  python cli.py list_new --execute           # 真跑(领 UPC/LLM 映射/提交 feed)
  python cli.py list_new -p store=A085朱丽霖

驱动表 = 上架表(registry.LISTING_SHEET,21 列):领任务条件 E 审核结果=pass
且 K 是否上架 空/No 且 L 无 feedid;K∈{Yes,Unknown} 与 O=SKU_LOCKED 跳过
(Unknown 也算已上架——沃尔玛可能已收单,重复提交 = 双上架,旧生死规则)。

闸门链(顺序即执行序,每道命中写 N=未上架理由或摘要计数):
  ① 店铺状态(ops.store_kpi_daily 非 ACTIVE 整店跳过,无记录视为 ACTIVE)
  ② 日配额(限额表「上架限制」- 今日已提交数(ops.feed_items MP_ITEM);
    北京日界)
  ③ PT spec 存在(pt_spec;无 spec 淘汰)+ 风控否决闸(risk_gate:禁售 PT)
  ④ 全局 ASIN 去重(catalog.walmart_items 在架任一店即拦——旧 server
    cache 的正确替代)+ product_risk 防呆(有删除史/删除未生效史即拦;
    "不明原因消失"史=疑似平台下架,只在摘要报警不拦截——所有者口径
    2026-08-12,积累观察后再定要不要升级成拦截;停用史不拦,等 RETIRE
    职责边界拍板)
  ⑤ 数据源(services/amz_source,暂不可用:该行本轮跳过**不写终态**,
    数据恢复自动续上)
  ⑥ 数据过滤:库存 <5 淘汰;配送 >12 天上架但库存写 0;品牌黑名单;
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

from api import feeds, feishu, llm, settings as settings_api
from registry import db, resources
from services import amz_source, kpi, listing_sheet, listing_sources, \
    llm_cache, mp_conform, mp_mapper, pricing, product_events, pt_spec, \
    risk_gate, stores as stores_svc, upc_pool

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
SELECT DISTINCT sku FROM catalog.walmart_items WHERE missing_since IS NULL
"""
_SQL_RISKY = """
SELECT asin, delete_times, delete_not_effective_times, listed_times,
       last_removed_at
FROM catalog.product_risk
WHERE delete_times > 0 OR delete_not_effective_times > 0
"""
_SQL_UNEXPLAINED = """
SELECT asin FROM catalog.product_risk WHERE unexplained_missing
"""


def _risk_reason(deletes: int, not_effective: int, listed: int,
                 last_removed) -> str:
    """输入:product_risk 一行的计数与最近移除时间 → 输出:N 列防呆理由。

    此前只写"有删除史"四个字,人工复核还得手查账本;计数和时间本来就在
    视图里,直接带出来当证据。"""
    bits = [f"提交删除{deletes}次"]
    if not_effective:
        bits.append(f"删除未生效{not_effective}次")
    if listed:
        bits.append(f"历史上架{listed}次")
    if last_removed:
        bits.append(f"最近移除{last_removed:%Y-%m-%d}")
    return f"防呆:该ASIN有删除史({','.join(bits)})"


def _load_gate_state():
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_INACTIVE)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
        cur.execute(_SQL_TODAY_LISTED)
        today_used = {s: int(n) for s, n in cur.fetchall()}
        cur.execute(_SQL_LISTED_ASINS)
        listed = {r[0] for r in cur.fetchall()}
        cur.execute(_SQL_RISKY)
        # 键是 coalesce(asin, sku)——视图身份键 2026-08-11 从订货号原文改成
        # 产品码,否则三段式 sku 名下的删除史拦不住同 ASIN 换号重上
        risky = {r[0]: r[1:] for r in cur.fetchall()}
        cur.execute(_SQL_UNEXPLAINED)
        unexplained = {r[0] for r in cur.fetchall()}
        gate = risk_gate.load_gate(conn)
    return inactive, today_used, listed, risky, unexplained, gate


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


def _map_visible(conn, pt: str, spec, product: dict) -> dict:
    """LLM 映射(缓存优先)→ mapper 硬约束清洗。"""
    messages = mp_mapper.build_llm_messages(pt, spec, product)
    key = llm_cache.cache_key(messages, 0.2, 4096)
    raw = llm_cache.get(conn, key)
    if raw is None:
        raw = llm.chat_json(messages)
        llm_cache.put(conn, key, raw)
    return mp_mapper.finalize_visible(pt, raw, spec,
                                      images=product.get("images"),
                                      product=product)


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

    ⚠ SKU_LOCKED 永不重试(SKU 已被旧 UPC 绑死);ASYNC_PENDING 不是失败。
    """
    cand = [r for r in rows
            if r["audit_result"].lower() == "pass"
            and r["list_result"] == "FAILED"
            and r["list_result"] != "SKU_LOCKED"]
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
                visible = _map_visible(conn, r["product_type"], spec, r["_p"])
            except Exception as e:
                lines.append(f"    {r['asin']}:LLM 映射失败 {e}")
                continue
            orderable = mp_mapper.build_orderable(
                r["asin"], "000000000000", r["_price"], r["_qty"], "0",
                pt=r["product_type"], product=r["_p"])
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
             and r["list_result"] != "SKU_LOCKED"]
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

    inactive, today_used, listed, risky, unexplained, gate = _load_gate_state()
    quota = _load_quota()
    mults = _load_multipliers()
    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    n = {"inactive": 0, "quota": 0, "no_spec": 0, "risk": 0, "dedup": 0,
         "guard": 0, "no_data": 0, "filtered": 0, "no_upc": 0,
         "stock_assumed": 0, "invalid": 0}
    reasons: list[tuple[int, str]] = []      # (rownum, N 理由)
    missing_warn: list[str] = []             # 不明消失史,放行但报警
    candidates: list[dict] = []

    by_store: dict[str, list[dict]] = {}
    for r in pending:
        by_store.setdefault(r["store"], []).append(r)

    for store_name, srows in sorted(by_store.items()):
        if store_name not in stores_by_name:
            lines.append(f"  {store_name}:凭证缺失,整店跳过")
            continue
        if store_name in inactive:
            n["inactive"] += len(srows)
            continue
        allow = max(0, quota.get(store_name, 999)
                    - today_used.get(store_name, 0))
        take, over = srows[:allow], srows[allow:]
        n["quota"] += len(over)
        for r in take:
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
            risk = risky.get(r["asin"])
            if risk:
                n["guard"] += 1
                reasons.append((r["rownum"], _risk_reason(*risk)))
                continue
            if r["asin"] in unexplained:
                # 只提示不拦截(所有者口径 2026-08-12):从目录消失过且我们
                # 没提交过删/停 = 疑似平台强制下架,放行但必须在摘要里亮出来
                missing_warn.append(r["asin"])
            candidates.append(r)

    products = amz_source.fetch_products([r["asin"] for r in candidates])
    ready: list[dict] = []
    for r in candidates:
        p = products.get(r["asin"])
        if p is None:
            n["no_data"] += 1        # 数据源缺席:不写终态,恢复后自动续上
            continue
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
        why = risk_gate.check(gate, None, p.get("brand"))
        if why:
            n["risk"] += 1
            reasons.append((r["rownum"], why))
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
        ready.append({**r, "_p": p, "_price": w_price, "_qty": qty})

    gate_line = (f"闸门:非ACTIVE店 {n['inactive']},超配额 {n['quota']},"
                 f"PT无spec {n['no_spec']},风控拦截 {n['risk']},"
                 f"去重 {n['dedup']},防呆 {n['guard']},"
                 f"待数据源 {n['no_data']},数据过滤 {n['filtered']}")
    if n["stock_assumed"]:
        # 亮出来:这些行的库存不是真值,是保守常量(高库存页面不显示具体数)
        gate_line += (f";库存数未采到按 {amz_source.IN_STOCK_QTY} 铺货"
                      f" {n['stock_assumed']} 行")
    lines.append(gate_line)
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
                    visible = _map_visible(conn, r["product_type"],
                                           pt_spec.load_pt(r["product_type"]),
                                           r["_p"])
                    if len(visible.get("productName") or "") < 10:
                        upc_pool.release(conn, [upc], "prep_failed")
                        reasons.append((r["rownum"], "标题不足10字符"))
                        continue
                    orderable = mp_mapper.build_orderable(
                        r["asin"], upc, r["_price"], r["_qty"], partner,
                        pt=r["product_type"], product=r["_p"])
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
