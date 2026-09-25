"""沃尔玛 feed 域:全项目唯一 feed 通道(蓝图 §5 定稿,矩阵 #10/#11/#12/#16/#17)。

  submit_feed(store, feed_type, entries, workflow) -> [切片结果 dict]
  iter_result_slices(results, entries) -> (切片结果, 本片条目) 生成器
  get_feed_status(store, feed_id) -> 汇总 dict
  iter_feed_items(store, feed_id) -> 逐 SKU 明细生成器(50/页自动翻)
  find_recent_feed(store, feed_type, items_received) -> 反查三态
  adopt_pending / close_pending / note_reconcile  pending 对账的落账原语
      (判据在 services/feed_track.reconcile_pending,feed_poll 每轮调)

旧系统之乱(本文件的存在理由):6 套 header schema 散落、DELETE_ITEM 3 处
裸 httpx 提交、防重语义七零八落。收口后:
- header schema 按 feedType 分发,版本字符串唯一出处 registry.FEED_SPEC_VERSIONS;
- 切片按「条数+字节」双约束(DELETE_ITEM 官方 400KB,定稿 350KB+2500 条);
- **三层防重**(安全铁律):①提交前先写 ops.feed_log(status=pending),同
  (feed_type, store, payload_key) **在途行**(pending/submitted)拒绝重复提交;
  终态行(done/failed)允许重占——防重拦的是并发/崩溃窗口内的双发,不是
  时间窗(所有者定稿:不设时间防重窗,重复删除无实害;上一笔已完结后再次
  提交同载荷是新一轮合法操作,顽固 SKU 每日双 feed 重发即依赖此语义);
  ②POST 网络异常后 find_recent_feed 反查三态(候选排除 feed_log 已占用的
  feedId,防同尺寸兄弟切片误收编),FOUND 收编、NOT_FOUND(30s 双确认)按
  同一方法补交一次、UNKNOWN 保持 pending;③pending 行由 feed_poll 每轮对账
  (2026-09-25 所有者批,services/feed_track.reconcile_pending):只读反查,查到
  收编、到期查不到落 failed「提交未确认」,**不补交**;本层只供台账原语。
- 写操作永不跨方法兜底(CLAUDE.md):补交只用同一 feedType 同一载荷。

本层只做接口适配:哪些 SKU 该删该停是 services/workflows 的事。
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

from api import _client
from registry import db, resources

logger = logging.getLogger("api.feeds")

# 终态与 SKU 级状态映射(官方枚举 2026-08-05 核验;不存在 COMPLETE)
FEED_TERMINAL = {"PROCESSED", "ERROR"}
FEED_STATUSES = {"RECEIVED", "INPROGRESS", "PROCESSED", "ERROR"}
_SKU_SUCCESS = {"SUCCESS"}
_SKU_FAILED = {"DATA_ERROR", "SYSTEM_ERROR", "TIMEOUT_ERROR"}
_SKU_INPROGRESS = {"INPROGRESS"}

_DETAIL_PAGE = 50          # includeDetails 明细页大小(官方两页矛盾 50 vs 1000,保守)
_PAGE_SLEEP = 0.2
_RECHECK_SLEEP = 30        # 反查 NOT_FOUND 的二次确认间隔(防索引滞后)

# ── 5xx 退避:官方阶梯 + **抖动**(2026-08-26 核验 developer.walmart.com;
# 官方原文与阶梯值见 api/_client.backoff —— #91 在本文件引入,店级重试标准
# 落地时**上提到 _client 作全项目唯一出处**,双轨禁止,此处只留别名)。
# ⚠ 抖动是官方明写的要求:没有它,一批同时失败的店会同时醒来,
# 把第一次的洪峰原样复制一遍。
_BACKOFF_LADDER = _client.BACKOFF_LADDER
_backoff = _client.backoff
# 一片最多补交几次(含第一次)。官方举例 5;我们取 3 ——
# 每次补交都吃一枚 feeds.post.MP_ITEM 令牌(8/hour/店),而且失败行还有
# 次日的 FAILED 重试通道兜着,不必在当轮把配额榨干
SETTLE_ATTEMPTS = 3

# 改码(SkuUpdate,SKU 改造批次 3)在本层**零改动**,核对结论写在这里,免得
# 下一个人照着计划再登记一遍变成双轨:形态 A 复用 MP_MAINTENANCE(切片 1000 条 /
# 24MB,见下),形态 B 复用 MP_ITEM(2000 条 / 24MB);两个桶
# `feeds.post.MP_MAINTENANCE` = 8/hour 与 `feeds.post.MP_ITEM` = 8/hour 都已在
# api/_client.py 登记。**不新增 feedType、不新增桶、不加业务判断**(铁律 2)。
# 切片双约束:(最大条数, 最大字节)。RETIRE_ITEM 官方无现值,按 DELETE 同档保守;
# price/inventory 官方 10MB(旧代码 25MB 超官方上限,蓝图 §5.4 收紧)。
# price 条数 8000(所有者定稿 2026-08-26:官方**硬限 10000 条**留两成余量,
# 1000 只是官方 "we recommend" 建议值 —— 新鲜度优先,单店整量当轮连发,
# 如 15000 条 = 8000+7000 两个 feed 连续提交;单条载荷约 130B,字节远不顶限)
_SLICE_LIMITS = {
    "DELETE_ITEM": (2500, 350_000),
    "RETIRE_ITEM": (1000, 350_000),
    "MP_MAINTENANCE": (1000, 24_000_000),
    "price": (8000, 9_500_000),
    "inventory": (4000, 9_500_000),
    # MP_INVENTORY v1.5:官方给的是 **1MB** 上限(与 price/inventory 的 10MB
    # 不同档),字节按 950KB 保守封顶;条数封顶另给,防单条异常大时切不动
    "MP_INVENTORY": (1000, 950_000),
    "MP_ITEM_MATCH": (1000, 24_000_000),
    "MP_ITEM": (2000, 24_000_000),
}


def _sanitize(v):
    """数值一律 round ≤2 位小数(Walmart 拒收 >2 位,蓝图 §5.1 sanitize 兜底)。"""
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    return v


def build_payload(feed_type: str, entries: list) -> dict:
    """输入:feedType + 条目(DELETE/RETIRE 传 SKU 字符串列表,MAINTENANCE 传
    MPItem dict 列表)→ 输出:完整 feed 载荷(header schema 按类型分发)。

    header 用旧系统实测值而非官方 sample(蓝图 §5.1:官方 sample 不可信)。
    """
    ver = resources.FEED_SPEC_VERSIONS.get(feed_type)
    if feed_type == "DELETE_ITEM":
        return {"ItemFeedHeader": {"businessUnit": "WALMART_US", "locale": "en",
                                   "version": ver},
                "Item": [{"Deletable": {"sku": str(s)}} for s in entries]}
    if feed_type == "RETIRE_ITEM":
        # feedDate 必须真 UTC(旧系统本地时间硬拼 Z 后缀是坑,已修)
        feed_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"RetireItemHeader": {"feedDate": feed_date, "version": ver},
                "RetireItem": [{"sku": str(s)} for s in entries]}
    if feed_type == "MP_MAINTENANCE":
        return {"MPItemFeedHeader": {"businessUnit": "WALMART_US", "locale": "en",
                                     "version": ver},
                "MPItem": [_sanitize(e) for e in entries]}
    if feed_type == "price":
        # PriceFeed v1.7:顶层**无外层包装**(加 {"PriceFeed":…} → feedStatus=ERROR,
        # itemsReceived=0,旧系统实证);条目 {"sku", "price"}
        return {"PriceHeader": {"version": ver},
                "Price": [{"sku": str(e["sku"]),
                           "pricing": [{"currentPrice": {
                               "currency": "USD",
                               "amount": _sanitize(float(e["price"]))},
                               "currentPriceType": "BASE"}]}
                          for e in entries]}
    if feed_type == "MP_ITEM":
        # 上架主链 v5:header **只收 3 字段**(照官方 sample 多传 subset →
        # 60670554076755,漏 businessUnit → 72600149546850,version 写 '5.0'
        # → 74597363510508,全是旧系统实证);条目为完整 MPItem dict
        # (Orderable+Visible,services/mp_mapper 构造)
        return {"MPItemFeedHeader": {"businessUnit": "WALMART_US",
                                     "locale": "en", "version": ver},
                "MPItem": [_sanitize(e) for e in entries]}
    if feed_type == "MP_ITEM_MATCH":
        # 跟卖 + 改码 v5(2026-09-07 升版,v4.2 同日退役)。header 与 MP_ITEM 同款
        # **businessUnit 制三字段**,依据是官方规范原件
        # refdata/specs/MP_ITEM_MATCH_5.0.20260607-22_38_54-api.json:
        # `MPItemFeedHeader` required = [businessUnit, locale, version] 且
        # additionalProperties=false ⇒ **多一个字段就是未知字段**。
        # ⚠ v4.2 那套 {processMode: REPLACE, subset: EXTERNAL,
        # sellingChannel: mpsetupbymatch} 在 v5 规范里**根本不存在**,已整段作废
        # —— REPLACE 语义仍在(同 GTIN + 新 SKU 原地换码、载荷给什么线上就是什么),
        # 只是它不再是 header 里的一个开关,而是这条 feedType 的固有行为。
        # 条目仍是 `{"Item": {...}}` 包装(v5 的 MPItem[] 每项 required=['Item'],
        # **不是** MP_ITEM 的 Orderable/Visible 分段),内容为完整 Item dict
        # (SPEC 预填模板 + 我方字段,services 层构造;api 层只包信封,铁律 2)
        return {"MPItemFeedHeader": {"businessUnit": "WALMART_US",
                                     "locale": "en", "version": ver},
                "MPItem": [{"Item": _sanitize(e)} for e in entries]}
    if feed_type == "inventory":
        # InventoryFeed v1.4:Inventory 首字母**必须大写**(小写 →
        # ERR_EXT_DATA_0503009,旧系统实证);条目 {"sku", "qty"}
        return {"InventoryHeader": {"version": ver},
                "Inventory": [{"sku": str(e["sku"]),
                               "quantity": {"unit": "EACH",
                                            "amount": int(e["qty"])}}
                              for e in entries]}
    if feed_type == "MP_INVENTORY":
        # 分节点批量库存 v1.5(多仓批次 2 启用;官方已无 BETA 标记)。
        # ⚠ 三处与 v1.4 不同,逐条都踩过或会踩:
        #   ① key 全小写(inventoryHeader/inventory),v1.4 是大写;
        #   ② 每 SKU 带 `shipNodes[]` —— **本仓恒单元素 = 该店受管仓**
        #      (一店一个受管仓,见 docs/multi_node_plan.md §0);
        #   ③ 数量字段名是 `quantity`(REST 写侧叫 inputQty、读侧叫
        #      availToSellQty,三套名字并存,别拿一套去套另一套)。
        # 条目 {"sku", "qty", "ship_node"};缺 ship_node 直接报错而不是
        # 悄悄发成"默认节点"——默认节点官方无定义,多仓下禁止依赖。
        rows = []
        for e in entries:
            node = str(e.get("ship_node") or "")
            if not node:
                raise ValueError(
                    f"MP_INVENTORY 条目缺 ship_node(sku={e.get('sku')})"
                    f":该 feedType 专供受管仓的店,不带节点的走 v1.4 inventory")
            rows.append({"sku": str(e["sku"]),
                         "shipNodes": [{"shipNode": node,
                                        "quantity": {"unit": "EACH",
                                                     "amount": int(e["qty"])}}]})
        return {"inventoryHeader": {"version": ver}, "inventory": rows}
    raise ValueError(f"feedType 未在 api/feeds.py 收录: {feed_type}"
                     f"(蓝图收录规则:预留端点只登记不实现)")


def _slices(feed_type: str, entries: list) -> list[list]:
    """按条数+字节双约束切片(字节按逐条序列化长度估算,含分隔符余量)。"""
    max_items, max_bytes = _SLICE_LIMITS[feed_type]
    base = len(json.dumps(build_payload(feed_type, []), ensure_ascii=False).encode())
    out, cur, size = [], [], base
    for e in entries:
        wrapped = build_payload(feed_type, [e])
        esz = (len(json.dumps(wrapped, ensure_ascii=False).encode()) - base) + 2
        if cur and (len(cur) >= max_items or size + esz > max_bytes):
            out.append(cur)
            cur, size = [], base
        cur.append(e)
        size += esz
    if cur:
        out.append(cur)
    return out


def payload_key(feed_type: str, entries: list) -> str:
    """输入:feedType + 条目 → 输出:内容指纹(防重键;条目顺序无关)。"""
    canon = sorted(json.dumps(e, ensure_ascii=False, sort_keys=True, default=str)
                   if isinstance(e, dict) else str(e) for e in entries)
    return hashlib.sha256("\x1f".join([feed_type, *canon]).encode()).hexdigest()[:32]


# ── ops.feed_log(三层防重的第①层)──────────────────────────────────────────

def _log_claim(workflow: str, store_name: str, feed_type: str, key: str,
               count: int | None = None, skus: list[str] | None = None):
    """输入:防重四元组(+ 本片条数与 SKU 列表)→ 输出:(log_id, None) 抢占成功 /
    (None, 既有行 dict)。

    防重只拦**在途行**:pending(结局不确定,宁停不重)/submitted(feed 处理中)
    拒绝重复提交;终态行 failed(确认未达)与 done(上一笔已完结)允许重占回
    pending——所有者定稿:不设时间防重窗,同载荷在上一笔完结后重发是合法新
    操作(顽固 SKU 每日停用+删除重发、反补第 2 次尝试都依赖此语义;2026-08-07
    审查修正:此前 done 永久拒绝,导致同载荷第二次反补/顽固重发永远发不出去)。

    条数与 SKU 列表(2026-09-25,pending 对账):POST 结局不确定时,feed_poll 事后按
    **条数**去沃尔玛 feed 列表反查、按 **SKU 集合**核对候选、收编后按 SKU 列表补落
    ops.feed_items —— 不在这里记下,事后就没有任何东西能认出"刚才那一笔"。
    重占时连同发送标记 / 对账计数 / 收口依据一并清空(那是上一笔的事实)。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.feed_log (workflow, store, feed_type, payload_key, status,"
            " item_count, skus) "
            "VALUES (%s, %s, %s, %s, 'pending', %s, %s) "
            "ON CONFLICT (feed_type, store, payload_key) DO NOTHING RETURNING id",
            (workflow, store_name, feed_type, key, count, skus))
        row = cur.fetchone()
        if row:
            return row[0], None
        cur.execute("SELECT id, status, feed_id FROM ops.feed_log "
                    "WHERE feed_type = %s AND store = %s AND payload_key = %s",
                    (feed_type, store_name, key))
        prev = cur.fetchone()
        if prev and prev[1] in ("failed", "done"):
            cur.execute("UPDATE ops.feed_log SET status = 'pending', "
                        "feed_id = NULL, workflow = %s, updated_at = now(), "
                        "item_count = %s, skus = %s, post_started_at = NULL, "
                        "recon_count = 0, close_basis = NULL "
                        "WHERE id = %s", (workflow, count, skus, prev[0]))
            logger.info("feed 防重:%s 行重占为 pending(%s %s)",
                        prev[1], store_name, feed_type)
            return prev[0], None
    return None, {"id": prev[0], "status": prev[1], "feed_id": prev[2]}


