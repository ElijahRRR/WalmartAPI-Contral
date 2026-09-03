"""店铺治理配置的快照与 diff(ops.cursors['store_config'] → ops.store_events)。

回答的是「**谁在什么时候把这家店的配置改了**」。这些格子是人在飞书上改的,
改完没有任何痕迹:类目1 被清空、配送限制从 fba 改成 fbm、单店最大在线数
从 3000 改成 0 —— 每一样都会让分配/上架/维护三条链当天起换一套行为,而
事后回查只能看到"链的行为变了",看不到"配置什么时候变的"。本模块每轮把
两张人工表拍一张快照存进 ops.cursors,与上一张逐格比,变化落治理类事件。

**它是观测器,不是判定器**:只记录变化,不评价对错、不阻止任何链。

三条纪律(与一期状态迁移同源,不重复实现别的口径):
  ① **首次快照不产事件**:没有上一版就没有"变化",首次观测不是变化;
  ② **飞书失败不产事件、不覆盖快照**:拿不到当下真值时,把"读不到"记成
     "被清空"会造出一整批假事件(而且下一轮读到了又造一批"改回来"),
     这是一期「空=没抓到不是状态」铁规的配置版;
  ③ **值存原文**:空串 ≠ 没填 ≠ 垃圾值,三态的处置各链不同(限额表同一列
     "没填"在分配侧是不限、在上架侧是回落全局基线)。存解析值等于替消费方
     先解释一遍,而它们的解释互相矛盾且都对。

读法故意不复用 `store_limits` / `store_targets` 的任何 loader:那些 loader
一人只取一列、还各自把空值折成默认值 —— 快照要的恰恰是**未经解释的原文**,
而且要**一次拿齐所有登记列**。

**值只收「已登记列」**(2026-09-01 生产实跑改口径,详见 `_limits_snapshot`):
未登记列没有任何代码消费它,改了系统行为一个字节都不变,不是治理事件;
但"表里多出/少了一列"本身要知道,单独记一条 `store_limits_columns_changed`。
"""

import json
import logging

from api import feishu
from registry import resources
from services import store_events as se

logger = logging.getLogger("services.store_config")

#: 快照结构版本。形状变了就 +1,`diff` 见到版本不同当**首次快照**处理
#: (拿两套形状硬比会产出满屏假事件,那比不报警更坏)。
#: v2(2026-09-01):`limits` 的值只收登记列,未登记列名另存 `limits_extra_cols`。
#: ⚠ v1 的 `limits` 里**存着未登记列的值**(SourceID 之类),拿它跟 v2 硬比会
#: 把那些列判成"整体消失" —— 版本不同一律当首次快照,见 `_version_migrated`。
SNAPSHOT_VERSION = 2

_CURSOR = "store_config"
SOURCE = "store_config"

# ── 列 → severity(2026-08-30 按"改错了当天有多疼"定级)────────────────────
# high:改错当天就整店换行为或整店停摆(类目准入 / 配送限制 / 容量归零 /
#       整店清零);mid:改错影响一条链的节奏(限额、时长、倍率);
# info:目标值只喂分配打分,改了不会让任何东西立刻做错事。
_SEV_HIGH = "high"
_SEV_MID = "mid"
_SEV_INFO = "info"

#: 属性名 → severity 档;取列名走 registry(铁律 3:字段名只准从 registry 取)
_SEV_BY_ATTR = {
    "category1": _SEV_HIGH, "category2": _SEV_HIGH, "category3": _SEV_HIGH,
    "channel_limit": _SEV_HIGH,
    "max_daily_list": _SEV_MID, "max_daily_retire": _SEV_MID,
    "lead_limit": _SEV_MID,
    "fba_range1": _SEV_MID, "fba_range2": _SEV_MID,
    "fbm_range1": _SEV_MID, "fbm_range2": _SEV_MID,
    "target_gmv_daily": _SEV_INFO, "target_orders_daily": _SEV_INFO,
}

