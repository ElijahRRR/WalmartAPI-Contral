"""SKU → ASIN 清洗规则(**唯一出处**,Python 单实现,批量清洗也走这里)。

⚠ 2026-09-02(SKU 改造批次 0a)起,身份有**两条腿**:登记簿 catalog.listing_sources
里 source_type='amz' 行的 source_key 是权威身份键,模式提取(extract_asin)只为存量
兜底。两条腿的优先级与形态闸收在本模块的 `pick_asin` 里(**唯一出处**),批量反查
是 `resolve_many` / 单条壳 `resolve`,三个入口的分工见文件末尾那一段。

沃尔玛侧 sku 是订货号,与产品源头侧 asin **不保证相等**(所有者给样
2026-08-11,推翻了"sku=asin"的全局约定;跟卖行早已是已知例外):

  JTZW-D01027HVK3W-38     「前缀-源头码-价格」三段式,中段即源头码
  XKJ-B0GXX75JN5-39.98      (价格可带小数;源头码 8~14 位、字母开头)
  YP-B09TDMGVRW-188.88
  B0ABCDEFGH              裸 ASIN(10 位含字母)
  102460018738            纯数字 = walmart item id,模式提不出源头码,
                          须倒查 catalog.walmart_items(item_id → 订货号
                          → 再提取);查不到就保持原文

原则:提得出就提,提不出**保留原文并可报告**——绝不猜。规则不全是常态
(所有者:偶尔清洗一次),新形态先进「其他」桶报出来,人认了再扩规则。

三个批量/单条入口,分工不许混(2026-09-02 定):
  · resolve(conn, store, sku)      单条壳,只给零星调用点(services 内部)
  · resolve_many(conn, pairs)      **纯登记簿 + 形态兜底**的批量反查;
                                   services 内部消费方(order_lines /
                                   product_events / blacklist)用它
  · resolve_pairs(conn, pairs)     **清洗类工作流的唯一批量入口**,在
                                   resolve_many 之上多一跳纯数字 item_id
                                   倒查(要查 walmart_items),并产出形态计数
workflows/ 只准用 resolve_pairs;直接用 resolve_many / 拼 listing_sources
的 SQL 由 tests/test_sku_guard.py 拦(守门白名单)。
"""

import re
from collections import Counter

# 裸 ASIN:10 位大写字母数字、至少含一个字母(纯数字 10 位是 item id 不是 ASIN)
_PLAIN = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{10}$")
# 三段式:前缀(字母开头 ≤8 位,**可含数字**——2026-08-11 生产实证
# A109-B08QF9XLMH-02 这类 208 个被纯字母版规则冤枉)- 源头码(字母开头
# 8~14 位)- 价格(可小数,可前导零)
_WRAPPED = re.compile(r"^[A-Z][A-Z0-9]{0,7}-([A-Z][A-Z0-9]{7,13})-\d+(?:\.\d+)?$")
_NUMERIC = re.compile(r"^\d{6,}$")


def extract_asin(sku) -> str | None:
    """输入:沃尔玛侧 sku → 输出:源头 asin(提不出返 None,调用方决定兜底)。"""
    s = str(sku or "").strip().upper()
    if _PLAIN.fullmatch(s):
        return s
    m = _WRAPPED.fullmatch(s)
    return m.group(1) if m else None


def is_standard_asin(v) -> bool:
    """输入:候选码 → 输出:是否标准 ASIN(10 位含字母)。推送采集前的
    过滤闸:非标准码(纯数字 item id / 11 位源头码 / 原文兜底)推去采集
    只会永远采不到 → 永远缺品牌 → 永远再推,无限循环。"""
    return bool(_PLAIN.fullmatch(str(v or "").strip().upper()))