def _log_update(log_id, status: str, feed_id: str | None = None,
                basis: str | None = None) -> None:
    """输入:台账行 + 新状态(+ feedId、收口依据)→ 输出:无。

    `basis` 落 close_basis:这一行**为什么**是 failed(请求没发出 / 沃尔玛拒收 HTTP
    码 / 反查未达补交未果 / 提交未确认…)—— 所有者 2026-09-25:每种结局在库里都要有
    有事实依据的终态,光一个 failed 分不出"确定没发"与"沃尔玛拒了"。
    """
    with db.pg_conn() as conn:
        conn.execute(
            "UPDATE ops.feed_log SET status = %s, "
            "feed_id = COALESCE(%s, feed_id), close_basis = %s, updated_at = now() "
            "WHERE id = %s",
            (status, feed_id, (basis or "")[:500] or None, log_id))


def _log_posting(log_id) -> None:
    """输入:台账行 → 输出:无(post_started_at 落当下,**单独提交**)。

    在 rate_acquire 与取 token 之后、请求真正发出之前写(2026-09-25,pending 对账):
    pending 行上这一格为空 = 请求**确定没发出**(进程死在发之前、取 token 阶段就抛了);
    有值 = 发了(或正在发),结局要去沃尔玛那边反查,反查的时间窗也按它开。
    补交(同一方法)会再写一次 —— 窗口跟着最近一次发送走。
    """
    with db.pg_conn() as conn:
        conn.execute("UPDATE ops.feed_log SET post_started_at = now() WHERE id = %s",
                     (log_id,))


