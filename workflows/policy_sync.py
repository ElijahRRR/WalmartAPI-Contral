"""policy_sync — 官方禁售政策转录件 → `audit.walmart_prohibited_policy`(手动跑)。

用法:
  python cli.py policy_sync --dry-run     # 看会写什么 ← 首次必须先跑这个
  python cli.py policy_sync               # 真跑 upsert(缺省即真跑)

来源是 **`refdata/policy_pages/en/*.md`**(进 git 的官方逐字转录件,由 skill
`.claude/skills/policy-refresh/` 产出),**不是爬虫**:所有者 2026-09-01 定稿
(`docs/policy_sync.md` §九/§十)——html.parser 抓回来的是拍平纯文本,标题/
列表/表格结构全丢,与官方页面不是同样的东西;改由子代理逐页忠实转录进仓,
今后同步 = 重跑 skill 更新 refdata,**git diff 即政策变更审计记录**。

写什么、不写什么(§二 逐字执行,保守到底):

- 对上的行**只 UPDATE 六个机器列**(full_policy / official_url /
  policy_updated_at / synced_at / raw),`category_en` **存量不改名**
  —— 旧结论与 L3 的 reason_category 挂在现值上,改名要随 L3 提版批走;
- **人工列一律不读不写**(category_zh / zh_seller_risk / zh_seller_notes /
  prohibited_items / conditional_items / preapproval_items / preapproval /
  legal_refs,以及同为人写的 overall_status)。它们是给人看的中文列,
  英文要点句填进去会中英混列(原「空时填要点句」条款 2026-09-01 作废);
- 官方有、表里没有 → **新增行**(id 从 max(id)+1 起连续分配,人工列留 NULL);
- 表里有、官方没有 → **不删行**,报告「官方已不含」等人工;
- 对不上的**不猜**:进「未对上清单」由所有者裁决(改名/新增/忽略)。

两种"读不全"分得很清,别混为一谈:

- **解析失败**:该类别**整行不刷新**(六个机器列一个都不写)——**绝不写空值
  覆盖**(政策表被写空 = L3 那一段判据凭空消失,而且它照样报成功)。摘要点名;
  标题还读得到就凭它认领表内那一行,进「解析失败」小节而**不算「官方已不含」**;
- **日期抽不到**:那一行照常刷新,**仅 `policy_updated_at` 置 NULL**
  (**会覆盖存量值**),原文留 `raw.last_updated_raw` —— 不拿抓取日顶替。

真跑的**连带后果三条**(摘要逐条点名;都不会自己发生,也都不会报错):

  ① 手动递增 `registry/resources.py` 的 `AUDIT_RULES_VERSION` —— 政策表内容变了
     = L3 判定输入变了,而 `audit_version` 是**仓库侧**的规则版本号,不会因为
     数据变了而递增;不提版,`rerule` / `mode=stale` 那些重审通道对这次变更完全
     无感(所有者 2026-08-21 在类目表上实遇过一次:全量扫过之后双双报「共 0 个」);
  ② 新增行的人工中文列全是 NULL,S4 现在会把它们渲染成**空壳标题**(只有英文
     类别名、没有判据),等运营把中文列填上才有用;
  ③ `services/audit_l3.py` 的 S1/S3 提示词硬写「37 条」,真跑后与实际行数不符 ——
     是否随第三步 L3 批一起改**由所有者决定**,本工作流不动 audit_l3.py。

喂 LLM 的机器喂入版由 `services/policy_feed` 渲染时派生,不落库。

手动跑,不进 `registry/schedule.py`(官方页低频变更,所有者按需重跑)。
"""

import hashlib
import json
import logging
import re
from datetime import date

from registry import db, paths

DANGEROUS = True        # 写 L3 判定输入,按纪律先 --dry-run

logger = logging.getLogger("workflows.policy_sync")

_REPORT_FILE = "policy_sync.txt"

# ── 机器列 / 人工列的分界(§二;人工列的名字只在这里出现一次)──────────────
# ⚠ `overall_status` 不在 §二/§八.1 那句"人工八列"里(定稿漏列),但它是 S4
#    喂给 LLM 的中文摘要列、由人维护——按同一保守口径**不读不写**。
_MACHINE_COLS = ("category_en", "full_policy", "official_url",
                 "policy_updated_at", "synced_at", "raw")
