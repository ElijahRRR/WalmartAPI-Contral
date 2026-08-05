"""沃尔玛 Orders 域接口(蓝图矩阵 #23)。

  iter_orders()  订单生成器,分页模型 2(蓝图 §4):meta.nextCursor 返回带 '?' 的
                 完整 query 串,直接拼在 /v3/orders 后;单店内必须串行翻页。

当前实现同步版(daily_report 单店顺序拉 24h 窗口够用);
order_audit 迁移时在本文件内部加 async 变体(30+ 店并发,蓝图 §6.3),不另起体系。
stats dict(可选传入)填充 {"total": 首页 meta.totalCount}——总单数以它为准,
不要数生成器条数(翻页中新单进来会导致两者不一致)。
"""

import logging

from api import _client

logger = logging.getLogger("api.orders")


def iter_orders(store: dict, *, created_start: str | None = None,
                created_end: str | None = None,
                last_modified_start: str | None = None,
                limit: int = 200, stats: dict | None = None):
    """输入:店铺 + 时间过滤(ISO8601 字符串)→ 输出:订单 dict 生成器。"""
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    params: dict = {"limit": limit}
    if created_start:
        params["createdStartDate"] = created_start
    if created_end:
        params["createdEndDate"] = created_end
    if last_modified_start:
        params["lastModifiedStartDate"] = last_modified_start

    url = f"{_client.base_url()}/v3/orders"
    cursor_suffix: str | None = None
    first_page = True
    while True:
        _client.rate_acquire("orders.list", store["client_id"])
        if cursor_suffix:
            # 分页模型 2:nextCursor 就是完整 query 串(含 '?'),直接拼 URL,不再带 params
            status, _, data = _client.safe_get_ex(
                url + cursor_suffix, token, store["client_id"], store["proxy"], max_retries=3)
        else:
            status, _, data = _client.safe_get_ex(
                url, token, store["client_id"], store["proxy"], params=params, max_retries=3)
        if status in (401, 403):
            raise _client.StoreDeadError(store["name"], status)
        if status == 404:       # 窗口内零订单(与 items 家族同款语义)
            if first_page and stats is not None:
                stats["total"] = 0
            return
        if status != 200 or data is None:
            raise RuntimeError(f"GET /v3/orders 返回 {status}(店铺 {store['name']})")

        lst = (data or {}).get("list") or {}
        meta = lst.get("meta") or {}
        if first_page and stats is not None:
            stats["total"] = int(meta.get("totalCount") or 0)
        first_page = False
        for order in (lst.get("elements") or {}).get("order") or []:
            yield order
        cursor_suffix = meta.get("nextCursor")
        if not cursor_suffix:
            return


def order_product_sales(order: dict) -> float:
    """输入:单个订单 dict → 输出:该订单 PRODUCT 类费用合计(销售额口径,旧系统同款)。"""
    total = 0.0
    lines = ((order.get("orderLines") or {}).get("orderLine")) or []
    for line in lines:
        charges = ((line.get("charges") or {}).get("charge")) or []
        for c in charges:
            if c.get("chargeType") == "PRODUCT":
                amt = (c.get("chargeAmount") or {}).get("amount")
                total += float(amt or 0)
    return round(total, 2)
