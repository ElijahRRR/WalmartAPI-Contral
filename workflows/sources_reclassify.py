"""sources_reclassify — 来源码人工归类导入(一次性人工件;**永不进调度**)。

用法:
  python cli.py sources_reclassify                        # 预览 + 导出待归类清单 csv
  python cli.py sources_reclassify -p out=/绝对/路径.csv  # 导出到指定路径
  python cli.py sources_reclassify -p file=/绝对/路径.csv           # 读回预览,不写库
  python cli.py sources_reclassify -p file=… -p apply=1             # 真改
  python cli.py sources_reclassify -p file=… -p apply=1 -p overwrite=1  # 允许覆盖已有键

背景(所有者 2026-09-03):存量里有一批商品的**来源码不是标准 ASIN** ——
`CMSQ-B0CLCX3Q1Z-169.99` 应为 `B0CLCX3Q1Z`、`B0822D9QQKS59` 应为 `B0822D9QQK`,
「还有很多其他类型」。`sources_backfill` 按设计把这些形态一律登记成
`source_type='unknown'` + `source_key=NULL`,于是它们按路由铁律**被排除在全部
自动维护之外**,也进不了 `sku_migrate` 的候选。这不是 bug(防误伤设计),
但也不该是终局:出身是数据不是代码,人认出来了就该能改。本工作流就是那条
"能改"的通道,写入唯一走 `services/listing_sources.reclassify`
(全仓唯一一条 UPDATE `catalog.listing_sources` 归类两列的路径)。

两保险(与 cleanup_history_import 同款):`DANGEROUS=False`(不发 feed、不碰
沃尔玛)+ **`-p apply=1` 才写库**;`--dry-run` 同样挡写。

⚠ **改完的行从此被自动链管到**:满足 `source_type='amz' AND source_key IS NOT
NULL` 之后,amz 快照驱动的改价 / 清库存 / **删除**对它们第一次生效 —— 盲区变
辖区。纪律与 `sources_backfill` 完全一致:真跑后先
`python cli.py maintenance_scan --dry-run` 看意图量(尤其「建议永久删除」那一
段),人眼确认再放行 13:00 的 `product_chain`。

机器提议只用**已有规则**,不在本文件自写任何形态正则(唯一之家
`services/sku_asin` 与 `services/sku_codec`):三段式走既有提取规则;
「标准 ASIN + 尾巴」形态只标 `guess`,**不自动应用** —— 它与"11~15 位的真源头
码"形态完全相同,机器分不开,必须人逐行认。提不出的留空,绝不猜。
"""

import csv
import logging
from pathlib import Path

from registry import db
from services import listing_sources, report_csv, sku_asin

DANGEROUS = False   # 不发 feed、不碰沃尔玛;真正的闸是 -p apply=1

logger = logging.getLogger("workflows.sources_reclassify")

# ── 清单列名(**读回按列名不按列位**:所有者会用 Excel 拖列、加批注列)────────
COL_STORE = "店铺"
COL_SKU = "SKU"
COL_CUR_TYPE = "当前来源类型"
COL_CUR_KEY = "当前来源码"
COL_PROPOSED = "机器提议来源码"
COL_BASIS = "提议依据"
COL_CONFIRM = "确认来源码"          # ← 人填的那一列,导入只认它
COL_CONFIRM_TYPE = "确认来源类型"   # ← 人填的第二列(amz/match/1688/self)
COL_NAME = "商品名"
COL_STATE = "在架状态"

_HEADER = [COL_STORE, COL_SKU, COL_CUR_TYPE, COL_CUR_KEY,
           COL_PROPOSED, COL_BASIS, COL_CONFIRM, COL_CONFIRM_TYPE,
           COL_NAME, COL_STATE]

#: 读回必须有的三列(缺一列就 fail loud:少一列的后果是整批行被静默当成"没填")。
#: ⚠ 「确认来源类型」**不在必需列里**:所有者可能拿着上一版导出的清单(没有
#: 这一列)回来,硬性要求会让已经填好的一份文件整个作废。缺列时按
#: `listing_sources.RECLASSIFY_DEFAULT_TYPE` 算,并在摘要里**明说**这件事 ——
#: 静默默认正是这条通道 2026-09-03 之前的毛病。
_NEED_COLS = (COL_STORE, COL_SKU, COL_CONFIRM)

_DEFAULT_FILE = "sources_reclassify_待归类.csv"

#: 形态桶的人读名(桶本身由 services/sku_asin.classify 划,这里只管中文)
_BUCKET_ZH = {"wrapped": "三段式", "asin": "裸 ASIN",
              "numeric": "纯数字(item id)", "other": "其他"}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}


def _state(row: dict) -> str:
    """输入:待归类行 → 输出:在架状态一句话(给人认这个码是不是还活着)。"""
    if not row.get("in_items"):
        return "在架表无此行"
    if row.get("missing_since"):
        return "已缺席"
    return str(row.get("published_status") or "(状态未知)")


