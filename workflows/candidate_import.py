"""candidate_import — 旧采集器 v3 存量导出 → catalog.candidate_pool(一次性)。

用法:
  python cli.py candidate_import -p file=/path/v3_export.csv            # 预览
  python cli.py candidate_import -p file=/path/v3_export.csv -p apply=1 # 入库

数据来源(Q2 拍板 2026-08-12,docs/allocation_plan.md §十一.1):
  v3 采集器(amazon-scraper-v3)的流式导出,推荐只导需要的列:
    GET /api/export/all?format=csv&fields=asin,title,brand,category_tree,\
rating,review_count,current_price,buybox_price,is_fba,stock_status,\
seller_name,crawl_time
  表头是 v3 的中文映射(其 common/config.py HEADER_MAP),本文件按**精确表头**
  取列——对不上说明导出参数不对或 v3 改了表头,预览会把真实表头打出来。

三条边界纪律:
  1. 候选池只承担「名单 + 粗筛字段」,保鲜/定价一律走 v4 增量
     (所有者已定:全量 ASIN 新采进 v4)。本表任何字段不做业务判定输入。
  2. "N/A"/空串 一律落 NULL(v3 全字段 text,缺失填 N/A);数值解析失败
     也落 NULL——**禁止 or 0**(与快照三态同一条铁律)。
  3. ON CONFLICT (asin) DO NOTHING:重复行/重复执行天然幂等,不覆盖已有行。
"""

import contextlib
import csv
import logging
import re
from pathlib import Path

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.candidate_import")

_BATCH = 5000

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# 逻辑列 → v3 导出中文表头(精确匹配;asin 必需,其余缺列=该字段整列 NULL)
_HEADERS = {
    "asin": "ASIN (商品ID)",
    "title": "商品标题",
    "brand": "品牌",
    "category_tree": "类目路径树",
    "rating": "商品评分",
    "review_count": "评论数",
    "current_price": "当前价格",
    "buybox_price": "BuyBox 价格",
    "channel": "是否 FBA 发货",
    "stock_status": "库存状态",
    "seller_name": "卖家店铺名",
    "crawl_time": "商品采集时间",
}

_INSERT_SQL = """
INSERT INTO catalog.candidate_pool
    (asin, title, brand, category_tree, category_root, rating, review_count,
     current_price, buybox_price, channel, stock_status, seller_name,
     crawl_time, source)
VALUES (%(asin)s, %(title)s, %(brand)s, %(category_tree)s, %(category_root)s,
        %(rating)s, %(review_count)s, %(current_price)s, %(buybox_price)s,
        %(channel)s, %(stock_status)s, %(seller_name)s, %(crawl_time)s, 'v3')
ON CONFLICT (asin) DO NOTHING
"""

_NULLS = {"", "N/A", "NA", "None", "null", "NULL"}


def _clean(v) -> str | None:
    s = str(v or "").strip()
    return None if s in _NULLS else s


def _num(v) -> float | None:
    s = _clean(v)
    if s is None:
        return None
    try:
        return float(s.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _channel(v) -> str | None:
    s = (_clean(v) or "").upper()
    return s if s in {"FBA", "FBM"} else None


def _parse_row(rec: dict, present: dict) -> dict | None:
    """输入:csv 行 dict + 就位表头映射 → 输出:入库行;asin 非法返回 None。"""
    def g(field):
        h = present.get(field)
        return rec.get(h) if h else None

    asin = (str(g("asin") or "")).strip().upper()
    if not _ASIN_RE.match(asin):
        return None
    tree = _clean(g("category_tree"))
    return {
        "asin": asin,
        "title": _clean(g("title")),
        "brand": _clean(g("brand")),
        "category_tree": tree,
        "category_root": tree.split(" > ")[0].strip() if tree else None,
        "rating": _num(g("rating")),
        "review_count": _int(g("review_count")),
        "current_price": _num(g("current_price")),
        "buybox_price": _num(g("buybox_price")),
        "channel": _channel(g("channel")),
        "stock_status": _clean(g("stock_status")),
        "seller_name": _clean(g("seller_name")),
        "crawl_time": _clean(g("crawl_time")),
    }


def run(params: dict) -> str:
    """输入:params(file 必填,apply 可选)→ 输出:预览或导入摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    path = str(params.get("file", "")).strip()
    if not path:
        return "⛔ 缺 -p file=<v3 导出 csv 路径>(导出方式见本文件 docstring)"
    f = Path(path)
    if not f.is_file():
        return f"⛔ 文件不存在:{path}"
    if f.suffix.lower() != ".csv":
        return ("⛔ 只收 csv(流式,百万行不吃内存)——v3 导出时用 format=csv;"
                f"给的是 {f.suffix}")

    total = ok = bad_asin = inserted = 0
    sample: list[dict] = []
    batch: list[dict] = []
    # utf-8-sig 兼容带/不带 BOM 两种导出;csv 流式读,百万行不吃内存
    with contextlib.ExitStack() as stack:
        fh = stack.enter_context(f.open(newline="", encoding="utf-8-sig"))
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        present = {k: h for k, h in _HEADERS.items() if h in headers}
        absent = [h for k, h in _HEADERS.items() if k not in present]
        if "asin" not in present:
            return (f"⛔ 找不到必需列「{_HEADERS['asin']}」;实际表头:{headers}"
                    "——确认导出自 v3 /api/export/all 且未改列名")
        conn = stack.enter_context(db.pg_conn()) if apply else None

        def _flush():
            nonlocal inserted
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, batch)
                inserted += max(cur.rowcount, 0)

        for rec in reader:
            total += 1
            row = _parse_row(rec, present)
            if row is None:
                bad_asin += 1
                continue
            ok += 1
            if len(sample) < 3:
                sample.append(row)
            if not apply:
                continue
            batch.append(row)
            if len(batch) >= _BATCH:
                _flush()
                batch = []
        if apply and batch:
            _flush()

    lines = [f"读 {total} 行,ASIN 合法 {ok},非法跳过 {bad_asin}"]
    if absent:
        lines.append(f"缺列(整列置 NULL):{absent}")
    if not apply:
        if sample:
            s = sample[0]
            lines.append(f"样例:{s['asin']} | {s['brand']} | {s['category_root']}"
                         f" | 评分 {s['rating']} | 评论 {s['review_count']}")
        lines.append("预览完毕;-p apply=1 入库(ON CONFLICT DO NOTHING 幂等)")
    else:
        lines.append(f"入库 {inserted} 行(已存在跳过 {ok - inserted})")
    return ";".join(lines)
