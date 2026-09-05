"""match_listing — 跟卖上架(listing 子计划 L1,替代旧 match_listing/;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py match_listing --dry-run          # 空跑:预检+打印将提交什么
  python cli.py match_listing                    # 真跑(提交 feed + 回写表格)
  python cli.py match_listing -p store=A085朱丽霖

驱动表(registry.MATCH_SHEET「跟卖表」,所有者定稿 2026-08-07 单路飞书读,
替代旧 xlsx 输入):运营填 A=UPC C=售价 D=重量 E=店铺;B=SKU 人工优先
(旧系统习惯:人工编号),**留空则由 services/sku_codec.mint 抽 12 位不透明码**
(2026-09-02 批次 2;旧的 PHUMWMT+日期+序号生成器已删,SKU 里不再带上架日期);
脚本填其余。发码与人工号登记都在**提交前**的短事务里做完(先落库再调接口),
提交成功后不再登记。

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

⚠ 真跑前置(对拍未完成前只许 --dry-run):Item 字段形态待旧 feed 备份对拍
(services/match_feed.py 标注)。SKU 形态已定稿为不透明码,不再对拍旧编号。
"""

import logging
from datetime import datetime

from api import feeds, items as items_api
from registry import db
from services import blacklist, kpi, listing_sources, match_feed, \
    match_sheet, notify_fmt as nf, product_events, risk_gate, sku_codec, \
    store_events, store_retry, stores as stores_svc

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

    # 两道闸数据每轮加载一次,逐行零查询(与 list_new 同款)
    with db.pg_conn() as conn:
        gate = risk_gate.load_gate(conn)
        banned = blacklist.load_banned_asins(conn)

    unknown_stores = sorted({r["store"] for r in todo
                             if r["store"] not in stores_by_name})
    if unknown_stores:
        logger.warning("店铺不识别 %d 个:表内样本=%s;凭证表样本=%s"
                       "(店名须与凭证表逐字一致,且该店 启用=是、ClientId "
                       "与代理三件套非空)", len(unknown_stores),
                       unknown_stores[:5], sorted(stores_by_name)[:5])

    # ── 第一趟:逐行 SPEC 预检 + 两道闸。**这一趟绝不开 PG 事务** ──────────
    # 每行 `_precheck` 都是一次沃尔玛 SPEC 接口调用(固定出口代理 + 速率桶 +
    # 退避)。几百行就是几百次网络往返;把它们吊在一个事务里,mint 在登记簿上
    # 留的行锁要到整轮结束才释放,与 list_new 的 mint 互相等锁 —— PG 上典型的
    # 长事务坏味道。
    ready: list[dict] = []
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
        r["_spec"] = spec
        ready.append(r)

    # ── 第二趟:**短事务**里发码与登记(纯数据库操作,零网络),退出即 commit,
    #    仍在 submit_feed 之前 ⇒「防重状态先落库再调接口」成立 ────────────────
    # B 列人工号优先(旧系统习惯)。人工号在**这里**就 register 进登记簿,而不
    # 是等提交成功:登记是「这个串归谁」的事实,与提交成不成功无关;提交成功
    # 才登记会让被拒的人工号成为维护链眼里的孤儿(source_type 路由不到,落进
    # unknown,而 unknown 不参与任何自动动作 = 这批货永久退出自动化)。
    # B 列留空 → sku_codec.mint 抽不透明码(旧 make_sku 是「日期 + 当日序号」,
    # 把上架日期写进 SKU,与货源隐匿目标直接冲突;而且每轮重发取新序号 ⇒ 载荷
    # 漂 ⇒ payload_key 防重失效。mint 对同 (店, 来源, GTIN) 复用活码,失败行
    # 下一轮拿到的正是同一个码 —— 这条护栏跟卖侧此前根本没有)。
    if ready and execute:
        with db.pg_conn() as conn:
            for r in ready:
                key = r["gtin"] or r["upc"]
                if r["sku"]:
                    listing_sources.register(conn, [
                        {"store": r["store"], "sku": r["sku"],
                         "source_type": listing_sources.SOURCE_MATCH,
                         "source_key": key, "workflow": "match_listing"}])
                else:
                    r["sku"] = sku_codec.mint(
                        conn, r["store"], listing_sources.SOURCE_MATCH, key,
                        workflow="match_listing")
    elif ready:
        # 空跑:不发码不登记(mint 是写库函数,没有"这次不写"模式),
        # 载荷里放占位码 —— 12 位但含 0,is_opaque 恒 False,永远不会被当成真码
        for r in ready:
            r["sku"] = r["sku"] or sku_codec.DRYRUN_PLACEHOLDER

    for r in ready:
        spec = r["_spec"]
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

    round_cnt: dict[str, dict] = {}     # 各店本轮四档计数(店铺事件账本)

    def _one_store(store_name: str, pairs: list[tuple[dict, dict]]) -> list[str]:
        """输入:店铺名 + 该店 (行, Item) 对 → 输出:该店的摘要行。

        第一轮与串行补试共用**同一条**路径(单一落地纪律):补试跑的就是本
        函数,不另写简化版。二次提交的安全性由 api/feeds 的 payload_key 在途
        防重看护 —— 首轮已发出去的那几片回 dedup(不重发、不落事件),表格
        那几行按同值再写一遍,写表幂等。
        ⚠ 回写攒在**外层共享** `updates` 上(逐片就地追加,店间串行不打架):
        抛异常时前几片的 I/J 列照样进 write_rows —— 表上有 feed_id 的行下一轮
        不会被当「待处理」重新提交(product_clear._one_store 同款判据)。
        """
        store = stores_by_name[store_name]
        n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
        # 账本计数**就地填进外层 round_cnt**、逐片追加(与 updates 同一条纪律):
        # 抛异常时前几片其实已经发出去了,局部计数随异常一丢那几条在账本上
        # 就成了零。摘要那行仍用局部 n —— 补试的叙述覆盖首轮,行里说的是
        # 这一次尝试;账本要的是整轮的和,两个口径故意不同
        bucket = round_cnt.setdefault(store_name, {})
        entries = [item for _, item in pairs]
        # 逐切片结果与 (行, Item) 对位:游标走法是 submit_feed 返回契约的机械
        # 后果,收在 api/feeds.iter_result_slices(错一位 = 整批结局落到别人
        # 行上,而且不报错)
        for res, batch in feeds.iter_result_slices(
                feeds.submit_feed(store, "MP_ITEM_MATCH", entries,
                                  workflow="match_listing"), pairs):
            n[res["outcome"]] = n.get(res["outcome"], 0) + len(batch)
            bucket[res["outcome"]] = bucket.get(res["outcome"], 0) + len(batch)
            if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                for r, _ in batch:
                    r["feed_id"], r["list_time"] = res["feed_id"], now
                    r["feed_result"] = "处理中"
                    updates.append((r["rownum"], match_sheet.row_vals(r)))
                if res["outcome"] == "submitted":
                    # 只记「提交这件事」;**来源登记已在提交前那一趟做完**
                    # (登记只有一个时点、一条实现路径,见上面第二趟的头注)
                    with db.pg_conn() as conn:
                        product_events.record_many(conn, [
                            {"sku": r["sku"], "store": store_name,
                             "event": product_events.MATCH_SUBMITTED,
                             "source": "match_listing",
                             "detail": {"feed_id": res["feed_id"],
                                        "upc": r["upc"],
                                        "price": r["price"]}}
                            for r, _ in batch])
            elif res["outcome"] == "failed":
                for r, _ in batch:
                    r["feed_result"] = "提交被拒"
                    updates.append((r["rownum"], match_sheet.row_vals(r)))
        # 四档计数的尾巴走 notify_fmt 的成品件(三个执行件逐字重复过);
        # failed 档字样由调用方给 —— 本件现行「提交被拒」,
        # problem_product_cleanup 现行「提交失败」,收口时逐字保留两处现状
        return [f"  {store_name}:跟卖"
                + nf.feed_outcome_tail(n["submitted"], n["dedup"],
                                       n["failed"], n["unknown"],
                                       failed_word="提交被拒")]

    todo_stores = sorted(by_store.items())
    per_store: dict[str, list[str]] = {}
    to_retry: list[tuple] = []
    for store_name, pairs in todo_stores:
        try:    # 单店隔离(与 cleanup/maintenance 同款纪律)
            per_store[store_name] = _one_store(store_name, pairs)
        except Exception as e:
            # 异常店也要在账本上留一条:计数可能全 0(第一片就炸),而
            # "这家店这一轮炸了"本身就是要能按时间线对齐的事实
            round_cnt.setdefault(store_name, {})["exception"] = True
            # 店级隔离 → **不当场判生死**:跑完别人再串行补试一遍(标准①,
            # 所有者定稿 2026-08-26)。此前这里只 diagnose 分诊、注释写着
            # 「下轮重试」,补一次的代码没有
            logger.exception("店铺 %s 跟卖提交异常(待串行补试): %s",
                             store_name, e)
            to_retry.append(((store_name, pairs), e))

    absent: list[tuple[str, str]] = []      # (店名, 归类词,唯一出处 diagnose)
    gate_note = ""
    if to_retry:
        # 标准①:失败店**串行**补试一遍(services/store_retry 是唯一实现,
        # 规模闸/退避阶梯/凭证死不补试三条判据都在里面,本文件不再另写)
        recovered, still, gate_note = store_retry.serial_second_pass(
            [({"name": sn, "_pairs": p}, e) for (sn, p), e in to_retry],
            lambda st: _one_store(st["name"], st["_pairs"]),
            total_stores=len(todo_stores))
        for st, out in recovered:
            per_store[st["name"]] = out
        for st, e in still:
            cls = store_retry.diagnose(e)
            absent.append((st["name"], cls))
            per_store[st["name"]] = [
                f"  ⚠ {st['name']}:提交异常已跳过({cls}:{e}),下轮重试"]
    # 按店名合并:补试是跑完别人之后才补的,摘要行序不跟着补试次序走
    for store_name, _ in todo_stores:
        lines.extend(per_store.get(store_name, []))
    if absent:
        # 标准③:缺席不炸整轮,但必须点名在**首行** —— 链通知对成功步骤
        # 只发首行(cli.first_line_of),写在后面等于只写进日志。
        # 尾句是本件的处置:没提交出去的行 I 列仍空,下一轮照样进待处理
        lines[0] += nf.absent_tail(absent, gate_note, tail="未提交行下轮重试")
    if gate_note:
        lines.append(gate_note)
    # 店铺事件账本(运营类):每店每轮一条(本段只在真跑走到 —— dry-run 在
    # write_rows 那里就 return 了)。记账失败只告警,不拖垮跟卖链
    store_events.record_round_safe("match_listing", store_events.MATCH_ROUND,
                                   round_cnt, lines)

    written = match_sheet.write_rows(updates)
    lines.append(f"回写 {written} 行;feed 结果轮询走 feed_poll")
    return "\n".join(lines)
