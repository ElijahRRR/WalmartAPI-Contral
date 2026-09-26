"""feed 明细的**实际结果**积木(生效 / 未生效)—— 所有者 2026-09-25 定稿,2026-09-26 改口径。

  judge(conn, store=None) -> dict      给**这一轮观测**能判的 feed 明细判一次实际结果,落 ops.feed_effects
  review_rows(conn, days, store) -> list[dict]   复核清单:feed 成功(或没给结论)∧ 未生效
  verdict(row) -> tuple | None         单行判词(纯函数,可测)

所有者原话:「feed 结果和实际结果……是两个东西,应该分开。feed 结果是为了知道沃尔玛侧
的执行情况,而实际结果是为了让开发人员或者运营知道自己之前做的操作实际上在沃尔玛是否
成功生效……对于实际结果,就只需要生效/未生效。如果 feed 显示该明细是成功的,但是观测
结果是未生效,这种是人需要看的。」

规矩(docs/feed_ledger_v2_design.md「〇.3」「〇.8」):
  · **两本账互不写对方**:本模块只读 ops.feed_items(feed 结果)、观测(catalog.walmart_items)
    与处置建议行(目标值),只写 ops.feed_effects 与自己的观测台账;feed_poll 不读本表。
    **不挂任何自动化**(所有者:自动再处理「目前来说没有必要」)。
  · **只在期限后的第一次观测判**(所有者 2026-09-26 批,取代「回看 7 天」—— 拿今天的快照
    比一周前的提交,中间同一参数可能又改过好几次,判不准):每店每轮只判「落定期限
    (feed_track.FEED_DEADLINE_MINUTES)落在该店**上一次观测与这一次观测之间**」的明细;
    错过这一次(店缺席、这一轮没扫到这个 SKU)的**不再补判**。上一次观测时刻记在
    ops.cursors(name=CURSOR);某店第一次出现只记基线、不判(不补判上线前的存量)。
  · **观测前同一参数又改过就不判**:这次观测只能证明最后那次改动(_SAME_PARAM;单品 PUT
    改价也算)。被盖掉的那次不判,最后那次在它自己的第一次观测上判。
  · **库存不判**(所有者 2026-09-26:「库存不观测结果」):订单随时在扣库存,快照对不上
    目标说明不了是 feed 没生效还是卖掉了。
  · **判一次,不回头改**:一旦落行不再改(ON CONFLICT DO NOTHING)。表里记的就是"那次观测
    看到了什么、什么时候",不推断中间发生过什么。

判据**只有一份,复用现有,不另写**:
  · 改价 / 改标题 = dispositions.maint_effective(维护链落定用的同一个现值比对,
    目标值取处置建议行的 detail.new);
  · 删除 = product_events.GONE_SQL(删除核验同一个"已经不在了"口径);
  · 上架 / 跟卖 = 目录里出现了这个 SKU;停用 = 生命周期 RETIRED 或标了缺席。
判哪些:feed 结果不是 failed 的(failed = 沃尔玛已拒,没什么可生效的)。
"""

import json
import logging
from datetime import datetime, timedelta

from services import dispositions, feed_track, product_events

logger = logging.getLogger("services.feed_effect")

EFFECTIVE, NOT_EFFECTIVE = "effective", "not_effective"
EFFECT_TEXT = {EFFECTIVE: "生效", NOT_EFFECTIVE: "未生效"}

#: 本步单条 SQL 的超时(秒)。附属判定不许拖住日报链:2026-09-26 生产实见候选查询
#: 逐条全表扫 ops.dispositions 跑了 44 分钟不报错(当时库里没设超时)。超时 = 本步失败,
#: catalog_sync 照常往下走(_judge_effects 的失败隔离)。
STATEMENT_TIMEOUT_S = 300

#: 每店上一次观测时刻的台账(ops.cursors):{店: ISO 时刻}。只由 judge 读写,与判决同一事务
#: (空跑 rollback 时一起回滚 —— 否则空跑一次就把这一轮的窗口吃掉了)。
CURSOR = "feed_effect:observed"

