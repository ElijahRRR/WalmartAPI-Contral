"""沃尔玛 Inventory 域接口(函数面定稿见 docs/api_blueprint.md §7)。

已实现端点(蓝图矩阵 #20/#21/#22):
  list_inventory_nodes() GET /v3/inventories      全店库存分页 + 单品兜底,**保留节点明细**
  list_inventories()     ↑ 的求和包装({sku: 合计}),现有消费方口径不变
  get_inventory()        GET /v3/inventories/{sku} 单品库存(**全节点合计**)
  put_inventory()        PUT /v3/inventory?sku=    单品改库存(同步快路径,maintenance 用)

⚠ **读侧口径统一为"全节点合计"**(2026-08-24,多仓批次 0)。此前单品兜底走
legacy `GET /v3/inventory?sku=`,该端点**不带 shipNode 时返回的是"默认节点"**
(官方原文)而不是合计 —— 于是 `catalog.walmart_items.avail_qty` 这一列里,
走 bulk 的行是合计、走兜底的行是默认节点值,**同列两种语义且无标记**,而走哪条
取决于沃尔玛分页有没有漏 SKU(不可预测)。当前单节点业态下两者恰好相等所以
无害,一旦有店真开了第二个仓就会静默漂。改用 `/v3/inventories/{sku}`
(与 bulk 同族,响应带 nodes)之后只剩一种语义。
⚠ 写侧仍是单仓(legacy PUT,无 shipNode)—— 那是批次 2 的事,见
`docs/multi_node_plan.md`。**读合计、写单仓**在多节点店的故障清单也在那里。

批量改库存走 feeds.submit_feed(feed_type="inventory");路由阈值由 services 层
显式 if 决定(蓝图取舍规则:能力不同的两个端点,严禁隐式降级)。

分页模型 4 的坑(蓝图 §4,历史 bug):终止只能看 nextCursor 是否为空,
**不能看页长**——某页可能 <limit 但仍有下页;单店 cursor 强制串行(2026-05-15 生产实证)。
"""

import logging

from api import _client

logger = logging.getLogger("api.inventory")


def _nodes(entry: dict, where: str = "") -> dict[str, int] | None:
    """输入:inventories 单条记录 → 输出:{shipNode: 可售数量};形状认不出返回 None。

    官方形状:`nodes[].{shipNode, availToSellQty:{unit,amount}}`(amount 也可能
    是裸值,两种都收)。**节点身份保留在键上**——上层要合计自己 sum,要探测
    多仓自己 len(),api 层不再替它们把节点抹平(多仓批次 0)。

    ⚠ **认不出的形状返回 None,绝不当 0**(本仓口诀:兜底把"没匹配上"变成一个
    合法结果,原本响亮失败的情况就变成静默走偏)。`nodes` 在而没有一个带
    `availToSellQty` 时正是这种情形:官方 `GET /v3/inventories/{sku}` 的文档
    样例给的是 PUT 风格的 `{shipNode, status}`(几乎肯定是文档错误,见
    docs/multi_node_plan.md §2.4)。真撞上那种响应,返回 0 会让全店库存被判成
    "线上是 0",维护链据此把 amz 库存整店重推一遍;返回 None 则该 SKU 落不进
    结果、`avail_qty` 走 COALESCE 保留上一轮值,且这里响亮告警。

    空串键 = **节点身份未知**(legacy 扁平 `quantity` 响应)。它照样算一个节点,
    多仓探测只关心"有几个数",不关心叫什么。
    """
    nodes = entry.get("nodes")
    if isinstance(nodes, list) and nodes:
        out: dict[str, int] = {}
        for i, n in enumerate(nodes):
            if not isinstance(n, dict) or "availToSellQty" not in n:
                continue
            v = n.get("availToSellQty")
            if isinstance(v, dict):
                v = v.get("amount")
            # 同一 shipNode 出现两次(官方未见,但键冲突会静默吃掉一个)→ 累加
            key = str(n.get("shipNode") or f"?{i}")
            out[key] = out.get(key, 0) + int(v or 0)
        if out:
            return out
        logger.warning("库存响应有 nodes 但无一带 availToSellQty%s,按'读不到'处理"
                       "(不当 0):%s", f"({where})" if where else "",
                       str(nodes)[:200])
        return None
    q = entry.get("quantity") or {}
    amt = q.get("amount")
    return {"": int(amt)} if amt is not None else None


def _qty(entry: dict, where: str = "") -> int | None:
    """输入:inventories 单条记录 → 输出:全节点可售合计;读不到返回 None。"""
    nodes = _nodes(entry, where)
    return sum(nodes.values()) if nodes else None


