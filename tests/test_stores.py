"""services/stores.py 行为回归:过滤规则(沿用旧 load_stores)/ 代理 URL 编码 / 快照兜底。"""

import json
import logging

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


# ── 在营判据:三层之间的边界(所有者定稿 2026-08-22)────────────────────

def _cred(name, **f):
    from registry import resources
    fl = resources.STORE_CREDENTIALS.fields
    fields = {fl.store: name, fl.client_id: "cid", fl.client_secret: "sec",
              fl.proxy_type: "http", fl.proxy_host: "1.2.3.4",
              fl.proxy_port: "8080"}
    fields.update(f)
    return {"fields": fields}


def test_is_enabled_is_the_single_predicate():
    """「启用」的判定只留一处 —— 此前埋在 `_normalize` 的循环里,
    和 ClientId、代理两条过滤混成一体,没有函数单独回答得了"在不在营"。"""
    from registry import resources
    from services import stores as st
    fl = resources.STORE_CREDENTIALS.fields
    assert st.is_enabled({}) is True                      # 缺省视为启用(旧表无此列)
    assert st.is_enabled({fl.enabled: True}) is True
    assert st.is_enabled({fl.enabled: False}) is False
    for no in ("否", "false", "0", " 否 "):
        assert st.is_enabled({fl.enabled: no}) is False
    for yes in ("是", "true", "1"):
        assert st.is_enabled({fl.enabled: yes}) is True


def test_enabled_names_ignores_client_id_and_proxy(monkeypatch):
    """★ **在营 ≠ 能调 API。**

    「启用」是所有者的**意图**开关,ClientId/代理是**技术就绪**。合并的后果:
    「在营但代理没配」的店会被当成死店 —— 而死店名录直通整店下线。
    """
    from registry import resources
    from services import stores as st
    fl = resources.STORE_CREDENTIALS.fields
    recs = [_cred("A085朱丽霖"),
            _cred("A102无代理", **{fl.proxy_host: "0"}),        # 在营,只是没配代理
            _cred("A107无凭证", **{fl.client_id: "0"}),
            _cred("Z001已停用", **{fl.enabled: False})]
    monkeypatch.setattr("api.feishu.list_records", lambda *a, **k: recs)
    assert st.enabled_names() == {"A085朱丽霖", "A102无代理", "A107无凭证"}


def test_load_stores_is_strictly_narrower_than_enabled_names(monkeypatch):
    """三层是包含关系:能调 API ⊆ 在营 ⊆ 在册。任何一层拿去当另一层用都会出事。"""
    from registry import resources
    from services import stores as st
    fl = resources.STORE_CREDENTIALS.fields
    recs = [_cred("能调"), _cred("在营没代理", **{fl.proxy_host: "0"}),
            _cred("已停用", **{fl.enabled: False})]
    monkeypatch.setattr("api.feishu.list_records", lambda *a, **k: recs)
    monkeypatch.setattr(st, "_write_snapshot", lambda s: None)
    api_ok = {s["name"] for s in st.load_stores()}
    assert api_ok == {"能调"}
    assert api_ok < st.enabled_names() == {"能调", "在营没代理"}
    assert st.enabled_names() < st.registered_names()


def test_disabled_store_is_not_registered_away(monkeypatch):
    """停用**不等于**从凭证表删行 —— 历史凭证留着,`registered_names` 照样有它。
    所以判在营只能看 `enabled_names`,看 `registered_names` 会把停用店当在营。"""
    from registry import resources
    from services import stores as st
    fl = resources.STORE_CREDENTIALS.fields
    monkeypatch.setattr("api.feishu.list_records",
                        lambda *a, **k: [_cred("Z001", **{fl.enabled: False})])
    assert st.registered_names() == {"Z001"}
    assert st.enabled_names() == set()


# ── 兜底的触发面:只补外部世界的缺陷,不补自己的不确定(conventions §六)──

def test_local_parse_bug_does_not_masquerade_as_a_feishu_failure(monkeypatch):
    """_normalize 抛 = **本仓自己的 bug**,必须照抛,不许静默退陈旧快照。

    旧写法把解析与落盘一起包进 try:飞书完全健康、只是凭证表字段被改名或
    单元格形状变了,同样会退到陈旧凭证快照,还报成「店铺凭证表读取失败」
    —— 把本地 bug 伪装成远端故障,指错路。这条路直通整店下线判据。
    """
    snapshot = [{"name": "陈旧店", "client_id": "c", "client_secret": "s",
                 "proxy": "http://h:1"}]
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    paths.stores_snapshot_file().write_text(json.dumps(snapshot), encoding="utf-8")
    _live(monkeypatch, [_rec(store="A1", client_id="c1", client_secret="s",
                             proxy_type="http", host="1.1.1.1", port=80)])

    def boom(records):
        raise KeyError("凭证表字段被改名了")

    monkeypatch.setattr(stores_svc, "_normalize", boom)
    with pytest.raises(KeyError):
        stores_svc.load_stores()


def test_feishu_failure_still_falls_back_after_narrowing(monkeypatch):
    """收窄 try 之后,**飞书故障的兜底行为一字未变**(收窄的安全方向)。"""
    snapshot = [{"name": "A1", "client_id": "c", "client_secret": "s",
                 "proxy": "socks5://h:1"}]
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    paths.stores_snapshot_file().write_text(json.dumps(snapshot), encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(stores_svc.feishu, "list_records", boom)
    assert stores_svc.load_stores() == snapshot


def test_corrupt_snapshot_says_so_instead_of_looking_like_no_snapshot(
        monkeypatch, caplog):
    """快照文件损坏 ≠ 没有快照:不记日志的话两种截然不同的故障合并成一句
    「无本地快照可兜底」,人会去找一个其实就在那儿的文件。"""
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    paths.stores_snapshot_file().write_text("{ 这不是 json", encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(stores_svc.feishu, "list_records", boom)
    with caplog.at_level(logging.WARNING, logger="services.stores"):
        with pytest.raises(RuntimeError):
            stores_svc.load_stores()
    assert "店铺凭证快照损坏,按无快照处理" in caplog.text


def test_no_shadow_imports_of_feishu_left(monkeypatch):
    """函数内影子 import(`from api import feishu as _feishu  # 惰性避免循环`)
    在本文件已失效:第 13 行的模块级 import 早就把它拉进来了,什么循环也没避到。
    留着比没有更糟 —— 下一个人会照它去别处也写惰性 import。
    """
    import inspect
    for fn in (stores_svc.enabled_names, stores_svc.registered_names):
        assert "_feishu" not in inspect.getsource(fn), fn.__name__
    # 行为不变:两支都用模块级 feishu,现有 monkeypatch 写法照旧生效
    f = resources.STORE_CREDENTIALS.fields
    recs = [{"fields": {f.store: "A1"}}, {"fields": {f.store: "A2", f.enabled: False}}]
    monkeypatch.setattr(stores_svc.feishu, "list_records", lambda *a, **k: recs)
    assert stores_svc.enabled_names() == {"A1"}
    assert stores_svc.registered_names() == {"A1", "A2"}