def query_pending() -> list[dict]:
    """输入:无 → 输出:pending/submitted 的 feed_log 行(feed_poll 轮询与 pending 对账、
    sku_migrate 闸⑤用)。

    带 `updated_at` 是给 feed_poll 算**在途年龄**用的(摘要折叠,见
    `services/feed_track.FEED_QUIET_HOURS`)。⚠ 年龄别拿 `created_at` 算:
    `_log_claim` 重占终态行时只改 status/feed_id/workflow/updated_at,
    created_at 留的是这个 payload_key **第一次**提交的时刻(可能是几个月前);
    submitted 行的 updated_at 才是这个 feedId 自己的提交时刻(`_log_update` 写的)。

    pending 对账(2026-09-25)要的几格一并带出:item_count / skus(反查与收编用)、
    post_started_at(请求发没发、反查窗口从哪开)、recon_count(反查过几次)。
    """
    sql = ("SELECT id, workflow, store, feed_type, payload_key, feed_id, status, "
           "created_at, updated_at, item_count, skus, post_started_at, recon_count "
           "FROM ops.feed_log WHERE status IN ('pending', 'submitted')")
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_feed_done(feed_id: str, ok: bool) -> None:
    """输入:feed_id + 终态是否成功 → 输出:无(feed_log 行落终态)。"""
    with db.pg_conn() as conn:
        conn.execute("UPDATE ops.feed_log SET status = %s, updated_at = now() "
                     "WHERE feed_id = %s", ("done" if ok else "failed", feed_id))