_HUMAN_COLS = ("category_zh", "overall_status", "preapproval", "zh_seller_risk",
               "prohibited_items", "conditional_items", "preapproval_items",
               "legal_refs", "zh_seller_notes")

# ── 头部解析 ──────────────────────────────────────────────────────────────
# 标准三行:`> 来源: URL` / `> 官方 Last Updated: …` / `> 抓取(UTC): 日期`。
# 16-general-use 是登录门禁页,第三行换成 `> 转录来源(2026-09-01): 所有者粘贴…`
# —— 两种都要吃(转录件忠实于官方页各自的样子,归一化在这一层做)。
_SRC_RE = re.compile(r"来源\s*[:：]\s*(\S+)")
_UPDATED_RE = re.compile(r"官方\s*Last\s*Updated\s*[:：]\s*(.+?)\s*$", re.I)
_FETCHED_RE = re.compile(r"抓取\s*[((]\s*UTC\s*[))]\s*[:：]\s*(\S+)")
_TRANSCRIBED_RE = re.compile(r"转录来源\s*[((]\s*([\d-]{8,10})\s*[))]")
_URL_RE = re.compile(r"https?://\S+")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
# `Mon D, YYYY`:官方各页写法不一(`Dec 10, 2025` / `Last updated on Dec 10, 2025`
# / `May 20, 2026(页面原文 "Last updated on May 20, 2026")`),按模式抽第一个
_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\s*,\s*(\d{4})\b", re.I)

# ── 对行归一化(§二 + 两条补充)────────────────────────────────────────────
_PAREN_SUFFIX_RE = re.compile(r"\s*[((][^()()]*[))]\s*$")


def norm_category(name: str | None) -> str:
    """输入:政策类别名 → 输出:官方名 ↔ 表内名的对行比对键。

    §二 定的四条:casefold + `&`↔`and` + 去逗号 + 去括号后缀(`(Covered Goods)`)
    + 空白折叠。另加两条(§〇 的实证词形差 §二 那四条盖不住,加了才全命中):

      · **去撇号**:`Children’s` / `Children's` / `Childrens` 三种写法归一;
      · **去词尾单数复数差**:`Cosmetic Products` ↔ `Cosmetics Products`
        (§〇 第一组;casefold 后仍差一个 s)。只削长度 >3 且不以 `ss` 收尾的
        词尾 `s`,42 个官方名两两不撞(测试钉死)。

    ⚠ 归一化只做**词形**,不做语义合并:`Drugs and Drug Paraphernalia` 与表内
    `Drugs & Paraphernalia` 这种缩写差**故意对不上**——那要所有者裁决改名还是
    新增,不许在这儿偷偷归到别的政策上(§二「对不上的不猜」)。
    """
    s = (name or "").replace("’", "'").replace("‘", "'")
    s = _PAREN_SUFFIX_RE.sub("", s.strip())
    s = s.replace("'", "").casefold()
    s = re.sub(r"\s*&\s*", " and ", s)
    s = s.replace(",", " ")
    out = []
    for t in s.split():
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return " ".join(out)


# ── 疑似改名对(纯报告提示;对行判定一个字都不受影响)──────────────────────
# 「未对上/新增」与「官方已不含」两张清单**同时**点到同一个概念时,真相多半是
# 官方**改了名**(存量 7 行用旧缩写:`Drugs & Paraphernalia` / `Electronics & RF`
# 那一族),不是"官方新增了一类、又删掉了另一类"。判反的后果不报错,是**同概念
# 双行**:S4 会拿到两份讲同一件事的政策文本,S2 候选也跟着脏。
# ⚠ 只提示、不合并 —— §二「对不上的不猜」照旧,合不合并是所有者的裁决。
_PAIR_STOPWORDS = frozenset({"and", "or", "the", "of", "for", "with",
                             "other", "product", "item", "good"})

