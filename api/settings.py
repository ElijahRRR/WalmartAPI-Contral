"""沃尔玛 Settings 域接口(listing L2d;矩阵内端点)。

  get_partner_id()  GET /v3/settings/partnerprofile → Partner ID
                    (Virtual Fulfillment Center ID)

用途:MP_ITEM 的 orderable.inventory[].fulfillmentCenterID 必填且必须是
Partner ID(旧系统实证陷阱之一);每店一值,进程内缓存。
"""

import logging
from functools import lru_cache

from api import _client

logger = logging.getLogger("api.settings")


@lru_cache(maxsize=128)
def _cached_partner_id(client_id: str, client_secret: str, proxy) -> str:
    _client.rate_acquire("settings.partnerprofile", client_id)
    token = _client.get_token(client_id, client_secret, proxy)
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/settings/partnerprofile",
        token, client_id, proxy, max_retries=3)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"partnerprofile 查询失败 HTTP {status}: {data}")
    # 实证结构(旧 auto_listing/store_info.py + 2026-08-09 生产响应):
    #   {"partner": {"partnerId": "...", "partnerDisplayName": "...",
    #                "partnerStoreId": "..."}, "configurations": [...]}
    # ⚠ 取 partnerId,**不是 partnerStoreId**——无实体仓的卖家用 Virtual Node,
    # 它等于 Partner ID;取错这个 ID 会让 MP_ITEM 的 fulfillmentCenterID 失效。
    pid = ((data.get("partner") or {}).get("partnerId")
           or data.get("partnerId")
           or (data.get("payload") or {}).get("partnerId") or "")
    if not pid:
        raise RuntimeError(f"partnerprofile 响应中无 partnerId: {str(data)[:200]}")
    return str(pid)


def get_partner_id(store: dict) -> str:
    """输入:店铺 → 输出:Partner ID(fulfillmentCenterID 用;进程内缓存)。"""
    return _cached_partner_id(store["client_id"], store["client_secret"],
                              store.get("proxy"))
