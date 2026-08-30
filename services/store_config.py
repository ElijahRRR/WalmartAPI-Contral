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
各自 `field_names=[店铺, 某一列]` 裁剪列、还各自把空值折成默认值 ——
快照要的恰恰是**未经解释的全表**。
"""

import json
import logging

from api import feishu
from registry import resources
from services import store_events as se

logger = logging.getLogger("services.store_config")

#: 快照结构版本。形状变了就 +1,`diff` 见到版本不同当**首次快照**处理
#: (拿两套形状硬比会产出满屏假事件,那比不报警更坏)。
SNAPSHOT_VERSION = 1

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

#: 未在上面登记的列(将来有人往表里加一列)= mid。不默认 info:新列多半是
#: 新加的闸,把它当"不值一提"会让它第一次生效时没人知道。
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

def _limits_snapshot() -> dict[str, dict]:
    """输入:无 → 输出:{店铺: {列名: 原文}}(上下架限额表**整表未裁剪**)。

    不传 `field_names`:裁剪了就只看得见今天登记过的列,**将来有人往表里加一列
    (比如又一道闸)时,它的第一次生效永远不会进事件流**。整表拉回来,新列
    自然按未知列(mid)入账。表一共几十行,整表拉回的代价可以忽略。
    """
    t = resources.RETIRE_LIMITS
    key = t.fields.store
    out: dict[str, dict] = {}
    for rec in feishu.list_records(t):
        fields = rec.get("fields") or {}
        name = feishu._plain_text(fields.get(key)).strip()
        if not name:
            continue
        # 原文入库(纪律 ③);店铺列本身不进值字典 —— 它是键
        out[name] = {k: feishu._plain_text(v)
                     for k, v in sorted(fields.items()) if k != key}
    return out


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

    形状:{"v": 1, "limits": {店: {列名: 原文}},
           "stores": {店: {"enabled": bool|None}}, "scope_excluded": [...]}。
    三部分各自独立,少一部分不补默认 —— 抛出去比拿半张快照覆盖上一张好
    (半张快照的下一轮 diff 会把没读到的那半边全报成"被删了")。
    """
    return {
        "v": SNAPSHOT_VERSION,
        "limits": _limits_snapshot(),
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


def diff(prev: dict | None, cur: dict) -> list[dict]:
    """输入:上一版快照(None=首次)+ 本轮快照 → 输出:治理事件行列表。

    **纯函数**(不碰库、不碰飞书),所以每一条分级规则都能被用例单独钉住。
    prev 为 None 或版本不同 → 返回空:首次观测不是变化。
    """
    if prev is None or prev.get("v") != cur.get("v"):
        if prev is not None:
            logger.warning("治理快照版本 %s → %s:本轮当首次快照处理,不产事件",
                           prev.get("v"), cur.get("v"))
        return []
    events = _diff_limits(prev.get("limits") or {}, cur.get("limits") or {})
    events += _diff_stores(prev.get("stores") or {}, cur.get("stores") or {})
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
    """
    prev = load_snapshot(conn)
    try:
        cur_snap = take_snapshot()
    except Exception as e:                                      # noqa: BLE001
        logger.warning("治理配置快照读取失败,本轮跳过(不产事件不覆盖快照):%s", e)
        return [], (f"⚠ 治理配置本轮没比对(飞书读不到:{e})——"
                    f"**不产事件、不覆盖快照**,下轮接着比上一版")
    events = diff(prev, cur_snap)
    if events:
        se.record_many(conn, events)
    save_snapshot(conn, cur_snap)
    return events, None


def brief(e: dict) -> str:
    """输入:治理事件行 → 输出:摘要用短句。"""
    d = e.get("detail") or {}
    who = e.get("store") or "全局"
    if e["event"] == se.STORE_LIMITS_CHANGED:
        return f"{who} {d.get('field')} 「{d.get('old')}」→「{d.get('new')}」"
    if e["event"] == se.STORE_ENABLED_CHANGED:
        return f"{who} 启用 {d.get('old')}→{d.get('new')}"
    if e["event"] == se.STORE_SCOPE_CHANGED:
        return (f"规划外名单 {d.get('old')}→{d.get('new')}")
    return f"{who} {e['event']}"