#: 「标准 ASIN + 尾巴」形态(所有者 2026-09-03 给样:`B0822D9QQKS59` 应为
#: `B0822D9QQK`)。前 10 位恰好是一个合法裸 ASIN,后面 1~5 位是运营自己贴的尾码。
#: ⚠ **这条规则只够"猜"**:它与"11~15 位的真源头码"形态完全相同,没有任何本地
#: 信息能把两者分开(`B0822D9QQKS59` 也可能就是一个 13 位的真码)。所以
#: `propose_source_key` 把它标成 `guess`,由人逐行认 —— 猜错的后果是往登记簿
#: 灌一个"看起来很像 ASIN、其实不存在"的键,而键错了下游一声不吭:采不到源
#: 数据 → 被判成"源头没了" → 清库存/删除。
_SUFFIXED = re.compile(r"^([A-Z0-9]{10})[A-Z0-9]{1,5}$")

#: `propose_source_key` 的依据取值:'asin'/'wrapped' 是**提取**(规则认得),
#: PROPOSE_GUESS 是**猜测**(要人认),'' 是提不出。消费方按这个常量分流,
#: 不写字面量。
PROPOSE_GUESS = "guess"


def propose_source_key(sku) -> tuple[str | None, str]:
    """输入:沃尔玛侧 sku → 输出:(机器提议的来源码, 提议依据)—— 提不出返 (None, '')。

    只给**人工归类导入**(workflows/sources_reclassify)的预览列用:登记簿里
    `source_type='unknown'` 的存量行按路由铁律进不了任何自动维护,人要逐行认
    出"这一串里的源头码是哪一段",本函数是给他打的草稿,**不是判据**。

    三档,别混:
      · `'wrapped'` / `'asin'` —— 走 `extract_asin` 的既有规则(三段式中段 /
        裸 ASIN),规则认得,可以直接采用;
      · `PROPOSE_GUESS` —— 「标准 ASIN + 尾巴」,**只是猜**,调用方必须让人
        逐行确认,不许自动应用(理由见 `_SUFFIXED` 头注);
      · `''` —— 提不出(纯数字 item id、跟卖人工号、12 位不透明新码……)。
        **绝不猜**,留空让人填,与 `extract_asin` 同一条纪律。

    不透明新码显式挡在猜测之外:12 位新码的形态与「10 位 ASIN + 2 位尾巴」
    撞脸,不挡就会给一个 mint 发的码提议出一个不存在的 ASIN(而它本来就有
    正确的 source_key,压根不该出现在待归类清单里)。
    """
    s = str(sku or "").strip().upper()
    a = extract_asin(s)
    if a:
        return a, classify(s)
    from services import sku_codec        # 惰性导入:sku_codec → product_events → 本模块
    if sku_codec.is_opaque(s):
        return None, ""
    m = _SUFFIXED.fullmatch(s)
    if m and is_standard_asin(m.group(1)):
        return m.group(1), PROPOSE_GUESS
    return None, ""


def classify(sku) -> str:
    """输入:sku → 输出:形态桶 'asin'/'wrapped'/'numeric'/'other'(清洗预览用)。"""
    s = str(sku or "").strip().upper()
    if _PLAIN.fullmatch(s):
        return "asin"
    if _WRAPPED.fullmatch(s):
        return "wrapped"
    if _NUMERIC.fullmatch(s):
        return "numeric"
    return "other"


# ── 批量清洗:模式提取 + 纯数字倒查两跳(两个清洗工作流共用)──────────────
# 纯数字形态那一跳:item_id → 沃尔玛订货号 → 再走 extract_asin。
# ⚠ 这条 SQL 与 numeric_resolved 的记账此前在 order_asin_normalize 与
# sku_normalize 各写一份(**字节相同**),而规则本身早就只在本模块 ——
# 缺的正是"倒查那一跳"没跟着沉下来,于是两份拷贝各自演化(2026-08-27 收编)。
# 2026-09-02 拆两级:第一级带 store(切码后同一 item_id 反查出的订货号还要再过
# 店维登记簿),第二级仍是这条全局 SQL —— **它是既有行为,不许删**:今天订单行
# 落在 T2 店、item_id 只在 T1 店的 walmart_items 里有行时,靠的就是它。
_ITEMID_STORE_SQL = """
SELECT DISTINCT store, item_id, sku FROM catalog.walmart_items
WHERE store = ANY(%s) AND item_id = ANY(%s)
"""

_ITEMID_SQL = """
SELECT DISTINCT item_id, sku FROM catalog.walmart_items
WHERE item_id = ANY(%s)
"""


