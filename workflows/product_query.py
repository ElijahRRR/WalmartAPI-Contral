"""product_query — 产品 ID 查询产品详情(替代旧 产品ID查询产品详情/query_product_detail.py)。

用法:
  python cli.py product_query -p ids="B0ABCD1234,036000291452,水杯"
  python cli.py product_query -p file=/path/to/ids.txt          # 每行一个
  python cli.py product_query -p ids=... -p store=A085          # 指定用哪家店的 token
  python cli.py product_query -p mode=catalog -p field=sku -p ids=SKU1,SKU2   # 查本店目录

零状态零调度,全部只读(蓝图矩阵 #5/#6)。输入 ID 自动分类:
  B0 开头 10 位 → ASIN(走 SPEC);8-14 位纯数字 → UPC/GTIN(DEFAULT+SPEC,
  含丢前导 0 的 zfill 候选轮试);其余 → 关键词(仅 DEFAULT)。
结果落 <DATA_ROOT>/exports/product_query_<时间戳>.csv,摘要给出命中统计。
公开目录端点与店铺无关,缺省用凭证表第一家有效店铺的 token。
"""

import csv
import logging
import re
from datetime import datetime

from api import items
from registry import paths
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.product_query")

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")

_CSV_COLS = ["input", "kind", "found", "item_id", "title", "brand", "product_type",
             "price", "rating", "reviews", "marketplace", "spec_feed_type",
             "spec_product_id", "spec_product_id_type", "spec_asin", "error"]


def _classify(value: str) -> str:
    """输入:一个查询值 → 输出:'asin' / 'code'(UPC/GTIN)/ 'keyword'。"""
    v = value.strip()
    if _ASIN_RE.match(v.upper()):
        return "asin"
    if v.isdigit() and 8 <= len(v) <= 14:
        return "code"
    return "keyword"


def _code_candidates(v: str) -> list[tuple[str, str]]:
    """输入:数字码 → 输出:[(参数名, 值)] 候选序列(处理 Excel 丢前导 0,命中即停)。"""
    cands = []
    if len(v) == 12:
        cands = [("upc", v), ("gtin", v.zfill(14))]
    elif len(v) in (13, 14):
        cands = [("gtin", v.zfill(14))]
    else:  # 8-11 位:大概率丢了前导 0
        cands = [("upc", v.zfill(12)), ("gtin", v.zfill(14))]
    return cands


def _read_ids(params: dict) -> list[str]:
    raw = []
    if params.get("file"):
        with open(params["file"], encoding="utf-8") as f:
            raw = [line.strip() for line in f]
    elif params.get("ids"):
        raw = re.split(r"[,\s]+", str(params["ids"]))
    ids, seen = [], set()
    for v in raw:
        if v and v not in seen:
            seen.add(v)
            ids.append(v)
    return ids


def _query_one_global(store: dict, value: str) -> dict:
    """输入:店铺 + 单个查询值 → 输出:一行结果 dict(全站 DEFAULT + 按需 SPEC)。"""
    kind = _classify(value)
    row = {c: None for c in _CSV_COLS}
    row.update(input=value, kind=kind, found=False)
    try:
        if kind == "asin":
            spec = items.search_walmart_spec(store, asin=value.upper())
            _fill_spec(row, spec)
            row["found"] = spec["feed_type"] is not None
        elif kind == "code":
            hit = []
            for pname, pval in _code_candidates(value):
                hit = items.search_walmart(store, **{pname: pval})
                if hit:
                    break
            if hit:
                row.update({k: v for k, v in items.summarize_search_item(hit[0]).items()
                            if k in row})
                row["found"] = True
            # SPEC 路由补充(能否跟卖/规范化标识)
            for pname, pval in _code_candidates(value):
                spec = items.search_walmart_spec(store, **{pname: pval})
                if spec["feed_type"] is not None:
                    _fill_spec(row, spec)
                    row["found"] = True
                    break
        else:  # keyword
            hit = items.search_walmart(store, query=value)
            if hit:
                row.update({k: v for k, v in items.summarize_search_item(hit[0]).items()
                            if k in row})
                row["found"] = True
    except Exception as e:
        row["error"] = str(e)
        logger.warning("查询失败 %s: %s", value, e)
    return row


def _fill_spec(row: dict, spec: dict) -> None:
    row["spec_feed_type"] = spec["feed_type"]
    row["spec_product_id"] = spec["product_id"]
    row["spec_product_id_type"] = spec["product_id_type"]
    row["spec_asin"] = spec["asin"]
    row["title"] = row["title"] or spec["title"]
    row["product_type"] = row["product_type"] or spec["product_type"]


def _query_one_catalog(store: dict, field: str, value: str) -> dict:
    row = {c: None for c in _CSV_COLS}
    row.update(input=value, kind=f"catalog:{field}", found=False)
    try:
        hits = items.catalog_search(store, field, value)
        if hits:
            h = hits[0]
            price = h.get("price") or {}
            row.update(found=True, item_id=h.get("itemId") or h.get("wpid"),
                       title=h.get("productName"), brand=h.get("brand"),
                       product_type=h.get("productType"), price=price.get("amount"))
    except Exception as e:
        row["error"] = str(e)
        logger.warning("目录查询失败 %s=%s: %s", field, value, e)
    return row


def run(params: dict) -> str:
    """输入:params(ids/file、可选 store、mode=catalog+field)→ 输出:命中统计与导出路径。"""
    ids = _read_ids(params)
    if not ids:
        return "无输入:用 -p ids=值1,值2 或 -p file=路径 提供查询值"

    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return "无可用店铺凭证(公开目录查询也需要任一店铺的 token)"
    store = store_list[0]

    mode = params.get("mode", "global")
    if mode == "catalog":
        field = params.get("field", "sku")
        rows = [_query_one_catalog(store, field, v) for v in ids]
    else:
        rows = [_query_one_global(store, v) for v in ids]

    out_dir = paths.data_root() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"product_query_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        w.writerows(rows)

    found = sum(1 for r in rows if r["found"])
    errors = sum(1 for r in rows if r["error"])
    matchable = sum(1 for r in rows if r["spec_feed_type"] == "MP_ITEM_MATCH")
    lines = [f"product_query:{found}/{len(rows)} 命中(店铺 token:{store['name']}),"
             f"可跟卖 {matchable} 个,失败 {errors} 个",
             f"明细已导出:{out}"]
    return "\n".join(lines)
