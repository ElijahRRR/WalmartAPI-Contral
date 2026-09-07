"""问题商品归类积木(problem_scan 用;规则逐字移植自旧
feishu_sync.py/relisting.py,2026-08-06 从 erpAPI@d5237fb 提取)。

归类输入只有一个:unpublished_reasons 文本(多条以 " | " 拼接),小写子串匹配。

⚠ **反补机制 2026-08-28 所有者定稿退役**:「publishedStatus 不是 PUBLISHED 的
都进行删除,不再修改 End Date 救商品」。归类不再决定处置走向(一律删除),
只服务三件事:病历(problem_categorized 事件)、黑名单收集(2026-09-04 换轨后
改吃 `error_taxonomy.PERMANENT_CODES`,本模块不再参与入选判据)、
摘要按类计数。反补构造器(build_relist_item/pick_product_id/NEW_END_DATE)
与计数常量(MAX_ATTEMPTS/ATTEMPT_RESET_DAYS)、Stage 豁免(is_stage_pending)
随之删除 —— 需要考古看 git;A 类反补当年的语病也一并留档:「end date has
passed」同时是沃尔玛给退市商品打的标记(批量退市 = Site End Date 设为过去),
把它当可修复故障反补,等于对退市档案走官方复活通道(2026-08-28 事件实证)。
"""

# 类别码 → (中文名, 判定函数)。匹配用旧系统原字符串,勿改。
_RULES = {
    "A": ("过期", lambda t: "end date has passed" in t),
    "B": ("禁售", lambda t: "prohibited product policy" in t
          or ("prohibited due to" in t and "walmart" in t)
          or "for violating walmart's marketplace" in t
          or "reference code biz" in t or "cpsc recall" in t
          or "safety warning" in t or "circumvent walmart" in t),
    "C": ("品牌", lambda t: "partnered with select brands" in t
          or "biz-cn" in t or "restricts certain brands" in t),
    "D": ("价格", lambda t: "price gouging" in t),
    "E": ("知产", lambda t: "intellectual property" in t),
    "F": ("限类", lambda t: "restricted to certain sellers" in t
          or "restricted category that requires" in t),
    "G": ("药品", lambda t: "drugs and drug paraphernalia" in t),
    "H": ("信息", lambda t: "tax code" in t or "no price was found" in t
          or "shipping information was not added" in t),
    "I": ("内容", lambda t: "content standards" in t or "content policy" in t
          or "authenticity claims" in t
          or "title wasn't in the correct format" in t
          or "missing a primary image" in t or "main image url" in t),
    "J": ("特殊", lambda t: "preorder program" in t or "restored program" in t
          or "pre-owned program" in t or "pre-owned policy" in t
          or "restored policy" in t or "refurbished or restored" in t
          or "stage status until you go live" in t),
    "K": ("审查", lambda t: "flagged by our internal team" in t),
    "L": ("系统", lambda t: "internal error occurred" in t),
}
# 严重性顺序:具体归类优先,B 通用禁售次之,A 过期最后(旧注释原文)
_SEVERITY_ORDER = ["C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "B", "A"]


# ⚠ `categorize()`(旧 A-L 单字母归类引擎)已于 2026-09-04 删除。
# 所有者:「删除旧码,我们已经迁移到新码,旧码不需要留。把口径做统一」。
# 全仓归类只有一条路:`services/error_taxonomy.classify_reasons`。
# **`_RULES` 保留**,但它现在只是一张「旧码 → 中文名」的查表,给**读历史数据**
# 的两处用(`services/cleanup_history` 认旧库的类别值、`error_reclass_report`
# 把黑名单表里入选那一刻的旧码显示成人话)—— 读历史 ≠ 判据路径。


# (反补构造器与 Stage 豁免原在此处,2026-08-28 退役删除 —— 见模块头注)