def _chunk_skus(feed_type: str, chunk: list) -> list[str]:
    # ⚠ MP_INVENTORY(受管仓分节点库存,条目 {sku, qty, ship_node})此前漏在这张表外
    #   (2026-09-07 生产实见,谭总12):漏了就走下面的 str(dict),台账 sku 列存的是
    #   整个 dict 的字符串,回执反哺永远「台账查无」、行永远「处理中」,而且不报错。
    if feed_type in ("MP_MAINTENANCE", "price", "inventory", "MP_INVENTORY",
                     "MP_ITEM_MATCH", "MP_ITEM"):
        # dict 条目:sku 在顶层或嵌在 Orderable 里(反补载荷是后者)
        # ⚠ 改码载荷的 Orderable.sku 是**新码**,故 ops.feed_items 台账按新码落账
        #    —— sku_migrate 的回执反查、feed_poll 的反哺都按新码找行,**这是有意的**
        #    (改成取旧码,回执就永远查不到而且不报错)。
        return [str(e.get("sku") or (e.get("Orderable") or {}).get("sku") or "")
                for e in chunk]
    return [str(s) for s in chunk]


_ITEMS_SQL = ("INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type, "
              "status, submitted_at) VALUES (%s, %s, %s, %s, %s, 'submitted', "
              "coalesce(%s::timestamptz, now())) "
              "ON CONFLICT (feed_id, sku) DO NOTHING")


def _items_rows(feed_id: str, workflow: str, store_name: str, feed_type: str,
                skus: list[str], submitted_at=None) -> list[tuple]:
    return [(feed_id, s, workflow, store_name, feed_type, submitted_at)
            for s in skus if s]