#: 值比对的两类 feed(目标值在处置建议行里,维护链是它们唯一的提交方)。
#: ⚠ 库存(inventory / MP_INVENTORY)**不判**:所有者 2026-09-26「库存不观测结果」。
_VALUE_FEEDS = ("price", "PRICE_AND_PROMOTION", "MP_MAINTENANCE")
#: 目标值只取这两种处置动作(库存动作的目标值不进来,免得被当成改价 / 改标题去比)
_VALUE_ACTIONS = ("price", "title")
_PRESENCE_FEEDS = ("MP_ITEM", "MP_ITEM_MATCH")
_JUDGED_FEEDS = _VALUE_FEEDS + _PRESENCE_FEEDS + ("RETIRE_ITEM", "DELETE_ITEM")

#: 「同一参数」:判一条明细时,若在它之后、这次观测之前,同店同 SKU 又提交过这些 feed 里的
#: 任何一种 ⇒ 这次观测证明不了它,不判。上架 / 跟卖 / 改码(MP_ITEM / MP_ITEM_MATCH)整条
#: 重写商品,价格、标题、在不在架都算动过。
_PRICE = ("price", "PRICE_AND_PROMOTION", "MP_ITEM", "MP_ITEM_MATCH")
_TITLE = ("MP_MAINTENANCE", "MP_ITEM", "MP_ITEM_MATCH")
_PRESENCE = ("MP_ITEM", "MP_ITEM_MATCH", "RETIRE_ITEM", "DELETE_ITEM")
_SAME_PARAM = {"price": _PRICE, "PRICE_AND_PROMOTION": _PRICE, "MP_MAINTENANCE": _TITLE,
               "MP_ITEM": _PRESENCE, "MP_ITEM_MATCH": _PRESENCE,
               "RETIRE_ITEM": _PRESENCE, "DELETE_ITEM": _PRESENCE}
#: 单品 PUT(维护链小批量,处置行 feed_id='sync')里会盖掉 feed 判定的只有改价:
#: 库存不判,标题没有 PUT 路由。
_PUT_PRICE_FEEDS = ("price", "PRICE_AND_PROMOTION")

#: feed 结果里可判的状态:沃尔玛接受了(success)或没给结论 / 明细无此条。
#: failed 不判 —— 沃尔玛拒了,没什么可生效的。
_JUDGEABLE = ("success", "missing") + feed_track.NO_VERDICT_STATUSES

#: 这一次观测:每店**在架行**的最新观测时刻 —— 与 store_absence 的缺席判据同一口径
#: (本轮没扫成的店水位停在上一轮,它的窗口自然是空的,等它下次扫成再判)。
_OBSERVED_SQL = """
SELECT store, max(last_seen_at) FROM catalog.walmart_items
WHERE missing_since IS NULL AND (%(store)s::text IS NULL OR store = %(store)s::text)
GROUP BY store
"""

_CURSOR_GET_SQL = "SELECT value FROM ops.cursors WHERE name = %s"
_CURSOR_PUT_SQL = ("INSERT INTO ops.cursors (name, value) VALUES (%s, %s::jsonb) "
                   "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()")

