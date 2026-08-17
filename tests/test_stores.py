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
    # 快照兜底时手上只有过滤后的名单,分不出"不在册"还是"未启用/缺代理"——
    # 那就明说分不出来,不硬猜(此前这里返回 [] 静默少跑)
    with pytest.raises(ValueError) as ei:
        stores_svc.load_stores(["不存在"])
    assert "快照兜底" in str(ei.value)


def test_no_snapshot_and_feishu_down_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(stores_svc.feishu, "list_records", boom)
    with pytest.raises(RuntimeError):
        stores_svc.load_stores()


def _live(monkeypatch, records):
    monkeypatch.setattr(stores_svc.feishu, "list_records", lambda *a, **kw: records)


def test_filter_names_all_hit_keeps_order(monkeypatch):
    """全部命中:按**给定顺序**返回,不按表里的顺序。"""
    _live(monkeypatch, [
        _rec(store="A1", client_id="c1", client_secret="s", proxy_type="http", host="1.1.1.1", port=80),
        _rec(store="A2", client_id="c2", client_secret="s", proxy_type="http", host="2.2.2.2", port=80),
        _rec(store="A3", client_id="c3", client_secret="s", proxy_type="http", host="3.3.3.3", port=80),
    ])
    assert [s["name"] for s in stores_svc.load_stores(["A3", "A1"])] == ["A3", "A1"]
    # 重复给同一个名字不该跑两遍
    assert [s["name"] for s in stores_svc.load_stores(["A1", "A1"])] == ["A1"]


def test_filter_names_typo_raises_not_silently_skipped(monkeypatch):
    """三个名字打错一个 ⇒ 抛错。

    此前是静默只跑两家、摘要报「2/2 家连通」满绿——人以为三家都验过了。
    """
    _live(monkeypatch, [
        _rec(store="A1", client_id="c1", client_secret="s", proxy_type="http", host="1.1.1.1", port=80),
        _rec(store="A2", client_id="c2", client_secret="s", proxy_type="http", host="2.2.2.2", port=80),
    ])
    with pytest.raises(ValueError) as ei:
        stores_svc.load_stores(["A1", "A2", "A9typo"])
    msg = str(ei.value)
    assert "A9typo" in msg and "查无此店" in msg
    assert "A1, A2" in msg          # 报错里带可用店铺清单,人能当场看出打错在哪


def test_filter_names_disabled_store_says_so(monkeypatch):
    """在册但被过滤掉的店,**不能**报成「查无此店」。

    把「没配代理」说成「不存在」会让人去凭证表里找一个明明在那儿的店;
    alloc_audit 的 docstring 专门警告过这一处混淆。
    """
    _live(monkeypatch, [
        _rec(store="A1", client_id="c1", client_secret="s", proxy_type="http", host="1.1.1.1", port=80),
        _rec(store="停用店", client_id="c2", client_secret="s",
             proxy_type="http", host="2.2.2.2", port=80, enabled=False),
        _rec(store="裸连店", client_id="c3", client_secret="s",
             proxy_type="0", host="0", port="0"),
    ])
    for name in ("停用店", "裸连店"):
        with pytest.raises(ValueError) as ei:
            stores_svc.load_stores([name])
        assert "在凭证表里,但被过滤掉了" in str(ei.value)
        assert "查无此店" not in str(ei.value)


def test_success_writes_snapshot(monkeypatch):
    records = [_rec(store="A1", client_id="cid", client_secret="s",
                    proxy_type="http", host="9.9.9.9", port=80)]
    monkeypatch.setattr(stores_svc.feishu, "list_records", lambda *a, **kw: records)
    out = stores_svc.load_stores()
    assert len(out) == 1
    saved = json.loads(paths.stores_snapshot_file().read_text(encoding="utf-8"))
    assert saved == out
    assert (paths.stores_snapshot_file().stat().st_mode & 0o777) == 0o600


def test_cross_store_concurrency_has_one_source():
    """跨店并发只准有一个出处 —— 六处各写一份正是它漂掉的原因。

    所有者 2026-08-16 推翻了旧 README 的「店铺级并发不要调高」,但那次只改了
    daily_report(6→16),perf_problems/settlement_sync 连**被推翻的注释**都
    原样留着 —— 同一条判断改一处漏两处,不报错,只是那两条链一直慢着。
    """
    import inspect

    from services import stores as ss
    assert ss.STORE_WORKERS == 24

    from workflows import (catalog_sync, daily_report, order_sync,
                           perf_problems, returns_sync, settlement_sync)
    for mod in (perf_problems, settlement_sync, daily_report):
        assert mod._STORE_WORKERS == ss.STORE_WORKERS, mod.__name__
    # 走 -p workers= 的三条:默认值必须取自同一个常量,不许写字面量
    for mod in (catalog_sync, order_sync, returns_sync):
        src = inspect.getsource(mod.run)
        assert "stores_svc.STORE_WORKERS" in src, mod.__name__

    # 被推翻的那句话不许再留在代码里(留着会让下一个人以为 6 是有依据的)
    for mod in (perf_problems, settlement_sync):
        text = inspect.getsource(mod)
        assert "店铺级并发不要调高" not in text, mod.__name__