def _items_record(feed_id: str, workflow: str, store_name: str,
                  feed_type: str, skus: list[str], submitted_at=None) -> None:
    """提交成功即落 SKU 级台账(ops.feed_items,status=submitted);
    终态由 services/feed_track 轮询回写。SKU 级状态权威在库,飞书只是投影。

    `submitted_at` 只有事后收编(pending 对账,`adopt_pending`)给:那一笔真正发出的
    时刻是 post_started_at,不是收编这一刻 —— 落定期限与实际结果都从它起算。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(_ITEMS_SQL, _items_rows(feed_id, workflow, store_name,
                                                feed_type, skus, submitted_at))


# ── 提交(唯一入口)────────────────────────────────────────────────────────────

def submit_feed(store: dict, feed_type: str, entries: list, *,
                workflow: str = "", defer_settle: bool = False) -> list[dict]:
    """输入:店铺 + feedType + 条目 → 输出:逐切片结果
    [{"feed_id", "count", "outcome": submitted/dedup/unknown/failed/deferred}]。

    写操作零自动重试(CLAUDE.md:换方法重试=重复提交制造机);网络异常走
    反查三态,只有**确认未达**才按同一载荷补交。

    `defer_settle=True`:遇 5xx / 网络异常**当场不结算**,返回
    `outcome="deferred"`(附 `_settle` 句柄),由调用方在合适的时机调
    `settle_deferred()` 收尾。所有者定稿 2026-08-26:「重试的等到完整跑完
    一轮再尝试」——内联结算的老毛病是**补交打进的正是造成失败的那片拥堵**
    (固定 30s 后另外二十几家还在传),而整轮跑完之后管子已经空了。
    调用方不传就是老行为(内联结算),别的链一个字都不用改。
    """
    if feed_type not in _SLICE_LIMITS:
        raise ValueError(f"feedType 未收录: {feed_type}")
    results = []
    for chunk in _slices(feed_type, entries):
        key = payload_key(feed_type, chunk)
        log_id, prev = _log_claim(workflow, store["name"], feed_type, key,
                                  len(chunk), _chunk_skus(feed_type, chunk))
        if log_id is None:
            logger.warning("feed 防重命中:%s %s 同载荷已存在(status=%s feed_id=%s),"
                           "拒绝重复提交", store["name"], feed_type,
                           prev["status"], prev["feed_id"])
            results.append({"feed_id": prev["feed_id"], "count": len(chunk),
                            "outcome": "dedup"})
            continue
        results.append(_submit_one(store, feed_type, chunk, log_id, workflow,
                                   defer_settle=defer_settle))
    return results


def iter_result_slices(results: list[dict], entries: list):
    """输入:submit_feed 的逐切片结果 + 与提交时同序等长的条目 → 输出:
    逐片 (res, batch) 生成器(batch = 本片对应的那几条)。

    submit_feed 按「条数+字节」双约束切片(`_slices`),每个结果只回 `count`
    不回条目本身;调用方要把每片的结局落回自己那一行(飞书行号、台账行、
    (行, 载荷) 对……)就得按 count 走一遍游标。这句
    `batch = entries[i:i + res["count"]]; i += res["count"]` 是 submit_feed
    返回契约的机械后果,6 个工作流(maintenance / problem_product_cleanup /
    product_clear / sku_locked_heal / list_new / match_listing)逐字各写一遍,
    错一位就是整批结局落到别人行上、而且不报错。与 build_payload/payload_key
    同类:纯函数、零 I/O、零业务判断,只是把接口的返回形状还原成调用方要的
    形状(不新增端点)。

    `entries` 不必是提交上去的那份载荷,只要与它**同序等长**——上面 6 处传
    的正是各自的业务行列表。
    """
    i = 0
    for res in results:
        n = res["count"]
        yield res, entries[i:i + n]
        i += n


_PRE_FAIL = object()    # token/代理阶段失败的哨兵:feed 请求尚未发出,确定未达


def _post(store: dict, feed_type: str, payload: dict, log_id=None):
    """输入:店铺 + feedType + 载荷(+ 台账行)→ 输出:(HTTP 码, 头, 响应体) /
    `_PRE_FAIL`(请求没发出)。给了台账行就在**真正发出之前**落 post_started_at
    (`_log_posting`)—— pending 行上它为空 = 确定没发出。"""
    _client.rate_acquire(f"feeds.post.{feed_type}", store["client_id"])
    try:
        token = _client.get_token(store["client_id"], store["client_secret"],
                                  store["proxy"])
    except _client.StoreDeadError:
        raise
    except Exception as e:
        # token/代理阶段异常(2026-08-07 生产实证:SOCKS 代理 TLS 握手断线):
        # 与 POST 网络异常的"不知道到没到"本质不同——请求根本没发出,
        # 无需反查三态,直接按确定未达处理(failed 可重占,下轮重试)
        logger.error("feed 提交前置失败(token/代理,请求未发出):%s %s: %s",
                     store["name"], feed_type, e)
        return _PRE_FAIL, None, None
    if log_id is not None:
        _log_posting(log_id)
    return _client.safe_post_ex(
        f"{_client.base_url()}/v3/feeds", token, store["client_id"],
        store["proxy"], json_body=payload, params={"feedType": feed_type},
        timeout=120)


def _ok_result(log_id, feed_id: str, workflow: str, store_name: str,
               feed_type: str, skus: list, count: int) -> dict:
    """输入:台账句柄 + feedId + 本片 SKU → 输出:submitted 结果(并落两张台账)。

    从 `_submit_one` 的内部闭包提到模块级:`settle_deferred` 要走**同一条**
    收编路径。两处各写一遍的话,延后结算收编的 feed 会漏掉 ops.feed_items,
    而回执反哺器正是按它找行的 —— 漏了就是"feed 成功了但表上永远不回填"。
    """
    _log_update(log_id, "submitted", feed_id)
    _items_record(feed_id, workflow, store_name, feed_type, skus)
    return {"feed_id": feed_id, "count": count, "outcome": "submitted"}


def _submit_one(store: dict, feed_type: str, chunk: list, log_id,
                workflow: str = "", defer_settle: bool = False) -> dict:
    payload = build_payload(feed_type, chunk)
    skus = _chunk_skus(feed_type, chunk)

    def _ok(feed_id: str) -> dict:
        return _ok_result(log_id, feed_id, workflow, store["name"],
                          feed_type, skus, len(chunk))

    status, _, data = _post(store, feed_type, payload, log_id)
    n = len(chunk)

    if status is _PRE_FAIL:
        # retryable:请求未发出的确定性失败(区别于 4xx 被拒——那个重试也没用),
        # 调用方可安全地对同一载荷做二轮重提(failed 行可重占)
        _log_update(log_id, "failed", basis="未发出:取 token / 代理阶段失败,请求没发出")
        return {"feed_id": None, "count": n, "outcome": "failed",
                "retryable": True}

    if status == 200 and data and data.get("feedId"):
        logger.info("feed 提交成功:%s %s %d 条 feedId=%s",
                    store["name"], feed_type, n, data["feedId"])
        return _ok(data["feedId"])

    if status is not None and status < 500:
        # 4xx 明确拒绝:载荷/权限问题,没提交上,落 failed,绝不自动换姿势重试
        _log_update(log_id, "failed", basis=f"沃尔玛拒收 HTTP {status}:{str(data)[:300]}")
        logger.error("feed 提交被拒:%s %s HTTP %s 响应=%s",
                     store["name"], feed_type, status, str(data)[:300])
        return {"feed_id": None, "count": n, "outcome": "failed"}

    if status is not None:
        # 5xx ≠ 4xx(2026-08-19 官方核验 developer.walmart.com error-codes:
        # SYSTEM_ERROR/INVALID_SYSTEM_STATE 的官方处置是 retry with backoff)。
        # 生产实证:Akamai「Internal Server Error - Read」= 边缘从**源站**读
        # 响应失败——请求可能已达业务层、feed 可能已建成而响应丢了。这与
        # 网络异常同属"不知道到没到",当终态拒会漏收编已达的 feed、白弃
        # 未达的提交。往下走反查三态(NOT_FOUND 自带 30s 双确认 = 天然退避;
        # 此前把 5xx 与 4xx 混在一起终态拒,C017 的 297 条删除因此反复搁浅)
        logger.warning("feed 提交遇 5xx:%s %s HTTP %s,"
                       "按'不知道到没到'走反查三态",
                       store["name"], feed_type, status)

    if defer_settle:
        # 当场不结算:UPC 不回收、表不写终态,把重放句柄交给调用方。
        # `chunk` 一起带走 —— 载荷由 build_payload 确定性重建,不必扛着几 MB
        # 的 dict 过整轮(而且重建保证补交与首发**逐字节同一份载荷**)
        logger.warning("feed 提交不确定(HTTP %s):%s %s %d 条,"
                       "**延后到整轮跑完再结算**", status, store["name"],
                       feed_type, n)
        return {"feed_id": None, "count": n, "outcome": "deferred",
                "_settle": {"log_id": log_id, "feed_type": feed_type,
                            "chunk": chunk, "skus": skus,
                            "workflow": workflow, "count": n}}

    # 网络异常(status=None)或 5xx:不知道到没到 → 反查三态
    verdict, feed = find_recent_feed(store, feed_type, n)
    if verdict == "FOUND":
        logger.warning("feed 网络异常但反查已达:%s %s feedId=%s(收编,不补交)",
                       store["name"], feed_type, feed["feedId"])
        return _ok(feed["feedId"])
    if verdict == "NOT_FOUND":
        logger.warning("feed 网络异常且双确认未达:%s %s,按同一载荷补交一次",
                       store["name"], feed_type)
        status2, _, data2 = _post(store, feed_type, payload, log_id)
        if status2 == 200 and data2 and data2.get("feedId"):
            return _ok(data2["feedId"])
        _log_update(log_id, "failed",
                    basis=f"反查双确认未达,同一载荷补交一次仍未成(HTTP {status2})")
        return {"feed_id": None, "count": n, "outcome": "failed"}
    # UNKNOWN:保持 pending,交 feed_poll 的 pending 对账(只读反查、不补交),
    # 人不在环时宁停不重
    logger.error("feed 网络异常且反查不确定:%s %s 保持 pending 待 feed_poll 对账",
                 store["name"], feed_type)
    return {"feed_id": None, "count": n, "outcome": "unknown"}


def settle_deferred(store: dict, settle: dict) -> dict:
    """输入:店铺 + `_settle` 句柄 → 输出:终局结果(submitted/failed/unknown)。

    延后结算(所有者定稿 2026-08-26:「重试的等到完整跑完一轮再尝试」)。
    每一轮都是**先反查、后补交**,循环最多 `SETTLE_ATTEMPTS` 次:

        反查 FOUND      → 收编那条 feed,**不补交**(它本来就到了)
        反查 NOT_FOUND  → 按同一载荷补交一次;再遇 5xx 就退避后重来
        反查 UNKNOWN    → 连查都查不动,**绝不补交**(宁停不重),退避后重来

    ⚠ **补交前必须重新反查,一次都不许省**。省掉的话第二次补交就可能撞上
    第一次其实已经落地的 feed —— 那是双上架,本仓最贵的错误。反查本身不贵
    (feeds.get 是 3000/min 的大桶),而每省一次都在赌。

    ⚠ 载荷由 `build_payload(chunk)` **重建**,不是扛着首发那份走完整轮:
    重建是确定性的,保证补交与首发逐字节同一份 —— 这正是防重指纹
    (payload_key)成立的前提,也是"同一方法补交"这条铁律的字面意思。

    退避走官方阶梯 + 抖动(见 `_backoff`)。全部尝试用尽仍未确认:
      · 最后一次是 NOT_FOUND ⇒ failed(调用方可回收 UPC,次日重试通道接手)
      · 最后一次是 UNKNOWN   ⇒ unknown(feed_log 保持 pending,交 feed_poll 对账)
    """
    log_id = settle["log_id"]
    feed_type, chunk = settle["feed_type"], settle["chunk"]
    skus, workflow, n = settle["skus"], settle["workflow"], settle["count"]
    verdict = "UNKNOWN"

    for attempt in range(SETTLE_ATTEMPTS):
        verdict, feed = find_recent_feed(store, feed_type, n)
        if verdict == "FOUND":
            logger.warning("延后结算:%s %s 反查已达 feedId=%s(收编,不补交)",
                           store["name"], feed_type, feed["feedId"])
            return _ok_result(log_id, feed["feedId"], workflow,
                              store["name"], feed_type, skus, n)
        if verdict == "NOT_FOUND":
            logger.warning("延后结算:%s %s 双确认未达,第 %d/%d 次补交",
                           store["name"], feed_type, attempt + 1,
                           SETTLE_ATTEMPTS)
            status, _, data = _post(store, feed_type,
                                    build_payload(feed_type, chunk), log_id)
            if status == 200 and data and data.get("feedId"):
                return _ok_result(log_id, data["feedId"], workflow,
                                  store["name"], feed_type, skus, n)
            if status is not None and status is not _PRE_FAIL and status < 500:
                # 4xx:载荷/权限问题,再补多少次都是同一个拒 —— 立刻收手
                _log_update(log_id, "failed",
                            basis=f"延后结算补交被拒 HTTP {status}:{str(data)[:300]}")
                logger.error("延后结算:%s %s 补交被拒 HTTP %s 响应=%s",
                             store["name"], feed_type, status, str(data)[:300])
                return {"feed_id": None, "count": n, "outcome": "failed"}
        # NOT_FOUND 补交又遇 5xx,或 UNKNOWN 查不动:退避后重来
        if attempt < SETTLE_ATTEMPTS - 1:
            wait = _backoff(attempt)
            logger.info("延后结算:%s %s 第 %d 次未果,退避 %.1fs(官方阶梯+抖动)",
                        store["name"], feed_type, attempt + 1, wait)
            time.sleep(wait)

    if verdict == "NOT_FOUND":
        _log_update(log_id, "failed",
                    basis=f"延后结算:反查未达,{SETTLE_ATTEMPTS} 次补交全未果")
        logger.error("延后结算:%s %s %d 次补交全未果,判未达(UPC 可回收,"
                     "次日重试通道接手)", store["name"], feed_type,
                     SETTLE_ATTEMPTS)
        return {"feed_id": None, "count": n, "outcome": "failed"}
    logger.error("延后结算:%s %s 反查始终不确定,保持 pending 待 feed_poll 对账",
                 store["name"], feed_type)
    return {"feed_id": None, "count": n, "outcome": "unknown"}


# ── 反查三态(蓝图 §5.2 ②)──────────────────────────────────────────────────

#: 事后对账(since 模式)的时间窗下沿余量:我们记的发送时刻与沃尔玛的 feedDate
#: 之间有时钟差,往前放 2 分钟
_SINCE_SLACK_MS = 2 * 60_000
#: since 模式最多往后翻几页(官方 limit 上限 50/页):列表**不按 feedType 过滤**
#: (官方参数只有 feedId / offset / limit),全店各类 feed 混在一起,20 页 = 最近
#: 1000 条,够覆盖任何落定期限内的那一笔
_PROBE_PAGES = 20


def find_recent_feed(store: dict, feed_type: str, items_received: int,
                     window_minutes: int = 30, *, since=None,
                     expect_skus: list[str] | None = None,
                     recheck: bool = True) -> tuple[str, dict | None]:
    """输入:店铺 + feedType + 精确条数(+ 事后对账参数)→ 输出:(FOUND/NOT_FOUND/UNKNOWN, feed)。

    按 (itemsReceived 精确数, feedDate 时间窗) 匹配"刚才那笔";
    **候选排除 ops.feed_log 已占用的 feedId**——同尺寸兄弟切片(如 5000 条
    删除切成 2500+2500)会满足同样的 (feedType, 条数) 指纹,不排除会把片 2
    误收编到片 1 的 feed 上,整片静默丢失(2026-08-07 审查修正)。
    NOT_FOUND 需 30s 后二次确认(防沃尔玛索引滞后)。查询自身失败 → UNKNOWN。

    **事后对账**(pending 对账,2026-09-25,由 services/feed_track 调):
      · `since`(post_started_at)给了 ⇒ 时间窗按「since 前 2 分钟 ~ since 后
        window 分钟」开,往后翻页(最多 _PROBE_PAGES 页);不给 ⇒ 老行为(此刻往回
        window 分钟,只看第一页);
      · `expect_skus` 给了 ⇒ 候选必须过 **SKU 集合核对**:读它的明细,SKU 集合与本片
        完全一致才算对得上。⚠ 官方列表参数里**没有 feedType**(只有 feedId / offset /
        limit),返回的 feedType 写法也没法核实("item" 还是 "MP_ITEM"),只按条数 +
        时间窗认,会把同店同条数的别类 feed 收编过来;SKU 集合是决定性的证据。
        扫完整个窗口,**恰好一条对得上、且没有核不了的候选**才 FOUND;不止一条对得上
        (同一批 SKU 的别类 feed 也还没记账),或有候选明细读不到 / 还是空的 / 只露出
        本片一部分 ⇒ 这一轮分不清,返回 ("UNKNOWN", {"matched": […], "unverified": […]})
        交调用方写进依据,下轮再核;
      · `recheck=False` ⇒ 不做 30 秒二次确认(事后对账离发送已久,不存在索引滞后)。
    候选一律排除本店 feed_log 上已记账的 feedId(**不分 feedType**,见 `_claimed_ids`)。
    本函数**只读**:一个 feed 都不发,补交与否由调用方按原规则决定。
    """
    def _claimed_ids() -> set:
        # 按店排除、**不分 feedType**(2026-09-25):列表是全店各类 feed 混在一起
        # (官方参数里没有 feedType),同店同条数、甚至同一批 SKU 的别类 feed(同一批
        # SKU 先停用后删除、同时改价又改库存)只要已记在 feed_log 上,就不可能是
        # "刚才那笔" —— 此前只排除同类,别类的会被 SKU 集合核对放行、错收编过来
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT feed_id FROM ops.feed_log WHERE store = %s "
                        "AND feed_id IS NOT NULL", (store["name"],))
            return {r[0] for r in cur.fetchall()}

    def _feed_date_ms(fd):
        if isinstance(fd, (int, float)):
            return fd
        if isinstance(fd, str):
            try:
                return datetime.fromisoformat(
                    fd.replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                return None
        return None

    since_ms = since.timestamp() * 1000 if since is not None else None

    def _in_window(fd_ms) -> bool:
        if since_ms is None:
            return fd_ms is None or fd_ms >= time.time() * 1000 - window_minutes * 60_000
        if fd_ms is None:
            # 时刻解析不了:只有 SKU 集合核对兜底时才认它当候选
            return expect_skus is not None
        return since_ms - _SINCE_SLACK_MS <= fd_ms <= since_ms + window_minutes * 60_000

    def _page(offset: int):
        _client.rate_acquire("feeds.get", store["client_id"])
        token = _client.get_token(store["client_id"], store["client_secret"],
                                  store["proxy"])
        status, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/feeds", token, store["client_id"],
            store["proxy"], params={"feedType": feed_type, "limit": 50,
                                    "offset": offset},
            max_retries=2)
        if status != 200 or not isinstance(data, dict):
            return None
        return ((data.get("results") or {}).get("feed")
                or data.get("feed") or [])

    def _verified(f) -> bool | None:
        """SKU 集合核对:True 完全一致 / False 有本片之外的 SKU(不是这一笔)/
        None 这一轮核不了 —— 明细读不到、还是空的,或**只露出本片的一部分**(可能还在
        处理,也可能沃尔玛明细漏条;两头都不拿它下结论,等它核得清)。"""
        try:
            got = {str(it.get("sku") or "") for it in iter_feed_items(store, f["feedId"])}
        except FeedQueryError:
            return None
        got.discard("")
        want = {str(s) for s in expect_skus}
        if not got or got < want:
            return None
        return got == want

    def _probe() -> tuple[str, dict | None]:
        claimed = _claimed_ids()
        matched: list[dict] = []
        unverified: list[dict] = []
        for n in range(1 if since_ms is None else _PROBE_PAGES):
            page = _page(n * 50)
            if page is None:
                return "UNKNOWN", None
            for f in page:
                if f.get("feedId") in claimed:
                    continue        # 已被本系统其他提交占用,不是"刚才那笔"
                if f.get("itemsReceived") != items_received:
                    continue
                if not _in_window(_feed_date_ms(f.get("feedDate"))):
                    continue
                if expect_skus is None:
                    return "FOUND", f
                ok = _verified(f)
                if ok:
                    matched.append(f)
                elif ok is None:
                    unverified.append(f)
            if len(page) < 50:
                break
        if len(matched) == 1 and not unverified:
            return "FOUND", matched[0]
        if matched or unverified:
            # 不止一条对得上,或还有核不了的:分不清是哪一笔,不收编,候选带回去写进依据
            return "UNKNOWN", {"matched": [f.get("feedId") for f in matched],
                               "unverified": [f.get("feedId") for f in unverified]}
        return "NOT_FOUND", None

    verdict, feed = _probe()
    if verdict == "NOT_FOUND" and recheck:
        time.sleep(_RECHECK_SLEEP)
        verdict, feed = _probe()
    return verdict, feed


# ── pending 对账的落账原语(2026-09-25;判据在 services/feed_track.reconcile_pending)──

def adopt_pending(row: dict, feed_id: str, posted_at) -> bool:
    """输入:pending 台账行(query_pending 的 dict)+ 核对过的 feedId + 发送时刻
    → 输出:是否收编成功(行已不是 pending 就不动)。

    与提交当场收编(`_ok_result`)落同样两张账 —— feed_log 转 submitted、
    ops.feed_items 按本片 SKU 列表落 submitted —— **同一个事务**:分两次提交会留下
    "feed_log 说在途、台账一行没有"的半截状态,轮询到期只能空收口,SKU 结论全丢。
    时刻一律用发送时刻(落定期限与实际结果从它起算),不是收编这一刻。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ops.feed_log SET status = 'submitted', feed_id = %s, "
                    "updated_at = %s, close_basis = NULL "
                    "WHERE id = %s AND status = 'pending' RETURNING id",
                    (feed_id, posted_at, row["id"]))
        if cur.fetchone() is None:
            return False
        cur.executemany(_ITEMS_SQL, _items_rows(
            feed_id, row["workflow"], row["store"], row["feed_type"],
            list(row.get("skus") or []), posted_at))
    return True