def _preview_rows(pending: list[dict]) -> list[list]:
    """输入:待归类行 → 输出:csv 行(含机器提议与依据)。

    ⚠ `确认来源码` 只在依据**不是 guess** 时才预填:guess 那一档不自动应用,
    人不动它就等于没填,读回时自然跳过 —— 这是"不自动应用"落到文件上的形状,
    而不是一句口头纪律。
    """
    out = []
    for r in pending:
        proposed, basis = sku_asin.propose_source_key(r["sku"])
        confirm = proposed if (proposed and basis != sku_asin.PROPOSE_GUESS) else ""
        # 类型列与来源码列**同步预填**:填了码却不填类型,读回时按默认算成
        # amz —— 那正是要消灭的静默。预填的是"提取规则认得的那种出身"(码里
        # 嵌着 ASIN ⇒ 搬运品),**人必须逐行核**:货源不是亚马逊的品照样可能
        # 有一个形态合法的 ASIN,认错的代价见 listing_sources.reclassify 头注。
        ctype = listing_sources.RECLASSIFY_DEFAULT_TYPE if confirm else ""
        out.append([r["store"], r["sku"], r.get("source_type") or "",
                    r.get("source_key") or "", proposed or "", basis,
                    confirm, ctype, r.get("product_name") or "", _state(r)])
    return out


def _bucket_lines(pending: list[dict]) -> list[str]:
    """输入:待归类行 → 输出:形态分桶计数 + 样本(每桶前 5 个)。

    ⚠ 计数单位是**登记行**(一个 sku 跨 3 店算 3 行),样本**先按 sku 去重**
    再取前 5:样本的全部作用是让人认形态,同一串跨 5 店就能把 5 个样本位全占满
    (与 services/sku_asin.samples 同款理由)。
    """
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for r in pending:
        kind = sku_asin.classify(r["sku"])
        counts[kind] = counts.get(kind, 0) + 1
        got = samples.setdefault(kind, [])
        if len(got) < 5 and r["sku"] not in got:
            got.append(r["sku"])
    return [f"  {_BUCKET_ZH[k]}×{counts[k]} 行 样本={samples[k]}"
            for k in ("wrapped", "asin", "numeric", "other") if k in counts]


