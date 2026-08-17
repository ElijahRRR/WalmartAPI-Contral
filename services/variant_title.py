"""同变体组内标题差异化(旧仓 Feature B 的等价物;纯函数,零 I/O)。

补迁于 2026-08-17,所有者批「都补」。旧仓出处:`auto_listing/mapper.py:1633`
(`differentiate_variant_titles_in_group`)。

**要解决的是哪一类**:同组几个成员的 `productName` 一字不差 —— 详情页上几个
色块/选项写着同样的名字,人分不出选的是哪个。成因是我们的标题来自"亚马逊标题
减品牌",而亚马逊同族成员的标题**常常本来就一样**(差异只在 twister 上)。

规则(逐条对齐旧仓):
  · 组只有 1 个成员 → 不动(没有可比对象,加后缀只是把标题弄脏);
  · 已经互不相同 → 不动(**只补差异,不统一格式** —— 亚马逊给的差异比我们拼的好);
  · 有任一成员标题为空 → 整组不动(空标题另有守卫,`list_new` 判 "标题不足10字符");
  · 全相同 → 每个成员追加 ` - <各维度取值>`,多维按 `variantAttributeNames`
    的顺序拼成 ` - Red, M`;
  · 超 199 字(spec maxLength)时**截 base 保 suffix** —— 后缀才是差异所在,
    截掉它等于白做;
  · 已带同样后缀 → 跳过(幂等:同一批重跑/失败重提不会叠成 ` - Red - Red`)。

⚠ 后缀取的是**载荷里那几个变体属性的现值**,不是原始亚马逊维度值:属性值在
`mp_conform` 里可能被 enum/类型校验改过(整数化、剔除),标题必须跟着最终载荷走,
否则标题说 Red 而 color 字段被剔了。
"""

import logging

logger = logging.getLogger("services.variant_title")

MAX_LEN = 199           # MP_ITEM spec 的 productName maxLength
SEP = " - "


def _suffix(visible: dict) -> str:
    """输入:一个成员的 visible → 输出:` - Red, M` 形式的后缀(拼不出返回 '')。"""
    names = visible.get("variantAttributeNames") or []
    if not isinstance(names, list):
        return ""
    parts = [str(visible.get(n)) for n in names
             if visible.get(n) not in (None, "", [], {})]
    return (SEP + ", ".join(parts)) if parts else ""


def differentiate(members: list[dict]) -> int:
    """输入:同组成员 [{"visible": {...}, ...}](就地改)→ 输出:改了几条。

    调用方保证 members 是**同一个 store + 同一个 variantGroupId** 的那几条。
    跨组混着传会把不相干的标题判成"全相同"。
    """
    if len(members) < 2:
        return 0
    names = [str((m.get("visible") or {}).get("productName") or "").strip()
             for m in members]
    if not all(names):
        return 0                        # 有空标题:整组不动
    if len(set(names)) > 1:
        return 0                        # 已经有差异
    changed = 0
    for m in members:
        visible = m.get("visible") or {}
        suffix = _suffix(visible)
        if not suffix:
            continue
        base = str(visible.get("productName") or "")
        if base.endswith(suffix):
            continue                    # 幂等
        new = base + suffix
        if len(new) > MAX_LEN:
            room = MAX_LEN - len(suffix)
            new = (base[:room].rstrip() + suffix) if room > 0 \
                else (base + suffix)[:MAX_LEN]
        visible["productName"] = new
        changed += 1
    if changed:
        logger.info("同变体组标题全同,已加维度后缀差异化 %d 条", changed)
    return changed