#: 「0 与非 0 之间」才是 high 的两列:0 在它们身上是**开关**不是数值 ——
#: 单店最大在线数 0 = 这家店一件都不许再上;库存特殊要求 0 = 整店清零。
#: 两列之间调数值(3000→2500)只是配额调整,mid。
_ZERO_IS_A_SWITCH = {"max_online", "inventory_note"}

#: 在 registry 登记了、但没在上面分档的列(如 `maint_node`,或将来新加的闸)
#: = mid。不默认 info:新列多半是新加的闸,当"不值一提"会让它第一次生效时
#: 没人知道。**未登记列走不到这里** —— 它们压根不产逐格事件(见 `_limits_snapshot`)。
_SEV_DEFAULT = _SEV_MID


def _limits_sev_map() -> dict[str, str]:
    """输入:无 → 输出:{限额表列名: severity}(属性名映射翻译成列名)。"""
    f = resources.RETIRE_LIMITS.fields
    return {getattr(f, attr): sev for attr, sev in _SEV_BY_ATTR.items()
            if hasattr(f, attr)}


def _zero_switch_cols() -> set[str]:
    f = resources.RETIRE_LIMITS.fields
    return {getattr(f, a) for a in _ZERO_IS_A_SWITCH if hasattr(f, a)}


def _is_zero(v: str) -> bool:
    """输入:单元格原文 → 输出:它是不是"填了 0"(空串不是 0,是没填)。"""
    s = str(v or "").strip()
    if not s:
        return False
    try:
        return float(s) == 0.0
    except ValueError:
        return False


def _limits_severity(col: str, old: str, new: str) -> str:
    """输入:列名 + 变化两端原文 → 输出:severity(依据见上面的分档注释)。"""
    if col in _zero_switch_cols():
        return _SEV_HIGH if _is_zero(old) != _is_zero(new) else _SEV_MID
    return _limits_sev_map().get(col, _SEV_DEFAULT)


# ── 拍快照 ───────────────────────────────────────────────────────────────────

def _registered_limits_cols() -> set[str]:
    """输入:无 → 输出:限额表在 registry 登记过的列名全集(不含店铺列)。

    ⚠ 列名唯一出处是 registry(铁律 3),所以这里必须**遍历登记项**而不是抄
    一份清单 —— 抄的那份会跟 registry 各漂各的。`resources._fields()` 造的是
    `SimpleNamespace`,取全部值只能走 `vars()`(不是 dataclass,没有 fields())。
    """
    f = resources.RETIRE_LIMITS.fields
    return set(vars(f).values()) - {f.store}