def list_inventories(store: dict, expected_skus: set[str] | None = None) -> dict[str, int]:
    """输入:店铺(可选期望 SKU 集合)→ 输出:{sku: 全节点可售合计}。

    `list_inventory_nodes` 的求和包装 —— 只要合计的消费方用这个,口径与改造前
    逐字节一致。要节点明细(多仓探测/受管仓取值)用那一个,**别两个都调**:
    一次调用就是一轮全店翻页。
    """
    return {sku: sum(nodes.values())
            for sku, nodes in list_inventory_nodes(store, expected_skus).items()}


def list_inventory_nodes(store: dict,
                         expected_skus: set[str] | None = None
                         ) -> dict[str, dict[str, int]]:
    """输入:店铺(可选期望 SKU 集合)→ 输出:{sku: {shipNode: 可售数量}}。

    GET /v3/inventories?limit=50 透明 cursor 串行翻页。传 expected_skus 时,
    bulk 漏掉的 SKU 走单品兜底(真兜底:记日志计数,蓝图 #22 语义);
    **兜底与主路径同族同口径**(见模块头注),不再混进"默认节点"语义。
    读不到的 SKU **不进结果**(不是 0),由上层 COALESCE 保留上一轮值。

    翻页中途 400 = 游标作废(生产实证 2026-08-30 谭总12:第 66 页代理断连,
    重试同一个 nextCursor 即 400 —— 透明游标一次性,断连那刻就死了),
    与 items 域同款自愈:**整轮重来一次**(result 从头攒,不能续接半截 ——
    游标死了没法从断点继续,续接旧 result 也没错但重扫会覆盖,索性清零重扫)。
    第一页就 400 不算游标问题,照旧抛错。
    """
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])

    def _sweep() -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
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
            if status == 404:   # 空库存店铺按零商品处理(与 items 的 404 语义对齐)
                logger.info("GET /v3/inventories 404(店铺 %s),按空处理", store["name"])
                return result
            if status == 400 and cursor:
                raise _CursorExpired()      # 游标作废,由外层整轮重来一次
            if status != 200:
                raise RuntimeError(f"GET /v3/inventories 返回 {status}(店铺 {store['name']}): {data}")
            elements = (data or {}).get("elements") or {}
            for entry in elements.get("inventories") or []:
                sku = entry.get("sku")
                if sku:
                    nodes = _nodes(entry, f"{store['name']} {sku} bulk")
                    if nodes is not None:
                        result[sku] = nodes
            cursor = ((data or {}).get("meta") or {}).get("nextCursor")
            if not cursor:  # 终止只看 cursor,绝不看页长(历史 bug)
                return result

    try:
        result = _sweep()
    except _CursorExpired:
        logger.info("GET /v3/inventories 游标作废(店铺 %s),重置整轮重试一次",
                    store["name"])
        result = _sweep()

    if expected_skus:
        missing = sorted(expected_skus - result.keys())
        if missing:
            logger.warning("inventories bulk 漏 %d 个 SKU(店铺 %s),单查兜底",
                           len(missing), store["name"])
            for sku in missing:
                nodes = _get_nodes_one(store, sku)
                if nodes is not None:
                    result[sku] = nodes
    return result


