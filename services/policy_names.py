"""政策类别名的归一化与旧名翻译 —— 仓内**唯一一处**(纯函数,零 DB、零 LLM)。

政策表 `audit.walmart_prohibited_policy` 的 `category_en` 是**全链唯一键**
(定稿 `docs/policy_sync.md` §十.7):L3 提示词的 S2 候选块与 S4 政策块、L3 的
reason_category 白名单、`audit_reason` 的理由映射、报错文本 join 全指着它。
于是"两个字符串是不是同一个政策类别"这件事在仓内被问了四遍 —— 2026-09-02
改名批之前,四处各写各的:

  · `workflows/policy_sync.norm_category`(官方转录件标题 ↔ 表内行);
  · `services/error_taxonomy._norm_key`(报错正文抠出的候选串 ↔ 表内行);
  · `services/audit_l3` 的 `_CATEGORY_ROUTES` / `_PT_KEYWORD_ROUTES`(写死缩写名);
  · `services/audit_l2._infer_walmart_policy`(同一批写死的缩写名)。

后两处是**写死的字面量**,表一改名它们就静默失效(路由条目被 `c in known`
过滤掉、理由映射落到政策表之外),而两种失效都不会红。按 CLAUDE.md「每个能力
只有一条实现路径」,归一化与旧名翻译从此**只在这里**实现,上面四处全改走本模块。

三个函数,职责不重叠:

  · `norm_category` —— 只做**词形**归一(casefold / `&`↔`and` / 逗号 / 括号后缀 /
    撇号 / 词尾单复数)。它敢削词形,是因为 42 个官方名两两归一化不碰撞
    (测试钉死);**不做语义合并**,`Drugs & Paraphernalia` ↔
    `Drugs and Drug Paraphernalia` 这种缩写差故意对不上;
  · `to_official` —— 语义合并那一步,查的是 `registry.resources.
    POLICY_LEGACY_NAMES`,即所有者逐条裁决过的落纸(**过渡期产物**,生产改名
    落地后随第三步 L3 批与它一起删);
  · `resolve` —— 上面两条 + 实时表内容,回**表内原拼写**。改名前(表内是旧缩写
    名)和改名后(表内是官方名)都能命中,这正是"跨改名可用"的那一层。

⚠ 铁律 1:本模块在 services 层,只 import `re` 与 `registry`,**不许**反向
  import workflows —— `norm_category` 的实现从 `workflows/policy_sync` 搬来
  这里,是为了让 services 侧的消费者能用它,而不是让 services 去 import 工作流。
⚠ 归一化放宽 = 把两条真不同的政策合成一条。改这里的规则前先想清楚代价:
  policy_sync 那边合错了是**覆盖正文**,audit 这边合错了是**理由指错政策**。
"""

import re

from registry import resources

# `(Covered Goods)` 那类**结尾**括号后缀(半角/全角都吃)
_PAREN_SUFFIX_RE = re.compile(r"\s*[((][^()()]*[))]\s*$")


def norm_category(name: str | None) -> str:
    """输入:政策类别名 → 输出:官方名 ↔ 表内名的对行比对键。

    `docs/policy_sync.md` §二 定的四条:casefold + `&`↔`and` + 去逗号 +
    去括号后缀(`(Covered Goods)`)+ 空白折叠。另加两条(§〇 的实证词形差
    §二 那四条盖不住,加了才全命中):

      · **去撇号**:`Children’s` / `Children's` / `Childrens` 三种写法归一;
      · **去词尾单数复数差**:`Cosmetic Products` ↔ `Cosmetics Products`
        (§〇 第一组;casefold 后仍差一个 s)。只削长度 >3 且不以 `ss` 收尾的
        词尾 `s`,42 个官方名两两不撞(测试钉死)。

    ⚠ 归一化只做**词形**,不做语义合并:`Drugs and Drug Paraphernalia` 与表内
    `Drugs & Paraphernalia` 这种缩写差**故意对不上**——那要所有者裁决改名还是
    新增,不许在这儿偷偷归到别的政策上(§二「对不上的不猜」)。认领缩写名的是
    `to_official`,查的是所有者裁决过的映射表。
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


def to_official(name: str | None) -> str | None:
    """输入:任意政策名 → 输出:官方名(登记过的旧名才翻译,否则**原样回**)。

    查 `registry.resources.POLICY_LEGACY_NAMES`,**精确等值**(只 strip 两端空白,
    不归一化):键是表内旧名的历史字面量,所有者逐条认过的那 7 条 —— 拿归一化
    去套等于又把语义合并塞回归一化里。

    ⚠ 认不出来时回**原串**而不是 None:调用方要的是"翻译一下再试",不是
      "这是不是旧名"。None 只在入参本身为空时出现。
    """
    if not name:
        return None
    s = str(name).strip()
    return resources.POLICY_LEGACY_NAMES.get(s, s)


def resolve(name: str | None, known) -> str | None:
    """输入:任意拼写的政策名 + 实时 `category_en` 集合 → 输出:**表内原拼写**。

    四级,顺序即"敢猜的程度",认不出给 None(绝不编一个表里没有的名字):

      1. 精确等值;
      2. casefold 等值(LLM/报错正文的大小写靠不住);
      3. `norm_category` 键等值(词形差:`&`↔`and` / 逗号 / 括号后缀 / 撇号 /
         单复数);
      4. `to_official` 翻译后重试 1-3(所有者裁决过的旧缩写名)。

    改名前后都成立:表内是旧缩写名时第 1 级就命中,改成官方拼写后走第 4 级。
    这正是路由表/理由映射那些**写死缩写名**的地方要的语义 —— 它们不必跟着
    改名批一起改字面量,也不会静默失效。

    ⚠ 命中回的是 `known` 里的**那一个原串**,不是入参、也不是归一化键:下游
      (S2 候选块 / reason_category 白名单 / 落库的 audit_reason)全按表内拼写
      对齐,回错拼写等于没解析。
    ⚠ **按名字序扫**:`known` 常是 frozenset,若表里真出现只差大小写/词形的两行
      (不该有,但那张表是反推的),不定序会让同一个输入在两次进程里给出不同
      答案 —— 那种漂移不会报错。
    """
    names = sorted(str(n) for n in (known or ()) if n)
    if not name or not names:
        return None
    return _resolve_sorted(str(name).strip(), names, translate=True)


def _resolve_sorted(s: str, names: list[str], *, translate: bool) -> str | None:
    """输入:候选串 + **已排序**的表内名 → 输出:命中的表内名(不中给 None)。"""
    if not s:
        return None
    if s in names:
        return s
    fold = s.casefold()
    for n in names:
        if n.casefold() == fold:
            return n
    key = norm_category(s)
    if key:
        for n in names:
            if norm_category(n) == key:
                return n
    if translate:
        official = to_official(s)
        if official and official != s:
            return _resolve_sorted(official, names, translate=False)
    return None


__all__ = ["norm_category", "to_official", "resolve"]