# ── SQL(全部经 registry/db,不自行 connect)────────────────────────────────
_COLS_SQL = ("SELECT column_name, data_type FROM information_schema.columns "
             "WHERE table_schema = 'audit' "
             "AND table_name = 'walmart_prohibited_policy'")

# information_schema 里的期望类型(只用来**点名**,不 ALTER TYPE:改类型可能
# 截断存量,那是人工决定。反推表在别的部署上可能把日期列建成 text —— 写得进去
# 不炸,但日期比较会静默按字符串走)
_EXPECTED_TYPES = {"official_url": "text",
                   "policy_updated_at": "date",
                   "synced_at": "timestamp with time zone",
                   "raw": "jsonb"}

_ROWS_SQL = ("SELECT id, category_en, full_policy "
             "FROM audit.walmart_prohibited_policy ORDER BY id")

_MAX_ID_SQL = "SELECT coalesce(max(id), 0) FROM audit.walmart_prohibited_policy"

# ⚠ 列清单就是安全边界:人工列一个字都不许出现在下面两条 SQL 里(测试钉死)
_UPDATE_SQL = """
UPDATE audit.walmart_prohibited_policy
   SET full_policy       = %(full_policy)s,
       official_url      = %(official_url)s,
       policy_updated_at = %(policy_updated_at)s,
       synced_at         = now(),
       raw               = %(raw)s::jsonb
 WHERE id = %(id)s
"""

_INSERT_SQL = """
INSERT INTO audit.walmart_prohibited_policy
       (id, category_en, full_policy, official_url, policy_updated_at,
        synced_at, raw)
VALUES (%(id)s, %(category_en)s, %(full_policy)s, %(official_url)s,
        %(policy_updated_at)s, now(), %(raw)s::jsonb)
"""

# 幂等补列:反推表(旧仓无 DDL)在别的部署上可能缺机器列,缺了就补
_ADD_COLUMN_SQL = {
    "official_url": "ALTER TABLE audit.walmart_prohibited_policy "
                    "ADD COLUMN IF NOT EXISTS official_url text",
    "policy_updated_at": "ALTER TABLE audit.walmart_prohibited_policy "
                         "ADD COLUMN IF NOT EXISTS policy_updated_at date",
    "synced_at": "ALTER TABLE audit.walmart_prohibited_policy "
                 "ADD COLUMN IF NOT EXISTS synced_at timestamptz DEFAULT now()",
    "raw": "ALTER TABLE audit.walmart_prohibited_policy "
           "ADD COLUMN IF NOT EXISTS raw jsonb",
}


# ══════════════════════════════════════════════════════════════════════════
#  一、解析转录件
# ══════════════════════════════════════════════════════════════════════════

def parse_last_updated(raw: str | None) -> date | None:
    """输入:官方 Last Updated 原文 → 输出:date(抽不到给 None,由调用方点名)。"""
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS.index(m.group(1).lower()[:3]) + 1,
                    int(m.group(2)))
    except ValueError:                                  # 2 月 30 号那种
        return None


def parse_policy_file(path) -> dict:
    """输入:`refdata/policy_pages/en/*.md` 路径 → 输出:一条待 upsert 的记录 dict。

    结构:`# 类别名` → 连续 `> ` 引用块(来源/官方 Last Updated/抓取或转录来源)
    → 空行 → 正文。正文 `full_policy` **原样保留**(chrome 行也留):清洗只在
    渲染层(`services/policy_feed`)做一次,库里存的永远是官方原文(单一清洗路径)。

    结构不对就抛 ValueError —— 由 run() 隔离该类别,本轮不刷新它。
    """
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or not lines[0].startswith("# "):
        raise ValueError("首行不是 `# 类别名`")
    name = lines[0][2:].strip()
    if not name:
        raise ValueError("首行类别名为空")

    i = 1
    while i < len(lines) and not lines[i].strip():       # 08/39 标题后隔了空行
        i += 1
    head = []
    while i < len(lines) and lines[i].lstrip().startswith(">"):
        head.append(lines[i].lstrip()[1:].strip())
        i += 1
    if not head:
        raise ValueError("缺 `> ` 头注块(来源/官方 Last Updated/抓取)")

    url = updated_raw = fetched = ""
    for ln in head:
        m = _SRC_RE.search(ln)
        if m and not url:
            url = m.group(1)
        m = _UPDATED_RE.search(ln)
        if m and not updated_raw:
            updated_raw = m.group(1).strip()
        m = _FETCHED_RE.search(ln) or _TRANSCRIBED_RE.search(ln)
        if m and not fetched:
            fetched = m.group(1)
    if not url:                                          # 兜底:头注里的第一个 URL
        m = _URL_RE.search(" ".join(head))
        url = m.group(0) if m else ""
    if not url:
        raise ValueError("头注里没有来源 URL")
    if not updated_raw:
        raise ValueError("头注里没有 `官方 Last Updated`")

    while i < len(lines) and not lines[i].strip():        # 引用块后的空行
        i += 1
    body = "\n".join(lines[i:]).strip("\n")
    if not body.strip():
        raise ValueError("正文为空")

    return {
        "file": path.name,
        "category_en": name,
        "official_url": url,
        "last_updated_raw": updated_raw,
        "policy_updated_at": parse_last_updated(updated_raw),
        "header_fetched_at": fetched or None,
        "full_policy": body,
        "sha": _sha(body),
        "chars": len(body),
    }