def close_pending(log_id, basis: str) -> bool:
    """输入:pending 台账行 + 收口依据 → 输出:是否落了 failed(行已不是 pending 就不动)。

    落 failed = 这个载荷**解锁**(终态行可重占):要不要再发由原业务工作流下一轮按原
    方法决定 —— 本函数与对账器都**不补交**(写操作永不自动兜底)。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ops.feed_log SET status = 'failed', close_basis = %s, "
                    "updated_at = now() WHERE id = %s AND status = 'pending' "
                    "RETURNING id", ((basis or "")[:500], log_id))
        return cur.fetchone() is not None


def note_reconcile(log_id) -> None:
    """输入:pending 台账行 → 输出:无(反查次数 +1,收口依据里要报"查了几次")。"""
    with db.pg_conn() as conn:
        conn.execute("UPDATE ops.feed_log SET recon_count = recon_count + 1 "
                     "WHERE id = %s", (log_id,))


# ── 状态轮询 ──────────────────────────────────────────────────────────────────

def _feed_url(feed_id: str) -> str:
    # feedId 含 '@',必须整体转义(蓝图 §5.3 实证)
    return f"{_client.base_url()}/v3/feeds/{quote(feed_id, safe='')}"


class FeedQueryError(RuntimeError):
    """feed 状态 / 明细 GET 没拿到 200 JSON。`status` 是 HTTP 状态码,网络未达为 None。

    带码上抛是为了让调用方分得清 **404**(官方:「The feedId does not exist or is
    not visible to your account.」,feeds-overview)与其他读取失败 —— 落定期限
    过后两者处置不同(services/feed_track)。文案用「返回 {status}」格式,
    store_retry.diagnose 靠它归类(沃尔玛NNN / 网络未达)。
    """

    def __init__(self, msg: str, status):
        super().__init__(msg)
        self.status = status


def get_feed_status(store: dict, feed_id: str) -> dict:
    """输入:店铺 + feed_id → 输出:汇总 dict(feedStatus/itemsReceived/…)。

    未知 feedStatus 告警而非静默"处理中"(防官方加值导致行永久卡死,C1 实证)。
    非 200 抛 FeedQueryError(带 HTTP 码)。
    """
    _client.rate_acquire("feeds.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    status, _, data = _client.safe_get_ex(
        _feed_url(feed_id), token, store["client_id"], store["proxy"],
        params={"limit": 0}, max_retries=2)
    if status != 200 or not isinstance(data, dict):
        raise FeedQueryError(f"feed 状态查询返回 {status}(feedId={feed_id})", status)
    fs = data.get("feedStatus")
    if fs not in FEED_STATUSES:
        logger.warning("未知 feedStatus=%r(feedId=%s),官方枚举可能已扩,请核对",
                       fs, feed_id)
    return data


def iter_feed_items(store: dict, feed_id: str):
    """输入:店铺 + feed_id → 输出:逐 SKU 明细生成器(itemDetails,50/页自动翻)。

    非 200 抛 FeedQueryError(带 HTTP 码,同 get_feed_status)。
    """
    offset = 0
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    while True:
        _client.rate_acquire("feeds.get", store["client_id"])
        status, _, data = _client.safe_get_ex(
            _feed_url(feed_id), token, store["client_id"], store["proxy"],
            params={"includeDetails": "true", "limit": _DETAIL_PAGE,
                    "offset": offset},
            max_retries=2)
        if status != 200 or not isinstance(data, dict):
            raise FeedQueryError(
                f"feed 明细查询返回 {status}(feedId={feed_id})", status)
        items = ((data.get("itemDetails") or {}).get("itemIngestionStatus")
                 or [])
        yield from items
        total = data.get("itemsReceived") or 0
        offset += len(items)
        # 双轨终止:累计条数够了,或本页不足一整页
        if offset >= total or len(items) < _DETAIL_PAGE:
            return
        time.sleep(_PAGE_SLEEP)


def sku_outcome(ingestion_status: str) -> str:
    """输入:SKU 级 ingestionStatus → 输出:success/failed/processing/unknown。

    官方五值(含旧系统遗漏的 INPROGRESS);未知值告警返回 unknown,不装成功也不装失败。
    """
    s = (ingestion_status or "").upper()
    if s in _SKU_SUCCESS:
        return "success"
    if s in _SKU_FAILED:
        return "failed"
    if s in _SKU_INPROGRESS:
        return "processing"
    logger.warning("未知 SKU ingestionStatus=%r,官方枚举可能已扩", ingestion_status)
    return "unknown"
