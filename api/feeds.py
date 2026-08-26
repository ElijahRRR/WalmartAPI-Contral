"""沃尔玛 feed 域:全项目唯一 feed 通道(蓝图 §5 定稿,矩阵 #10/#11/#12/#16/#17)。

  submit_feed(store, feed_type, entries, workflow) -> [切片结果 dict]
  get_feed_status(store, feed_id) -> 汇总 dict
  iter_feed_items(store, feed_id) -> 逐 SKU 明细生成器(50/页自动翻)
  find_recent_feed(store, feed_type, items_received) -> 反查三态

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
  同一方法补交一次、UNKNOWN 保持 pending 留给启动对账;③工作流启动时对账
  pending/submitted 行(query_pending 供工作流用)。
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
        # 跟卖 v4.2(蓝图 §5.4 定稿):sellingChannel 制 header,与 v5 的
        # businessUnit 制不同套;processMode 只有 REPLACE(同 sku 覆盖,幂等);
        # 条目为完整 Item dict(SPEC 预填模板 + 我方字段,services 层构造)
        return {"MPItemFeedHeader": {"processMode": "REPLACE",
                                     "subset": "EXTERNAL", "locale": "en",
                                     "sellingChannel": "mpsetupbymatch",
                                     "version": ver},
                "MPItem": [{"Item": _sanitize(e)} for e in entries]}
    if feed_type == "inventory":
        # InventoryFeed v1.4:Inventory 首字母**必须大写**(小写 →
        # ERR_EXT_DATA_0503009,旧系统实证);条目 {"sku", "qty"}
        return {"InventoryHeader": {"version": ver},
                "Inventory": [{"sku": str(e["sku"]),
                               "quantity": {"unit": "EACH",
                                            "amount": int(e["qty"])}}
                              for e in entries]}
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

def _log_claim(workflow: str, store_name: str, feed_type: str, key: str):
    """输入:防重四元组 → 输出:(log_id, None) 抢占成功 / (None, 既有行 dict)。

    防重只拦**在途行**:pending(结局不确定,宁停不重)/submitted(feed 处理中)
    拒绝重复提交;终态行 failed(确认未达)与 done(上一笔已完结)允许重占回
    pending——所有者定稿:不设时间防重窗,同载荷在上一笔完结后重发是合法新
    操作(顽固 SKU 每日停用+删除重发、反补第 2 次尝试都依赖此语义;2026-08-07
    审查修正:此前 done 永久拒绝,导致同载荷第二次反补/顽固重发永远发不出去)。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.feed_log (workflow, store, feed_type, payload_key, status) "
            "VALUES (%s, %s, %s, %s, 'pending') "
            "ON CONFLICT (feed_type, store, payload_key) DO NOTHING RETURNING id",
            (workflow, store_name, feed_type, key))
        row = cur.fetchone()
        if row:
            return row[0], None
        cur.execute("SELECT id, status, feed_id FROM ops.feed_log "
                    "WHERE feed_type = %s AND store = %s AND payload_key = %s",
                    (feed_type, store_name, key))
        prev = cur.fetchone()
        if prev and prev[1] in ("failed", "done"):
            cur.execute("UPDATE ops.feed_log SET status = 'pending', "
                        "feed_id = NULL, workflow = %s, updated_at = now() "
                        "WHERE id = %s", (workflow, prev[0]))
            logger.info("feed 防重:%s 行重占为 pending(%s %s)",
                        prev[1], store_name, feed_type)
            return prev[0], None
    return None, {"id": prev[0], "status": prev[1], "feed_id": prev[2]}


def _log_update(log_id, status: str, feed_id: str | None = None) -> None:
    with db.pg_conn() as conn:
        conn.execute(
            "UPDATE ops.feed_log SET status = %s, "
            "feed_id = COALESCE(%s, feed_id), updated_at = now() WHERE id = %s",
            (status, feed_id, log_id))