def _limits_snapshot() -> tuple[dict[str, dict], list[str]]:
    """输入:无 → 输出:({店铺: {**登记列**: 原文}}, [未登记列名…排序])。

    **整表照拉**(不传 `field_names`):要知道表里有没有多出一列,就得先看见
    它。但**只有登记列的值进快照**,未登记列**只留列名、绝不留值**。

    为什么(2026-09-01 生产实跑定稿,原先是整表未裁剪):
      · 表里有 `SourceID` 这种飞书内部字段,值是 base64 复合键,**里面含行内容
        的哈希** —— 谁动一下那张表的任何一格,它就跟着变。整表入快照的后果是
        凭空刷出一批 `store_limits_changed`(所有者实跑第一轮 4 条变更里 2 条
        是这种噪音)。治理账本的价值是"谁改了配置事后查得到",灌噪音会把真
        信号淹掉,而账本只追加、删不掉。
      · 口径依据:未登记列**没有任何代码消费它**,改了它系统行为一个字节都不
        变,所以它不是治理事件。而"有人往表里加了一列"本身是值得知道的信号,
        单独记一条 `store_limits_columns_changed`(表结构级,一次一条)。
      · 原先那条"新列的第一次生效要能进事件流"的担心不成立:新列要生效,必须
        先有人写代码并把列名登记进 registry;登记那一刻它就自动进快照,按既有
        「缺键约定」首次出现不产事件 —— 正是想要的。
      · 只记列名不记值:记了值等于把噪音换个地方存,下一轮照样比出一堆变化。

    ⚠ 未登记列名取的是**所有记录的并集**(列是表的属性,不是某一行的属性);
    飞书对空单元格不返回键,所以整列全空的列在快照里看不见 —— 与逐格的
    「缺键约定」同一个盲点,不另立解释。

    ── 未登记列登记表(所有者定稿 2026-09-01 盘点,防下次被"顺手补上")──────
    实测生产库:限额表共 19 列 = 已登记 17 列(16 个值列 + 「店铺」键)+ **未登记
    2 列**;"登记了但表里没有的"为空(无列名漂移)。这两列**都是有意不登记的**:

      · `SourceID` —— 飞书内部字段,值是 base64 复合键,**第三段是行内容的哈希**
        (`7670866633484766507:H006詹松涛:a02d434c…:1`),行被编辑就变。它就是
        本次改口径的直接起因:整表入快照 ⇒ 谁动一格就刷一批假 changed 事件。
      · `店铺状态` —— **所有者明确定稿「不登记」**。真实店铺状态由沃尔玛 API 落
        `ops.store_kpi_daily.store_status`,并且已经在产 `store_status_changed`
        风险事件;限额表那一列是**人工维护、只给人看**的副本,没有任何代码读它。
        登记进来 = 同一件事两个来源各报一次,而两边**一定会漂**(本仓铁规:
        同一个数字两处存必漂)。

    ⚠ **下一轮盘点看到"限额表有列没登记"不是疏漏**,别顺手补进 registry ——
    补了噪音就回来了。要登记一列,先答上来"哪段代码读它";答不上来就是这两列
    里的一类,留在 `limits_extra_cols` 即可。
    """
    t = resources.RETIRE_LIMITS
    key = t.fields.store
    known = _registered_limits_cols()
    out: dict[str, dict] = {}
    extra: set[str] = set()
    for rec in feishu.list_records(t):
        fields = rec.get("fields") or {}
        extra |= {k for k in fields if k != key and k not in known}
        name = feishu._plain_text(fields.get(key)).strip()
        if not name:
            continue
        # 原文入库(纪律 ③);店铺列本身不进值字典 —— 它是键
        out[name] = {k: feishu._plain_text(v)
                     for k, v in sorted(fields.items()) if k in known}
    return out, sorted(extra)


def _stores_snapshot() -> dict[str, dict]:
    """输入:无 → 输出:{店铺: {"enabled": True/False/None}}(凭证表**不过滤**)。

    读法照 `stores.registered_names()`(在册全体,连停用的也要 —— 停用正是
    本模块要抓的那个变化),只多取一个「启用」列。
    ⚠ **密钥列绝不进快照**:`field_names` 只点名两列,ClientSecret / 代理密码
    连取都不取 —— 快照落在 ops.cursors 里,那张表没有凭证快照的 chmod 600。

    `enabled=None` 的唯一含义是「**这一列没读到**」(表头被改名 / 列被删):
    判据是**整张表**没有任何一行带这个键。单行缺键不算 —— 飞书对未勾选的
    复选框可能整个不返回该字段,而 `stores.is_enabled` 的既定口径是"缺省视为
    启用"(旧 xlsx 无此列),这里不另立第二套解释。
    """
    from services import stores as stores_svc

    f = resources.STORE_CREDENTIALS.fields
    recs = feishu.list_records(resources.STORE_CREDENTIALS,
                               field_names=[f.store, f.enabled])
    col_seen = any(f.enabled in (r.get("fields") or {}) for r in recs)
    if not col_seen and recs:
        logger.warning("凭证表「%s」列一行都没读到(表头改名?):本轮 enabled "
                       "全记 None,不产任何启用变化事件", f.enabled)
    out: dict[str, dict] = {}
    for rec in recs:
        fields = rec.get("fields") or {}
        name = feishu._plain_text(fields.get(f.store)).strip()
        if not name:
            continue
        out[name] = {"enabled": stores_svc.is_enabled(fields) if col_seen
                     else None}
    return out


