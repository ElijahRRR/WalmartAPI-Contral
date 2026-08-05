"""services/stores.py 行为回归:过滤规则(沿用旧 load_stores)/ 代理 URL 编码 / 快照兜底。"""

import json

import pytest

from registry import paths, resources
from services import stores as stores_svc


@pytest.fixture(autouse=True)
def _tmp_root(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    yield


def _rec(**fields):
    f = resources.STORE_CREDENTIALS.fields
    mapping = {"store": f.store, "client_id": f.client_id, "client_secret": f.client_secret,
               "proxy_type": f.proxy_type, "host": f.proxy_host, "port": f.proxy_port,
               "user": f.proxy_user, "pw": f.proxy_pass, "enabled": f.enabled}
    return {"record_id": "r", "fields": {mapping[k]: v for k, v in fields.items()}}


def test_normalize_filters_and_proxy_url():
    records = [
        _rec(store="A1", client_id="cid1", client_secret="s1",
             proxy_type="socks5", host="1.2.3.4", port=1080, user="u@x", pw="p:w"),
        _rec(store="无凭证", client_id="0", client_secret="s",
             proxy_type="socks5", host="1.1.1.1", port=1080),
        _rec(store="无代理", client_id="cid2", client_secret="s2",
             proxy_type="0", host="0", port="0"),
        _rec(store="已停用", client_id="cid3", client_secret="s3",
             proxy_type="http", host="2.2.2.2", port=8080, enabled=False),
        # 文本字段可能以富文本段列表返回
        _rec(store=[{"text": "A2"}], client_id=[{"text": "cid4"}], client_secret="s4",
             proxy_type="http", host="3.3.3.3", port=8080.0),
    ]
    out = stores_svc._normalize(records)
    assert [s["name"] for s in out] == ["A1", "A2"]
    # 用户名/密码 URL 编码,特殊字符不破坏代理 URL
    assert out[0]["proxy"] == "socks5://u%40x:p%3Aw@1.2.3.4:1080"
    # 数字字段 8080.0 → 8080
    assert out[1]["proxy"] == "http://3.3.3.3:8080"


def test_snapshot_fallback_when_feishu_down(monkeypatch):
    snapshot = [{"name": "A1", "client_id": "c", "client_secret": "s",
                 "proxy": "socks5://h:1"}]
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    paths.stores_snapshot_file().write_text(json.dumps(snapshot), encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(stores_svc.feishu, "list_records", boom)
    assert stores_svc.load_stores() == snapshot
    assert stores_svc.load_stores(["不存在"]) == []


def test_no_snapshot_and_feishu_down_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(stores_svc.feishu, "list_records", boom)
    with pytest.raises(RuntimeError):
        stores_svc.load_stores()


def test_success_writes_snapshot(monkeypatch):
    records = [_rec(store="A1", client_id="cid", client_secret="s",
                    proxy_type="http", host="9.9.9.9", port=80)]
    monkeypatch.setattr(stores_svc.feishu, "list_records", lambda *a, **kw: records)
    out = stores_svc.load_stores()
    assert len(out) == 1
    saved = json.loads(paths.stores_snapshot_file().read_text(encoding="utf-8"))
    assert saved == out
    assert (paths.stores_snapshot_file().stat().st_mode & 0o777) == 0o600
