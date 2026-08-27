"""match_listing — 跟卖上架(listing 子计划 L1,替代旧 match_listing/;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py match_listing --dry-run          # 空跑:预检+打印将提交什么
  python cli.py match_listing                    # 真跑(提交 feed + 回写表格)
  python cli.py match_listing -p store=A085朱丽霖

驱动表(registry.MATCH_SHEET「跟卖表」,所有者定稿 2026-08-07 单路飞书读,
替代旧 xlsx 输入):运营填 A=UPC C=售价 D=重量 E=店铺;B=SKU 人工优先
(旧系统习惯:人工编号),留空则按同款格式自动生成;脚本填其余。

流程:读表 → 待处理行 SPEC 预检(api/items.search_walmart_spec,按位数
生成 upc/gtin 候选依次试)→ 三路:
  MP_ITEM_MATCH(已在售)→ 可跟卖 → **两道闸**(2026-08-12 接入,补齐
                          "只有 list_new 有闸"的洞)→ 过闸才提交:
                          SPEC 预填模板 + sku/price/ShippingWeight
                          → 按店打包 MP_ITEM_MATCH feed(REPLACE 幂等)
  MP_ITEM(未在售)     → F=需完整建品,终态跳过(那是 L2 主链的活)
  无匹配               → F=目录无,终态跳过

两道闸(跟卖无 amz 侧身份,全靠 SPEC 交叉字段;交叉不出的字段跳过
该道闸,不是放行整行;命中写 F=终态,运营核对后清 F 重新排队):
  ① 风控闸 risk_gate:SPEC 的 product_type 禁售 / brand 黑名单品牌
  ② ASIN 黑名单:SPEC 交叉出的 ASIN 命中 catalog.asin_blacklist(永久
    产品级禁止六类——出现过侵权/审查等拉黑类别的产品想换跟卖通道回来,
    拦的就是这个)
防呆=黑名单,不看删除史(所有者口径 2026-08-12:因产品问题删过的
修好重上是正常经营;曾按 product_risk 删除史/GTIN 删除史一刀切拦过,
当日拆除)。
结果:J/K 由 feed_poll 反哺器按 ops.feed_items 回填;跟卖新 offer 默认
0 库存是正常现象(v4.2 spec 无库存字段),不当失败——库存由 maintenance
的 match_inventory provider 铺(offer 进目录后自动补到保守值;所有者批复
2026-08-12,补"建成即 0 库存永远没人补"的结构洞,旧 inventory_push
因 --no-poll 从未真跑)。

与地基的融合:提交走 api/feeds 唯一通道(三层防重/切片/限速);轮询走
全局 feed_poll;match_submitted + 回执进产品事件账本(上架类=生死事件,
⚠ 跟卖商品无 amz 侧身份,sku≠asin 是账本约定的已登记例外)。

⚠ 真跑前置(对拍未完成前只许 --dry-run):Item 字段形态与 SKU 生成
规则待旧 feed 备份对拍(services/match_feed.py 标注两处)。
"""

import logging
from datetime import datetime

from api import feeds, items as items_api
from registry import db
from services import blacklist, kpi, listing_sources, match_feed, \
    match_sheet, product_events, risk_gate, store_retry, \
    stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.match_listing")


def _gate_reason(spec: dict, gate: dict, banned: dict) -> str | None:
    """输入:SPEC 预检结果 + 两道闸数据 → 输出:拦截原因(None=放行)。

    纯函数便于测试;字段缺失(SPEC 交叉不出 ASIN/品牌)跳过该道闸。
    只按拉黑类别拦(黑名单),不按删除史拦(所有者口径 2026-08-12)。"""
    why = risk_gate.check(gate, spec.get("product_type"), spec.get("brand"))
    if why:
        return f"风控拦截:{why}"
    asin = spec.get("asin")
    if asin and asin in banned:
        cat, src = banned[asin]
        return f"ASIN黑名单:{src}({cat}类)"
    return None


