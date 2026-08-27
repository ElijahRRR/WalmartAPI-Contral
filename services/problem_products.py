"""问题商品归类积木(problem_product_cleanup 用;规则逐字移植自旧
feishu_sync.py/relisting.py,2026-08-06 从 erpAPI@d5237fb 提取,别优化)。

归类输入只有一个:unpublished_reasons 文本(多条以 " | " 拼接),小写子串匹配。
"""

NEW_END_DATE = "2049-12-31T00:00:00.000Z"   # 纯日期会被拒,必须 ISO datetime(旧实证)
MAX_ATTEMPTS = 2            # 同 SKU 反补 N 次仍 UNPUBLISHED → 转 DELETE 兜底
ATTEMPT_RESET_DAYS = 30     # 反补计数 30 天后重置(给 SKU 二次机会)

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


def categorize(reason_text: str | None) -> tuple[str, str]:
    """输入:错误原因文本 → 输出:(类别码, 中文名);无命中 ('Z', '其他')。"""
    t = (reason_text or "").lower()
    for code in _SEVERITY_ORDER:
        name, pred = _RULES[code]
        if pred(t):
            return code, name
    return "Z", "其他"


def is_stage_pending(reason_text: str | None) -> bool:
    """输入:下架原因文案 → 输出:是否 Stage 待发布。

    不是错误,排除删除;独立于 categorize,旧 main() 同款。
    """
    return "stage status until you go live" in (reason_text or "").lower()


def pick_product_id(gtin, upc) -> tuple[str, str] | None:
    """输入:gtin/upc → 输出:(productId, productIdType) 或 None(不可反补)。

    旧优先级:GTIN(补足14位)> UPC(补足12位);都不满足 8 位下限 → None。
    """
    g = str(gtin or "").strip()
    if len(g) >= 8:
        return g.zfill(14), "GTIN"
    u = str(upc or "").strip()
    if len(u) >= 8:
        return u.zfill(12), "UPC"
    return None


def build_relist_item(sku: str, gtin, upc) -> dict | None:
    """输入:sku/gtin/upc → 输出:反补(设置商品到期日期)MPItem;无 productId 返 None。"""
    pid = pick_product_id(gtin, upc)
    if pid is None:
        return None
    return {"Orderable": {"sku": str(sku),
                          "productIdentifiers": {"productId": pid[0],
                                                 "productIdType": pid[1]},
                          "endDate": NEW_END_DATE}}