def resolve_pairs(conn, pairs: list[tuple[str | None, str]]) -> tuple[dict, dict]:
    """输入:连接 + [(店铺, sku)] → 输出:({(店铺,sku): asin}, 计数)。

    ⚠ **计数混着两种单位,别读串**(摘要最容易被误读的地方):
    `asin`/`wrapped`/`numeric`/`other`/`numeric_resolved` 五档按 **distinct sku**
    计(与 2026-08-27 的 resolve_skus 逐字同口径,所以摘要与改前对得上);
    `pairs`/`registry_differs`/`numeric_cross_store` 三档按 **(店,sku) 组合** 计。

    三跳:① 登记簿(`resolve_many`:amz 行给 source_key,其余一律回落
    `extract_asin`);② 仍没解出的纯数字按 (店, item_id) 倒查 walmart_items,
    反查出的订货号**再过一次登记簿**(切码后它本身就是不透明码);③ 还没解出的
    按 item_id **全局**倒查一次(既有行为,保住跨店补齐的覆盖面)再走
    `extract_asin`。**解析不了的不进映射**(调用方留 NULL,绝不猜)。

    纯读,不改任何行;真正的 UPDATE 在各工作流自己的 `_FILL_SQL`
    (打哪张表是两条链唯一的真差异)。
    """
    pairs = list(dict.fromkeys(pairs))          # 去重保序
    mapping: dict = {}
    buckets: Counter = Counter()
    for k in dict.fromkeys(k for _s, k in pairs):
        buckets[classify(k)] += 1
    buckets["pairs"] = len(pairs)

    reg = resolve_many(conn, pairs)
    mapping.update(reg)
    # 登记簿给出的答案与形态提取不同的组合数 —— **切换前它必须是 0**:非 0 说明
    # 登记簿里有 source_key ≠ sku 的存量 amz 行(schema.sql 回填正则缺右锚那批)
    buckets["registry_differs"] = sum(
        1 for (_s, k), a in reg.items() if a != extract_asin(k))

    still = [(s, k) for (s, k) in pairs
             if (s, k) not in mapping and classify(k) == "numeric"]
    if still:
        got: set = set()                        # 倒查救回的 distinct sku
        cross = 0                               # 只靠第二级救回的组合数
        scoped = [(s, k) for s, k in still if s is not None]
        if scoped:
            with conn.cursor() as cur:
                cur.execute(_ITEMID_STORE_SQL,
                            ([s for s, _ in scoped], [k for _, k in scoped]))
                hits = {(st, iid): sku for st, iid, sku in cur.fetchall()}
            back = resolve_many(conn, [(s, hits[(s, k)])
                                       for s, k in scoped if (s, k) in hits])
            for s, k in scoped:
                a = back.get((s, hits.get((s, k))))
                if a:
                    mapping[(s, k)] = a
                    got.add(k)
        rest = [(s, k) for s, k in still if (s, k) not in mapping]
        if rest:
            with conn.cursor() as cur:
                cur.execute(_ITEMID_SQL, ([k for _s, k in rest],))
                hits2 = dict(cur.fetchall())    # item_id → 沃尔玛订货号
            for s, k in rest:
                a = extract_asin(hits2.get(k))
                if a:
                    mapping[(s, k)] = a
                    got.add(k)
                    cross += 1
        if got:
            buckets["numeric_resolved"] = len(got)
        if cross:
            buckets["numeric_cross_store"] = cross
    return mapping, dict(buckets)


def samples(pairs: list[tuple[str | None, str]], buckets: dict) -> dict:
    """输入:待洗 (店, sku) 对 + 形态计数 → 输出:{形态: 前 5 个样本}(只给没解析出的桶)。

    只报 numeric/other 两个桶:asin/wrapped 是提得出的,不需要人认。
    "规则不全是常态"——新形态先进「其他」桶带样本报出来,人认了再扩规则。
    样本**先按 sku 去重再取前 5**,且只放 sku 串不放店名:样本的全部作用是让人
    认新形态,同一个 sku 跨 5 家店就能把 5 个样本位全占满,那等于把它废掉。
    """
    def _five(kind):
        seen, out = set(), []
        for _st, s in pairs:
            if classify(s) == kind and s not in seen:
                seen.add(s)
                out.append(s)
                if len(out) == 5:
                    break
        return out
    return {k: _five(k) for k in ("numeric", "other") if buckets.get(k)}


