"""api/feishu.py 行为回归:token 缓存与失效重试 / 瞬时退避 / 批量切块 / 分页 / 错误模型。"""

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from api import feishu
from registry.resources import Bitable, Spreadsheet

TABLE = Bitable(name="测试表", app_token="appX", table_id="tblY",
                fields=SimpleNamespace(a="字段A"))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    feishu._token_cache.clear()
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 退避不真等
    yield
    if feishu._client is not None:
        feishu._client.close()
    feishu._client = None
    feishu._token_cache.clear()


def _use_handler(handler):
    feishu._client = httpx.Client(transport=httpx.MockTransport(handler))


def _token_ok(n=1):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": f"tt{n}",
                                     "expire": 7200})


def test_transient_code_retries_then_succeeds():
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(200, json={"code": 90235, "msg": "data not ready"})
        return httpx.Response(200, json={"code": 0, "data": {
            "records": [{"record_id": "rec1"}]}})

    _use_handler(handler)
    ids = feishu.batch_create(TABLE, [{"字段A": 1}])
    assert ids == ["rec1"]
    assert seq["n"] == 2


def test_transient_text_fallback_track():
    # int code 不在瞬时集合,但错误文本命中子串轨 → 仍应重试
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(200, json={"code": 424242, "msg": "Request Timeout"})
        return httpx.Response(200, json={"code": 0, "data": {"records": []}})

    _use_handler(handler)
    feishu.batch_update(TABLE, [{"record_id": "r", "fields": {"字段A": 2}}])
    assert seq["n"] == 2


