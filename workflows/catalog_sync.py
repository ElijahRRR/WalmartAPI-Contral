"""catalog_sync — 沃尔玛在线商品全量同步(替代旧 tools/sync_online_products.py 的沃尔玛侧)。

用法:
  python cli.py catalog_sync                    # 全部启用店铺
  python cli.py catalog_sync -p store=A085朱丽霖  # 单店
  python cli.py catalog_sync -p workers=8       # 跨店并发(默认 4)
  python cli.py catalog_sync -p skip_inventory=1  # 只同步商品目录,跳过库存合并
  python cli.py catalog_sync -p rounds=fast     # 实验:无参全量(300/min)+RETIRED 两轮
                                                # 需先与默认 full(5 轮)对拍数量一致再采用

每店流程:GET /v3/items 5 轮全量扫店(去重)→ offset 截断时用 PG 已知 SKU 单查补漏
→ GET /v3/inventories 合并可售数量 → upsert catalog.walmart_items → 标记本轮缺席行。
凭证失效(StoreDeadError)跳过全店并计入摘要,不中断其它店铺。

本期范围(2026-08-05 决策):只做沃尔玛侧,不拉采集服务数据(增量导出契约未定稿,
见 docs/scraper_migration_brief.md 第六条)。同步完成后把 PG 全量投影回写到
**新建的飞书电子表格**「在线产品总表」(非多维表格——13 万行超 bitable 5 万行套餐上限;
新表与旧系统写的旧 spreadsheet 互不干扰,可并跑对拍)。PG 是权威,飞书表可随时整表重建。
-p skip_feishu=1 跳过回写;表格未在 .env 登记时跳过并在摘要中提示。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from api import _client, feishu, inventory as inv_api, items
from registry import db, resources
from services import stores as stores_svc, walmart_catalog

DANGEROUS = False

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

    inventory: dict[str, int] = {}
    if not skip_inventory:
        inventory = inv_api.list_inventories(store)

    rows = walmart_catalog.merge_rows(name, summaries, inventory, run_at)
    with db.pg_conn() as conn:
        written = walmart_catalog.upsert_items(conn, rows)
        missing = walmart_catalog.mark_missing(conn, name, run_at)
    return {"store": name, "fetched": stats.get("total", 0), "written": written,
            "missing": missing, "truncated": bool(stats.get("truncated")),
            "filled": filled, "inv": len(inventory)}


def run(params: dict) -> str:
    """输入:params(可选 store/workers/skip_inventory)→ 输出:各店同步统计摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
    workers = int(params.get("workers", 4))
    skip_inventory = str(params.get("skip_inventory", "")) in ("1", "true", "yes")
    mode = str(params.get("rounds", "full"))
    if mode not in ("full", "fast"):
        return f"rounds 参数只接受 full/fast,收到:{mode}"
    run_at = datetime.now(timezone.utc)

    results, dead, failed = [], [], []
    with ThreadPoolExecutor(max_workers=min(workers, len(store_list))) as pool:
        futures = {pool.submit(_sync_one_store, s, run_at, skip_inventory, mode): s["name"]
                   for s in store_list}
        for f in as_completed(futures):
            name = futures[f]
            try:
                results.append(f.result())
            except _client.StoreDeadError as e:
                logger.error("%s", e)
                dead.append(name)
            except Exception as e:
                logger.exception("店铺 %s 同步失败: %s", name, e)
                failed.append(f"{name}({e})")

    total_written = sum(r["written"] for r in results)
    total_missing = sum(r["missing"] for r in results)
    truncated = [r["store"] for r in results if r["truncated"]]
    lines = [f"catalog_sync:{len(results)}/{len(store_list)} 店完成,"
             f"入库 {total_written} 行,本轮缺席标记 {total_missing} 行"]
    if truncated:
        lines.append(f"offset 截断已补漏:{','.join(truncated)}")
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")

    if results and str(params.get("skip_feishu", "")) not in ("1", "true", "yes"):
        lines.append(_write_projection())   # 全部店铺失败时不动飞书表

    if failed:
        lines.append(f"失败:{'; '.join(failed)}")
        raise RuntimeError("\n".join(lines))   # 有店失败 → 整体判失败,飞书通知会带明细
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
