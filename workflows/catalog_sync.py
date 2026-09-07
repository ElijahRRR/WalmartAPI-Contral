"""catalog_sync — 沃尔玛在线商品全量同步(替代旧 tools/sync_online_products.py 的沃尔玛侧)。

用法:
  python cli.py catalog_sync                    # 全部启用店铺
  python cli.py catalog_sync -p store=A085朱丽霖  # 单店
  python cli.py catalog_sync -p workers=8       # 跨店并发(默认 24,生产实测)
  python cli.py catalog_sync -p skip_inventory=1  # 只同步商品目录,跳过库存合并
  python cli.py catalog_sync -p rounds=full     # 备用:旧式逐状态 5 轮显式扫
                                                # (默认 fast 两轮已实证更快且更全,见 items.py)

每店流程:GET /v3/items 5 轮全量扫店(去重)→ offset 截断时用 PG 已知 SKU 单查补漏
→ GET /v3/inventories 合并可售数量 → upsert catalog.walmart_items → 标记本轮缺席行。
失败处理走店级重试标准(所有者定稿 2026-08-26,CLAUDE.md 工程规范):
凭证失效(StoreDeadError)跳过全店不补试;其余失败店跑完别人后**串行补试
一遍**,仍失败以「⚠ 缺席」点名进摘要首行、**不炸整轮**(零店完成仍失败);
缺席店由下游按水位避让、链尾逐店重赛。

本期范围(2026-08-05 决策):只做沃尔玛侧,不拉采集服务数据(增量导出契约未定稿,
见 docs/scraper_migration_brief.md 第六条)。同步完成后把 PG 全量投影回写到
**新建的飞书电子表格**「在线产品总表」(非多维表格——13 万行超 bitable 5 万行套餐上限;
新表与旧系统写的旧 spreadsheet 互不干扰,可并跑对拍)。PG 是权威,飞书表可随时整表重建。
-p skip_feishu=1 跳过回写;表格未在 .env 登记时跳过并在摘要中提示。

`--dry-run` 的边界(2026-09-06 补,审计缺口 G-1):本工作流 DANGEROUS=False,
cli 恒给 `execute=True`,但 `dry_run` 单独透传进 params。**目录同步本身照常**
(扫店、合并库存、upsert catalog.walmart_items、标缺席、飞书投影 —— 它们是可
重放的快照写入,空跑关掉反而看不出同步结果);空跑只挡住**不可逆的那一段**:
弃码点 1(`sku_codec.abandon` 弃码 + 烧 UPC)以及给它封口的删除核验事件
(`delete_verified` / `delete_not_effective`,写下去下一轮就不再产出这一对)。
空跑时该段只报数,摘要第二行打「🧪 [DRY-RUN] 弃码点跳过:将弃码 N 个」。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


from api import _client, feishu, inventory as inv_api, items
from registry import db, resources
from services import notify_fmt as nf, product_events, sku_codec, \
    store_limits, store_retry, stores as stores_svc, walmart_catalog

DANGEROUS = False
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

logger = logging.getLogger("workflows.catalog_sync")

_FILL_WORKERS = 8   # 补漏单查并发上限(items.get 桶 800/min,蓝图定稿 ≤8 并发)


def _sync_one_store(store: dict, run_at, skip_inventory: bool, mode: str) -> dict:
    """输入:店铺 + 本轮时间 + 扫描模式 → 输出:该店统计 dict(拉取/入库/缺席/截断/补漏)。"""
    name = store["name"]
    stats: dict = {}
    summaries = [items.summarize_item(it)
                 for it in items.iter_all_items(store, stats, mode=mode)]
    logger.info("店铺 %s 扫描(%s)各轮条数:%s,去重后 %d",
                name, mode, stats.get("rounds"), stats.get("total", 0))

    filled = 0
    if stats.get("truncated"):
        seen = {s["sku"] for s in summaries}
        with db.pg_conn() as conn:
            candidates = walmart_catalog.known_skus(conn, name) - seen
        logger.warning("店铺 %s 扫描被 offset 截断,对 PG 已知 %d 个未见 SKU 单查补漏",
                       name, len(candidates))
        with ThreadPoolExecutor(max_workers=_FILL_WORKERS) as pool:
            futures = [pool.submit(items.get_item, store, sku) for sku in sorted(candidates)]
            for f in as_completed(futures):
                item = f.result()
                if item:
                    summaries.append(items.summarize_item(item))
                    filled += 1

    # {sku: {发货节点: 可售数量}} —— 合计与节点数都由 merge_rows 从这一份算
    inventory: dict[str, dict[str, int]] = {}
    inv_failed = False
    if not skip_inventory:
        try:
            # ⚠ **不传 expected_skus,bulk 拉到什么就是什么**(所有者定稿
            # 2026-08-28 撤线,推翻自己 08-26「拍板接上」)。撤的依据是接上后的
            # **第一次生产触发**(08-28 06:42,A109):目录 6,976 - bulk 3,511 =
            # 3,465 个"漏",逐个单查**全 404**,一店多烧 43 分钟 —— 因为 404 的
            # 语义是「库存台账没有这一行」:退市/Stage 死档案永远不会有库存行,
            # 部分**真在线**商品同样没有(所有者实见),单查对两者都问不出新
            # 信息,只烧时长与 inventory 配额。"bulk 没给"≠"翻页漏了",
            # 这正是蓝图 #22 那个假设在生产的证伪。
            # bulk 真漏的行不会丢数:avail_qty 由 upsert 的 COALESCE 沿用
            # 上一轮值(walmart_catalog.py,#93 之前生产一直如此)。
            # api 层的 expected_skus 能力**保留不删**,只撤本调用方的接线。
            # 取节点明细版(多仓批次 1):合计口径与 list_inventories 一致
            # (它就是本函数的求和包装),多出来的节点身份供 upsert_node_inventory
            inventory = inv_api.list_inventory_nodes(store)
        except _client.StoreDeadError:
            raise                       # 凭证失效仍按跳店处理
        except Exception as e:
            # 库存失败不弃店:商品目录照常入库,avail_qty 由 COALESCE 保留上一轮值
            logger.warning("店铺 %s 库存拉取失败,本轮沿用旧库存值: %s", name, e)
            inv_failed = True

    rows = walmart_catalog.merge_rows(name, summaries, inventory, run_at)
    with db.pg_conn() as conn:
        written = walmart_catalog.upsert_items(conn, rows)
        # 分节点明细(多仓批次 1):合计仍在 walmart_items.avail_qty,这里落
        # 每个节点各多少 —— 维护链的"受管仓现值"读它。与 upsert_items 同事务:
        # 合计与明细必须是同一轮观测,分两个事务会出现"合计已更新、明细还是
        # 上一轮"的窗口,而维护链正好在那个窗口里比对就会误判
        walmart_catalog.upsert_node_inventory(conn, name, inventory, run_at)
        missing = walmart_catalog.mark_missing(conn, name, run_at)

    # 多仓探测(批次 0):铺在 2 个及以上发货节点的 SKU 数。谭总12 自建中山仓
    # 之后它不再是 0 —— 摘要按"该店配没配「维护仓库」"分两种措辞(见 run())
    multi = sum(1 for nodes in inventory.values() if len(nodes) > 1)
    return {"store": name, "fetched": stats.get("total", 0), "written": written,
            "missing": missing, "truncated": bool(stats.get("truncated")),
            "filled": filled, "inv": len(inventory), "inv_failed": inv_failed,
            "multi_node": multi}


def run(params: dict) -> str:
    """输入:params(可选 store/workers/skip_inventory)→ 输出:各店同步统计摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
    workers = int(params.get("workers", stores_svc.STORE_WORKERS))    # 2026-08-05 生产实测 24 并发无压力
    skip_inventory = str(params.get("skip_inventory", "")) in ("1", "true", "yes")
    mode = str(params.get("rounds", "fast"))
    if mode not in ("full", "fast"):
        return f"rounds 参数只接受 full/fast,收到:{mode}"
    # item_id 不在这里回填(2026-09-07 归 item_id_sync 独立工作流:报表要等
    # 15–45 分钟、创建每小时一次,挂在每店同步里会把本步拖长一倍;双轨禁止)
    run_at = datetime.now(timezone.utc)

    # 标准①②(所有者定稿 2026-08-26):跨店并发 → 凭证死跳全店不补试 → 其余
    # 失败先收着**不判生死**,跑完别人再串行补试一遍 → 仍失败按 diagnose 归缺席。
    # 骨架与「08-26 13:00 两家店 SOCKS 报错被直接判成 failed → 整轮 raise →
    # 八步链全停」的事故背景都在 services/store_retry.fan_out;补试跑的就是
    # 这里的同一个 _sync_one_store(单一落地路径,不另写简化版)。
    results, dead, absent, gate_note = store_retry.fan_out(
        store_list,
        lambda s: _sync_one_store(s, run_at, skip_inventory, mode),
        workers, log_label="同步")

    total_written = sum(r["written"] for r in results)
    total_missing = sum(r["missing"] for r in results)
    truncated = [r["store"] for r in results if r["truncated"]]
    # ⚠ 首行 = 结论 + 最要紧的数,且链通知(product_chain)对成功步骤**只发
    # 首行**(cli first_line_of)—— 缺席店必须写在这一行,放后面等于只写日志
    lines = [f"catalog_sync:{len(results)}/{len(store_list)} 店完成,"
             f"入库 {total_written} 行,本轮缺席标记 {total_missing} 行"
             + nf.absent_tail(absent, gate_note,
                              tail="下游按水位避让,链尾重赛")]
    if gate_note:
        lines.append(gate_note)
    if truncated:
        lines.append(f"offset 截断已补漏:{','.join(truncated)}")
    inv_failed = [r["store"] for r in results if r.get("inv_failed")]
    if inv_failed:
        lines.append(f"库存拉取失败(沿用旧值,目录已更新):{','.join(inv_failed)}")
    # 多仓探测必须见人(批次 0):多仓一旦发生而没人知道,维护链会按"全节点合计"
    # 比对却只写单个节点 —— 清零永久失效、库存每轮重写、生效永久判未生效,
    # 三条全是静默的(docs/multi_node_plan.md §1)。这一行是它的唯一告警面。
    # ⚠ 措辞按**该店配没配「维护仓库」**分两种(2026-08-31 改):批次 2 之后
    # 配置店的维护链已按受管仓写,再喊"仍按单仓写、库存会漂"是**过时告警**
    # ——它出现在搬仓当天的摘要里,读起来像"改造没生效",正好把人引向反面。
    multi = {r["store"]: r["multi_node"] for r in results if r.get("multi_node")}
    if multi:
        managed = store_limits.maint_nodes()
        done = {s: n for s, n in multi.items() if s in managed}
        todo = {s: n for s, n in multi.items() if s not in managed}
        lines.append(
            f"多发货节点:{sum(multi.values())} 个 SKU 铺在 2 个及以上节点("
            + ",".join(f"{s}×{n}" for s, n in sorted(multi.items())) + ")")
        if done:
            lines.append(
                "  已配「维护仓库」的店按受管仓维护(其余节点自动链不碰):"
                + ",".join(f"{s}={managed[s]}" for s in sorted(done))
                + " —— 旧节点的存量货归 `node_clear` 收尾")
        if todo:
            lines.append(
                f"  ⚠ **未配「维护仓库」的店库存维护会漂**:"
                + ",".join(sorted(todo))
                + "(按全节点合计比对却只写单节点 —— 清零失效/每轮重写/"
                  "生效永判未生效,见 docs/multi_node_plan.md §1)")
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")

    if results:
        # 删除核验(事件账本):回执成功的删除,以本轮观测定生效/未生效
        # ⚠ **弃码点 1 就在这里,只在 delete_verified 落地,不在删除回执成功
        #   那一刻**:「回执成功但后台没删」是所有者实证过的故障模式
        #   (delete_not_effective),按回执弃码 = 下一轮拿新码新 UPC 去上一个
        #   还活着的 item = 同店重复 listing,沃尔玛不会替你拦。
        # ⚠ 弃码与 delete_verified 事件**同一个连接同一事务**:分两个事务会留下
        #   "事件记了、码没弃"的半截状态,而 verify_deletions 的 open_ok CTE 正是
        #   靠 delete_verified 事件封口 —— 下一轮不会再产出这一对,那个码就永远
        #   弃不掉了。
        # ⚠ `--dry-run` 只挡这一段(模块头注「dry-run 的边界」):cli 对
        #   DANGEROUS=False 恒给 execute=True,不读 dry_run 的话空跑会真弃码真烧号,
        #   而且横幅都不打(cli 的 [DRY-RUN] 横幅只对 DANGEROUS 打)。
        #   空跑连 verify_deletions 自己记的 delete_verified / delete_not_effective
        #   也要一起挡:那两条事件写下去,open_ok CTE 就封了口,下一轮不再产出
        #   这一对 —— 事件留下了、码没弃,那个码这辈子弃不掉了。所以走
        #   **同一份判据 + rollback**,不另写一条只读 SQL(第二份判据迟早漂开)。
        dry_run = bool(params.get("dry_run"))
        with db.pg_conn() as conn:
            verified, not_eff, gone_pairs = product_events.verify_deletions(conn)
            if dry_run:
                conn.rollback()     # 事件不落库;abandon 一次都不调
                n_ab = 0
            else:
                n_ab = sum(sku_codec.abandon(conn, s, k,
                                             sku_codec.ABANDON_DELETE_VERIFIED)
                           for s, k in gone_pairs)
        if dry_run:
            lines.insert(1, f"🧪 [DRY-RUN] 弃码点跳过:将弃码 {len(gone_pairs)} 个")
        if verified or not_eff:
            lines.append(("删除核验(空跑未落库):将生效 " if dry_run
                          else "删除核验:生效 ") + str(verified)
                         + (f",弃码 {n_ab}" if n_ab else "")
                         + (f",⚠ 未生效 {not_eff}(回执成功但仍在架,查日志)"
                            if not_eff else ""))

    if results and str(params.get("skip_feishu", "")) not in ("1", "true", "yes"):
        lines.append(_write_projection())   # 全部店铺失败时不动飞书表
        if absent:
            # 投影恒为全表一致快照(单店同步也整表重写),缺席店的行因此是
            # 上一轮的旧值 —— 说出来,别让飞书表静默陈旧
            lines.append(f"  ⚠ 飞书投影中缺席店({','.join(n for n, _ in absent)})"
                         f"仍是上一轮快照")

    # ⚠ 缺席店不再炸整轮(所有者定稿 2026-08-26 标准②;此前 `if failed: raise`
    # 让两家店的代理抖动放倒八步链)。零店闸**保留**:全部店都没跑通意味着
    # 凭证表整体出了问题或换 token 请求形状被改坏,报成功没人会来看,
    # 而目录数据就那么静静地陈旧下去。
    if not results:
        lines.append("⚠ 零店完成 —— 本轮没有同步任何数据")
        raise RuntimeError("\n".join(lines))   # 飞书通知带的是这份全文(cli._err_brief)
    if absent and str(params.get("strict", "")) in ("1", "true", "yes"):
        # strict=1(daily_report 链专用,调度里配 catalog_sync:strict=1):
        # 那条链的语义是「同步失败就不出日报 —— 拿旧数出报不如不出」
        # (registry/schedule.py 定稿),缺席不炸链的新标准会把这道闸静默
        # 降级成"照出,缺席店三列是昨天的数"。给它保留严格口径;
        # product_chain 不配 strict(维护链按店避让,不需要整链陪跪)。
        raise RuntimeError("\n".join(lines + ["⚠ strict 模式:缺席即失败(此链宁可不出产物)"]))
    return "\n".join(lines)


def _write_projection() -> str:
    """输入:无(读 PG 全量)→ 输出:飞书回写结果摘要一行。

    只在全量店铺同步(不论本次跑了几家店)后调用:投影始终是 PG 全表,
    单店同步也会刷新整张飞书表,保证表内永远是一致快照。
    """
    sheet = resources.ONLINE_PRODUCTS_SHEET
    try:
        sheet.require()
    except LookupError as e:
        return f"飞书回写跳过:{e}"
    with db.pg_conn() as conn:
        data_rows = walmart_catalog.projection_rows(conn)
    rows = [list(sheet.columns)] + data_rows
    written = feishu.sheet_overwrite(sheet, rows)
    return f"飞书「{sheet.name}」整表重写 {written - 1} 行(含表头共 {written})"