def peek_title(path) -> str | None:
    """输入:**解析失败**的转录件路径 → 输出:首行 `# 类别名`(读不到给 None)。

    解析失败 ≠ 官方删了这个类别。标题还在就凭它认领表内那一行,把它挡在
    「官方已不含」之外 —— 否则一份坏掉的转录件会让人按"官方下架了这一类"
    去人工处置(判反的方向:该修文件,却去动库)。
    """
    try:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except Exception:                                   # noqa: BLE001
        return None                                     # 连读都读不了
    first = text.split("\n", 1)[0].strip()
    if not first.startswith("# "):
        return None
    return first[2:].strip() or None


def _sha(text: str) -> str:
    """输入:正文 → 输出:sha256 十六进制(变没变、变了多少的唯一判据)。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _raw_json(page: dict) -> str:
    """输入:解析记录 → 输出:`raw` 列的 jsonb 文本(§十.2 的六个键)。"""
    return json.dumps({
        "source": "refdata",
        "file": page["file"],
        "content_sha256": page["sha"],
        "chars": page["chars"],
        "last_updated_raw": page["last_updated_raw"],
        "header_fetched_at": page["header_fetched_at"],
    }, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════
#  二、对行
# ══════════════════════════════════════════════════════════════════════════

def plan_upsert(pages: list[dict], rows: list[tuple], next_id: int,
                broken_names=()) -> dict:
    """输入:官方页记录 + 表内行 `(id, category_en, full_policy)` + 起始 id
    + 解析失败件的标题 → 输出:{refresh, insert, held, absent, stale}(纯函数)。

    `held` = 对上了但**不敢动**的(表里同名歧义 / 两个官方页指向同一行):
    宁可少写一行让人来判,也不许猜——猜错就是把 A 政策的正文写进 B 行。

    `stale` = 官方页解析失败、但**标题还认得出**的那些表内行:本轮不刷新,
    却也**不是「官方已不含」**——转录件坏了而已,官方并没有删这一类。
    """
    table: dict[str, list[tuple]] = {}
    for rid, cat, full in rows:
        table.setdefault(norm_category(cat), []).append((rid, cat, full or ""))

    refresh, insert, held = [], [], []
    claimed: dict[int, str] = {}
    touched: set[int] = set()       # 官方点到了名的行(含歧义未动的)
    for page in pages:
        key = norm_category(page["category_en"])
        hits = table.get(key, [])
        if len(hits) > 1:
            touched.update(h[0] for h in hits)
            held.append((page, f"表内有 {len(hits)} 行同名(id "
                               + "/".join(str(h[0]) for h in hits) + "),不猜"))
            continue
        if not hits:
            insert.append({"id": next_id, "page": page})
            next_id += 1
            continue
        rid, cat, full = hits[0]
        touched.add(rid)
        if rid in claimed:
            held.append((page, f"与 {claimed[rid]} 同时指向 id {rid},不猜"))
            continue
        claimed[rid] = page["file"]
        refresh.append({"id": rid, "table_name": cat, "page": page,
                        "old_sha": _sha(full), "old_chars": len(full)})

    # 解析失败件的标题也算"官方点到了名":坏文件不等于官方下架了这一类,
    # 它的行落 `stale`,与「官方已不含」分开报(判反的方向:该修文件,却去动库)
    broken_keys = {norm_category(n) for n in broken_names if n}
    stale = [(rid, cat) for rid, cat, _ in rows
             if rid not in touched and norm_category(cat) in broken_keys]
    touched.update(rid for rid, _ in stale)

    # 「官方已不含」只收**官方一次都没点到名**的行:歧义行是"点到了但不敢动",
    # 混进来会让人以为官方删了这个类别(然后按缺席去人工处置,判反了)
    absent = [(rid, cat) for rid, cat, _ in rows if rid not in touched]
    return {"refresh": refresh, "insert": insert, "held": held,
            "absent": absent, "stale": stale}


def rename_candidates(plan: dict) -> list[tuple[str, int, str, str]]:
    """输入:计划 → 输出:[(官方名, 表内 id, 表内名, 依据)] 疑似改名对。

    两条候选判据(命中任一即提示,宁可多报也别漏):归一化后**实词有交集**,
    或一边的归一化串是另一边的**前缀**。纯提示,不改任何写库决定 ——
    「未对上」+「官方已不含」同时点到同一个概念,多半是官方改了名而不是
    一增一删;当成两件事处理就会写出**同概念双行**。
    """
    unmatched = [i["page"]["category_en"] for i in plan["insert"]] + \
                [p["category_en"] for p, _ in plan["held"]]
    absent = [(rid, cat, norm_category(cat), _pair_tokens(cat))
              for rid, cat in plan["absent"]]
    pairs: list[tuple[str, int, str, str]] = []
    for name in unmatched:
        key, toks = norm_category(name), _pair_tokens(name)
        for rid, cat, other, other_toks in absent:
            shared = [t for t in other_toks if t in toks]
            if shared:
                why = "共同词:" + "/".join(dict.fromkeys(shared))
            elif key and other and (key.startswith(other)
                                    or other.startswith(key)):
                why = "前缀重合"
            else:
                continue
            pairs.append((name, rid, cat, why))
    return pairs


def _pair_tokens(name: str) -> list[str]:
    """输入:类别名 → 输出:配对提示用的实词(归一化后去虚词/万能词)。"""
    return [t for t in norm_category(name).split() if t not in _PAIR_STOPWORDS]


# ══════════════════════════════════════════════════════════════════════════
#  三、库侧
# ══════════════════════════════════════════════════════════════════════════

def _missing_columns(conn) -> tuple[list[str], list[tuple[str, str, str]]]:
    """输入:连接 → 输出:(缺的机器列, [(列, 实际类型, 期望类型)] 类型不符的列)。

    缺列会补(`ADD COLUMN IF NOT EXISTS`,幂等);类型不符**只点名不动手** ——
    `ALTER TYPE` 可能截断存量数据,那是人工决定。类型歪了不炸也不报错:
    `policy_updated_at` 被建成 text 照样写得进去,只是日期比较悄悄按字符串走。
    """
    with conn.cursor() as cur:
        cur.execute(_COLS_SQL)
        have = {str(r[0]): str(r[1] or "") for r in cur.fetchall()}
    if not have:
        raise RuntimeError("audit.walmart_prohibited_policy 不存在 —— "
                           "先跑 `python cli.py db_init` 建表,本工作流只同步数据")
    missing = [c for c in _ADD_COLUMN_SQL if c not in have]
    bad = [(c, have[c], want) for c, want in _EXPECTED_TYPES.items()
           if c in have and have[c].strip().lower() != want]
    return missing, bad


def _load_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(_ROWS_SQL)
        return [tuple(r) for r in cur.fetchall()]


def _next_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(_MAX_ID_SQL)
        return int((cur.fetchone() or (0,))[0] or 0) + 1


def _apply(conn, plan: dict, missing: list[str]) -> None:
    """输入:连接 + 计划 + 待补列 → 输出:无(同一事务内补列 + upsert)。"""
    with conn.cursor() as cur:
        for col in missing:
            cur.execute(_ADD_COLUMN_SQL[col])
        for item in plan["refresh"]:
            page = item["page"]
            cur.execute(_UPDATE_SQL, {
                "id": item["id"],
                "full_policy": page["full_policy"],
                "official_url": page["official_url"],
                "policy_updated_at": page["policy_updated_at"],
                "raw": _raw_json(page),
            })
        for item in plan["insert"]:
            page = item["page"]
            cur.execute(_INSERT_SQL, {
                "id": item["id"],
                "category_en": page["category_en"],
                "full_policy": page["full_policy"],
                "official_url": page["official_url"],
                "policy_updated_at": page["policy_updated_at"],
                "raw": _raw_json(page),
            })


# ══════════════════════════════════════════════════════════════════════════
#  四、报告
# ══════════════════════════════════════════════════════════════════════════

def _report(plan: dict, broken: list[tuple], undated: list[dict],
            missing: list[str], mismatched: list[tuple], execute: bool,
            src) -> list[str]:
    """输入:计划 + 异常清单 → 输出:逐类别 diff 报告文本行。"""
    pairs = rename_candidates(plan)
    paired_names = {name for name, _, _, _ in pairs}
    paired_ids = {rid for _, rid, _, _ in pairs}
    out = [f"政策表官方同步(policy_sync)—— 来源 {src}"
           f"({'真跑' if execute else 'DRY-RUN,零写库'})",
           f"写入口径:只动机器列 {'/'.join(_MACHINE_COLS)};"
           f"人工列 {'/'.join(_HUMAN_COLS)} 一律不读不写",
           ""]
    if not missing:
        out.append("▍将补列:(无,表结构已齐)")
    else:
        out.append("▍将补列:" + "、".join(missing)
                   + ("(已补)" if execute else
                      "(真跑时 ALTER TABLE … ADD COLUMN IF NOT EXISTS)"))
    out.append("")

    if not mismatched:
        out.append("▍类型不符:(无,机器列类型都对)")
    else:
        out.append("▍类型不符(只点名,**不** ALTER TYPE —— 改类型可能截断存量,"
                   f"人来决定):{len(mismatched)} 条")
        for col, got, want in mismatched:
            out.append(f"    {col}  实际 {got}  ≠  预期 {want}")
    out.append("")

    out.append(f"▍新增(官方有、表里没有 → INSERT,人工列留 NULL 等人工)"
               f":{len(plan['insert'])} 条")
    for item in plan["insert"]:
        p = item["page"]
        out.append(f"    id {item['id']:>4}  {p['category_en']}"
                   f"  [{p['last_updated_raw']}]  {p['chars']} 字  ({p['file']})")

    out.append("")
    out.append(f"▍对上(UPDATE 六个机器列;category_en 存量不改名)"
               f":{len(plan['refresh'])} 条")
    for item in sorted(plan["refresh"], key=lambda x: x["id"]):
        p = item["page"]
        changed = item["old_sha"] != p["sha"]
        out.append(f"    id {item['id']:>4}  {item['table_name']}"
                   f"  {'变' if changed else '同'}"
                   f"  sha {item['old_sha'][:8]}→{p['sha'][:8]}"
                   f"  {item['old_chars']}→{p['chars']} 字"
                   + ("" if item["table_name"] == p["category_en"] else
                      f"  ←官方名「{p['category_en']}」(表内名不改)"))

    out.append("")
    out.append(f"▍未对上(官方页有、表里没有对应行 —— 所有者裁决:改名/新增/忽略)"
               f":{len(plan['insert']) + len(plan['held'])} 条")
    for item in plan["insert"]:
        name = item["page"]["category_en"]
        out.append(f"    {name}  → 本轮按新增处理(id {item['id']})"
                   + ("  ⚠ 见下「疑似改名对」" if name in paired_names else ""))
    for page, why in plan["held"]:
        out.append(f"    {page['category_en']}  → ⚠ 本轮不动:{why}"
                   + ("  ⚠ 见下「疑似改名对」"
                      if page["category_en"] in paired_names else ""))

    out.append("")
    out.append("▍疑似改名对(**不是**新增+删除:同一概念的两种写法。按新增裁决"
               f"会写出同概念双行,S4 会拿到两份互相矛盾的政策文本):{len(pairs)} 对")
    for name, rid, cat, why in pairs:
        out.append(f"    官方「{name}」  ↔  表内 id {rid}「{cat}」  ({why})")
    if not pairs:
        out.append("    (无 —— 两张清单没有词形上重合的候选)")

    out.append("")
    out.append(f"▍官方已不含(表里有、本轮官方转录件没有 —— **不删行**,待人工)"
               f":{len(plan['absent'])} 条")
    titleless = [f for f, _, title in broken if not title]
    if titleless:
        out.append(f"    ⚠ 本轮有 {len(titleless)} 份解析失败且连 `# 类别名` 都读不到"
                   f"({'、'.join(titleless)})—— 下列可能只是转录件坏了,"
                   "不是官方删了这些类别")
    for rid, cat in plan["absent"]:
        out.append(f"    id {rid:>4}  {cat}"
                   + ("  ⚠ 见上「疑似改名对」" if rid in paired_ids else ""))

    out.append("")
    out.append("▍解析失败(本轮不刷新)—— 表内行原样保留,**不算「官方已不含」**"
               f":{len(plan['stale'])} 条")
    for rid, cat in plan["stale"]:
        out.append(f"    id {rid:>4}  {cat}  —— 转录件坏了,官方并没有删这一类")

    out.append("")
    out.append(f"▍解析失败的转录件(该类别整行不刷新,绝不写空值):{len(broken)} 条")
    for name, why, title in broken:
        out.append(f"    {name}  —— {why}"
                   + (f"(标题「{title}」)" if title else "(标题也读不到)"))

    out.append("")
    out.append(f"▍官方 Last Updated 抽不到日期(**仅** policy_updated_at 置 NULL"
               f"——**会覆盖存量值**;原文进 raw.last_updated_raw)"
               f":{len(undated)} 条")
    for p in undated:
        out.append(f"    {p['file']}  原文「{p['last_updated_raw']}」")
    return out


# ══════════════════════════════════════════════════════════════════════════
#  五、入口
# ══════════════════════════════════════════════════════════════════════════

def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    src = paths.policy_pages_dir("en")
    files = sorted(src.glob("*.md"))
    if not files:
        raise FileNotFoundError(
            f"{src} 里一份转录件都没有 —— 先跑 skill policy-refresh 生成 refdata")

    pages, broken = [], []
    for path in files:
        try:
            pages.append(parse_policy_file(path))
        except Exception as e:                                  # noqa: BLE001
            broken.append((path.name, str(e), peek_title(path)))
            logger.warning("政策转录件解析失败(隔离该类别,本轮不刷新):%s / %s",
                           path.name, e)
    # 单份坏了隔离,**全军覆没就炸**:一份都没解析出来还往下走,只会写出
    # 「新增 0 / 刷新 0」然后报成功 —— 那正是"目录被清空/格式整体改了"的样子,
    # 不许让它长得像"官方没变化"(空转报成功是这条工作流最坏的失败形态)
    if files and not pages:
        raise RuntimeError(
            f"{src} 里 {len(files)} 份转录件**全部解析失败**,本轮什么都不做 —— "
            "先修转录件(或重跑 skill policy-refresh)。首份原因:"
            f"{broken[0][0]} / {broken[0][1]}")
    undated = [p for p in pages if p["policy_updated_at"] is None]

    with db.pg_conn() as conn:
        missing, mismatched = _missing_columns(conn)
        rows = _load_rows(conn)
        plan = plan_upsert(pages, rows, _next_id(conn),
                           [t for _, _, t in broken])
        if execute:
            _apply(conn, plan, missing)

    body = _report(plan, broken, undated, missing, mismatched, execute, src)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    report = paths.reports_dir() / _REPORT_FILE
    report.write_text("\n".join(body) + "\n", encoding="utf-8")

    pairs = rename_candidates(plan)
    n_new, n_up = len(plan["insert"]), len(plan["refresh"])
    n_miss = len(plan["insert"]) + len(plan["held"])
    lines = [f"新增 {n_new} / 刷新 {n_up} / 未对上 {n_miss} / "
             f"官方缺席 {len(plan['absent'])}"
             + ("" if execute else "(🧪 DRY-RUN:一行未写库)"),
             f"来源 {src}:{len(files)} 份转录件"
             f"(解析成功 {len(pages)} / 失败 {len(broken)})"]
    if pairs:
        lines.insert(1, f"⚠ 疑似改名对 {len(pairs)} 对(不是新增+删除):"
                        + "、".join(f"「{n}」↔ id {i}「{c}」"
                                    for n, i, c, _ in pairs[:6])
                        + (f"…另有 {len(pairs) - 6} 对" if len(pairs) > 6 else "")
                        + " —— 按新增裁决会写出同概念双行,先看报告「疑似改名对」")
    if broken:
        lines.append("⚠ 解析失败(该类别整行不刷新,绝不写空值):"
                     + "、".join(n for n, _, _ in broken))
    if plan["stale"]:
        lines.append("▍解析失败故本轮不刷新(表内行原样保留,不算官方已不含):"
                     + "、".join(f"{c}(id {i})" for i, c in plan["stale"]))
    if undated:
        lines.append("⚠ 官方 Last Updated 抽不到日期(仅 policy_updated_at 置 NULL,"
                     "会覆盖存量值;原文进 raw):"
                     + "、".join(p["file"] for p in undated))
    if missing:
        lines.append(("⚠ 表缺机器列,已补:" if execute else
                      "⚠ 表缺机器列,真跑时补(ADD COLUMN IF NOT EXISTS):")
                     + "、".join(missing) + " —— 同步 docs/db_schema.md")
    if mismatched:
        lines.append("⚠ 机器列类型与预期不符(只点名,不 ALTER TYPE —— 人来决定):"
                     + "、".join(f"{c} 是 {got}(预期 {want})"
                                 for c, got, want in mismatched))
    if plan["insert"]:
        names = [i["page"]["category_en"] for i in plan["insert"]]
        lines.append(f"▍新增(id {plan['insert'][0]['id']}-"
                     f"{plan['insert'][-1]['id']}):" + "、".join(names[:12])
                     + (f"…另有 {len(names) - 12} 条(见报告)"
                        if len(names) > 12 else ""))
    for page, why in plan["held"]:
        lines.append(f"⚠ 未对上且本轮不动:{page['category_en']} —— {why}")
    if plan["absent"]:
        lines.append("▍官方已不含(不删行,待人工):"
                     + "、".join(f"{c}(id {i})" for i, c in plan["absent"]))
    lines.append(f"▍逐类别 diff 全文 → {report}")
    if execute:
        # 真跑的三条连带后果:都**不会自己发生**,也都不会报错(定稿 §十.6)
        lines.append("⚠ 真跑连带后果三条(没有任何东西会替你做,也不会报错):")
        lines.append("  ① 政策表已变更 = L3 输入已变更:手动递增 "
                     "registry/resources.py 的 AUDIT_RULES_VERSION 再跑审核 —— "
                     "不提版,rerule / mode=stale 对这次变更完全无感;")
        lines.append(f"  ② 新增 {n_new} 行的人工中文列全是 NULL:S4 现在会把它们"
                     "渲染成**空壳标题**(只有英文类别名、没有判据),"
                     "等运营把中文列填上才有用;")
        lines.append(f"  ③ services/audit_l3.py 的 S1/S3 提示词硬写「37 条」,"
                     f"与实际行数({len(rows) + n_new} 行)不符 —— 是否随第三步 "
                     "L3 批一起改由所有者决定(本工作流不动 audit_l3.py)。")
    else:
        lines.append("(dry-run:一行未写库;去掉 --dry-run 才 upsert)")
        lines.append("⚠ 首跑必须人眼核对报告两处:①「未对上」清单(存量缩写名"
                     "要逐条裁决改名/新增/忽略,否则同概念双行)、"
                     "②「对上」清单里带 `←官方名` 标记的行(表内名与官方拼写不同)")
    return "\n".join(lines)