#: 候选 + 观测一次取齐,判词在 Python 里出(verdict,纯函数)。
#: ⚠ 每个参数带显式 ::类型(本仓 SQL 纪律:PG 推不出类型在生产上连炸过三次)。
#: ⚠ 全部**整批 join**,不许写成逐候选的 LATERAL:2026-09-26 生产那版是 `LATERAL (… FROM
#:   ops.dispositions WHERE feed_id = … ORDER BY id DESC LIMIT 1)`,feed_id 上没有索引,
#:   1.88 万个候选每个都把 65.8 万行的处置表扫一遍,44 分钟不出结果。
#:   候选、后续提交、单品 PUT 都先按 `since` 截在窗口期内(走 submitted_at / executed_at
#:   索引),目标值走 dispositions_feed_sku_idx。
_CANDIDATES_SQL = f"""
WITH win AS (       -- 每店这一轮的窗口 (上一次观测, 这一次观测]
    SELECT * FROM unnest(%(stores)s::text[], %(prev_at)s::timestamptz[],
                         %(this_at)s::timestamptz[]) AS w(store, prev_at, this_at)
),
due AS (            -- 落定期限落在窗口里、还没判过的明细
    SELECT f.feed_id, f.sku, f.store, f.feed_type, f.workflow,
           f.status AS feed_status, f.submitted_at,
           f.submitted_at + make_interval(
               mins => (%(deadlines)s::jsonb ->> f.feed_type)::int) AS due_at,
           w.this_at
    FROM ops.feed_items f
    JOIN win w ON w.store = f.store
    WHERE f.status = ANY(%(judgeable)s::text[])
      AND f.feed_type = ANY(%(feed_types)s::text[])
      AND f.submitted_at > %(since)s::timestamptz
      AND f.submitted_at + make_interval(
              mins => (%(deadlines)s::jsonb ->> f.feed_type)::int) > w.prev_at
      AND f.submitted_at + make_interval(
              mins => (%(deadlines)s::jsonb ->> f.feed_type)::int) <= w.this_at
      AND NOT EXISTS (SELECT 1 FROM ops.feed_effects e
                      WHERE e.feed_id = f.feed_id AND e.sku = f.sku)
),
same AS (           -- 「同一参数」对照表(_SAME_PARAM 平铺)
    SELECT * FROM unnest(%(same_ft)s::text[], %(same_other)s::text[]) AS s(ft, other)
),
recent AS (         -- 窗口期内的全部 feed 提交
    SELECT n.store, n.sku, n.feed_type, n.submitted_at
    FROM ops.feed_items n
    WHERE n.submitted_at > %(since)s::timestamptz
),
puts AS (           -- 窗口期内的单品 PUT 改价(维护链小批量,处置行 feed_id='sync')
    SELECT x.store, x.sku, x.executed_at
    FROM ops.dispositions x
    WHERE x.executed_at > %(since)s::timestamptz
      AND x.feed_id = 'sync' AND x.action = 'price'
),
overridden AS (     -- 这次观测之前,同一参数又改过
    SELECT d.feed_id, d.sku
    FROM due d
    JOIN same s ON s.ft = d.feed_type
    JOIN recent n ON n.store = d.store AND n.sku = d.sku AND n.feed_type = s.other
    WHERE n.submitted_at > d.submitted_at AND n.submitted_at <= d.this_at
    UNION
    SELECT d.feed_id, d.sku
    FROM due d
    JOIN puts p ON p.store = d.store AND p.sku = d.sku
    WHERE d.feed_type = ANY(%(put_feeds)s::text[])
      AND p.executed_at > d.submitted_at AND p.executed_at <= d.this_at
),
disp AS (           -- 值比对类的目标值:同 (feed, SKU) 取最新一条维护处置行
    SELECT DISTINCT ON (x.feed_id, x.sku) x.feed_id, x.sku, x.action, x.detail
    FROM due d
    JOIN ops.dispositions x ON x.feed_id = d.feed_id AND x.sku = d.sku
    WHERE d.feed_type = ANY(%(value_feeds)s::text[])
      AND x.action = ANY(%(maint_actions)s::text[])
    ORDER BY x.feed_id, x.sku, x.id DESC
)
SELECT d.feed_id, d.sku, d.store, d.feed_type, d.workflow, d.feed_status,
       d.submitted_at, d.due_at, d.this_at AS scanned_at,
       (o.feed_id IS NOT NULL) AS overridden,
       (w.sku IS NOT NULL) AS seen, w.missing_since, w.lifecycle_status,
       w.last_seen_at, w.price, w.product_name,
       {product_events.GONE_SQL} AS gone,
       disp.action, disp.detail ->> 'new' AS want
FROM due d
LEFT JOIN overridden o ON o.feed_id = d.feed_id AND o.sku = d.sku
LEFT JOIN catalog.walmart_items w ON w.store = d.store AND w.sku = d.sku
LEFT JOIN disp ON disp.feed_id = d.feed_id AND disp.sku = d.sku
"""

_COLS = ("feed_id", "sku", "store", "feed_type", "workflow", "feed_status",
         "submitted_at", "due_at", "scanned_at", "overridden", "seen",
         "missing_since", "lifecycle_status", "last_seen_at", "price",
         "product_name", "gone", "action", "want")

_INSERT_SQL = """
INSERT INTO ops.feed_effects (feed_id, sku, store, feed_type, workflow,
    feed_status, effect, want, observed, observed_at, basis)
VALUES (%(feed_id)s, %(sku)s, %(store)s, %(feed_type)s, %(workflow)s,
    %(feed_status)s, %(effect)s, %(want)s, %(observed)s, %(observed_at)s, %(basis)s)
ON CONFLICT (feed_id, sku) DO NOTHING
"""


def _after(ts, cut) -> bool:
    return ts is not None and cut is not None and ts > cut


