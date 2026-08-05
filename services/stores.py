"""店铺凭证积木:从飞书店铺凭证表读取 → 标准店铺列表;飞书故障时用本地快照兜底。

替代旧仓库 walmart_client.load_stores()(读 店铺API.xlsx);过滤规则与输出形状
与旧版完全一致,便于后续 workflow 迁移时直接替换 import。
"""

import json
import logging
import os
from urllib.parse import quote

from api import feishu
from registry import paths, resources

logger = logging.getLogger("services.stores")


def load_stores(filter_names: list[str] | None = None) -> list[dict]:
    """输入:可选店铺名列表(None=全部)→ 输出:有效店铺 list[dict](name/client_id/client_secret/proxy)。

    数据源:飞书店铺凭证表(registry.STORE_CREDENTIALS)。
    成功读取后写本地快照(<DATA_ROOT>/cache/stores_snapshot.json,chmod 600);
    飞书不可用时回退读最近一次快照并告警——保证飞书故障不瘫痪全部工作流。

    过滤规则(沿用旧系统):
      1. ClientId 为空或 "0" → 跳过
      2. 无代理(代理类型/IP/端口 任一为空或 "0")→ 跳过(严禁直连,店铺关联风险)
      3. 「启用」字段显式为否 → 跳过(旧 xlsx 无此列,新表新增;缺省视为启用)
    """
    try:
        records = feishu.list_records(resources.STORE_CREDENTIALS)
        stores = _normalize(records)
        _write_snapshot(stores)
    except Exception as e:
        stores = _read_snapshot()
        if stores is None:
            raise RuntimeError(f"店铺凭证表读取失败且无本地快照可兜底: {e}") from e
        logger.warning("店铺凭证表读取失败,已回退本地快照(%d 家): %s", len(stores), e)

    if filter_names:
        stores = [s for s in stores if s["name"] in filter_names]
    return stores


def _cell(fields: dict, key: str, default: str = "") -> str:
    """多维表格单元格 → 纯文本:文本字段可能返回 [{'text':...}] 段列表,数字字段返回 int/float。"""
    v = fields.get(key, default)
    if isinstance(v, list):
        v = "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip()


def _normalize(records: list[dict]) -> list[dict]:
    f = resources.STORE_CREDENTIALS.fields
    stores = []
    for rec in records:
        fields = rec["fields"]

        enabled = fields.get(f.enabled)
        if enabled is False or (isinstance(enabled, str) and enabled.strip() in ("否", "false", "0")):
            continue

        name = _cell(fields, f.store)
        client_id = _cell(fields, f.client_id, "0")
        client_secret = _cell(fields, f.client_secret, "0")
        if not client_id or client_id == "0":
            continue

        proxy_type = _cell(fields, f.proxy_type, "0")
        ip = _cell(fields, f.proxy_host, "0")
        port = _cell(fields, f.proxy_port, "0")
        username = _cell(fields, f.proxy_user)
        password = _cell(fields, f.proxy_pass)
        if not proxy_type or proxy_type == "0" or not ip or ip == "0" or not port or port == "0":
            continue

        proto = proxy_type if proxy_type in ("socks5", "http", "https") else "http"
        if username and password:
            # URL 编码用户名/密码,防止特殊字符(@:/#)破坏代理 URL
            u = quote(username, safe="")
            p = quote(password, safe="")
            proxy_url = f"{proto}://{u}:{p}@{ip}:{port}"
        else:
            proxy_url = f"{proto}://{ip}:{port}"

        stores.append({
            "name": name,
            "client_id": client_id,
            "client_secret": client_secret,
            "proxy": proxy_url,
        })
    return stores


def _write_snapshot(stores: list[dict]) -> None:
    """快照含密钥:写完立即 chmod 600;写失败只告警,不影响主流程。"""
    try:
        path = paths.stores_snapshot_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stores, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
    except Exception as e:
        logger.warning("店铺凭证快照写入失败:%s", e)


def _read_snapshot() -> list[dict] | None:
    path = paths.stores_snapshot_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
