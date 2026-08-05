"""沃尔玛 Inventory 域接口(函数面定稿见 docs/api_blueprint.md §7)。

已实现端点(catalog_sync,蓝图矩阵 #21/#22):
  list_inventories()  GET /v3/inventories   全店库存分页(分页模型4)+ 单品兜底补漏内置
  get_inventory()     GET /v3/inventory?sku= 单品库存

put_inventory() 按蓝图 §7 预留,随 maintenance 工作流迁移时实现。

分页模型 4 的坑(蓝图 §4,历史 bug):终止只能看 nextCursor 是否为空,
**不能看页长**——某页可能 <limit 但仍有下页;单店 cursor 强制串行(2026-05-15 生产实证)。
"""

import logging

from api import _client

logger = logging.getLogger("api.inventory")


def _qty(entry: dict) -> int | None:
    """输入:inventories 单条记录 → 输出:可售数量(多 node 求和;单 node 取 amount)。"""
    nodes = entry.get("nodes")
    if isinstance(nodes, list) and nodes:
        total = 0
        for n in nodes:
            v = n.get("availToSellQty")
            if isinstance(v, dict):
                v = v.get("amount")
            total += int(v or 0)
        return total
    q = entry.get("quantity") or {}
    amt = q.get("amount")
    return int(amt) if amt is not None else None


def list_inventories(store: dict, expected_skus: set[str] | None = None) -> dict[str, int]:
    """输入:店铺(可选期望 SKU 集合)→ 输出:{sku: 可售数量}。

    GET /v3/inventories?limit=50 透明 cursor 串行翻页。传 expected_skus 时,
    bulk 漏掉的 SKU 走 get_inventory 单查兜底(真兜底:记日志计数,蓝图 #22 语义)。
    """
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    result: dict[str, int] = {}
    cursor = None
    while True:
        params: dict = {"limit": 50}
        if cursor:
            params["nextCursor"] = cursor
        _client.rate_acquire("inventory.list", store["client_id"])
        status, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/inventories",
            token, store["client_id"], store["proxy"], params=params, max_retries=3)
        if status in (401, 403):
            raise _client.StoreDeadError(store["name"], status)
        if status != 200:
            raise RuntimeError(f"GET /v3/inventories 返回 {status}(店铺 {store['name']}): {data}")
        elements = (data or {}).get("elements") or {}
        for entry in elements.get("inventories") or []:
            sku = entry.get("sku")
            if sku:
                qty = _qty(entry)
                if qty is not None:
                    result[sku] = qty
        cursor = ((data or {}).get("meta") or {}).get("nextCursor")
        if not cursor:      # 终止只看 cursor,绝不看页长(历史 bug)
            break

    if expected_skus:
        missing = sorted(expected_skus - result.keys())
        if missing:
            logger.warning("inventories bulk 漏 %d 个 SKU(店铺 %s),单查兜底",
                           len(missing), store["name"])
            for sku in missing:
                qty = get_inventory(store, sku)
                if qty is not None:
                    result[sku] = qty
    return result


def get_inventory(store: dict, sku: str) -> int | None:
    """输入:店铺 + SKU → 输出:可售数量;404/无记录返回 None。"""
    _client.rate_acquire("inventory.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/inventory",
        token, store["client_id"], store["proxy"], params={"sku": sku}, max_retries=3)
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GET /v3/inventory?sku={sku} 返回 {status}: {data}")
    q = (data or {}).get("quantity") or {}
    amt = q.get("amount")
    return int(amt) if amt is not None else None
