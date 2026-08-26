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
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


from api import _client, feishu, inventory as inv_api, items, reports
from registry import db, resources
from services import product_events, store_retry, stores as stores_svc, \
    walmart_catalog

DANGEROUS = False
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

logger = logging.getLogger("workflows.catalog_sync")

_FILL_WORKERS = 8   # 补漏单查并发上限(items.get 桶 800/min,蓝图定稿 ≤8 并发)


def _sync_one_store(store: dict, run_at, skip_inventory: bool, mode: str,
                    backfill_ids: bool) -> dict:
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

    inventory: dict[str, int] = {}
    inv_failed = False
    if not skip_inventory:
        try:
            inventory = inv_api.list_inventories(store)
        except _client.StoreDeadError:
            raise                       # 凭证失效仍按跳店处理
        except Exception as e:
            # 库存失败不弃店:商品目录照常入库,avail_qty 由 COALESCE 保留上一轮值
            logger.warning("店铺 %s 库存拉取失败,本轮沿用旧库存值: %s", name, e)
            inv_failed = True

    rows = walmart_catalog.merge_rows(name, summaries, inventory, run_at)
    with db.pg_conn() as conn:
        written = walmart_catalog.upsert_items(conn, rows)
        missing = walmart_catalog.mark_missing(conn, name, run_at)

    backfilled = _backfill_item_ids(store) if backfill_ids else 0
    return {"store": name, "fetched": stats.get("total", 0), "written": written,
            "missing": missing, "truncated": bool(stats.get("truncated")),
            "filled": filled, "inv": len(inventory), "inv_failed": inv_failed,
            "item_ids": backfilled}


def _backfill_item_ids(store: dict) -> int:
    """输入:店铺 → 输出:本次回填的 item_id 数量。

    来源 = On-request ITEM 报表(一店一份,覆盖全部商品):从 Item ID 列或
    Item Page URL 提取数字 itemId。其余候选路径全部实证排除——GET /v3/items
    与 catalog/search 响应无此字段,全站搜索按 gtin/upc 召回率 3/131。
    itemId 平时不变,只有新品和"缺席后复现"(upsert 已重置 NULL)的行触发报表拉取;
    报表失败只记警告,下轮重试,不影响店铺同步结果。
    """
    name = store["name"]
    with db.pg_conn() as conn:
        todo = walmart_catalog.skus_missing_item_id(conn, name)
    if not todo:
        return 0
    try:
        rows = reports.fetch_item_report(store)
    except Exception as e:
        logger.warning("店铺 %s ITEM 报表拉取失败,item_id 本轮不回填: %s", name, e)
        return 0

    found: dict[str, str] = {}
    no_id = 0
    for row in rows:
        sku = reports.report_row_sku(row)
        if not sku or sku not in todo:
            continue
        iid = reports.extract_item_id(row)
        if iid:
            found[sku] = iid
        else:
            no_id += 1
    if no_id and not found:     # 一个都提不出来 = 列名/URL 格式假设错了,打样本诊断
        logger.warning("店铺 %s 报表 %d 行均提取不到 itemId,首行字段:%s",
                       name, len(rows), sorted(rows[0].keys()) if rows else [])
    with db.pg_conn() as conn:
        updated = walmart_catalog.set_item_ids(conn, name, found)
    logger.info("店铺 %s item_id 报表回填:待补 %d / 报表行 %d / 提取成功 %d / 入库 %d",
                name, len(todo), len(rows), len(found), updated)
    return updated


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
    # item_id 报表回填默认关闭(2026-08-05 决策:报表请求配额极低,单店当日个位数,
    # 测试期即打到 429;功能保留,-p item_ids=1 显式开启,后续迭代再转正)
    backfill_ids = str(params.get("item_ids", "")) in ("1", "true", "yes")
    run_at = datetime.now(timezone.utc)

    results, dead, to_retry = [], [], []
    by_name = {s["name"]: s for s in store_list}
    with ThreadPoolExecutor(max_workers=min(workers, len(store_list))) as pool:
        futures = {pool.submit(_sync_one_store, s, run_at, skip_inventory, mode,
                               backfill_ids): s["name"]
                   for s in store_list}
        for f in as_completed(futures):
            name = futures[f]
            try:
                results.append(f.result())
            except _client.StoreDeadError as e:
                # 凭证死是确定性的:跳过全店,不补试(重试只会再死一次)
                logger.error("%s", e)
                dead.append(name)
            except Exception as e:
                # 代理故障(StoreProxyError/socksio)与泛化异常都先收着 ——
                # 标准①(所有者定稿 2026-08-26):跑完别人再串行补试,
                # 此刻不判生死。08-26 13:00 的事故正是在这里把两家店的
                # SOCKS 报错直接判成 failed → 整轮 raise → 八步链全停
                logger.exception("店铺 %s 同步失败(待串行补试): %s", name, e)
                to_retry.append((by_name[name], e))

    # 标准①:失败店串行补试一遍。单一落地路径 —— 补试跑的就是第一轮
    # 同一个 _sync_one_store,不另写简化版
    absent: list[tuple[str, str]] = []          # (店名, 归类词:代理/其他/凭证)
    gate_note = ""
    if to_retry:
        recovered, still, gate_note = store_retry.serial_second_pass(
            to_retry, lambda s: _sync_one_store(s, run_at, skip_inventory,
                                                mode, backfill_ids),
            total_stores=len(store_list))
        results.extend(r for _s, r in recovered)
        for s, e in still:
            cls = store_retry.classify(e)
            if cls == "凭证":                    # 补试中才暴露的凭证死,归 dead 口径
                dead.append(s["name"])
            else:
                absent.append((s["name"], cls))

    total_written = sum(r["written"] for r in results)
    total_missing = sum(r["missing"] for r in results)
    total_item_ids = sum(r.get("item_ids", 0) for r in results)
    truncated = [r["store"] for r in results if r["truncated"]]
    # ⚠ 首行 = 结论 + 最要紧的数,且链通知(product_chain)对成功步骤**只发
    # 首行**(cli first_line_of)—— 缺席店必须写在这一行,放后面等于只写日志
    lines = [f"catalog_sync:{len(results)}/{len(store_list)} 店完成,"
             f"入库 {total_written} 行,本轮缺席标记 {total_missing} 行,"
             f"回填 item_id {total_item_ids} 个"
             + (f";⚠ 缺席 {len(absent)} 店:"
                + ",".join(f"{n}({c})" for n, c in absent)
                + ("——超补试规模闸未补试(疑似系统性故障)"
                   if gate_note else
                   "——已串行补试仍失败")
                + ",本轮不炸链(下游按水位避让,链尾重赛)"
                if absent else "")]
    if gate_note:
        lines.append(gate_note)
    if truncated:
        lines.append(f"offset 截断已补漏:{','.join(truncated)}")
    inv_failed = [r["store"] for r in results if r.get("inv_failed")]
    if inv_failed:
        lines.append(f"库存拉取失败(沿用旧值,目录已更新):{','.join(inv_failed)}")
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")

    if results:
        # 删除核验(事件账本):回执成功的删除,以本轮观测定生效/未生效
        with db.pg_conn() as conn:
            verified, not_eff = product_events.verify_deletions(conn)
        if verified or not_eff:
            lines.append(f"删除核验:生效 {verified}"
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
