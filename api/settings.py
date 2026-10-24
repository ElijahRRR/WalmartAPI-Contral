"""沃尔玛 Settings 域接口(listing L2d;矩阵内端点)。

  get_partner_id()    GET /v3/settings/partnerprofile      → Partner ID
                      (无自建仓时的 Virtual Fulfillment Center ID)
  list_ship_nodes()   GET /v3/settings/shipping/shipnodes  → 该店的发货节点
  list_carrier_methods()  GET /v3/settings/shipping/carriers → 可用承运方法(建仓前校验)
  create_ship_node()  POST /v3/settings/shipping/shipnodes → 建仓(node_create 专用,
                      2026-10-24 所有者定稿;不重试,三种结局三种信号,见函数注)

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
    但同族端点两种都出现过)—— 两种都收,认不出返回空 dict,由调用方
    _cached_ship_nodes 抛错(不缓存)。
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
    # 文案里的「返回 NNN」/「返回 None」是 services/store_retry.diagnose 认的
    # 格式(沃尔玛NNN / 网络未达),改字样要同步那边
    if status != 200:
        raise RuntimeError(f"shipnodes 查询失败,沃尔玛返回 {status}: {str(data)[:200]}")
    nodes = _parse_nodes(data)
    if not nodes:
        # 200 但一个节点都没解析出来(空体 / 非 JSON / 形状变了):**抛而不是
        # 返回空**(2026-09-19)。返回空会被调用方判成「FC ID 不在列表(认识的:
        # (空))」—— 瞬时故障伪装成配置错,而且空结果进 lru 缓存,整个进程再也
        # 不重打接口。每家店的列表至少含 Virtual 节点(2026-08-30 谭总12 单店
        # 实证,docs/multi_node_plan.md §2.4),空列表不可能是合法 200
        raise RuntimeError("shipnodes 返回 200 但没解析出任何节点(响应形状变了?):"
                           + str(data)[:200])
    # 存 tuple、每次调用重建 dict:缓存的是**同一个对象**,直接返回 dict 的话
    # 任何调用方 pop 一下就改到了全进程的缓存里(get_partner_id 返回的是 str,
    # 不可变,没这个问题)
    return tuple(sorted(nodes.items(), key=lambda kv: kv[0]))


def list_ship_nodes(store: dict) -> dict[str, dict]:
    """输入:店铺 → 输出:{shipNode: 节点字段};进程内缓存(节点不会天天变)。"""
    return dict(_cached_ship_nodes(store["client_id"], store["client_secret"],
                                   store.get("proxy")))


# ── 建仓(2026-10-24 所有者定稿:建仓改由 `node_create` 工作流执行)──────────
# 此前蓝图把 POST shipnodes 标为「人工 runbook,不自动化」(multi_node_plan §8)。
# 所有者 2026-10-24 翻案:「建仓是在我有需要的时候给信息并手动执行创建的」——
# 仍是人触发、人给信息,只是从 Seller Center 点鼠标换成 cli 一条命令。
# 运费模板绑定与「维护仓库」切换**仍然人工**(两者是独立决策,见 multi_node_plan §7)。
#
# 官方(developer.walmart.com create-fulfillment-center,2026-10-24 核验):
#   body = {"shipNodeHeader": {"version": "1.2"}, "shipNode": [ {...} ]}
#   必填 shipNodeName / status / timeZone / postalAddress / 至少一种 shipping method;
#   响应 = 节点数组,带生成的 shipNode(17 位数字)与 nodeType=PHYSICAL。
#   原件存 refdata/specs/ship_node_create_1.2_sample.json(守门测试读它校键集合)。
# 节点元素怎么组是业务(services/ship_nodes.build_node),本层只负责把接口调对。

#: shipNodeHeader.version —— **唯一出生地**(官方样例 2026-10-24 核验值)
SHIP_NODE_SPEC_VERSION = "1.2"


class ShipNodeOutcomeUnknown(RuntimeError):
    """建仓 POST 发出去了但不知道到没到(网络中断 / 5xx):**既不算成功也不算未达**。

    调用方必须把台账行留在 pending、下次用 GET shipnodes 反查收编(写操作永不
    自动兜底;「不知道到没到」不许当失败去重发 —— 重发 = 重名建两个仓)。
    """


def _parse_rows(data, *keys: str) -> list[dict]:
    """输入:响应(裸数组或包在 payload/elements/… 里)→ 输出:字典行列表;认不出 []。"""
    rows = data
    if isinstance(data, dict):
        for key in keys:
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _error_text(data) -> str:
    """输入:沃尔玛错误响应体 → 输出:人读的一句(errors[].description 拼起来;认不出给原文片段)。

    首跑实见的形状:`{"errors":[{"code":"INVALID_REQUEST_CONTENT.INVALID_REQUEST","field":"shipNode",
    "description":"shipNode object has invalid data: distributorSupportedServices can't be null",…}]}`。
    """
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            parts = []
            for e in errs:
                if isinstance(e, dict):
                    desc = e.get("description") or e.get("info") or e.get("code") or ""
                    field = e.get("field")
                    parts.append(f"[{field}] {desc}" if field and desc else str(desc or e))
            if parts:
                return "; ".join(parts)[:400]
        if "text" in data and len(data) == 1:
            return str(data["text"])[:400]
    return str(data)[:300]


def list_carrier_methods(store: dict) -> list[dict]:
    """输入:店铺 → 输出:该店可用承运方法 [{carrierMethodName, carrierMethodType, …}]。

    GET /v3/settings/shipping/carriers(10/min)。**不缓存**:只在建仓前校验
    一次「填的承运商沃尔玛认不认」,不值得一个 lru。非 200 抛 RuntimeError
    (文案「沃尔玛返回 NNN」是 store_retry.diagnose 认的格式)。
    """
    _client.rate_acquire("settings.carriers", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store.get("proxy"))
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/settings/shipping/carriers",
        token, store["client_id"], store.get("proxy"), max_retries=3)
    if status != 200:
        raise RuntimeError(f"carriers 查询失败,沃尔玛返回 {status}: {str(data)[:200]}")
    rows = _parse_rows(data, "payload", "elements", "carrierMethods")
    if not rows:
        raise RuntimeError("carriers 返回 200 但没解析出任何承运方法(响应形状变了?):"
                           + str(data)[:200])
    return rows


def create_ship_node(store: dict, node: dict) -> dict:
    """输入:店铺 + 一个节点元素(官方 shipNode[] 元素形状)→ 输出:沃尔玛回的该节点(含 shipNode)。

    POST /v3/settings/shipping/shipnodes(10/min),**不自动重试**(非幂等:重发
    = 重名建两个仓)。三种结局分三种信号,调用方按它定台账:
      · 200 且解析出恰一个节点 → 返回它(成功);
      · 4xx(含 401/403/400)→ RuntimeError(**确认未达**,沃尔玛明确拒了);
      · status None / 5xx / 200 但解析不出 → ShipNodeOutcomeUnknown(不知道到没到)。
    成功后清掉 `list_ship_nodes` 的进程内缓存:同进程里紧接着的校验要看得见新仓。
    """
    _client.rate_acquire("settings.shipnodes.create", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store.get("proxy"))
    body = {"shipNodeHeader": {"version": SHIP_NODE_SPEC_VERSION},
            "shipNode": [node]}
    status, _, data = _client.safe_post_ex(
        f"{_client.base_url()}/v3/settings/shipping/shipnodes",
        token, store["client_id"], store.get("proxy"), json_body=body,
        max_retries=0, error_body=True)
    if status is None or status >= 500:
        raise ShipNodeOutcomeUnknown(
            f"建仓请求结局不明,沃尔玛返回 {status}: {str(data)[:200]}")
    if status != 200:
        raise RuntimeError(f"建仓被拒,沃尔玛返回 {status}: {_error_text(data)}")
    rows = _parse_rows(data, "payload", "elements", "shipNodes", "shipNode")
    created = [r for r in rows if r.get("shipNode")]
    if len(created) != 1:
        # 200 却读不出那一个节点:多半已经建成只是形状变了 —— 当「不明」留 pending,
        # 由反查按名字收编,绝不重发
        raise ShipNodeOutcomeUnknown(
            f"建仓返回 200 但没解析出恰一个节点({len(created)} 个):{str(data)[:200]}")
    _cached_ship_nodes.cache_clear()
    return created[0]
