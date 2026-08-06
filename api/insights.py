"""沃尔玛 Insights 域接口(daily_report 用,蓝图矩阵 #28/#29)。

  performance_summary(store, metric) -> 比率 float | None(204=该店无合规数据,正常语义)
  performance_report(store, metric)  -> xlsx bytes | None(204=无数据)

8 项指标(旧系统在用顺序):otd/cancellations/vtr/srr/refunds/negativeFeedback/
returns/itemNotReceived。官方限速 1/min/端点,summary 与 report 独立计;
同店 8 端点可并发(桶互相独立)。
⚠ refunds 系列官方已标 Deprecated(被 returns 取代),旧系统两个都拉
(refunds→退款率列、returns→退货率列,维度不同),迁移期照搬,替代方案另议。
"""

import logging

from api import _client

logger = logging.getLogger("api.insights")

METRICS = ("otd", "cancellations", "vtr", "srr", "refunds",
           "negativeFeedback", "returns", "itemNotReceived")

# 比率字段提取优先级(旧系统实测):sellerAccountableRate > cumulativeRate > overallRate
_RATE_KEYS = ("sellerAccountableRate", "cumulativeRate", "overallRate")


def _find_key(node, key: str):
    """输入:任意 JSON 树 + 键名 → 输出:深度优先找到的第一个值(找不到 None)。

    insights 各端点响应嵌套结构不一(官方无统一 schema),按键名递归搜索兜住。
    """
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def performance_summary(store: dict, metric: str) -> float | None:
    """输入:店铺 + 指标名 → 输出:比率(round 2)或 None(204 无数据)。"""
    if metric not in METRICS:
        raise ValueError(f"未知 insights 指标: {metric}(可选 {METRICS})")
    _client.rate_acquire(f"insights.summary.{metric}", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/insights/performance/{metric}/summary",
        token, store["client_id"], store["proxy"], max_retries=3)
    if status == 204:       # 该店无合规数据,不是错误(旧系统实证)
        return None
    if status != 200:
        raise RuntimeError(f"insights {metric}/summary 返回 {status}(店铺 {store['name']})")
    for key in _RATE_KEYS:
        val = _find_key(data, key)
        if val is not None:
            try:
                return round(float(val), 2)
            except (TypeError, ValueError):
                continue
    logger.warning("insights %s/summary 未找到比率字段(店铺 %s),响应顶层键:%s",
                   metric, store["name"], sorted(data.keys()) if isinstance(data, dict) else type(data))
    return None


def performance_report(store: dict, metric: str) -> bytes | None:
    """输入:店铺 + 指标名 → 输出:问题订单明细 xlsx 字节;204/无内容返回 None。"""
    if metric not in METRICS:
        raise ValueError(f"未知 insights 指标: {metric}(可选 {METRICS})")
    _client.rate_acquire(f"insights.report.{metric}", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    # 2026-08-06 生产实证:默认 Accept(application/json)返回 406,
    # 二进制报表端点必须 octet-stream(与 reconFile 同一坑)。
    # 重试 6 次(1/2/4/8/10/10s):该端点现场生成 xlsx,是全 API 最脆的一族,
    # 多店并发时沃尔玛边缘面状 520 抖动实证(2026-08-06 全店首跑),
    # 默认 3 连快退避会整段打在同一个抖动窗口里
    status, _, blob = _client.safe_get_raw(
        f"{_client.base_url()}/v3/insights/performance/{metric}/report",
        token, store["client_id"], store["proxy"], timeout=90,
        max_retries=6, accept="application/octet-stream")
    if status == 204 or not blob:
        return None
    if status != 200:
        raise RuntimeError(f"insights {metric}/report 返回 {status}(店铺 {store['name']})")
    return blob
