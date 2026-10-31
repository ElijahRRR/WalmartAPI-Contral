"""影刀本地任务接口(2026-10-31:1.22.x 删了 shadowbot:Run 协议之后)。

钉三层:
  ① api/yingdao:每次重读发现文件、Bearer 鉴权、不带 Origin、不走环境代理、
     信封按 code 判、传输层故障分型(没送达 / 结果不明);令牌不进异常文本
  ② services/yingdao.run_robot:就绪 → 提交 → 跟踪 → 判定,每一类结论分开;
     **结果不明绝不重提**,只有能证明没送达的才同一方法再提
  ③ 协议入口整条删掉,不留回退(双轨禁止)
"""

import ast
import json
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from api import yingdao as yd
from registry import paths
from services import yingdao

_ROOT = pathlib.Path(__file__).resolve().parent.parent
UUID = "0df955ab-ecbc-4b5d-a215-223f32c237c9"
TOKEN = "s3cr3t-token-value"


def _write_instance(path, **over):
    inst = {"instanceId": "inst-1", "edition": "general", "protocolVersion": "1",
            "hostname": "127.0.0.1", "port": 5000, "pid": 4242, "token": TOKEN,
            "startedAt": "2026-10-31T08:00:00+08:00"}
    inst.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inst), encoding="utf-8")
    return inst


@pytest.fixture
def instance(monkeypatch, tmp_path):
    p = tmp_path / "Shadowbot" / "shadowbot-cli" / "instance.json"
    monkeypatch.setenv("YINGDAO_INSTANCE_JSON", str(p))
    _write_instance(p)
    return p


