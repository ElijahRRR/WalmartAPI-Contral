"""沃尔玛 Settings 域接口(listing L2d;矩阵内端点)。

  get_partner_id()    GET /v3/settings/partnerprofile      → Partner ID
                      (无自建仓时的 Virtual Fulfillment Center ID)
  list_ship_nodes()   GET /v3/settings/shipping/shipnodes  → 该店的发货节点

用途:MP_ITEM 的 orderable.inventory[].fulfillmentCenterID 必填。官方原文:
**建了自建仓就填该仓的 shipNode**;"没建过 FC、走 Default Node (Virtual) 时
才用 Virtual Node ID(它等于 Partner ID)"。本仓此前恒填 partnerId ——
那是**退化写法**,对已建实体仓的店铺是错的(多仓改造背景见
`docs/multi_node_plan.md`)。两者都每店一值、进程内缓存。
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


# ── 发货节点(多仓批次 1)──────────────────────────────────────────────────
# 官方字段(2026-08-24 核验):`shipNode`(唯一标识,17-18 位数字)、
# `shipNodeName`、`status`(ACTIVE/INACTIVE)、`nodeType`(PHYSICAL/3PL)。
# ⚠ **Virtual Node 会不会出现在这个列表里,官方文档没有明文**
# (页面自述只覆盖 physical 与 3PL)。所以本函数的消费方**只用它做"填的这个
# FC ID 认不认识"的正向校验**,不能反过来拿"列表为空"推断"这店没有节点"
# —— 那正是按推断编码。校验口径见 services/store_limits.maint_nodes()。
def _parse_nodes(data) -> dict[str, dict]:
    """输入:shipnodes 响应 → 输出:{shipNode: 该节点原始字段}。

    响应可能是裸数组,也可能包在 payload/elements 里(官方样例给的是裸数组,
    但同族端点两种都出现过)—— 两种都收,认不出返回空 dict 并由调用方告警。
    """
    rows = data
    if isinstance(data, dict):
        for key in ("payload", "elements", "shipNodes"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return {}
    return {str(r["shipNode"]): r for r in rows
            if isinstance(r, dict) and r.get("shipNode")}


@lru_cache(maxsize=128)
def _cached_ship_nodes(client_id: str, client_secret: str, proxy) -> tuple:
    _client.rate_acquire("settings.shipnodes", client_id)
    token = _client.get_token(client_id, client_secret, proxy)
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/settings/shipping/shipnodes",
        token, client_id, proxy, max_retries=3)
    if status != 200:
        raise RuntimeError(f"shipnodes 查询失败 HTTP {status}: {str(data)[:200]}")
    nodes = _parse_nodes(data)
    if not nodes:
        logger.warning("shipnodes 响应里没解析出任何节点(响应形状变了?):%s",
                       str(data)[:200])
    # 存 tuple、每次调用重建 dict:缓存的是**同一个对象**,直接返回 dict 的话
    # 任何调用方 pop 一下就改到了全进程的缓存里(get_partner_id 返回的是 str,
    # 不可变,没这个问题)
    return tuple(sorted(nodes.items(), key=lambda kv: kv[0]))


def list_ship_nodes(store: dict) -> dict[str, dict]:
    """输入:店铺 → 输出:{shipNode: 节点字段};进程内缓存(节点不会天天变)。"""
    return dict(_cached_ship_nodes(store["client_id"], store["client_secret"],
                                   store.get("proxy")))