def take_snapshot() -> dict:
    """输入:无 → 输出:治理配置快照 dict(飞书读失败直接抛,由调用方兜)。

    形状:{"v": 2, "limits": {店: {登记列: 原文}}, "limits_extra_cols": [列名…],
           "stores": {店: {"enabled": bool|None}}, "scope_excluded": [...]}。
    四部分各自独立,少一部分不补默认 —— 抛出去比拿半张快照覆盖上一张好
    (半张快照的下一轮 diff 会把没读到的那半边全报成"被删了")。
    """
    limits, extra_cols = _limits_snapshot()
    return {
        "v": SNAPSHOT_VERSION,
        "limits": limits,
        # 只有列名,没有值(理由见 `_limits_snapshot`)
        "limits_extra_cols": extra_cols,
        "stores": _stores_snapshot(),
        # 规划外名单是 registry 里的**代码/env 配置**,不是飞书表:它一变,
        # 分配链对一整批店的归属判定就换了口径,同样要留痕
        "scope_excluded": list(resources.alloc_excluded_stores()),
    }


# ── diff(纯函数)──────────────────────────────────────────────────────────

def _row(store, event, severity, detail) -> dict:
    return {"store": store, "event": event, "severity": severity,
            "source": SOURCE, "detail": detail}


def _diff_limits(prev: dict, cur: dict) -> list[dict]:
    """输入:两版 {店: {列: 原文}} → 输出:限额表的行级与格级事件。

    ⚠ **"这一格没了"有两种截然不同的成因**,必须分开(飞书对空单元格
    根本不返回那个键,所以"人把格子清空了"在快照里长得就像"这一列没了"):
      · 列在**所有存续店**上同时消失 ⇒ 表头改名 / 列被删,是**表结构**变化。
        逐店各报一条会刷几十条,而且它压根不是某一家店的配置变化 ⇒ 只告警。
        (表头改名会让所有按列名取数的 loader 静默读空 —— registry 那处
        注释记的老坑,所以这一句必须喊出来。)
      · 只有个别店的那一格没了 ⇒ **人清空了这一格**,是真的配置变化 ⇒ 按
        `原文 → ""` 产事件(类目1 被清空 = 该店从此不限类目,这必须留痕)。

    ⚠ 这里的"列"全是**登记列**:未登记列在拍快照那一步就没进值字典,逐格
    比对根本见不到它们(见 `_limits_snapshot`),它们的增减走
    `_diff_extra_cols` 的表结构事件。
    """
    events: list[dict] = []
    both = sorted(set(prev) & set(cur))
    # 只看两版都在的店:新增/消失的店整行走行级事件,不该参与列级判定
    prev_cols = {c for s in both for c in prev[s]}
    gone_cols = prev_cols - {c for s in both for c in cur[s]}
    if gone_cols:
        logger.warning("限额表有 %d 列在**所有店**上同时消失(表头改名/列被删?)"
                       ":%s —— 按表结构变化处理,不逐店产事件",
                       len(gone_cols), "、".join(sorted(gone_cols)[:8]))
    for store in sorted(set(prev) | set(cur)):
        p, c = prev.get(store), cur.get(store)
        if p is None:
            events.append(_row(store, se.STORE_LIMITS_ROW_ADDED, _SEV_INFO,
                               {"row": c}))
            continue
        if c is None:
            # 行消失 = 这家店从今天起全表回落默认值(限额、类目、渠道一起没)。
            # 不是 high 只因为它**不会让任何链做错事**,只会让闸门变宽 —— 但
            # 变宽本身要有人知道,所以也不是 info。
            events.append(_row(store, se.STORE_LIMITS_ROW_REMOVED, _SEV_MID,
                               {"row": p}))
            continue
        for col in sorted(set(p) | set(c)):
            if col not in p or col in gone_cols:
                # 缺键约定:上一版没有这一列的键 = 上一版还没这列(新列 /
                # 上一版那一格是空的没被飞书返回),不是"从有到无"。
                # 首次见到不是变化 —— 与首次快照同一条纪律。
                continue
            old, new = p[col], c.get(col, "")
            if old == new:
                continue
            events.append(_row(store, se.STORE_LIMITS_CHANGED,
                               _limits_severity(col, old, new),
                               {"field": col, "old": old, "new": new}))
    return events