# ── 登记簿那一跳(SKU 改造批次 0a,2026-09-02)────────────────────────────────
# 三个入口的分工(**别在旁边另起第四条**):
#   · pick_asin(source_key, sku)  纯函数,一对一;五个调用点共用的优先级规则,
#     谁也别再各写一遍 `key or extract_asin(sku)`。
#   · resolve_many(conn, pairs)   services 内部的**有界批量反查**(几十~几百对)。
#   · resolve(conn, store, sku)   单条薄壳,内部就是 resolve_many。
# ⚠ **全表级取数一律在 SQL 里 LEFT JOIN 登记簿再取 coalesce(ls.source_key, w.sku)**,
#   不要拿十万对 (store, sku) 去 unnest —— 那是把一次 JOIN 换成一次巨型数组传参。
# ⚠ 批次 0b 的 resolve_pairs(清洗工作流的唯一批量入口,含纯数字倒查那两级)
#   **建在 resolve_many 之上**,不是旁边另起的第二条批量入口(conventions §六)。

#: 登记簿只按 (store, sku) 反查 amz 身份键。**不按 abandoned_at 过滤**:旧码
#: 带着订单/售后回来时必须还查得到(消费方契约,sku_plan §5.3)。
_REG_SQL = """
SELECT store, sku, source_key FROM catalog.listing_sources
JOIN unnest(%s::text[], %s::text[]) AS t(s, k) ON store = t.s AND sku = t.k
WHERE source_type = 'amz' AND source_key IS NOT NULL
"""


def pick_asin(source_key, sku) -> str | None:
    """输入:登记簿 amz 行的 source_key(可空)+ sku → 输出:ASIN(登记簿优先但
    要过形态闸,两条腿都提不出返 None)。

    **登记簿只是优先级,不是免检通道**:source_key 是运营在上架表里手填、由
    list_new 原样落库的(读表只 strip 不 upper),裸取会把一个小写 ASIN 变成
    下游集合里的垃圾键 —— 而真 ASIN 仍然缺席,于是"已在架"被判成"不在架",
    已上架的品被重新派工。故这条腿与 extract_asin 同口径:先归一(strip+upper)
    再过裸 ASIN 形态闸,不过就落回模式提取。
    """
    k = str(source_key or "").strip().upper()
    if k and is_standard_asin(k):
        return k
    return extract_asin(sku)


def resolve_many(conn, pairs) -> dict:
    """输入:连接 + [(store, sku)] → 输出:{(store, sku): asin}(有界批量反查)。

    一条 SQL 取登记簿键,逐对过 `pick_asin`;**提不出的不进映射**(与
    resolve_pairs 同纪律:调用方留 NULL,绝不猜)。纯读,不改任何行。

    ⚠ store 为 None 的对(product_events 的平台级事件行)**原样进出**:返回的键
    与传进来的对逐字相同,不是 "None" 字符串。登记簿主键是 (store, sku),
    store 为空压根查不到 —— 这批对直接走形态腿,一次往返都不占。
    """
    want = [(st if st is None else str(st), str(sk)) for st, sk in pairs]
    if not want:
        return {}
    keys: dict = {}
    probe = [p for p in want if p[0] is not None]
    if probe:
        with conn.cursor() as cur:
            cur.execute(_REG_SQL, ([p[0] for p in probe], [p[1] for p in probe]))
            keys = {(st, sk): key for st, sk, key in cur.fetchall()}
    out = {}
    for pair in want:
        a = pick_asin(keys.get(pair), pair[1])
        if a:
            out[pair] = a
    return out


def resolve(conn, store, sku) -> str | None:
    """输入:连接 + 店铺 + sku → 输出:ASIN(提不出返 None)。resolve_many 的单条壳。"""
    return resolve_many(conn, [(store, sku)]).get((str(store), str(sku)))
