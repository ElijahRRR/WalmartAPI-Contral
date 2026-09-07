"""政策类别名的归一化 —— 仓内**唯一一处**(纯函数,零 DB、零 LLM、零 registry)。

政策表 `audit.walmart_prohibited_policy` 的 `category_en` 是**全链唯一键**
(定稿 `docs/policy_sync.md` §十.7):L3 提示词的 S2 候选块与 S4 政策块、L3 的
`policy` 白名单、`audit_reason` 的类别、报错文本 join 全指着它。于是"两个字符串
是不是同一个政策类别"这件事在仓内被问了好几遍,2026-09-02 改名批之前四处各写
各的(policy_sync 对行 / error_taxonomy 报错 join / audit_l3 路由表 /
audit_l2 推政策名)。按 CLAUDE.md「每个能力只有一条实现路径」,归一化从此
**只在这里**实现;后两处那些写死的缩写名连同它们的规则已在 2026-09-02/09-03
两批里整体删除。

两个函数,职责不重叠:

  · `norm_category` —— 只做**词形**归一(casefold / `&`↔`and` / 逗号 / 括号后缀 /
    撇号 / 词尾单复数)。它敢削词形,是因为 44 个官方名两两归一化不碰撞
    (测试钉死);**不做语义合并**,`Drugs & Paraphernalia` ↔
    `Drugs and Drug Paraphernalia` 这种缩写差故意对不上 —— 那要人来裁决;
  · `resolve` —— 三级(精确 / casefold / 词形键)+ 实时表内容,回**表内原拼写**。

⚠ **2026-09-03 C 批删掉了第 4 级 `to_official`**(查
  `registry.resources.POLICY_LEGACY_NAMES` 认领旧缩写名)。它是**改名过渡期
  产物**:2026-09-02 `policy_sync` 真跑已经把表内名全部改成官方拼写,那张
  映射表从此指向的是"表里已经不存在的旧名"—— 留着 = 一份永远不会再被验证的
  历史映射,而且给了一条"归一化认不出就翻译一下再试"的暗道。
  今后再遇到改名:`policy_sync` 报告的「疑似改名对」是**人工入口**,由人裁决,
  不是代码偷偷合并(规格 §4.3)。
⚠ 铁律 1:本模块在 services 层,只 import `re`,**不许**反向 import workflows
  —— `norm_category` 的实现从 `workflows/policy_sync` 搬来这里,是为了让
  services 侧的消费者能用它,而不是让 services 去 import 工作流。
⚠ 归一化放宽 = 把两条真不同的政策合成一条。改这里的规则前先想清楚代价:
  policy_sync 那边合错了是**覆盖正文**,audit 这边合错了是**类别指错政策**。
"""

import re

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
    新增,不许在这儿偷偷归到别的政策上(§二「对不上的不猜」)。人工入口是
    `policy_sync` 报告里的「疑似改名对」清单。
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


def resolve(name: str | None, known) -> str | None:
    """输入:任意拼写的政策名 + 实时 `category_en` 集合 → 输出:**表内原拼写**。

    三级,顺序即"敢猜的程度",认不出给 None(绝不编一个表里没有的名字):

      1. 精确等值;
      2. casefold 等值(LLM/报错正文的大小写靠不住);
      3. `norm_category` 键等值(词形差:`&`↔`and` / 逗号 / 括号后缀 / 撇号 /
         单复数)。

    ⚠ **没有第 4 级**(2026-09-03 C 批删):旧缩写名经
      `registry.resources.POLICY_LEGACY_NAMES` 认领那一级是改名过渡期的桥,
      改名 2026-09-02 已落地,表内就是官方拼写。语义缩写(`Electronics & RF`
      ↔ `Electronics and Radio Frequency Devices`)从此**解析不到就是解析不到**
      —— 那是正确答案:它要人来裁决是改名还是新增,不是代码替它选一个。

    ⚠ 命中回的是 `known` 里的**那一个原串**,不是入参、也不是归一化键:下游
      (S2 候选块 / L3 的 policy 白名单 / 落库的 audit_reason)全按表内拼写
      对齐,回错拼写等于没解析。
    ⚠ **按名字序扫**:`known` 常是 frozenset,若表里真出现只差大小写/词形的两行
      (不该有,但那张表是反推的),不定序会让同一个输入在两次进程里给出不同
      答案 —— 那种漂移不会报错。
    """
    names = sorted(str(n) for n in (known or ()) if n)
    if not name or not names:
        return None
    return _resolve_sorted(str(name).strip(), names)


def _resolve_sorted(s: str, names: list[str]) -> str | None:
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
    return None


__all__ = ["norm_category", "resolve"]