def _diff_extra_cols(prev: list, cur: list) -> list[dict]:
    """输入:两版未登记列名表 → 输出:0 或 1 条表结构事件(**一次变化一条**)。

    store=None:一张表多出/少了一列是**全局事实**,不属于任何一家店
    (与 `STORE_SCOPE_CHANGED` 同款)。severity info:未登记列没有任何代码
    消费它,它的出现本身不会让任何链当天做错事 —— 但"谁往表里加了一列"
    要留痕,加完的下一步多半就是有人要写代码消费它。
    detail 里**只有列名没有值**(记了值就等于把噪音换个地方存)。
    """
    added = sorted(set(cur) - set(prev))
    removed = sorted(set(prev) - set(cur))
    if not (added or removed):
        return []
    return [_row(None, se.STORE_LIMITS_COLUMNS_CHANGED, _SEV_INFO,
                 {"added": added, "removed": removed})]


def _diff_stores(prev: dict, cur: dict) -> list[dict]:
    """输入:两版 {店: {"enabled": ...}} → 输出:在册与启用两类事件。"""
    events: list[dict] = []
    for store in sorted(set(prev) | set(cur)):
        p, c = prev.get(store), cur.get(store)
        if p is None:
            events.append(_row(store, se.STORE_REGISTERED, _SEV_INFO,
                               {"enabled": (c or {}).get("enabled")}))
            continue
        if c is None:
            # 凭证表里一家店整行没了 = store_release 的 `-p dead=1` 会把它
            # 整店标缺席。删行之前必须留下"它曾经在册"这条记录
            events.append(_row(store, se.STORE_DEREGISTERED, _SEV_HIGH,
                               {"enabled": (p or {}).get("enabled")}))
            continue
        old, new = p.get("enabled"), c.get("enabled")
        if old is None or new is None or old == new:
            # None = 列没读到,不是状态(一期铁规的配置版)
            continue
        events.append(_row(store, se.STORE_ENABLED_CHANGED,
                           _SEV_INFO if new else _SEV_HIGH,
                           {"old": old, "new": new}))
    return events


def _version_migrated(prev: dict | None, cur: dict) -> bool:
    """输入:两版快照 → 输出:上一版是不是**别的结构版本**(唯一判据)。

    `diff` 与摘要文案共用这一个谓词 —— 两处各写一遍 `prev["v"] != cur["v"]`,
    改版本时必然只改到一处,于是"跳过了比对"这件事会静默掉。
    """
    return prev is not None and prev.get("v") != cur.get("v")


def diff(prev: dict | None, cur: dict) -> list[dict]:
    """输入:上一版快照(None=首次)+ 本轮快照 → 输出:治理事件行列表。

    **纯函数**(不碰库、不碰飞书),所以每一条分级规则都能被用例单独钉住。
    prev 为 None 或版本不同 → 返回空:首次观测不是变化。

    ⚠ 版本不同为什么必须当首次(v1→v2 的现场教训):v1 的 `limits` 里存着
    未登记列(SourceID 之类)的**值**,而 v2 只存登记列。硬比的话那些列会被
    判成"在所有店上整体消失",刷出一批假的表结构/清空事件 —— 没有可比的
    上一版就不是变化,这与"首次快照零事件"是同一条铁规。
    """
    if prev is None or _version_migrated(prev, cur):
        if prev is not None:
            logger.warning("治理快照版本 %s → %s:本轮当首次快照处理,不产事件",
                           prev.get("v"), cur.get("v"))
        return []
    events = _diff_limits(prev.get("limits") or {}, cur.get("limits") or {})
    events += _diff_stores(prev.get("stores") or {}, cur.get("stores") or {})
    events += _diff_extra_cols(prev.get("limits_extra_cols") or [],
                               cur.get("limits_extra_cols") or [])
    old_scope = list(prev.get("scope_excluded") or [])
    new_scope = list(cur.get("scope_excluded") or [])
    if old_scope != new_scope:
        # store=None:规划范围是**全局事实**,不属于任何一家店(与 TRO 源头
        # 行同一形状)。一改就是一整批店的归属判定换口径
        events.append(_row(None, se.STORE_SCOPE_CHANGED, _SEV_MID,
                           {"old": old_scope, "new": new_scope}))
    return events


