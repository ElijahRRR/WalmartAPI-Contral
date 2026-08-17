"""沃尔玛 Returns 域接口(蓝图矩阵 #24;returns_sync / 订单域共用)。

  iter_returns()  GET /v3/returns  售后单生成器(增量时间窗)

实证修正(订单中心v1 sync_aftersales,2026-08 实测,修正蓝图 §4 模型3 描述):
- 时间过滤可用,但 returnCreationStartDate 与 EndDate **必须成对**,只传 start 返 400
  (legacy_survey"不支持时间过滤只能全量"的结论过时);
- meta.nextCursor 是**完整 query string**(?limit=...&offset=...),直接拼 URL,
  不能 parse_qs 拆参重发(与 orders 同款,当成参数传会被服务端忽略、原样返回第一页);
- 同一 cursor 重复出现 = 服务端未推进,必须立即停止防死循环。
"""

import logging
from datetime import datetime, timezone

from api import _client

logger = logging.getLogger("api.returns")

_MAX_PAGES = 500


def iter_returns(store: dict, *, created_start: str,
                 created_end: str | None = None, limit: int = 200):
    """输入:店铺 + 创建时间窗(ISO,end 缺省=当前时刻)→ 输出:returnOrder 生成器。

    翻页/限速/凭证失效语义全部内藏;首页 404/空按无数据处理(与 items 语义对齐)。
    """
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    end = created_end or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{_client.base_url()}/v3/returns"
    params: dict | None = {
        "returnCreationStartDate": created_start,
        "returnCreationEndDate": end,       # 必须成对传,只传 start 返 400(实证)
        "limit": str(limit),
    }
    seen_cursors: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        _client.rate_acquire("returns.list", store["client_id"])
        status, _, data = _client.safe_get_ex(
            url, token, store["client_id"], store["proxy"],
            params=params, max_retries=3)
        if status in (401, 403):
            raise _client.StoreDeadError(f"{store.get('name')} GET /v3/returns", status)
        if status == 404:
            return                          # 窗口内无售后单
        if status != 200:
            raise RuntimeError(f"GET /v3/returns 返回 {status}(店铺 {store.get('name')}): {data}")

        orders = (data or {}).get("returnOrders") or []
        yield from orders

        cursor = ((data or {}).get("meta") or {}).get("nextCursor")
        if not cursor or not orders:
            return
        if cursor in seen_cursors:
            logger.warning("GET /v3/returns nextCursor 重复,停止翻页(店铺 %s 第 %d 页)",
                           store.get("name"), page)
            return
        seen_cursors.add(cursor)
        url = f"{_client.base_url()}/v3/returns{cursor}"   # cursor 自带全部参数
        params = None
    logger.warning("GET /v3/returns 触达翻页上限 %d 页,可能未拉全(店铺 %s)",
                   _MAX_PAGES, store.get("name"))