def test_token_invalid_refreshes_and_retries():
    state = {"token": 0, "biz": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            state["token"] += 1
            return _token_ok(state["token"])
        state["biz"] += 1
        if request.headers["Authorization"] == "Bearer tt1":
            return httpx.Response(200, json={"code": 99991663, "msg": "token invalid"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    _use_handler(handler)
    assert feishu.list_records(TABLE) == []
    assert state["token"] == 2 and state["biz"] == 2


def test_non_transient_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        return httpx.Response(200, json={"code": 1254045, "msg": "FieldNameNotFound"})

    _use_handler(handler)
    with pytest.raises(feishu.FeishuError) as ei:
        feishu.list_records(TABLE)
    assert ei.value.code == 1254045


def test_batch_create_chunks_at_registry_limit():
    """切块取限额登记表 _BITABLE_BATCH_CREATE_MAX(官方 1000 条 ×95% = 950),
    不是随手写的 500。"""
    batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        import json
        records = json.loads(request.content)["records"]
        batches.append(len(records))
        return httpx.Response(200, json={"code": 0, "data": {
            "records": [{"record_id": f"r{i}"} for i in range(len(records))]}})

    _use_handler(handler)
    ids = feishu.batch_create(TABLE, [{"字段A": i} for i in range(1200)])
    assert batches == [feishu._BITABLE_BATCH_CREATE_MAX,
                       1200 - feishu._BITABLE_BATCH_CREATE_MAX] == [950, 250]
    assert len(ids) == 1200


def test_list_records_paginates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        calls.append(dict(request.url.params))
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"code": 0, "data": {
                "items": [{"record_id": "r1", "fields": {"字段A": 1}}],
                "has_more": True, "page_token": "PT2"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "items": [{"record_id": "r2", "fields": {"字段A": 2}}],
            "has_more": False}})

    _use_handler(handler)
    recs = feishu.list_records(TABLE)
    assert [r["record_id"] for r in recs] == ["r1", "r2"]
    assert calls[1]["page_token"] == "PT2"


def test_http_client_ignores_env_proxy(monkeypatch):
    # 生产 Mac 挂着 Clash:HTTP(S)_PROXY 环境变量绝不能劫持飞书请求
    # (旧系统 2026-05-07 事故:本机代理拒连 → 14,610 个单元格写回失败)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    feishu._client = None  # 强制走 _http() 真实构造路径
    client = feishu._http()
    assert client.trust_env is False
    assert client._mounts == {}  # 无任何代理挂载


def test_unregistered_table_rejected():
    empty = Bitable(name="未登记", app_token="", table_id="",
                    fields=SimpleNamespace())
    with pytest.raises(LookupError):
        feishu.list_records(empty)



def test_sheet_write_ranges_splits_big_range_and_scrubs(monkeypatch):
    """单个范围裹上千行会被飞书 90202 拒(所有者 2026-08-09 实遇);按行切。"""
    sent = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    sheet = Spreadsheet(name="X", token="TOK", sheet_id="SID",
                        columns=("a", "b"))
    monkeypatch.setattr(feishu, "_SHEET_WRITE_MAX_ROWS", 2)
    rows = [["v%d" % i, "x\x00y"] for i in range(5)]
    n = feishu.sheet_write_ranges(sheet, [("A11:B15", rows)])
    ranges = [vr["range"] for body in sent for vr in body["valueRanges"]]
    assert ranges == ["SID!A11:B12", "SID!A13:B14", "SID!A15:B15"]
    assert n == 5          # 行数,不是范围数(所有调用方都当行数在用)
    # 控制字符会让飞书整批拒收,先剔掉(剔到了记日志,不静默)
    assert sent[0]["valueRanges"][0]["values"][0][1] == "xy"


def test_consecutive_single_row_ranges_are_coalesced(monkeypatch):
    """⚠ 一行一个 range = 100 行/请求 + 0.3s 节流,几万行要跑好几分钟。

    所有者 2026-08-16 实遇并发问:「写飞书的速度怎么各处都不一样,有的 4000
    有的几百几十,甚至有的逐行」。差别不在接口,在调用方给的形状:
    整表重写给的是一段 4000 行,定点回写给的是一行一段。**在 api 层补齐**,
    调用方不用改。
    """
    sent = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    sheet = Spreadsheet(name="X", token="TOK", sheet_id="SID",
                        columns=("a", "b"))
    ups = [(f"C{r}:G{r}", [[f"v{r}"] * 5]) for r in range(2, 1002)]
    n = feishu.sheet_write_ranges(sheet, ups)
    ranges = [vr["range"] for body in sent for vr in body["valueRanges"]]
    assert ranges == ["SID!C2:G1001"]      # 1000 行 → 一个请求一个范围
    assert n == 1000                       # 行数照旧
    assert len(sent) == 1
    # 值的顺序不能乱:第 k 行还是第 k 行
    vals = sent[0]["valueRanges"][0]["values"]
    assert vals[0][0] == "v2" and vals[-1][0] == "v1001"


def test_coalesce_never_merges_across_a_gap_or_a_column_change():
    """粘段只认**紧邻的下一行 + 完全相同的列区间** —— 别的一律另起一段。

    跨空行粘 = 把中间那些行一起覆盖成空(它们不在本次回写范围里,
    是别人的数据);跨列粘 = 写到隔壁列去。两者都不报错。
    """
    c = feishu._coalesce
    # 断号:2,3 粘;跳过 4;5 单独
    assert [r for r, _ in c([("C2:G2", [[1]]), ("C3:G3", [[2]]),
                             ("C5:G5", [[3]])])] == ["C2:G3", "C5:G5"]
    # 换列:同一行号连着也不粘
    assert [r for r, _ in c([("C2:G2", [[1]]), ("O3:Q3", [[2]])])] \
        == ["C2:G2", "O3:Q3"]
    # 倒序不粘(不排序:同一行被写两次时,先后覆盖语义必须与逐行写一致)
    assert [r for r, _ in c([("C3:G3", [[1]]), ("C2:G2", [[2]])])] \
        == ["C3:G3", "C2:G2"]
    # 同一行写两次也不粘,顺序原样保留(后写的仍然后写)
    assert [(r, v) for r, v in c([("C2:G2", [["旧"]]), ("C2:G2", [["新"]])])] \
        == [("C2:G2", [["旧"]]), ("C2:G2", [["新"]])]
    # 多行段接单行段照样粘
    assert [r for r, _ in c([("C2:G3", [[1], [2]]), ("C4:G4", [[3]])])] \
        == ["C2:G4"]
    # 形状看不懂(行数与范围对不上)就原样放过,不猜
    assert [r for r, _ in c([("C2:G9", [[1]]), ("C3:G3", [[2]])])] \
        == ["C2:G9", "C3:G3"]


def test_sheet_writes_serialize_per_spreadsheet(monkeypatch):
    """同一张电子表格的写**互斥**;不同表格互不阻塞。

    起因:跨店线程池(services.stores.STORE_WORKERS=24)让 24 个线程各自进
    sheet_* 写函数,而那些函数压 QPS 靠的是**调用内部**的
    `time.sleep(_SHEET_WRITE_THROTTLE_SECS)` —— 每个线程都以为自己是唯一写者,
    节流被整体绕过,飞书那边看到的是 24 倍瞬时写入。
    锁必须是**按表**的,不是全局一把:否则上架表的回写会挡住黑名单表的重写,
    把并发省下来的时间又赔回去。

    ⚠ 用 Barrier 判"同时在飞",不用 sleep 掐秒表:本文件的 _clean_state 把
    `time.sleep` 打成空操作(退避不真等),掐秒表在这里恒等于 0;而且时间断言
    在慢机器上本来就会抖。会合成功 = 真的同时有 N 个线程在写,是**确定性**的。
    """
    import threading

    def _make(barrier, met):
        def _call(method, path, **kw):
            try:
                barrier.wait()
                met.append(1)          # 会合上了 = 同时在飞
            except threading.BrokenBarrierError:
                pass                   # 超时 = 没人陪它,即被锁挡住了
            return {}
        return _call

    # ① 同一张表:8 个线程抢写,任意两个都不该同时在 _call 里
    met_same: list = []
    monkeypatch.setattr(feishu, "_call",
                        _make(threading.Barrier(2, timeout=0.15), met_same))
    same = Spreadsheet(name="X", token="TOK", sheet_id="SID", columns=("a",))
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda r: feishu.sheet_write_ranges(
            same, [(f"A{r}:A{r}", [["v"]])]), range(2, 10)))
    assert met_same == [], "同一张表的写没串起来"

    # ② 四张不同的表:必须四个一起在飞,否则就是锁做粗了
    met_diff: list = []
    monkeypatch.setattr(feishu, "_call",
                        _make(threading.Barrier(4, timeout=5), met_diff))
    others = [Spreadsheet(name=f"S{i}", token=f"TOK{i}", sheet_id="SID",
                          columns=("a",)) for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda s: feishu.sheet_write_ranges(s, [("A2:A2", [["v"]])]),
                    others))
    assert len(met_diff) == 4, f"不同表格之间互相挡住了({len(met_diff)}/4 会合)"


