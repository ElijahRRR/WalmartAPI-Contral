"""政策全文 → **喂入版**渲染(纯函数;口径 `docs/policy_sync.md` §十.3-§十.5)。

给谁用:L3 的 S4 政策块。库里 `audit.walmart_prohibited_policy.full_policy`
存的是官方转录件**原样正文**(`refdata/policy_pages/en/*.md` 的引用块之后那段),
喂 LLM 前由本模块**渲染时派生**——**不落库、不留第二份**(§十.3:清洗只有
一条路径;政策表存原文,喂入版随渲染生成,规则改了不用回头刷库)。

剥掉什么、为什么:

- **超链接**:`[文字](url)` 只留文字,裸 URL 与 `<http…>` 自动链接整条删。
  所有者定稿(§十.4):URL 对判定零贡献、徒耗 token;要点链接的人去看 PG 的
  `official_url` 或飞书 E 列;
- **「In this guide」页内导览**:官方页顶部的锚点目录,对 LLM 是纯噪声
  (42 份转录件里它有 `**In this guide:**` / 纯文本 / `##` / `###` 四种形态,
  官方自己就不统一——转录件忠实保留,归一化放在这一层做);
- **Notes 免责声明**:那句"third-party information … not legal advice"在 41/42
  份里逐字重复,是律师话不是判据。**只删这一句已知免责文**(及其紧邻的 Notes
  标题行)——27 号 PFAS 页的 `**Notes:**` 段装的是"Covered Products 各州不同"
  这种**真判据**,按标题一刀切会把它一起删掉;
- **页面 chrome 独行**:`Guide` / `Reading time: …` / `Last updated on …` /
  `Bookmark`(部分转录件把这几行连同正文一起收了);
- **已知源页残句**:32 号官方源页把 PDF 页眉串进了正文中间(转录忠实照录,
  见 `refdata/policy_pages/README.md` 第 3 条),清洗在这一层剪掉 ——
  **逐字固定串,不做通配**(通配会连真判据一起吃);
- **头部四行**:`full_policy` 本身就不含(解析时已剥),本模块不再处理。

表格为什么分两条路(机械无损变换,§十.3):

- **只有一行数据**的表(77/80 张,`Prohibited | Allowed with restriction |
  Allowed` 那种)→ 转成「列名: + 条目清单」。原样喂的话 LLM 得先解析一个
  三列宽表、再拆单元格里的 `<br>` 与 `&nbsp;` 缩进才能知道哪条属于哪一列;
- **多行数据**的表(3 张:33 的 e-Bike 分级、27 的 PFAS 产品类别、26 的标签
  要求)→ **原样保留 markdown 表格**。这类表**按行承载语义**(Class 2 =
  这一行的三条定义),拆成列会把行对应关系毁掉——那是内容损失,不是清洗。

零第三方依赖(只用 re);函数**幂等**:对自身输出再跑一遍不变(测试钉死)。
"""

import re

# ── 免责声明锚:只认这一句开头 ────────────────────────────────────────────
# 官方在 41 份里有四种细微变体(third party / third-party、有无牛津逗号、
# 37 号拆成两句),但**开头一律是这 47 个字符**——按前缀认,变体全吃。
_DISCLAIMER_HEAD = "The third-party information found within this policy"

# ── 各类行形态 ────────────────────────────────────────────────────────────
# 引用标记:10/31/35 三份把 Notes 整段放进 blockquote,判形态前先剥 `> `
_QUOTE_RE = re.compile(r"^\s*(?:>\s?)+")
# 「In this guide」标题:`**In this guide:**` / `In this guide:` / `## …` / `### …`
_IN_GUIDE_RE = re.compile(r"^(?:#{1,6}\s*)?\*{0,2}In this guide:?\*{0,2}$", re.I)
# Notes 标题:`**Notes:**` / `## Notes:` / `### Notes:` / `#### Notes:`
_NOTES_RE = re.compile(r"^(?:#{1,6}\s*)?\*{0,2}Notes?:?\*{0,2}$", re.I)
# 列表行(导览块的构成)
_LIST_RE = re.compile(r"^[-*+]\s|^\d+[.)]\s")
# 页面 chrome 独行(§十.3 点名的四种)
_CHROME_RES = (re.compile(r"^Guide$"),
               re.compile(r"^Reading time:.*$"),
               re.compile(r"^Last updated on .*$"),
               re.compile(r"^Bookmark$"))
# markdown 链接的左半 `[文字](`;右括号靠配对扫描找,**不能用 `[^)]*`**:
# 官方链接自己带括号(`…intellectual-property-(IP)`),正则会在第一个 `)`
# 上截断,把半截 URL 留在正文里(06/19 两份实见)
_LINK_OPEN_RE = re.compile(r"\[([^\[\]]*)\]\(")
_AUTOLINK_RE = re.compile(r"<https?://[^>\s]*>")
_BARE_URL_RE = re.compile(r"(?<!\S)https?://\S+")
# 兜底扫描:上面那条要求 URL 前面是空白(避免误伤 `](url)` 的右半),于是
# **粘连形态**漏网 —— `Ref:https://…`(前面是冒号)、`(https://…)`(前面是
# 左括号)。渲染末尾不带前视再扫一遍,连同紧裹的一对括号一起删。
_LOOSE_URL_RE = re.compile(r"\(?\s*https?://[^\s)>\]]*\)?")