def _read_csv(path: Path) -> list[dict]:
    """输入:清单路径 → 输出:[{store, sku, source_key, source_type}]
    (按列名取;缺**必需**列直接抛)。

    「确认来源类型」缺列或留空 ⇒ 值为 ''(空),由 listing_sources 按默认补 ——
    补在那边而不是这边:默认值是归类的业务口径,不是文件格式的细节。

    `utf-8-sig` 读:导出侧带 BOM(Excel 直开不乱码),不吃 BOM 的话第一列列名
    会变成 `\\ufeff店铺`,表现是"表头明明在却说缺列"。
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in _NEED_COLS if c not in cols]
        if missing:
            raise ValueError(f"清单缺列 {missing};实际表头 {cols} —— "
                             f"读回按列名不按列位,补上列名再跑")
        return [{"store": (r.get(COL_STORE) or "").strip(),
                 "sku": (r.get(COL_SKU) or "").strip(),
                 "source_key": (r.get(COL_CONFIRM) or "").strip(),
                 "source_type": (r.get(COL_CONFIRM_TYPE) or "").strip()}
                for r in reader]


def _skip_lines(skipped: dict) -> list[str]:
    """输入:跳过点名 → 输出:逐条摘要行(每类最多点 5 个,余数报数字)。"""
    lines = []
    for why, items in skipped.items():
        tail = f" 样本={items[:5]}" + (f" …共 {len(items)}" if len(items) > 5 else "")
        lines.append(f"  · {why}:{len(items)} 行{tail}")
    return lines


def _export(conn, params: dict) -> str:
    """输入:连接 + params(out)→ 输出:预览摘要(导出待归类清单,不写库)。"""
    pending = listing_sources.pending_reclassify(conn)
    if not pending:
        return ("待归类(自动链看不见)0 行 —— 登记簿里没有 unknown / 空来源码的行,"
                "无事可做")
    rows = _preview_rows(pending)
    auto = sum(1 for r in rows if r[6])                       # 确认列已预填
    guess = sum(1 for r in rows if r[5] == sku_asin.PROPOSE_GUESS)
    blank = len(rows) - auto - guess
    out = str(params.get("out", "")).strip()
    path = (report_csv.write_to(out, _HEADER, rows) if out
            else report_csv.write(_DEFAULT_FILE, _HEADER, rows))
    lines = [f"待归类(自动链看不见){len(pending)} 行 —— "
             f"规则提得出 {auto}(已预填「{COL_CONFIRM}」),"
             f"只能猜 {guess}(依据标 {sku_asin.PROPOSE_GUESS},**留空要人认**),"
             f"提不出 {blank}"]
    lines += _bucket_lines(pending)
    lines.append(f"  清单已导出 → {path}")
    lines.append(f"  下一步:人工核「{COL_CONFIRM}」列(guess 那批逐行认,"
                 f"不认就留空)+「{COL_CONFIRM_TYPE}」列"
                 f"({'/'.join(listing_sources.RECLASSIFY_TYPES)};"
                 f"**非亚马逊货源的品必须在这里改掉**,否则它的价格/标题/库存"
                 f"会跟着某个亚马逊页面走)→ `-p file=<清单>` 读回预览 → "
                 f"`-p apply=1` 才写库")
    return "\n".join(lines)


def _import(conn, params: dict, path: Path, raw: list[dict]) -> str:
    """输入:连接 + params(apply/overwrite/dry_run) + 清单路径与已解析的行
    → 输出:预览或改写摘要。"""
    overwrite = _truthy(params.get("overwrite"))
    # ⚠ DANGEROUS=False ⇒ cli 恒给 execute=True(缺省即真跑),写库的闸是
    # `-p apply=1`;`--dry-run` 单独透传,漏认它的话本工作流的 --dry-run 会照写不误
    execute = _truthy(params.get("apply")) and not params.get("dry_run")
    filled = [r for r in raw if r["source_key"]]
    lines = [f"sources_reclassify:{path.name} 读入 {len(raw)} 行,"
             f"填了「{COL_CONFIRM}」{len(filled)} 行"
             + (",覆盖模式 overwrite=1(允许盖掉已有键)" if overwrite else "")]
    # ⚠ 类型分布**每次都报**:归成 amz 就是把这一行交给 amz 的改价/清库存/
    # 删除三条 provider,人必须在按下 apply 之前看见"这一批里有多少行是 amz"。
    # 缺列时更要喊 —— 静默默认成 amz 正是 2026-09-03 之前的口子。
    if not any(r.get("source_type") for r in filled):
        lines.append(f"  ⚠ 清单没有「{COL_CONFIRM_TYPE}」列(或整列留空)——"
                     f"**全部按 {listing_sources.RECLASSIFY_DEFAULT_TYPE} 算**。"
                     f"非亚马逊货源的品会被 amz 快照驱动的改价/清库存/删除管到,"
                     f"确认这批都是搬运品再 apply")
    else:
        lines.append("  按「%s」分:%s" % (COL_CONFIRM_TYPE, ",".join(
            f"{t or listing_sources.RECLASSIFY_DEFAULT_TYPE}"
            f" {sum(1 for r in filled if (r.get('source_type') or listing_sources.RECLASSIFY_DEFAULT_TYPE) == (t or listing_sources.RECLASSIFY_DEFAULT_TYPE))} 行"
            for t in sorted({(r.get("source_type") or
                              listing_sources.RECLASSIFY_DEFAULT_TYPE)
                             for r in filled}))))

    if not execute:
        todo, skipped = listing_sources.plan_reclassify(conn, filled, overwrite)
        n_amz = sum(1 for w in todo
                    if w["source_type"] == listing_sources.SOURCE_AMZ)
        lines.append(f"🧪 [未写库] 本次将改 {len(todo)} 行,其中 **{n_amz} 行归成 "
                     f"{listing_sources.SOURCE_AMZ}** —— 只有这 {n_amz} 行"
                     f"从「自动链看不见」变成「自动链管得到」"
                     f"(改价/清库存/删除对它们第一次生效);"
                     f"其余 {len(todo) - n_amz} 行只是拿到身份,没有 provider 认领")
        lines += _skip_lines(skipped)
        lines.append("  确认无误后加 `-p apply=1` 真改;真改后**先** "
                     "`python cli.py maintenance_scan --dry-run` 看意图量"
                     "(尤其删除段)再放行 product_chain")
        return "\n".join(lines)

    todo, _ = listing_sources.plan_reclassify(conn, filled, overwrite)
    n_amz = sum(1 for w in todo
                if w["source_type"] == listing_sources.SOURCE_AMZ)
    changed, skipped = listing_sources.reclassify(conn, filled, overwrite)
    lines.append(f"已归类 {changed} 行 ✓ —— 其中 {n_amz} 行归成 "
                 f"{listing_sources.SOURCE_AMZ},自此被 amz 快照驱动的"
                 f"改价/清库存/删除管到;其余只是拿到身份")
    lines += _skip_lines(skipped)
    lines.append("⚠ 先 `python cli.py maintenance_scan --dry-run` 看意图量"
                 "(尤其删除段)再放行 product_chain")
    return "\n".join(lines)


def run(params: dict) -> str:
    """输入:params(file/out/apply/overwrite)→ 输出:预览+导出 或 归类导入摘要。"""
    raw_file = str(params.get("file", "")).strip()
    if not raw_file:
        with db.pg_conn() as conn:
            return _export(conn, params)
    # 路径检查与解析**放在开库之前**(cleanup_history_import 2026-08-11 实操教训:
    # 路径打错时重活已经跑完才炸在文件上,摘要全丢,人对着 traceback 猜发生了什么)
    path = Path(raw_file).expanduser()
    if not path.is_file():
        raise ValueError(f"清单文件不存在:{path}"
                         f"(先跑一次缺省预览导出清单,再改那份文件)")
    rows = _read_csv(path)
    with db.pg_conn() as conn:
        return _import(conn, params, path, rows)