def _shown(action: str, price, name) -> str:
    """输入:动作 + 现值 → 输出:复核清单里给人看的现值(价格两位小数)。"""
    if action == "price":
        try:
            return f"{float(price):.2f}"
        except (TypeError, ValueError):
            return "无"         # 现值拿不到:maint_effective 同样判未生效
    return str(name or "")


def verdict(r: dict) -> tuple[str, str, str, object] | None:
    """输入:一行候选(_CANDIDATES_SQL 的列)→ 输出:(生效与否, 观测到的, 判据, 观测时刻)
    或 None(这一轮判不了:期限后没观测到这个 SKU / 目标值不在库里)。纯函数。

    未生效**只认期限之后的观测**(官方期限内没变不算没生效);生效只要提交后看到了。
    """
    ft = r["feed_type"]
    if ft in _VALUE_FEEDS:
        action = r.get("action")
        if not action:
            return None     # 没有处置建议行 = 目标值不在库里(反补等),判不了
        if not (r["seen"] and _after(r["last_seen_at"], r["due_at"])):
            return None     # 这个 SKU 期限后这一轮没被观测到
        ok = dispositions.maint_effective(action, r.get("want"), r["price"], None,
                                          r["product_name"])
        return (EFFECTIVE if ok else NOT_EFFECTIVE,
                _shown(action, r["price"], r["product_name"]),
                f"catalog_sync 现值比对({action})", r["last_seen_at"])
    if ft in _PRESENCE_FEEDS:
        if r["seen"] and r["missing_since"] is None and _after(
                r["last_seen_at"], r["submitted_at"]):
            return EFFECTIVE, "在架", "目录里出现了这个 SKU", r["last_seen_at"]
        if _after(r["scanned_at"], r["due_at"]) and (
                not r["seen"] or r["missing_since"] is not None):
            return (NOT_EFFECTIVE, "缺席", "期限后该店扫描过,目录里没有这个 SKU",
                    r["scanned_at"])
        return None
    if ft == "RETIRE_ITEM":
        if not r["seen"]:
            return None     # 从没观测到过的 SKU,停没停无从说起
        if r["lifecycle_status"] == "RETIRED" or r["missing_since"] is not None:
            return (EFFECTIVE, r["lifecycle_status"] or "缺席",
                    "目录里生命周期 RETIRED 或已缺席", r["last_seen_at"])
        if _after(r["last_seen_at"], r["due_at"]):
            return (NOT_EFFECTIVE, f"在架({r['lifecycle_status'] or '-'})",
                    "期限后仍在架且未 RETIRED", r["last_seen_at"])
        return None
    if ft == "DELETE_ITEM":
        if r["gone"]:
            return (EFFECTIVE, "已不在", "目录缺席 / 标了缺席 / RETIRED(删除核验同口径)",
                    r["missing_since"] or r["last_seen_at"] or r["scanned_at"])
        if _after(r["last_seen_at"], r["due_at"]):
            return NOT_EFFECTIVE, "仍在架", "期限后观测仍在架", r["last_seen_at"]
        return None
    return None


def _load_cursor(cur) -> dict[str, datetime]:
    """输入:游标 → 输出:{店: 上一次观测时刻}(台账没有就是空 —— 首轮只记基线)。"""
    cur.execute(_CURSOR_GET_SQL, (CURSOR,))
    row = cur.fetchone()
    raw = (row[0] if row else None) or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {s: datetime.fromisoformat(v) for s, v in raw.items()}