def _precheck(store: dict, code: str, cache: dict) -> dict:
    """输入:店铺 + 商品码 → 输出:SPEC 预检结果(同码进程内缓存)。"""
    cands = match_feed.spec_candidates(code)
    if not cands:
        return {"status": "码无效", "spec": None}
    key = cands[0][1]
    if key in cache:
        return cache[key]
    result = {"status": "预检失败", "spec": None}
    for param, value in cands:
        try:
            spec = items_api.search_walmart_spec(store, **{param: value})
        except Exception as e:
            logger.warning("SPEC 预检异常(%s=%s): %s", param, value, e)
            result = {"status": f"预检失败:{e}", "spec": None}
            continue
        if spec["feed_type"] == "MP_ITEM_MATCH":
            result = {"status": "可跟卖", "spec": spec}
            break
        if spec["feed_type"] == "MP_ITEM":
            result = {"status": "需完整建品", "spec": spec}
            break
        result = {"status": "目录无", "spec": None}
    cache[key] = result
    return result


def run(params: dict) -> str:
    """输入:params(execute/store)→ 输出:预检与提交摘要。"""
    execute = bool(params.get("execute"))
    rows = match_sheet.read_rows()
    if not rows:
        return "跟卖表无数据行"
    if params.get("store"):
        rows = [r for r in rows if r["store"] == params["store"]]

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    now = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d %H:%M")
    mode = "" if execute else "🧪 [DRY-RUN] "

    # 行分拣:在途(反哺器管)/终态/待处理(F 空或=可跟卖 且 I 空)。
    # 待处理是**包含式白名单**,白名单之外的 F 值一律隐式终态(不重复预检/
    # 提交;运营清空 F 即重新排队)。
    # "预检失败"**不是终态**(2026-08-12 旧仓对照纠正:那多半是 SPEC 接口网络
    # 抖动,旧系统每次跑批全新 xlsx 自然重试;当终态会把行永久停摆)——
    # 每轮自动重新预检,持续失败的行留在表上反复出现即是信号
    inflight = [r for r in rows if r["feed_id"]]
    todo = [r for r in rows if not r["feed_id"]
            and (r["status"] in ("", "可跟卖")
                 or r["status"].startswith("预检失败"))]
    lines = [f"{mode}跟卖表 {len(rows)} 行:待处理 {len(todo)},"
             f"在途/已提交 {len(inflight)},终态 "
             f"{len(rows) - len(todo) - len(inflight)}"]
    if not todo:
        return "\n".join(lines)

    updates: list[tuple[int, list]] = []
    spec_cache: dict = {}
    by_store: dict[str, list[tuple[dict, dict]]] = {}   # 店铺 → [(行, item)]

    # SKU 自动编号起点(B 列留空的行用;人工填了 B 的行不占号)
    # + 两道闸数据每轮加载一次,逐行零查询(与 list_new 同款)
    date_str = datetime.now(kpi.CN_TZ).strftime("%Y%m%d")
    with db.pg_conn() as conn:
        serial = match_feed.next_serial_start(conn, date_str)
        gate = risk_gate.load_gate(conn)
        banned = blacklist.load_banned_asins(conn)

    unknown_stores = sorted({r["store"] for r in todo
                             if r["store"] not in stores_by_name})
    if unknown_stores:
        logger.warning("店铺不识别 %d 个:表内样本=%s;凭证表样本=%s"
                       "(店名须与凭证表逐字一致,且该店 启用=是、ClientId "
                       "与代理三件套非空)", len(unknown_stores),
                       unknown_stores[:5], sorted(stores_by_name)[:5])

    for r in todo:
        store = stores_by_name.get(r["store"])
        if store is None:
            r["status"] = "店铺不识别"
            updates.append((r["rownum"], match_sheet.row_vals(r)))
            continue
        pre = _precheck(store, r["upc"], spec_cache)
        r["status"] = pre["status"]
        if pre["status"] != "可跟卖":
            updates.append((r["rownum"], match_sheet.row_vals(r)))
            lines.append(f"  第{r['rownum']}行 {r['upc']}:{pre['status']}")
            continue
        spec = pre["spec"]
        why = _gate_reason(spec, gate, banned)
        if why:
            r["status"] = why[:60]      # F 列终态,运营核对后清 F 重排队
            updates.append((r["rownum"], match_sheet.row_vals(r)))
            lines.append(f"  第{r['rownum']}行 {r['upc']}:{why}")
            continue
        r["gtin"] = spec["product_id"] or ""
        if not r["sku"]:        # B 列人工优先,留空自动按旧格式续号
            r["sku"] = match_feed.make_sku(date_str, serial)
            serial += 1
        try:
            item = match_feed.build_match_item(
                spec["raw"], r["sku"], r["price"], r["weight"],
                product_id=spec["product_id"],
                product_id_type=spec["product_id_type"])
        except (TypeError, ValueError) as e:
            r["status"] = f"数据无效:{e}"     # 售价/重量填的不是数
            updates.append((r["rownum"], match_sheet.row_vals(r)))
            continue
        by_store.setdefault(r["store"], []).append((r, item))

    n_ok = sum(len(v) for v in by_store.values())
    lines.append(f"预检通过可跟卖 {n_ok} 行,涉及 {len(by_store)} 店")

    if not execute:
        for store_name, pairs in sorted(by_store.items()):
            sample = [(r["upc"], r["sku"], r["price"]) for r, _ in pairs[:5]]
            lines.append(f"  [DRY-RUN] {store_name} 将提交 {len(pairs)} 条:{sample}")
        if by_store:
            # 对拍窗口:打印首条完整 Item,与旧系统 match_*.json 备份比对
            first = next(iter(by_store.values()))[0][1]
            lines.append(f"  [DRY-RUN] 首条 Item 载荷(对拍用):{first}")
        match_sheet.write_rows(updates, execute=False)
        return "\n".join(lines)

    for store_name, pairs in sorted(by_store.items()):
        store = stores_by_name[store_name]
        try:    # 单店隔离(与 cleanup/maintenance 同款纪律)
            i = 0
            n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
            entries = [item for _, item in pairs]
            for res in feeds.submit_feed(store, "MP_ITEM_MATCH", entries,
                                         workflow="match_listing"):
                batch = pairs[i:i + res["count"]]
                i += res["count"]
                n[res["outcome"]] = n.get(res["outcome"], 0) + len(batch)
                if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                    for r, _ in batch:
                        r["feed_id"], r["list_time"] = res["feed_id"], now
                        r["feed_result"] = "处理中"
                        updates.append((r["rownum"], match_sheet.row_vals(r)))
                    if res["outcome"] == "submitted":
                        with db.pg_conn() as conn:
                            product_events.record_many(conn, [
                                {"sku": r["sku"], "store": store_name,
                                 "event": product_events.MATCH_SUBMITTED,
                                 "source": "match_listing",
                                 "detail": {"feed_id": res["feed_id"],
                                            "upc": r["upc"],
                                            "price": r["price"]}}
                                for r, _ in batch])
                            # 来源登记簿(所有者定稿):跟卖品 sku≠asin,
                            # 登记出身后 amz 驱动的自动流程不会误伤它们
                            listing_sources.register(conn, [
                                {"store": store_name, "sku": r["sku"],
                                 "source_type": listing_sources.SOURCE_MATCH,
                                 "source_key": r["gtin"] or r["upc"],
                                 "workflow": "match_listing"}
                                for r, _ in batch])
                elif res["outcome"] == "failed":
                    for r, _ in batch:
                        r["feed_result"] = "提交被拒"
                        updates.append((r["rownum"], match_sheet.row_vals(r)))
            line = f"  {store_name}:跟卖提交 {n['submitted']}"
            if n["dedup"]:
                line += f",在途防重跳过 {n['dedup']}"
            if n["failed"]:
                line += f",⚠ 提交被拒 {n['failed']}(查日志)"
            if n["unknown"]:
                line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
            lines.append(line)
        except Exception as e:
            logger.exception("店铺 %s 跟卖提交异常,跳过继续其它店: %s",
                             store_name, e)
            lines.append(f"  ⚠ {store_name}:提交异常已跳过"
                         f"({store_retry.diagnose(e)}:{e}),下轮重试")

    written = match_sheet.write_rows(updates)
    lines.append(f"回写 {written} 行;feed 结果轮询走 feed_poll")
    return "\n".join(lines)