# ── 电子表格写锁的取法(2026-08-17 实验反证)────────────────────────────────

def test_sheet_lock_is_not_a_defaultdict_of_rlock():
    """⚠ `defaultdict(threading.RLock)` 会给同一个 key 发出**两把不同的锁**。

    `threading.RLock` 是 **Python 函数**(threading.py 里挑 _CRLock/_PyRLock 的
    那个包装),所以 `defaultdict.__missing__` 调它时会执行 Python 字节码 ⇒
    eval breaker 可能触发 ⇒ 线程切换 ⇒ 两个线程各造一把,只有一把落进字典,
    **落空的那个线程拿着一把没人认的锁**。实测 64 线程同时首次取同一 key,
    拿到 2 个不同的锁对象。

    而这正是这把锁存在的那个场景(24 个店铺线程同时第一次写同一张表),
    故障表现恰好就是它要防的那件事:一阵 90227 限流。

    `dict.setdefault` 是 C 实现、原子 —— 多造出来的 RLock 直接被 GC。
    ⚠ 对照:`_table_locks` 用的 `threading.Lock` 是 `_thread.allocate_lock`
    (**C 工厂**,不执行 Python 字节码),那处 defaultdict 是安全的,别一起改。
    """
    import inspect
    import threading
    from collections import defaultdict
    # 前提复核:RLock 是 Python 函数、Lock 不是。哪天 CPython 把 RLock 换成
    # 纯 C 工厂,这条会红 —— 那时这个用例的理由消失了,可以连同 _sheet_lock 一起简化
    assert inspect.isfunction(threading.RLock)
    assert not inspect.isfunction(threading.Lock)

    assert isinstance(feishu._sheet_locks, dict)
    assert not isinstance(feishu._sheet_locks, defaultdict)
    src = inspect.getsource(feishu._sheet_lock)
    assert "setdefault" in src
    # 同一 token 恒为同一把;不同 token 各自一把
    a = feishu._sheet_lock("tok-A")
    assert feishu._sheet_lock("tok-A") is a
    assert feishu._sheet_lock("tok-B") is not a
    # 必须可重入:sheet_overwrite 内部还会调 sheet_ensure_rows(同一把锁)
    with a:
        with a:
            pass


def test_every_sheet_write_goes_through_the_lock():
    """四个写函数都得裹锁 —— 漏一个就是那张表的节流被跨店线程池整体绕过。"""
    import inspect
    for fn in (feishu.sheet_ensure_rows, feishu.sheet_write_ranges,
               feishu.sheet_overwrite, feishu.sheet_set_formatter):
        src = inspect.getsource(fn)
        assert "_sheet_lock(" in src, fn.__name__
        # 别退回下标取法(那就又是 defaultdict 的坑)
        assert "_sheet_locks[" not in src, fn.__name__