def query_pending(store_name: str | None = None) -> list[dict]:
    """输入:可选店铺 → 输出:pending/submitted 的 feed_log 行(启动对账用)。"""
    sql = ("SELECT id, workflow, store, feed_type, payload_key, feed_id, status, "
           "created_at FROM ops.feed_log WHERE status IN ('pending', 'submitted')")
    args: tuple = ()
    if store_name:
        sql += " AND store = %s"
        args = (store_name,)
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_feed_done(feed_id: str, ok: bool) -> None:
    """输入:feed_id + 终态是否成功 → 输出:无(feed_log 行落终态)。"""
    with db.pg_conn() as conn:
        conn.execute("UPDATE ops.feed_log SET status = %s, updated_at = now() "
                     "WHERE feed_id = %s", ("done" if ok else "failed", feed_id))


def _chunk_skus(feed_type: str, chunk: list) -> list[str]:
    if feed_type in ("MP_MAINTENANCE", "price", "inventory",
                     "MP_ITEM_MATCH", "MP_ITEM"):
        # dict 条目:sku 在顶层或嵌在 Orderable 里(反补载荷是后者)
        return [str(e.get("sku") or (e.get("Orderable") or {}).get("sku") or "")
                for e in chunk]
    return [str(s) for s in chunk]


def _items_record(feed_id: str, workflow: str, store_name: str,
                  feed_type: str, skus: list[str]) -> None:
    """提交成功即落 SKU 级台账(ops.feed_items,status=submitted);
    终态由 services/feed_track 轮询回写。SKU 级状态权威在库,飞书只是投影。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type, "
            "status) VALUES (%s, %s, %s, %s, %s, 'submitted') "
            "ON CONFLICT (feed_id, sku) DO NOTHING",
            [(feed_id, s, workflow, store_name, feed_type) for s in skus if s])


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
        log_id, prev = _log_claim(workflow, store["name"], feed_type, key)
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


_PRE_FAIL = object()    # token/代理阶段失败的哨兵:feed 请求尚未发出,确定未达


def _post(store: dict, feed_type: str, payload: dict):
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

    status, _, data = _post(store, feed_type, payload)
    n = len(chunk)

    if status is _PRE_FAIL:
        # retryable:请求未发出的确定性失败(区别于 4xx 被拒——那个重试也没用),
        # 调用方可安全地对同一载荷做二轮重提(failed 行可重占)
        _log_update(log_id, "failed")
        return {"feed_id": None, "count": n, "outcome": "failed",
                "retryable": True}

    if status == 200 and data and data.get("feedId"):
        logger.info("feed 提交成功:%s %s %d 条 feedId=%s",
                    store["name"], feed_type, n, data["feedId"])
        return _ok(data["feedId"])

    if status is not None and status < 500:
        # 4xx 明确拒绝:载荷/权限问题,没提交上,落 failed,绝不自动换姿势重试
        _log_update(log_id, "failed")
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
        status2, _, data2 = _post(store, feed_type, payload)
        if status2 == 200 and data2 and data2.get("feedId"):
            return _ok(data2["feedId"])
        _log_update(log_id, "failed")
        return {"feed_id": None, "count": n, "outcome": "failed"}
    # UNKNOWN:保持 pending,留给启动对账(query_pending),人不在环时宁停不重
    logger.error("feed 网络异常且反查不确定:%s %s 保持 pending 待启动对账",
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
      · 最后一次是 UNKNOWN   ⇒ unknown(feed_log 保持 pending,交启动对账)
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
                                    build_payload(feed_type, chunk))
            if status == 200 and data and data.get("feedId"):
                return _ok_result(log_id, data["feedId"], workflow,
                                  store["name"], feed_type, skus, n)
            if status is not None and status is not _PRE_FAIL and status < 500:
                # 4xx:载荷/权限问题,再补多少次都是同一个拒 —— 立刻收手
                _log_update(log_id, "failed")
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
        _log_update(log_id, "failed")
        logger.error("延后结算:%s %s %d 次补交全未果,判未达(UPC 可回收,"
                     "次日重试通道接手)", store["name"], feed_type,
                     SETTLE_ATTEMPTS)
        return {"feed_id": None, "count": n, "outcome": "failed"}
    logger.error("延后结算:%s %s 反查始终不确定,保持 pending 待启动对账",
                 store["name"], feed_type)
    return {"feed_id": None, "count": n, "outcome": "unknown"}


# ── 反查三态(蓝图 §5.2 ②)──────────────────────────────────────────────────

def find_recent_feed(store: dict, feed_type: str, items_received: int,
                     window_minutes: int = 30) -> tuple[str, dict | None]:
    """输入:店铺 + feedType + 精确条数 → 输出:(FOUND/NOT_FOUND/UNKNOWN, feed)。

    按 (feedType, itemsReceived 精确数, feedDate 时间窗) 匹配"刚才那笔";
    **候选排除 ops.feed_log 已占用的 feedId**——同尺寸兄弟切片(如 5000 条
    删除切成 2500+2500)会满足同样的 (feedType, 条数) 指纹,不排除会把片 2
    误收编到片 1 的 feed 上,整片静默丢失(2026-08-07 审查修正)。
    NOT_FOUND 需 30s 后二次确认(防沃尔玛索引滞后)。查询自身失败 → UNKNOWN。
    """
    def _claimed_ids() -> set:
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT feed_id FROM ops.feed_log WHERE store = %s "
                        "AND feed_type = %s AND feed_id IS NOT NULL",
                        (store["name"], feed_type))
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

    def _probe():
        _client.rate_acquire("feeds.get", store["client_id"])
        token = _client.get_token(store["client_id"], store["client_secret"],
                                  store["proxy"])
        status, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/feeds", token, store["client_id"],
            store["proxy"], params={"feedType": feed_type, "limit": 50},
            max_retries=2)
        if status != 200 or not isinstance(data, dict):
            return None
        feeds = ((data.get("results") or {}).get("feed")
                 or data.get("feed") or [])
        claimed = _claimed_ids()
        cutoff = time.time() * 1000 - window_minutes * 60_000
        for f in feeds:
            if f.get("feedId") in claimed:
                continue        # 已被本系统其他提交占用,不是"刚才那笔"
            fd_ms = _feed_date_ms(f.get("feedDate"))
            if (f.get("itemsReceived") == items_received
                    and (fd_ms is None or fd_ms >= cutoff)):
                return f
        return {}

    first = _probe()
    if first is None:
        return "UNKNOWN", None
    if first:
        return "FOUND", first
    time.sleep(_RECHECK_SLEEP)
    second = _probe()
    if second is None:
        return "UNKNOWN", None
    if second:
        return "FOUND", second
    return "NOT_FOUND", None


# ── 状态轮询 ──────────────────────────────────────────────────────────────────

def _feed_url(feed_id: str) -> str:
    # feedId 含 '@',必须整体转义(蓝图 §5.3 实证)
    return f"{_client.base_url()}/v3/feeds/{quote(feed_id, safe='')}"


def get_feed_status(store: dict, feed_id: str) -> dict:
    """输入:店铺 + feed_id → 输出:汇总 dict(feedStatus/itemsReceived/…)。

    未知 feedStatus 告警而非静默"处理中"(防官方加值导致行永久卡死,C1 实证)。
    """
    _client.rate_acquire("feeds.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    status, _, data = _client.safe_get_ex(
        _feed_url(feed_id), token, store["client_id"], store["proxy"],
        params={"limit": 0}, max_retries=2)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"feed 状态查询失败 HTTP {status}(feedId={feed_id})")
    fs = data.get("feedStatus")
    if fs not in FEED_STATUSES:
        logger.warning("未知 feedStatus=%r(feedId=%s),官方枚举可能已扩,请核对",
                       fs, feed_id)
    return data


def iter_feed_items(store: dict, feed_id: str):
    """输入:店铺 + feed_id → 输出:逐 SKU 明细生成器(itemDetails,50/页自动翻)。"""
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
            raise RuntimeError(f"feed 明细查询失败 HTTP {status}(feedId={feed_id})")
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