@pytest.fixture
def transport(monkeypatch):
    """把 api.yingdao 里的 httpx.Client 换成 MockTransport,记下每个请求与构造参数。"""
    seen = {"requests": [], "client_kw": []}
    routes = {}
    real = httpx.Client

    def handler(request):
        seen["requests"].append(request)
        fn = routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(404, json={"code": 4041, "message": "resource not found",
                                             "data": None})
        return fn(request)

    def make(**kw):
        seen["client_kw"].append(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(yd.httpx, "Client", make)
    seen["routes"] = routes
    return seen


def _ok(data):
    return lambda request: httpx.Response(200, json={"code": 0, "message": "ok",
                                                     "requestId": "r", "data": data})


# ── ① api 层 ────────────────────────────────────────────────────────────────

def test_instance_file_default_path_and_overrides(monkeypatch, tmp_path):
    """路径读自 1.22.20 安装包:$APPDATA 优先,否则 ~/Library/Application Support,
    后接 Shadowbot/shadowbot-cli/instance.json(**不是** Application Support/影刀)。"""
    monkeypatch.delenv("YINGDAO_INSTANCE_JSON", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.yingdao_instance_file() == (
        tmp_path / "Library" / "Application Support" / "Shadowbot" / "shadowbot-cli"
        / "instance.json")
    monkeypatch.setenv("APPDATA", str(tmp_path / "ad"))
    assert paths.yingdao_instance_file() == tmp_path / "ad" / "Shadowbot" / \
        "shadowbot-cli" / "instance.json"
    monkeypatch.setenv("YINGDAO_INSTANCE_JSON", str(tmp_path / "x.json"))
    assert paths.yingdao_instance_file() == tmp_path / "x.json"


def test_authed_call_sends_bearer_no_origin_and_ignores_env_proxy(instance, transport):
    transport["routes"][("GET", "/api/v1/system/state")] = _ok({"mode": "console"})
    assert yd.state() == {"mode": "console"}
    req = transport["requests"][0]
    assert str(req.url) == "http://127.0.0.1:5000/api/v1/system/state"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "origin" not in {k.lower() for k in req.headers}      # 带 Origin 一律 403
    assert transport["client_kw"][0]["trust_env"] is False       # 不走店铺/系统代理


def test_every_call_rereads_the_discovery_file(instance, transport):
    """客户端每次启动都换端口与令牌 —— 缓存一次就会拿旧令牌撞 403。"""
    transport["routes"][("GET", "/api/v1/system/state")] = _ok({})
    yd.state()
    _write_instance(instance, port=5123, token="new-token")     # 客户端重启过
    transport["requests"].clear()
    yd.state()
    req = transport["requests"][0]
    assert req.url.port == 5123 and req.headers["Authorization"] == "Bearer new-token"


def test_health_is_unauthenticated_and_detects_a_stale_file(instance, transport):
    transport["routes"][("GET", "/api/v1/system/health")] = _ok(
        {"status": "ok", "instanceId": "inst-1", "pid": 4242})
    assert yd.health() == {"instanceId": "inst-1", "pid": 4242, "port": 5000}
    assert "authorization" not in {k.lower() for k in transport["requests"][0].headers}
    # 崩溃后残留的旧文件:端口上回应的是另一个实例
    transport["routes"][("GET", "/api/v1/system/health")] = _ok(
        {"status": "ok", "instanceId": "inst-2", "pid": 1})
    with pytest.raises(yd.YingdaoUnreachable, match="旧文件"):
        yd.health()


def test_non_shadowbot_listener_on_a_stale_port_reads_as_unreachable(instance, transport):
    """客户端非正常退出后发现文件残留,端口(优先 5000)被别的程序占着 —— macOS 的
    隔空播放接收器就在 5000,回 403 空体。真影刀的 health 永远回信封,所以"不是信封"
    必须按连不上报:报成「未就绪」的话 kickstart 永远不会发生,天天卡在这里。"""
    transport["routes"][("GET", "/api/v1/system/health")] = \
        lambda r: httpx.Response(403, content=b"")
    with pytest.raises(yd.YingdaoUnreachable, match="不是影刀"):
        yd.health()
    assert yingdao.check_ready(UUID).verdict == yingdao.V_DOWN      # → 会去 kickstart
    # 真影刀回的错误信封(比如过载 503/4091)仍是"起来了但没准备好",不拉起
    transport["routes"][("GET", "/api/v1/system/health")] = lambda r: httpx.Response(
        503, json={"code": 4091, "message": "too many local API requests", "data": None})
    with pytest.raises(yd.YingdaoApiError):
        yd.health()


def test_business_errors_come_back_as_http_200_and_are_still_errors(instance, transport):
    transport["routes"][("GET", "/api/v1/auth/current")] = lambda r: httpx.Response(
        200, json={"code": 4011, "message": "ShadowBot is not logged in", "data": None})
    with pytest.raises(yd.YingdaoApiError) as ei:
        yd.auth_current()
    assert ei.value.http_status == 200 and ei.value.code == 4011


def test_submit_posts_json_body_with_request_id(instance, transport):
    transport["routes"][("POST", "/api/v1/tasks")] = _ok({"accepted": True, "taskId": "t-1"})
    assert yd.submit_task(UUID, "subscribed", request_id="walmartapi-x")["taskId"] == "t-1"
    req = transport["requests"][0]
    assert req.headers["content-type"] == "application/json"     # 缺了是 415
    assert req.headers["X-Request-Id"] == "walmartapi-x"
    assert json.loads(req.content) == {"appId": UUID, "appType": "subscribed"}


def test_transport_failures_are_split_into_not_delivered_and_no_response(
        instance, monkeypatch):
    """连不上 = 肯定没送达(可再提);读超时 = 可能已建任务(结果不明,不许重提)。"""
    real = httpx.Client         # 先存:monkeypatch 换的是 httpx 模块本身的属性

    def make_raising(exc):
        def handler(request):
            raise exc("boom", request=request)
        return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(yd.httpx, "Client", make_raising(httpx.ConnectError))
    with pytest.raises(yd.YingdaoUnreachable):
        yd.submit_task(UUID, "subscribed")
    monkeypatch.setattr(yd.httpx, "Client", make_raising(httpx.ReadTimeout))
    with pytest.raises(yd.YingdaoNoResponse) as ei:
        yd.submit_task(UUID, "subscribed")
    assert TOKEN not in str(ei.value)                             # 令牌不进异常文本


def test_discovery_file_problems(instance, transport, monkeypatch, tmp_path):
    _write_instance(instance, protocolVersion="2")
    with pytest.raises(yd.YingdaoProtocolError, match="协议版本"):
        yd.state()
    _write_instance(instance, hostname="10.0.0.5")                # 令牌不发往非本机
    with pytest.raises(yd.YingdaoProtocolError):
        yd.state()
    monkeypatch.setenv("YINGDAO_INSTANCE_JSON", str(tmp_path / "nope.json"))
    with pytest.raises(yd.YingdaoUnreachable, match="没有发现文件"):
        yd.health()
    assert transport["requests"] == []


# ── ② services 层:run_robot ────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _FakeApi:
    """api.yingdao 的桩:按脚本回放每个端点,记下调用次数。"""

    def __init__(self):
        self.calls = []
        self.health_seq = [{"instanceId": "i", "pid": 1, "port": 5000}]
        self.auth = {"username": "op"}
        self.state_seq = [{"mode": "console",
                           "task": {"running": None, "queueLength": 0, "queueState": "idle"}}]
        self.submit_seq = [{"accepted": True, "taskId": "t-1"}]
        self.task_seq = [{"status": "running"}, {"status": "completed"}]
        self.app_version = "1.22.20"

    @staticmethod
    def _next(seq):
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(v, Exception):
            raise v
        return v

    def health(self):
        self.calls.append("health")
        return self._next(self.health_seq)

    def version(self):
        return {"appVersion": self.app_version}

    def auth_current(self):
        self.calls.append("auth")
        if isinstance(self.auth, Exception):
            raise self.auth
        return self.auth

    def state(self):
        self.calls.append("state")
        return self._next(self.state_seq)

    def submit_task(self, app_id, app_type, request_id=None):
        self.calls.append(("submit", app_id, app_type))
        return self._next(self.submit_seq)

    def get_task(self, task_id):
        self.calls.append(("get", task_id))
        return self._next(self.task_seq)

    @property
    def submits(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "submit"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(yingdao.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(yingdao.time, "sleep", clock.sleep)
    fake = _FakeApi()
    for name in ("health", "version", "auth_current", "state", "submit_task", "get_task"):
        monkeypatch.setattr(yingdao.yd, name, getattr(fake, name))
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", UUID)
    monkeypatch.delenv("YINGDAO_APP_TYPE", raising=False)
    latest = tmp_path / "latest.json"
    monkeypatch.setenv("FRONTEND_SCRAPE_JSON", str(latest))
    app = tmp_path / "影刀"
    app.write_text("")
    monkeypatch.setenv("YINGDAO_APP", str(app))
    kicks = []

    def run(argv, **kw):
        kicks.append(argv)
        return SimpleNamespace(returncode=fake_rc[0], stdout="", stderr="boom")

    fake_rc = [0]
    monkeypatch.setattr(yingdao.subprocess, "run", run)
    monkeypatch.setattr(yingdao.os, "getuid", lambda: 501)
    trigger = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    return {"fake": fake, "clock": clock, "latest": latest, "kicks": kicks,
            "rc": fake_rc, "trigger": trigger}


def _fresh(env, minutes=5):
    env["latest"].write_text(json.dumps(
        {"scraped_at": (env["trigger"] + timedelta(minutes=minutes)).isoformat()}),
        encoding="utf-8")


def test_happy_path_submits_once_tracks_and_needs_fresh_output(env):
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert res.ok and res.verdict == yingdao.V_OK and res.task_id == "t-1"
    assert env["fake"].submits == [("submit", UUID, "subscribed")]   # 缺省跑发布版
    assert env["kicks"] == []                     # 客户端开着就不 kickstart


def test_client_down_is_kickstarted_once_without_k(env):
    """⚠ 不带 -k:代理的子进程就是影刀本身,-k 会把它手上的任务一起杀掉。"""
    from services import launchd
    env["fake"].health_seq = [yd.YingdaoUnreachable("没开"), yd.YingdaoUnreachable("没开"),
                              {"instanceId": "i", "pid": 1, "port": 5000}]
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert res.ok
    assert env["kicks"] == [["launchctl", "kickstart", f"gui/501/{launchd.YINGDAO_LABEL}"]]


def test_kickstart_failure_points_at_the_agent(env):
    env["fake"].health_seq = [yd.YingdaoUnreachable("没开")]
    env["rc"][0] = 3
    res = yingdao.run_robot(env["trigger"])
    assert not res.ok and res.verdict == yingdao.V_DOWN and "launchd_install" in res.detail
    assert env["fake"].submits == []


def test_still_down_after_kickstart_reports_down_at_ready_deadline(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_READY_SEC", "60")
    env["fake"].health_seq = [yd.YingdaoUnreachable("没开")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_DOWN and "已 kickstart" in res.detail
    assert len(env["kicks"]) == 1 and env["fake"].submits == []


def test_placeholder_state_is_never_taken_as_idle(env, monkeypatch):
    """渲染进程没回应时 state 照样 code 0、队列假报空闲 —— 认 studioWindowState。"""
    monkeypatch.setenv("YINGDAO_READY_SEC", "30")
    env["fake"].state_seq = [{"mode": "console", "studioWindowState": "unknown",
                              "task": {"running": None, "queueState": "idle"}}]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_NOT_READY and env["fake"].submits == []


def test_not_logged_in_and_assistant_mode_never_submit(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_READY_SEC", "30")
    env["fake"].auth = yd.YingdaoApiError(200, 4011, "ShadowBot is not logged in")
    assert yingdao.run_robot(env["trigger"]).verdict == yingdao.V_NOT_LOGGED_IN
    env["fake"].auth = {"username": "op"}
    env["fake"].state_seq = [{"mode": "assistant", "currentModeDisplayName": "调度模式",
                              "studioWindowState": "not_supported", "task": None}]
    assert yingdao.run_robot(env["trigger"]).verdict == yingdao.V_ASSISTANT
    assert env["fake"].submits == []


def test_previous_run_still_running_is_reported_not_stacked(env):
    env["fake"].state_seq = [{"mode": "console", "task": {
        "running": {"taskId": "old-7", "appId": UUID, "status": "running",
                    "sourceKind": "api"}, "queueState": "running"}}]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_BUSY and res.task_id == "old-7"
    assert "上一轮" in res.detail and env["fake"].submits == []


def test_ambiguous_submit_is_never_resubmitted_and_is_reconciled(env):
    """⚠ 'did not respond in time' 时渲染进程不会被取消,任务可能已建好在跑。
    重提 = 同一份清单跑两遍 + 多扣一次额度。只许对账。"""
    idle = {"mode": "console", "task": {"running": None, "queueState": "idle"}}
    ours = {"mode": "console", "task": {"running": {
        "taskId": "t-late", "appId": UUID, "sourceKind": "api", "status": "running"}}}
    env["fake"].state_seq = [idle, ours]
    env["fake"].submit_seq = [yd.YingdaoApiError(
        200, 4091, "The desktop client did not respond in time. Try again.")]
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert len(env["fake"].submits) == 1
    assert res.ok and res.task_id == "t-late" and "对账找到了任务" in res.detail


@pytest.mark.parametrize("err", [
    yd.YingdaoNoResponse("ReadTimeout"),
    yd.YingdaoApiError(200, 4091, "The desktop client did not respond in time. Try again."),
    yd.YingdaoApiError(200, 4091, "The desktop client became unavailable. Try again."),
    {"accepted": True},                         # code 0 却没有 taskId
], ids=["no-response", "did-not-respond", "became-unavailable", "no-task-id"])
def test_every_ambiguous_submit_is_submitted_exactly_once(env, monkeypatch, err):
    """⚠ 红线:四种"可能已建任务"的情形,任何一种被改成可重提都要在这里变红。"""
    monkeypatch.setenv("YINGDAO_TIMEOUT_SEC", "60")
    env["fake"].submit_seq = [err, {"accepted": True, "taskId": "t-2"}]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_UNKNOWN and len(env["fake"].submits) == 1


@pytest.mark.parametrize("err", [
    yd.YingdaoApiError(503, 4091, "too many local API requests"),
    yd.YingdaoApiError(403, 4031, "invalid local API token"),       # 客户端重启,令牌已换
], ids=["503", "token-changed"])
def test_gateway_rejections_never_reached_the_renderer_and_may_resubmit(env, err):
    env["fake"].submit_seq = [err, {"accepted": True, "taskId": "t-2"}]
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert res.ok and res.task_id == "t-2" and len(env["fake"].submits) == 2


def test_other_403_is_a_rejection_not_a_retry_loop(env):
    env["fake"].submit_seq = [yd.YingdaoApiError(403, 4031, "API is disabled by security policy")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_REJECTED and len(env["fake"].submits) == 1


def test_no_response_without_trace_ends_as_unknown_not_resubmitted(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_TIMEOUT_SEC", "120")
    env["fake"].submit_seq = [yd.YingdaoNoResponse("ReadTimeout")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_UNKNOWN and len(env["fake"].submits) == 1


def test_provably_undelivered_submit_is_retried_with_the_same_method(env):
    env["fake"].submit_seq = [
        yd.YingdaoApiError(200, 4091, "The desktop client is still starting. Try again shortly."),
        yd.YingdaoUnreachable("ConnectError"),
        {"accepted": True, "taskId": "t-3"}]
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert res.ok and res.task_id == "t-3" and len(env["fake"].submits) == 3


def test_undelivered_retries_are_capped(env):
    env["fake"].submit_seq = [yd.YingdaoUnreachable("ConnectError")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_NOT_READY
    assert len(env["fake"].submits) == yingdao._MAX_SUBMITS


@pytest.mark.parametrize("message,verdict", [
    ("a task is already running", yingdao.V_BUSY),
    ("task run is unavailable while Studio is open", yingdao.V_BUSY),
    ("影刀CLI社区版今日体验额度已用尽,次日00:00重置", yingdao.V_QUOTA),
    ("daily CLI run quota exhausted", yingdao.V_QUOTA),
    ("Only PC applications can be run by console task run.", yingdao.V_REJECTED),
])
def test_explicit_rejections_are_named_and_not_retried(env, message, verdict):
    env["fake"].submit_seq = [yd.YingdaoApiError(200, 4091, message)]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == verdict and message in res.detail
    assert len(env["fake"].submits) == 1


def test_cloud_precheck_failure_is_a_rejection(env):
    env["fake"].submit_seq = [yd.YingdaoApiError(
        200, 5000, "The desktop client could not complete the request.")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_REJECTED and "云端预检" in res.detail


def test_failed_task_carries_the_flow_error(env):
    env["fake"].task_seq = [{"status": "failed", "error": "主流程 第12行 元素未找到"}]
    _fresh(env)                     # 就算文件新鲜,跑挂了也不算成
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_FAILED and "第12行" in res.detail and not res.ok


def test_completed_without_fresh_output_is_its_own_verdict(env):
    """流程内部吞掉的错误也会以 completed 收尾:completed ≠ 写出来了。"""
    _fresh(env, minutes=-60)
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_NO_OUTPUT and "FRONTEND_SCRAPE_JSON" in res.detail


def test_timeout_reports_and_does_not_stop_the_task(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_TIMEOUT_SEC", "60")
    env["fake"].task_seq = [{"status": "running"}]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_TIMEOUT and res.task_id == "t-1"
    assert not hasattr(yd, "stop_task")          # 停止接口压根没接:超时只报不停


def test_task_lost_after_client_restart(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_TIMEOUT_SEC", "60")
    env["fake"].task_seq = [yd.YingdaoApiError(200, 4041, "task not found")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_LOST and res.task_id == "t-1"


def test_unconfigured_never_touches_the_client(env, monkeypatch):
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", "")
    assert yingdao.run_robot(env["trigger"]).verdict == yingdao.V_UNCONFIGURED
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", UUID)
    monkeypatch.setenv("YINGDAO_APP_TYPE", "develop")          # 打错一个字不许落到缺省
    assert yingdao.run_robot(env["trigger"]).verdict == yingdao.V_UNCONFIGURED
    assert env["fake"].calls == [] and env["kicks"] == []


def test_protocol_change_stops_with_its_own_verdict(env):
    env["fake"].health_seq = [yd.YingdaoProtocolError("协议版本 '2'")]
    res = yingdao.run_robot(env["trigger"])
    assert res.verdict == yingdao.V_PROTOCOL and env["fake"].submits == []


def test_unverified_client_version_is_noted_not_blocked(env):
    env["fake"].app_version = "1.23.0"
    _fresh(env)
    res = yingdao.run_robot(env["trigger"])
    assert res.ok and "1.23.0" in res.note and "1.23.0" in res.line()


# ── ③ 协议入口整条删掉,不留回退 ───────────────────────────────────────────

def test_the_removed_run_protocol_is_not_used_anywhere_in_code():
    """1.22.x 没有 Run 分支了;代码里再出现它 = 有人加了第二条触发路(双轨禁止)。

    只查**运行期字符串**(docstring 与注释里讲历史照常允许)。
    """
    hits = []
    for d in ("api", "services", "workflows", "registry"):
        for p in (_ROOT / d).rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef,
                                      ast.AsyncFunctionDef))
                    and n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant)}
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                        and id(n) not in docs and "shadowbot:" in n.value.lower():
                    hits.append(f"{p.relative_to(_ROOT)}:{n.lineno}")
    assert not hits, hits