def test_write_ranges_caps_rows_per_request(monkeypatch):
    """⚠ 2026-08-18 生产事故:audit_sheet 回填 28,498 行 × C:G,粘段后
    8 个 4000 行段被塞进**同一个** values_batch_update,飞书 90227
    (request too large)整批拒收。

    每请求的行预算 = _SHEET_WRITE_MAX_ROWS(官方 5000 行 ×95% = 4750;
    事故当时是旧值 4000)。这里把行预算缩小到 4 行来演同一形状:
    18 个连号单行段 → 粘成一段 → 切成 5 个 ≤4 行的子段 → 必须发 5 个请求,
    每个请求 ≤4 行,而不是 5 段合一个请求。
    """
    sent = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    monkeypatch.setattr(feishu, "_SHEET_WRITE_MAX_ROWS", 4)
    monkeypatch.setattr(feishu, "_SHEET_WRITE_THROTTLE_SECS", 0)
    sheet = Spreadsheet(name="X", token="TOK", sheet_id="SID",
                        columns=("a", "b"))
    ups = [(f"C{r}:G{r}", [[f"v{r}"] * 5]) for r in range(2, 20)]   # 18 行连号
    n = feishu.sheet_write_ranges(sheet, ups)
    assert n == 18
    rows_per_req = [sum(len(vr["values"]) for vr in body["valueRanges"])
                    for body in sent]
    assert len(sent) == 5                       # 4+4+4+4+2,不是一锅端
    assert all(r <= 4 for r in rows_per_req), rows_per_req
    # 顺序与覆盖语义不变:拼回去仍是 v2..v19
    flat = [row[0] for body in sent for vr in body["valueRanges"]
            for row in vr["values"]]
    assert flat == [f"v{r}" for r in range(2, 20)]


def test_write_ranges_still_caps_100_ranges_per_request(monkeypatch):
    """不连号的小段照旧受「≤100 范围/请求」约束(_SHEET_RANGES_PER_REQUEST;
    ⚠ 这条是**工程值不是官方限额** —— 2026-08-27 调研结论「valueRanges 数组
    长度上限官方未说明」,沿用现行 100),行预算不该反过来把这条放宽。
    120 个互不相邻的单行段 → 2 个请求。"""
    sent = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    monkeypatch.setattr(feishu, "_SHEET_WRITE_THROTTLE_SECS", 0)
    sheet = Spreadsheet(name="X", token="TOK", sheet_id="SID", columns=("a",))
    ups = [(f"A{r}:A{r}", [["v"]]) for r in range(2, 242, 2)]   # 120 段,行号隔一
    n = feishu.sheet_write_ranges(sheet, ups)
    assert n == 120
    assert [len(b["valueRanges"]) for b in sent] == [100, 20]


# ── 分块读(sheet_values_rows):单响应 10MB 上限的解法 ─────────────────────────

def _rows_sheet():
    return Spreadsheet(name="X", token="TOK", sheet_id="SID", columns=("a",))


def test_values_rows_blocks_and_keeps_rownums(monkeypatch):
    """行方向分块拼接,行号按块首偏移——飞书裁掉块尾空行也不许错位。"""
    asked = []

    def fake_values(sheet, rng):
        asked.append(rng)
        # A2:C6 返回 5 行;A7:C11 只回 3 行(尾部两行为空被飞书裁掉)
        if rng == "A2:C6":
            return [[f"r{i}"] for i in range(2, 7)]
        return [[f"r{i}"] for i in range(7, 10)]
    monkeypatch.setattr(feishu, "_values_raw", fake_values)
    out = feishu.sheet_values_rows(_rows_sheet(), "A", "C", 2, 11,
                                   block_rows=5)
    assert asked == ["A2:C6", "A7:C11"]
    assert [(n, r[0]) for n, r in out] == [(i, f"r{i}") for i in range(2, 10)]


def test_values_rows_halves_on_90221(monkeypatch):
    """单块仍超 10MB(90221)→ 对半再切,数据一行不丢;其他错误照抛。

    2026-08-19 生产实证:上架表 21 列全量一把读撞 90221,audit_sheet 整链
    失败——分块 + 对半兜底后同一张表怎么长都读得完。
    """
    asked = []

    def fake_values(sheet, rng):
        asked.append(rng)
        if rng == "A2:U9":                    # 整块太大
            raise feishu.FeishuError(90221, "data exceeded 10485760 bytes")
        lo, hi = rng[1:].split(":U")
        return [[f"r{i}"] for i in range(int(lo), int(hi) + 1)]
    monkeypatch.setattr(feishu, "_values_raw", fake_values)
    out = feishu.sheet_values_rows(_rows_sheet(), "A", "U", 2, 9,
                                   block_rows=8)
    assert asked == ["A2:U9", "A2:U5", "A6:U9"]      # 对半各读一次
    assert [n for n, _ in out] == list(range(2, 10))  # 一行不丢

    def always_401(sheet, rng):
        raise feishu.FeishuError(401, "auth")
    monkeypatch.setattr(feishu, "_values_raw", always_401)
    with pytest.raises(feishu.FeishuError):           # 只兜 90221,不当 catch-all
        feishu.sheet_values_rows(_rows_sheet(), "A", "U", 2, 9)
