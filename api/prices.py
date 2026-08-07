"""沃尔玛 Price 域接口(函数面定稿见 docs/api_blueprint.md §7,矩阵 #19)。

  put_price()  PUT /v3/price  单品改价(同步快路径,结果当场已知)

批量改价走 feeds.submit_feed(feed_type="price");≤N 条走本函数的路由阈值
由 services 层显式 if 决定(蓝图取舍规则:能力不同的两个端点,严禁隐式降级)。
⚠ 官方 Deprecation Guide 列 "Price management Sunset 2026"(蓝图 §3 注),
maintenance 上线前需另行核验本端点仍可用。
"""

import logging

from api import _client

logger = logging.getLogger("api.prices")


def put_price(store: dict, sku: str, amount: float) -> tuple[bool, str]:
    """输入:店铺 + SKU + 新价 → 输出:(是否成功, 失败原因串)。

    同步接口,结果当场已知,不产生 feed_id 不进 feed 台账;
    金额 round 2 位(Walmart 拒收 >2 位小数)。401/403 抛 StoreDeadError。
    """
    _client.rate_acquire("prices.put", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    body = {"sku": str(sku),
            "pricing": [{"currentPrice": {"currency": "USD",
                                          "amount": round(float(amount), 2)},
                         "currentPriceType": "BASE"}]}
    status, _, data = _client.safe_put_ex(
        f"{_client.base_url()}/v3/price", token, store["client_id"],
        store["proxy"], json_body=body, timeout=60)
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    if status == 200:
        return True, ""
    why = f"HTTP {status}: {str(data)[:200]}"
    logger.warning("PUT /v3/price 失败(%s %s → %s): %s",
                   store["name"], sku, amount, why)
    return False, why