def judge(conn, store: str | None = None) -> dict:
    """输入:连接(+限定店铺)→ 输出:{effective, not_effective, review, overridden,
    missed, no_target, baseline}。

    每店的窗口 = (上一次观测, 这一次观测]:只判落定期限落在窗口里的明细,判完把这一次观测
    记成下一轮的"上一次"。review = 新判的「feed 成功 ∧ 未生效」条数(复核清单的新增);
    overridden = 观测前同一参数又改过、不判;missed = 期限后这一轮没观测到这个 SKU,**不再
    补判**;no_target = 值比对类找不到处置建议行(目标值不在库里,如反补的 MP_MAINTENANCE);
    baseline = 第一次出现、只记基线的店数。
    调用方负责事务(空跑时 rollback —— 判决与观测台账一起回滚;本函数不提交)。
    """
    out = {EFFECTIVE: 0, NOT_EFFECTIVE: 0, "review": 0, "overridden": 0,
           "missed": 0, "no_target": 0, "baseline": 0}
    with conn.cursor() as cur:
        # 本事务内的超时(SET LOCAL 随事务结束失效,不影响调用方连接上的其他步骤)
        cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_S * 1000)}")
        cur.execute(_OBSERVED_SQL, {"store": store})
        this = {s: at for s, at in cur.fetchall() if at is not None}
        prev = _load_cursor(cur)
        out["baseline"] = sum(1 for s in this if s not in prev)
        wins = [(s, prev[s], at) for s, at in sorted(this.items())
                if s in prev and at > prev[s]]
        rows: list[dict] = []
        if wins:
            # 候选、后续提交与单品 PUT 的下界:最早的上一次观测 − 最长落定期限
            since = min(p for _, p, _ in wins) - timedelta(
                minutes=max(feed_track.FEED_DEADLINE_MINUTES.values()))
            pairs = [(ft, other) for ft, group in _SAME_PARAM.items() for other in group]
            cur.execute(_CANDIDATES_SQL, {
                "stores": [w[0] for w in wins], "prev_at": [w[1] for w in wins],
                "this_at": [w[2] for w in wins], "since": since,
                "deadlines": _deadlines_json(), "judgeable": list(_JUDGEABLE),
                "feed_types": list(_JUDGED_FEEDS),
                "same_ft": [p[0] for p in pairs], "same_other": [p[1] for p in pairs],
                "put_feeds": list(_PUT_PRICE_FEEDS), "value_feeds": list(_VALUE_FEEDS),
                "maint_actions": list(_VALUE_ACTIONS)})
            rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]
        params = []
        for r in rows:
            if r["overridden"]:
                out["overridden"] += 1
                continue
            if r["feed_type"] in _VALUE_FEEDS and not r.get("action"):
                out["no_target"] += 1
                continue
            v = verdict(r)
            if v is None:
                out["missed"] += 1
                continue
            effect, observed, basis, observed_at = v
            out[effect] += 1
            if effect == NOT_EFFECTIVE and r["feed_status"] == "success":
                out["review"] += 1
            params.append({"feed_id": r["feed_id"], "sku": r["sku"], "store": r["store"],
                           "feed_type": r["feed_type"], "workflow": r["workflow"],
                           "feed_status": r["feed_status"], "effect": effect,
                           "want": r.get("want") if r.get("action") else None,
                           "observed": (observed or "")[:500], "observed_at": observed_at,
                           "basis": basis})
        if params:
            cur.executemany(_INSERT_SQL, params)
        if this:
            # 这一次观测记成下一轮的"上一次";只往前走(水位倒退不认),没扫到的店不动
            nxt = dict(prev)
            for s, at in this.items():
                nxt[s] = max(at, prev[s]) if s in prev else at
            cur.execute(_CURSOR_PUT_SQL, (CURSOR, json.dumps(
                {s: at.isoformat() for s, at in sorted(nxt.items())})))
    return out


def _deadlines_json() -> str:
    return json.dumps(feed_track.FEED_DEADLINE_MINUTES)


_REVIEW_SQL = """
SELECT e.store, e.feed_type, e.workflow, e.feed_id, e.sku, e.feed_status,
       e.want, e.observed, e.observed_at, e.basis, e.judged_at
FROM ops.feed_effects e
WHERE e.effect = 'not_effective'
  AND e.feed_status = ANY(%(statuses)s::text[])
  AND e.judged_at > now() - make_interval(days => %(days)s::int)
  AND (%(store)s::text IS NULL OR e.store = %(store)s::text)
ORDER BY e.store, e.feed_type, e.judged_at DESC, e.sku
"""


def review_rows(conn, days: int = 7, store: str | None = None) -> list[dict]:
    """输入:连接 + 回看天数(+店铺)→ 输出:复核清单行(未生效,且 feed 成功或没给结论)。只读。

    「feed 成功 ∧ 未生效」是所有者点名要人看的那一档;「没给结论(超期未完成 / 未知
    状态 / 无法查询)∧ 未生效」一并列出 —— 同样是"沃尔玛没说成、线上也没变"。
    (days 是**清单**的回看天数 —— 近几天判出来的给人看,与判哪些明细无关。)
    """
    with conn.cursor() as cur:
        cur.execute(_REVIEW_SQL, {
            "statuses": ["success", *feed_track.NO_VERDICT_STATUSES],
            "days": int(days), "store": store})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