def _node_result(data, sku: str, ship_node: str) -> tuple[bool, str]:
    """输入:PUT /v3/inventories/{sku} 响应 + 目标节点 → 输出:(该节点是否成功, 原因)。

    ⚠ **部分成功语义**:HTTP 200 不代表写进去了。官方响应里每个
    `nodes[]` 各带自己的 `status`(失败项还带 `errors[]`),整单 200 而某个
    节点 failure 是正常返回。只看 HTTP 码的话,写失败会被当成写成功 ——
    随后 settle 判 ineffective、下一轮重发,而"为什么没生效"没有任何线索。

    认不出响应形状(字段改名/文档与实测不符)一律判**失败**:
    宁可多重发一轮,也不能把没写进去的当成写进去了。
    """
    nodes = ((data or {}).get("inventories") or data or {}).get("nodes") \
        if isinstance(data, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return False, f"响应里没有 nodes[](形状变了?):{str(data)[:200]}"
    for n in nodes:
        if not isinstance(n, dict):
            continue
        # 只认目标节点那一条:一次只写一个节点,别的节点的 status 与本次无关
        if str(n.get("shipNode") or "") not in (str(ship_node), ""):
            continue
        st = str(n.get("status") or "")
        if st.upper() in ("SUCCESS", "SUCCESSFUL", "OK"):
            return True, ""
        errs = n.get("errors") or n.get("error") or ""
        return False, f"节点 {ship_node} status={st or '(空)'}: {str(errs)[:200]}"
    return False, (f"响应 nodes[] 里没有目标节点 {ship_node}"
                   f"(有的是 {[n.get('shipNode') for n in nodes if isinstance(n, dict)]})")


def _put_inventory_node(store: dict, sku: str, qty: int,
                        ship_node: str) -> tuple[bool, str]:
    """输入:店铺 + SKU + 数量 + 发货节点 → 输出:(是否成功, 失败原因串)。

    `PUT /v3/inventories/{sku}`:shipNode 在 **body** 里(不是 query),数量字段
    名是 **`inputQty`**(读侧是 `availToSellQty`、feed 侧是 `quantity` ——
    三套名字并存,序列化器不能共用,见 docs/multi_node_plan.md §2.1)。
    """
    _client.rate_acquire("inventory.put_node", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    body = {"inventories": {"nodes": [
        {"shipNode": str(ship_node),
         "inputQty": {"unit": "EACH", "amount": int(qty)}}]}}
    status, _, data = _client.safe_put_ex(
        f"{_client.base_url()}/v3/inventories/{sku}", token,
        store["client_id"], store["proxy"], json_body=body, timeout=60)
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    if status != 200:
        why = f"HTTP {status}" + (f": {str(data)[:200]}" if data else "")
        logger.warning("PUT /v3/inventories/%s 失败(%s 节点 %s → %s): %s",
                       sku, store["name"], ship_node, qty, why)
        return False, why
    ok, why = _node_result(data, sku, ship_node)
    if not ok:
        logger.warning("PUT /v3/inventories/%s 整单 200 但节点未生效"
                       "(%s 节点 %s → %s): %s",
                       sku, store["name"], ship_node, qty, why)
    return ok, why


def put_inventory(store: dict, sku: str, qty: int,
                  ship_node: str | None = None) -> tuple[bool, str]:
    """输入:店铺 + SKU + 新可售数量(+ 发货节点)→ 输出:(是否成功, 失败原因串)。

    同步接口,结果当场已知,不产生 feed_id 不进 feed 台账。
    401/403 抛 StoreDeadError。

    带 `ship_node` 走分节点端点(受管仓的店);不带走 legacy
    `PUT /v3/inventory`(未配置「维护仓库」的店,逐字节维持现状)。
    ⚠ 两条路是**显式路由**,不是"试 A 失败落 B":写操作永不自动兜底
    (换方法重试 = 重复提交制造机,CLAUDE.md 口诀)。
    """
    if ship_node:
        return _put_inventory_node(store, sku, qty, ship_node)
    _client.rate_acquire("inventory.put", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    body = {"sku": str(sku), "quantity": {"unit": "EACH", "amount": int(qty)}}
    status, _, data = _client.safe_put_ex(
        f"{_client.base_url()}/v3/inventory", token, store["client_id"],
        store["proxy"], json_body=body, params={"sku": str(sku)}, timeout=60)
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    if status == 200:
        return True, ""
    why = f"HTTP {status}" + (f": {str(data)[:200]}" if data else "")
    logger.warning("PUT /v3/inventory 失败(%s %s → %s): %s",
                   store["name"], sku, qty, why)
    return False, why


class _CursorExpired(Exception):
    """翻页中途游标作废(断连后重试同游标 400)—— 只在本模块内触发整轮重试。"""


def _get_nodes_one(store: dict, sku: str) -> dict[str, int] | None:
    """输入:店铺 + SKU → 输出:{shipNode: 可售数量};404/读不到返回 None。

    `GET /v3/inventories/{sku}`(与 bulk 同族,响应带 nodes)。**不是** legacy 的
    `GET /v3/inventory?sku=` —— 换端点的理由见模块头注(那条不带 shipNode 时
    返回"默认节点"而非合计,与 bulk 不同语义)。官方配额同为 200/min,
    沿用 `inventory.get` 令牌桶。
    """
    _client.rate_acquire("inventory.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/inventories/{sku}",
        token, store["client_id"], store["proxy"], max_retries=3)
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GET /v3/inventories/{sku} 返回 {status}: {data}")
    return _nodes(data or {}, f"{store['name']} {sku} 单品兜底")


def get_inventory(store: dict, sku: str) -> int | None:
    """输入:店铺 + SKU → 输出:全节点可售合计;404/读不到返回 None。"""
    nodes = _get_nodes_one(store, sku)
    return sum(nodes.values()) if nodes else None