# ── 已知源页残句(逐字匹配,不做通配)──────────────────────────────────────
# 出处:`refdata/policy_pages/README.md` 第 3 条 —— 32-restricted-illegal-products
# 的官方源页把 PDF 页眉("… Walmart Confidential …")混进了正文里一个条目的
# **中间**,转录件按忠实纪律照录。剪掉残句、**保留条目其余文字**(那半句
# "Products determined to be unsafe through other means" 是真判据)。
# ⚠ 破折号是 `–`(U+2013)不是 `-`;新残句只能逐字加进这张表,别改成通配。
_KNOWN_STRAY = (
    "Marketplace Prohibited Product Policy by Category 5 "
    "Walmart Confidential & Proprietary Information – Do Not Distribute",
)
# 表格分隔行 `| --- | --- |`
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")


def render_feed_text(full_policy: str) -> str:
    """输入:政策原文正文(`full_policy`)→ 输出:喂 LLM 的纯判据文本。

    幂等:`render_feed_text(render_feed_text(x)) == render_feed_text(x)`。
    """
    lines = (full_policy or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [_strip_stray(_strip_urls(_strip_links(ln))) for ln in lines]
    lines = [ln for ln in lines if not _is_chrome(ln)]
    lines = _drop_in_this_guide(lines)
    lines = _drop_disclaimer(lines)
    lines = _transform_tables(lines)
    lines = [_sweep_urls(ln) for ln in lines]         # 兜底:粘连/括号内的漏网 URL
    return _tidy(lines)


# ══════════════════════════════════════════════════════════════════════════
#  一、链接
# ══════════════════════════════════════════════════════════════════════════

def _closing_paren(text: str, start: int) -> int:
    """输入:文本 + 左括号下标 → 输出:配对右括号下标(配不上给 -1)。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_links(line: str) -> str:
    """输入:一行 → 输出:`[文字](url)` 换成文字的同一行(链接文字一律保留)。"""
    out: list[str] = []
    i = 0
    while True:
        m = _LINK_OPEN_RE.search(line, i)
        if not m:
            out.append(line[i:])
            return "".join(out)
        close = _closing_paren(line, m.end() - 1)
        if close < 0:                      # 括号配不上:原样留着,不猜
            out.append(line[i:m.end()])
            i = m.end()
            continue
        head = m.start()
        if head > 0 and line[head - 1] == "!":      # 图片 `![alt](url)`:整个删
            # 图片没有判据文字,alt 只是文件名(44 号页的示意图
            # `2505_product-detail-page-Color.svg`);留下来就是一行噪声
            out.append(line[i:head - 1])
            i = close + 1
            continue
        out.append(line[i:head])
        out.append(m.group(1))
        i = close + 1


def _strip_urls(line: str) -> str:
    """输入:一行 → 输出:删掉裸 URL 与 `<http…>` 自动链接的同一行。

    只认"前面是空白"的裸 URL(否则会误伤 `](url)` 的右半);粘连形态交给
    渲染末尾的 `_sweep_urls` 兜底。
    """
    return _respace(line, _BARE_URL_RE.sub("", _AUTOLINK_RE.sub("", line)))


def _sweep_urls(line: str) -> str:
    """输入:已清洗过的一行 → 输出:再删一遍漏网 URL 的同一行(兜底)。

    `_strip_urls` 要求 URL 前面是空白,`Ref:https://…` 与 `(https://…)` 这两种
    **粘连形态**它按设计不管。链接不进提示词是硬口径(§十.4),所以渲染末尾
    不带前视再扫一次 —— 这一遍不该有活儿干,有就说明上游漏了一种形态。
    """
    return _respace(line, _LOOSE_URL_RE.sub("", line))


def _strip_stray(line: str) -> str:
    """输入:一行 → 输出:剪掉已知源页残句(PDF 页眉)后的同一行。"""
    new = line
    for stray in _KNOWN_STRAY:
        new = new.replace(stray, "")
    return _respace(line, new)


def _respace(line: str, new: str) -> str:
    """输入:原行 + 删过东西的行 → 输出:补空格的行(**没删过就原样返回**)。

    ⚠ 只在确实删过的行上折叠多余空格,且保留行首缩进 —— 16 号那份的嵌套
    要求列表(`  - Identification of…`)靠行首空格表达层级,全局折叠会拍平它。
    """
    if new == line:
        return line
    indent = new[:len(new) - len(new.lstrip(" \t"))]
    return (indent + re.sub(r"[ \t]{2,}", " ", new.strip())).rstrip()


# ══════════════════════════════════════════════════════════════════════════
#  二、chrome / 导览 / 免责声明
# ══════════════════════════════════════════════════════════════════════════

def _bare(line: str) -> str:
    """输入:一行 → 输出:剥掉引用标记与首尾空白的行(只用于判形态)。"""
    return _QUOTE_RE.sub("", line).strip()


def _is_chrome(line: str) -> bool:
    s = _bare(line)
    return any(r.match(s) for r in _CHROME_RES)


def _drop_in_this_guide(lines: list[str]) -> list[str]:
    """输入:行列表 → 输出:删掉「In this guide」标题行及其后导览列表的行列表。

    标题与列表之间官方**隔着一个空行**(42 份都如此),所以跳空行再吃列表;
    列表止于第一个空行或第一个非列表行(那已经是正文了)。
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _IN_GUIDE_RE.match(_bare(lines[i])):
            out.append(lines[i])
            i += 1
            continue
        i += 1
        while i < len(lines) and not _bare(lines[i]):        # 标题与列表间的空行
            i += 1
        while i < len(lines) and _LIST_RE.match(_bare(lines[i])):
            i += 1
    return out


def _drop_disclaimer(lines: list[str]) -> list[str]:
    """输入:行列表 → 输出:删掉已知免责声明段(及其紧邻 Notes 标题)的行列表。

    ⚠ 只删「以 `The third-party information found within this policy` 开头」
    那一段。别的 Notes 段(27 号 PFAS 的 Covered Products 说明)照留——
    按 Notes 标题一刀切会删掉真判据。
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _bare(lines[i]).startswith(_DISCLAIMER_HEAD):
            out.append(lines[i])
            i += 1
            continue
        while i < len(lines) and _bare(lines[i]):             # 整段到空行为止
            i += 1
        k = len(out)                                          # 回退吃掉标题
        while k > 0 and not _bare(out[k - 1]):
            k -= 1
        if k > 0 and _NOTES_RE.match(_bare(out[k - 1])):
            del out[k - 1:]
    return out


# ══════════════════════════════════════════════════════════════════════════
#  三、表格(机械无损变换)
# ══════════════════════════════════════════════════════════════════════════

def _split_row(line: str) -> list[str]:
    """输入:markdown 表行 → 输出:各单元格原文(不做转义处理:转录件无 `\\|`)。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _plain(cell: str) -> str:
    """输入:单元格/表头原文 → 输出:去粗体星号与首尾空白的文本。"""
    return cell.strip().strip("*").strip()


def _cell_items(cell: str) -> list[str]:
    """输入:单元格原文 → 输出:按 `<br>` 拆开的条目行(去行首 `-`,缩进保层级)。

    `&nbsp;` 串是官方表达嵌套的方式(2 个 = 一层,4 个 = 两层),逐个换成
    等宽空格,层级就原样保住了。
    """
    items: list[str] = []
    for chunk in cell.split("<br>"):
        s = chunk.replace("&nbsp;", " ")
        indent = len(s) - len(s.lstrip(" "))
        body = s.strip()
        if body.startswith("-"):                    # 去行首 "- "(也吃 "-x")
            body = body[1:].lstrip(" ")
        if body:
            items.append(" " * indent + body)
    return items


def _transform_tables(lines: list[str]) -> list[str]:
    """输入:行列表 → 输出:单数据行表转条目清单、多数据行表原样的行列表。"""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            j += 1
        block = lines[i:j]
        i = j
        # 官方源码偶有整行空单元格的尾行(44 号页「Product title」表):
        # 不承载任何语义,判"几行数据"时不算,原样保留时也不留
        data = [row for row in block[2:] if any(_split_row(row))]
        # 不是"表头 + 分隔 + 恰好一行数据"的,一律原样(多行表按行承载语义)
        if len(data) != 1 or len(block) < 3 \
                or not _TABLE_SEP_RE.match(block[1].strip()):
            out += block
            continue
        heads, cells = _split_row(block[0]), _split_row(data[0])
        rendered: list[str] = []
        for n, head in enumerate(heads):
            items = _cell_items(cells[n]) if n < len(cells) else []
            if not items:
                continue
            name = _plain(head)
            if rendered:
                rendered.append("")
            rendered.append(name if name.endswith(":") else f"{name}:")
            rendered += items
        if rendered:
            out += [""] + rendered + [""]
        else:
            out += block
    return out


# ══════════════════════════════════════════════════════════════════════════
#  四、收尾
# ══════════════════════════════════════════════════════════════════════════

def _tidy(lines: list[str]) -> str:
    """输入:行列表 → 输出:连续 3+ 空行折叠为 1、去首尾空行的文本。

    3+ 才折(不是 2+):2 个空行是官方自己的段落间距,清洗层不改它;
    3 个以上只可能是"删走了一段"留下的坑。这样对自身输出再跑一遍不变。
    """
    out: list[str] = []
    run = 0
    for ln in lines:
        ln = ln.rstrip()
        if ln:
            out += [""] * (1 if run >= 3 else run)
            out.append(ln)
            run = 0
        else:
            run += 1
    return "\n".join(out).strip("\n")