# ── 快照存取(ops.cursors 三件套,照 services/maint_sheet 的先例)────────────

_LOAD_SQL = "SELECT value FROM ops.cursors WHERE name = %(name)s::text"

_SAVE_SQL = """
INSERT INTO ops.cursors (name, value) VALUES (%(name)s::text, %(value)s::jsonb)
ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""


def load_snapshot(conn) -> dict | None:
    """输入:连接 → 输出:上一版快照(从没存过返 None = 首次)。"""
    with conn.cursor() as cur:
        cur.execute(_LOAD_SQL, {"name": _CURSOR})
        row = cur.fetchone()
    return dict(row[0]) if row else None


def save_snapshot(conn, snap: dict) -> None:
    """输入:连接 + 快照 → 输出:无(整份覆盖,只留最近一版)。"""
    with conn.cursor() as cur:
        cur.execute(_SAVE_SQL, {"name": _CURSOR,
                                "value": json.dumps(snap, ensure_ascii=False,
                                                    default=str)})


def check_and_record(conn) -> tuple[list[dict], str | None]:
    """输入:连接 → 输出:(本轮落库的治理事件行, 告警文案 或 None)。

    顺序是有讲究的:**先拿到新快照才动库**。飞书这一跳失败时立刻返回 ——
    不产事件、**不覆盖快照**(纪律 ②):把"飞书挂了"记成"配置被清空",
    下一轮读到了还会再记一遍"配置被改回来",两轮假事件把真事件淹掉。

    try 只包飞书那一跳(与 `stores.load_stores` 2026-08-27 收窄同款):
    解析/落库出错照抛,那是本地 bug,不该伪装成远端故障。

    ⚠ **版本升级那一轮要明说**:`diff` 见到旧版本会当首次快照(不产事件),
    但快照**照常覆盖**成新形状 —— 与飞书失败那条路径正好相反。静默跳过的话,
    摘要上只会显示"无变更",而这一轮里真发生的配置改动确实丢了一轮
    (下一轮起恢复正常比对),这件事得有人知道。
    """
    prev = load_snapshot(conn)
    try:
        cur_snap = take_snapshot()
    except Exception as e:                                      # noqa: BLE001
        logger.warning("治理配置快照读取失败,本轮跳过(不产事件不覆盖快照):%s", e)
        return [], (f"⚠ 治理配置本轮没比对(飞书读不到:{e})——"
                    f"**不产事件、不覆盖快照**,下轮接着比上一版")
    if _version_migrated(prev, cur_snap):
        save_snapshot(conn, cur_snap)
        return [], (f"⚠ 治理配置本轮没比对(快照结构 v{prev.get('v')} → "
                    f"v{cur_snap.get('v')} 升级,没有可比的上一版)——"
                    f"**不产事件、已按新形状覆盖快照**,下一轮起正常比对")
    events = diff(prev, cur_snap)
    if events:
        se.record_many(conn, events)
    save_snapshot(conn, cur_snap)
    return events, None


# 治理事件的摘要渲染在 `store_events.brief`(2026-08-30 收口):本模块原先自带
# 一份 `brief`,而账本里另有一份管状态迁移的 —— 消费方(store_watch 的推送
# 明细)一轮里同时拿到两族事件,按族分发渲染就是第三处路由。
# 「每个能力只有一条实现路径」:码的唯一出处在 store_events,渲染跟着码走。
