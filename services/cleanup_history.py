"""旧问题商品历史 → 新库的纯转换积木(cleanup_history_import 用)。

三笔旧账各一个解析器(全是纯函数,不碰库,数据到位前就能测):

  error_items 41.7 万行   → 时间线事件(所有者定稿 2026-08-11:同一 ASIN 的
                            多次报错保留时间线;⇒ 不是 per-run 快照平移)
  seen_sku_categories.json → (sku, 类别) 唯一对(错误统计累计数的唯一真值)
  brand_cache.json         → 已处理 ASIN(防重)+ pending 批次(存档)

时间线拼接的核心规则(**这是导入不膨胀的关键**):旧表是每轮 run 的全量快照,
同一 (store, sku) 连续 N 轮报同一类错就有 N 行;导入只保留**类别发生变化**的
那些行(含首次出现)。丢掉的是"没变"这个非信息,留下的是完整的变迁史——
41.7 万行进来,出去的是每个组合的类别变迁序列。
"""

import logging

from services.problem_products import _RULES

logger = logging.getLogger("services.cleanup_history")

# 类别码 → 中文名(problem_products._RULES 的投影;Z 是无命中兜底)
CODE_TO_NAME = {code: name for code, (name, _) in _RULES.items()} | {"Z": "其他"}
NAME_TO_CODE = {name: code for code, name in CODE_TO_NAME.items()}


def norm_category(v) -> str | None:
    """输入:旧库的类别值(码字母 / 中文名 / 空)→ 输出:类别码;认不出返回 None。

    旧库两个时期的存法不一样(2026-05-14 前 category 为空,后期存码或中文名,
    真实形态要到生产机上才见得到)——两种都认,认不出**不猜**:调用方计数
    告警,人工看过再定映射。空值也返回 None(早期行,类别只能事后重算)。
    """
    t = str(v or "").strip()
    if not t:
        return None
    up = t.upper()
    if up in CODE_TO_NAME:
        return up
    return NAME_TO_CODE.get(t)


class Folder:
    """时间线折叠器:push 一行,类别变了返回事件,没变返回 None。

    做成流式(而不是只有 list→list 的函数)是给 workflows 侧用的:41.7 万行
    要走服务端游标一行行过,折叠逻辑只能有这一份——抄进 workflow 一份的话,
    折叠键规则改一处漏一处,两边导出来的时间线就不一样了。

    折叠键 = 归一码;**认不出的类别用原文当键**——"" 和 "怪类别" 都归一成
    None,若拿 None 当键,空→未识别会被当"没变"吞掉,而那是一次真实变迁
    (两种不同的历史状态)。detail.category 仍记归一码(None),原文进
    raw_category。
    """

    _SENTINEL = object()

    def __init__(self):
        self.last: dict = {}
        self.stats = {"rows": 0, "kept": 0, "unknown_category": 0}

    def push(self, r: dict):
        self.stats["rows"] += 1
        key = (r.get("store"), r.get("sku"))
        raw = str(r.get("category") or "").strip()
        code = norm_category(raw)
        if code is None and raw:
            self.stats["unknown_category"] += 1
        fold_key = code if code is not None else (raw or None)
        if self.last.get(key, self._SENTINEL) == fold_key:
            return None                   # 类别没变:非信息,丢
        self.last[key] = fold_key
        self.stats["kept"] += 1
        return {
            "sku": r.get("sku"), "store": r.get("store"),
            "occurred_at": r.get("run_ts"),
            "detail": {"category": code,
                       "name": CODE_TO_NAME.get(code, "未识别"),
                       "reason": (str(r.get("reason") or "")[:200]) or None,
                       "raw_category": (raw or None) if code is None else None},
        }


def timeline(rows: list[dict]) -> tuple[list[dict], dict]:
    """输入:旧 error_items 行(**必须已按 run_ts 升序**)
    → 输出:(变迁事件列表, 统计)。Folder 的一次性便捷封装。"""
    f = Folder()
    events = [e for r in rows if (e := f.push(r)) is not None]
    return events, f.stats


def parse_seen(obj) -> list[tuple[str, str]]:
    """输入:seen_sku_categories.json 反序列化结果 → 输出:[(sku, 类别码)]。

    旧文件形态待生产机实证,先兼容两种最可能的:
      {"SKU1": ["C", "E"], ...}          sku → 类别列表
      [["SKU1", "C"], ["SKU1", "E"], ...] 平铺对
    其余形态直接抛错报出真实形状——猜错静默丢对,累计数就永远对不上了。
    """
    if isinstance(obj, dict) and ("processed_asins" in obj
                                  or "pending_batches" in obj):
        # 2026-08-11 生产实操差点踩到:seen/brand 两个 -p 参数传反时,
        # brand_cache 的形状在这里会被"成功"解析成 0 对——静默空导比报错
        # 危险得多(累计数缺一笔且无人知道)。按形状指纹当场拒绝。
        raise ValueError("这是 brand_cache.json 的形状(含 processed_asins/"
                         "pending_batches 键)——-p seen 与 -p brand 传反了?")
    pairs: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for sku, cats in obj.items():
            for c in (cats if isinstance(cats, (list, tuple)) else [cats]):
                code = norm_category(c)
                if sku and code:
                    pairs.append((str(sku), code))
        if obj and not pairs:
            raise ValueError("seen 文件非空却解析出 0 对——类别值全认不出,"
                             "多半是文件传错或格式变了,先人工看一眼")
        return pairs
    if isinstance(obj, list) and all(isinstance(x, (list, tuple)) and len(x) == 2
                                     for x in obj):
        for sku, c in obj:
            code = norm_category(c)
            if sku and code:
                pairs.append((str(sku), code))
        return pairs
    raise ValueError(f"seen_sku_categories 形态未识别:顶层是 {type(obj).__name__},"
                     f"样本 {str(obj)[:120]!r}——先人工确认结构再补解析分支")


def parse_brand_cache(obj) -> tuple[list[str], list[dict]]:
    """输入:brand_cache.json 反序列化结果 → 输出:(已处理 ASIN 列表, pending 批次)。

    processed 进 ops.dedupe(scope='cleanup:brand_asin')——品牌采集(Step 6)
    开跑前必须先有它,否则 2544 个已处理 ASIN 会被重新采一遍、往飞书重复追加。
    pending 批次原样存档:旧采集器上的在途批次结果多半早已过期,但"当时有哪些
    在途"这条事实不该丢,Step 6 接手时人工核一眼再销。
    """
    if not isinstance(obj, dict):
        raise ValueError(f"brand_cache 形态未识别:顶层是 {type(obj).__name__}")
    if obj and "processed_asins" not in obj and "pending_batches" not in obj:
        # 同上的反向:seen 的形状喂进来会"成功"解析成 (0 个 ASIN, 0 批次)
        raise ValueError(f"brand_cache 缺 processed_asins/pending_batches 键,"
                         f"顶层键样本:{sorted(obj)[:5]}"
                         f"——-p seen 与 -p brand 传反了?")
    processed = [str(a) for a in (obj.get("processed_asins") or []) if a]
    pending = obj.get("pending_batches") or []
    if not isinstance(pending, list):
        pending = [{"raw": pending}]
    return processed, pending
