"""ping_stores — Phase 0 端到端验收:读凭证表 → 每店经代理调只读端点 → 汇总。

用法:python cli.py ping_stores [-p stores=A085,A086]
链路:飞书店铺凭证表(services.stores)→ api/_client 换 token(经该店固定代理)
     → GET /v3/token/detail(只读、无业务副作用)→ 汇总每店结果。
此工作流全部通过 = 地基验收(docs/plan.md Phase 0 最后一条)。
"""

import logging

from api import _client
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.ping_stores")


def _ping_one(store: dict) -> tuple[str, str]:
    """输入:店铺 dict → 输出:(店铺名, 'ok' 或错误描述)。"""
    name = store["name"]
    try:
        token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    except Exception as e:
        return name, f"换 token 失败: {e}"
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/token/detail",
        token, store["client_id"], store["proxy"], max_retries=2,
    )
    if status == 200:
        return name, "ok"
    return name, f"token/detail 返回 {status}" + (f" {data}" if data else "")


def run(params: dict) -> str:
    """输入:params 可选 stores="名1,名2"(缺省全部)→ 输出:每店连通结果汇总。"""
    names = None
    if params.get("stores"):
        names = [s.strip() for s in str(params["stores"]).split(",") if s.strip()]

    store_list = stores_svc.load_stores(names)
    if not store_list:
        return "无有效店铺(检查店铺凭证表登记与「启用」列)"

    ok, bad = [], []
    for s in store_list:
        name, result = _ping_one(s)
        (ok if result == "ok" else bad).append((name, result))
        logger.info("ping %s: %s", name, result)

    lines = [f"ping_stores:{len(ok)}/{len(store_list)} 家连通"]
    for name, result in bad:
        lines.append(f"  ✗ {name}: {result}")
    return "\n".join(lines)
