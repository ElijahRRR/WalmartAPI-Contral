"""asin_blacklist_import — 黑名单 ASIN 批量导入(一次性;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py asin_blacklist_import -p file=~/Desktop/黑名单asin.txt --dry-run
  python cli.py asin_blacklist_import -p file=~/Desktop/黑名单asin.txt   # 真导

所有者定稿 2026-08-13:历史继承的一批黑名单 ASIN 导入 catalog.asin_blacklist
(黑名单中心——审核 ASIN 闸与上架拦截共用同一份)。规则:
  · 逐行一个 ASIN,空行跳过,文件内去重;
  · 库内已有的 **DO NOTHING**(不覆盖既有类别/来源——先到先得,与
    services/blacklist.record_asins 的"永久禁止一次入选"同款语义);
  · category='LEGACY',source='历史继承'(区别于自产 B/C/E/F/G/K 六类);
  · 非标准格式(非 10 位大写字母数字)**照灌不丢行**(黑名单哲学:宁可键
    不标准也不丢行,record_asins 同款),单独计数亮出——它们拦不着按 ASIN
    建档的产品,但也不误拦。
dry-run 打印完整体检(总行/去重后/文件内重复/非标准/库内已有/将新增),
真跑才写库(空跑用 --dry-run)。
"""

import logging
import re
from pathlib import Path

from registry import db

DANGEROUS = True

logger = logging.getLogger("workflows.asin_blacklist_import")

_ASIN_RE = re.compile(r"[A-Z0-9]{10}")

_INSERT_SQL = """
INSERT INTO catalog.asin_blacklist (asin, category, source)
VALUES (%s, 'LEGACY', '历史继承')
ON CONFLICT (asin) DO NOTHING
"""


def parse_asin_lines(text: str) -> tuple[list[str], int, int]:
    """输入:文件全文 → 输出:(去重后有序 ASIN 列表, 文件内重复数, 非标准格式数)。

    纯函数(便于测试)。保留原始大小写与原文(不做 upper/清洗——键以原文为准,
    与黑名单中心既有键口径一致)。
    """
    seen: set = set()
    out: list[str] = []
    dups = nonstd = 0
    for line in text.splitlines():
        v = line.strip()
        if not v:
            continue
        if v in seen:
            dups += 1
            continue
        seen.add(v)
        out.append(v)
        if not _ASIN_RE.fullmatch(v):
            nonstd += 1
    return out, dups, nonstd


def run(params: dict) -> str:
    """输入:params(file=路径,必填)→ 输出:导入体检/结果摘要。"""
    raw_path = str(params.get("file", "")).strip()
    if not raw_path:
        raise ValueError("必填参数 file=<ASIN 清单文件路径>(逐行一个 ASIN)")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"文件不存在:{path}")
    execute = bool(params.get("execute"))

    asins, dups, nonstd = parse_asin_lines(path.read_text(encoding="utf-8"))
    if not asins:
        return f"文件 {path.name} 没有可导入的行"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM catalog.asin_blacklist "
                        "WHERE asin = ANY(%s)", (asins,))
            (existing,) = cur.fetchone()
        lines = [f"asin_blacklist_import:{path.name}",
                 f"文件 {len(asins) + dups} 行 → 去重后 {len(asins)}"
                 f"(文件内重复 {dups},非标准格式 {nonstd} 照灌)",
                 f"库内已有 {existing}(DO NOTHING 不覆盖),将新增 "
                 f"{len(asins) - existing}(category=LEGACY,source=历史继承)"]
        if not execute:
            lines.append("(dry-run:未写库;确认无误后**去掉 --dry-run** 重跑)")
            return "\n".join(lines)

        added = 0
        with conn.cursor() as cur:
            for a in asins:
                cur.execute(_INSERT_SQL, (a,))
                added += cur.rowcount or 0
    lines.append(f"实际新增 {added} 行 ✓")
    return "\n".join(lines)
