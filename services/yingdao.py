"""影刀 RPA 衔接积木(daily_report 用;仅 macOS 生产机有效)。

衔接是**两个文件**,不经飞书(所有者定稿 2026-08-15,新影刀应用):
    本仓 write_input() → input.json → 影刀读它决定抓哪些卖家页
    → 影刀写 latest.json → 本仓核对新鲜度后回填
旧路径「先把总览写飞书 → 影刀读飞书拿 sellerId」同期删除,不留双轨。
反转的理由是**新鲜度是被保证的而不是碰巧的**:input.json 由触发它的
那一次运行在提交前几秒写出,影刀物理上读不到隔夜的清单;若让影刀自己
连库,它在任何时刻都能读到任意一天的数据,而 cli.py 的 flock 管不到它。

怎么让影刀跑起来 —— 两段,各管一件事(2026-10-31 影刀 1.22.x 之后):

① **拉起客户端**:只在本地接口探活失败时 `launchctl kickstart gui/<uid>/
   com.walmartapi.yingdao`(launchd_install 落盘的代理,plist 只写主程序,
   不带参数)。⚠ 为什么非得绕 launchd(2026-09-01 崩溃报告实证,incident
   6B391891):日报链 runner=gpt,进程在智能体上下文里(coalitionName=
   com.openai.codex),没有 Aqua GUI session;影刀是 Electron/AppKit 应用,
   直接 spawn 会在 `_RegisterApplication` → abort()。launchd 的 gui/<uid>
   域本来就在图形会话里。2026-08-24 那次 `open` 退 1 是同一个根因。
   ⚠ **不带 -k**:代理的子进程就是影刀本身,-k 会把正在跑的影刀连同它
   手上的任务一起杀掉;客户端已开着时再 kickstart,第二个实例会自己退出。

② **提交运行**:走客户端的本地任务接口(api/yingdao):预检 → 提交拿
   taskId → 轮询到终态 → 再核对 latest.json 新鲜度。
   ⚠ 2026-10-31 生产事故:影刀升级到 1.22.20 后 `shadowbot:Run?robot-uuid=`
   协议被删(官方 1.22.20 安装包代码实读:协议处理只剩 Homework/View/
   Feedback 三种,1.21.9 还有 Run),客户端照收照记日志但什么也不做 ——
   日报那边"指令发出去了、latest.json 却停在 10-25"。协议入口**整条删掉,
   不留回退**:两条触发路只会让"是哪条在跑"变成猜谜。

判定(每一类分开报,摘要里看得出卡在哪一步;归类词唯一出处是下方 V_* 常量):
- 只有「任务 completed **并且** latest.json 的 scraped_at 晚于本次触发」才算
  成功 —— 流程内部自己吞掉的错误也会以 completed 收尾,completed ≠ 写出来了
- **结果不明绝不重提**:提交后没拿到回应、或客户端回"did not respond in time",
  任务可能已经建好在跑(渲染进程不会被取消),转对账;只有能证明"请求没送达"
  的几种(连不上、"still starting"、令牌已换、503)才允许同一方法再提,有次数上限
- 超时只报不停:到点仍在跑就带 taskId 报「超时」,不调停止接口;下一轮预检会
  看见它还在跑,不会叠加提交
- 任何失败都只降级:调用方沿用旧值(卖家名称有跨日延续兜底)
- 超时 / 轮询 / 就绪等待:env YINGDAO_TIMEOUT_SEC(600,从提交成功算起)/
  YINGDAO_POLL_INTERVAL(15)/ YINGDAO_READY_SEC(180,含冷启动)
- 跑哪一版:env YINGDAO_APP_TYPE,缺省 subscribed(跑「我获取的应用」里的发布版,
  与旧 Run 协议一致);developed 跑开发草稿。现场用 yingdao_probe 核对
- latest.json 路径经 registry.paths.frontend_scrape_file()(影刀应用内部写死
  自己的输出路径,env FRONTEND_SCRAPE_JSON 必须指到同一文件)

⚠ 额度:社区版/创业版账号**每提交一次就扣一次**云端每日额度(官方口径个人版
每天 5 次;旧 Run 协议不计额度)。日报主跑、链尾重赛、人工补跑都算。
⚠ 并跑纪律:旧系统 8 点调度也会触发影刀。新系统开 yingdao=1 之前必须
先停旧 walmart-kpi-daily,严禁两边同天各触发一次。
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from api import yingdao as yd
from registry import paths
from services import kpi, launchd, stores

logger = logging.getLogger("services.yingdao")


def robot_uuid() -> str:
    """输入:无 → 输出:目标机器人 UUID(env YINGDAO_ROBOT_UUID;未配置为空串)。"""
    return os.environ.get("YINGDAO_ROBOT_UUID", "").strip()


def app_type() -> str:
    """输入:无 → 输出:提交时的应用类型(env YINGDAO_APP_TYPE,缺省 subscribed)。

    subscribed = 跑「我获取的应用」里的最新发布版(旧 Run 协议就是这么跑的);
    developed = 跑开发草稿。**不认识的值报错**,不许静默落到缺省 ——
    打错一个字就会悄悄跑成另一个版本。
    """
    v = os.environ.get("YINGDAO_APP_TYPE", "").strip().lower() or "subscribed"
    if v not in yd.APP_TYPES:
        raise ValueError(f"YINGDAO_APP_TYPE 只接受 {'/'.join(yd.APP_TYPES)},收到:{v!r}")
    return v


# input.json 每行对影刀公开的字段。**store 是主键不是装饰**:sellerId 可能为空
# 或变更,而 latest.json 现仍按 sellerId 回键(格式不动,一次只改一端)。
_INPUT_FIELDS = ("store", "seller_id", "store_status", "payment_status")

# 「什么算在营」的判断在 kpi.store_activity,本模块不自己写 —— 同一个谓词
# 还被 kpi.derived_sales_status 用着(不抓的那些店直接标「不可售」),
# 两处各写一份 upper() != "ACTIVE" 迟早会飘成"抓了却标不可售"。
_STAT_KEYS = ("written", "no_seller_id", "inactive", "dup_seller_id",
              "unknown_status")


def write_input(rows: list[dict]) -> dict[str, int]:
    """输入:当轮已入库的 KPI 行 → 输出:各口径计数 dict(见 _STAT_KEYS)。

    整文件覆盖写,只含本轮真跑到的店——不追加、不与上一版合并,所以不会重复。

    过滤三道 + 一个警戒计数,**每项单独计数**:合成一个数字的话,"今天少抓了
    8 家"就没法回答"为什么少"(丢弃静默常态化 = 少抓一半没人知道)。
    - `no_seller_id` 空 sellerId。⚠ A147 事故防线:影刀拿它拼出
      https://www.walmart.com/seller//cp/shopall(路径中段为空)会崩掉整条
      RPA 循环,**后续店铺全被跳过**——不是少抓一家,是少抓一半。
    - `inactive` store_status **有值且**非 ACTIVE → 排除。
    - `dup_seller_id` 同一 sellerId 出现多次(店铺凭证表有重复行时会发生),
      抓两遍同一个页面纯属浪费,后者覆盖前者也看不出来。
    - `unknown_status` store_status **为空 → 照常纳入**,只计数报警。
      ⚠ 空不等于停用,是"没拿到":`extract_settlement` 的 store_status 取
      `sellerInfo.sellerStatus` 且**没有 `_find_key` 兜底**(payment_status 有),
      结算响应换个形状它就是空串。把空当停用 = 让解析故障静默地少抓一半店,
      违反本仓「判不准就判活」。这个数长期非 0 说明结算解析该修,不是店停了。

    被排除的店**照常入库**,只是不参与前台抓取;它们当天的销售状态会留空
    (卖家名称有跨日延续兜底)。

    原子写(tmp + rename):影刀随时可能在读,写一半被读到会让它解析失败。
    latest.json 那侧本仓早就在防这件事(读到半截文件继续等),这边同样欠不得。
    """
    path = paths.yingdao_input_file()
    out: list[dict] = []
    seen: set[str] = set()
    stats = dict.fromkeys(_STAT_KEYS, 0)
    for r in rows:
        sid = str(r.get("seller_id") or "").strip()
        activity = kpi.store_activity(r.get("store_status"))
        if not sid:
            stats["no_seller_id"] += 1
            continue
        if activity == "inactive":
            stats["inactive"] += 1
            continue
        if sid in seen:
            stats["dup_seller_id"] += 1
            continue
        if activity == "unknown":
            stats["unknown_status"] += 1      # 纳入,但要看得见
        seen.add(sid)
        out.append({f: ("" if r.get(f) is None else str(r[f]))
                    for f in _INPUT_FIELDS})
    out.sort(key=lambda r: stores.sort_key(r["store"]))     # 与看板同一店铺序
    stats["written"] = len(out)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "count": len(out), "stores": out}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    logger.info("影刀输入:写出 %s,计数 %s", path, stats)
    if stats["unknown_status"]:
        logger.warning("影刀输入:%d 店 store_status 为空仍纳入 —— 空≠停用,"
                       "长期非 0 说明结算解析拿不到 sellerStatus,该查 settle_debug",
                       stats["unknown_status"])
    return stats


# ── 判定归类词(唯一出处;摘要、日志、测试都引用这几个常量)──────────────
V_OK = "成功"
V_UNCONFIGURED = "未配置"           # UUID / 应用类型 / 主程序路径没配对
V_DOWN = "客户端没开"               # 本地接口连不上(kickstart 过仍起不来)
V_NOT_READY = "客户端未就绪"        # 接口起来了,渲染进程/账号还没准备好
V_PROTOCOL = "接口不匹配"           # 影刀升级换了接口,停下来看,不猜
V_NOT_LOGGED_IN = "未登录"
V_ASSISTANT = "调度模式"            # 调度模式下本地任务接口一律拒绝
V_BUSY = "客户端忙"                 # 有任务在跑 / Studio 开着或在编辑
V_QUOTA = "额度拒绝"                # 云端每日额度用尽或查不了
V_REJECTED = "任务被拒"             # 其他明确拒绝(参数、云端预检、应用不可用)
V_UNKNOWN = "结果不明"              # 提交可能已生效但没拿到回应;已对账,未重提
V_FAILED = "执行失败"
V_CANCELED = "被停止"
V_TIMEOUT = "超时"
V_NO_OUTPUT = "完成但未写出"        # completed 了,latest.json 却不新鲜
V_LOST = "任务丢失"                 # 跟踪中客户端重启,任务查不到了

# 就绪等待里"再等等可能就好"的几类;其余一律立刻报
_RETRYABLE = (V_DOWN, V_NOT_READY, V_NOT_LOGGED_IN)
_MAX_SUBMITS = 3                    # 证明"没送达"后的同一方法再提,总次数上限
_READY_POLL_SEC = 5
_GRACE_SEC = 30                     # completed 之后给 latest.json 落盘的宽限

# 提交被拒时按 message 原文分流(接口不给 reasonCode,只能认原文;全小写比对)
_MSG_NOT_DELIVERED = ("still starting",)                     # 没进渲染进程,可再提
_MSG_AMBIGUOUS = ("did not respond in time", "became unavailable")   # 可能已建任务
_MSG_BUSY = ("already running", "studio is editing", "studio is open")
_MSG_QUOTA = ("quota", "额度")


@dataclass(frozen=True)
class RunResult:
    """一次影刀运行的结论。ok 只在 completed 且 latest.json 新鲜时为 True。"""
    ok: bool
    verdict: str                    # V_* 之一
    detail: str = ""
    task_id: str | None = None
    note: str = ""                  # 不影响成败的提醒(版本没验证过等)

    def line(self) -> str:
        """输入:无 → 输出:摘要用的一行人话。"""
        out = self.verdict + (f"({self.detail})" if self.detail else "")
        if self.task_id:
            out += f",taskId={self.task_id}"
        return out + (f",⚠ {self.note}" if self.note else "")


def _fail(verdict: str, detail: str = "", task_id: str | None = None) -> RunResult:
    return RunResult(False, verdict, detail, task_id)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def is_placeholder_state(st: dict) -> bool:
    """输入:state() 的 data → 输出:是不是渲染进程没回应时的占位结果。

    ⚠ 占位结果照样 code 0,队列假报空闲(queueState=idle、running=null);
    认它的唯一标记是多出来的 `studioWindowState` 键(真实结果里没有)。
    拿占位结果当"空闲"去提交,渲染进程一旦随后才回应,就是结果不明。
    """
    return "studioWindowState" in (st or {})


def _kickstart() -> RunResult | None:
    """输入:无 → 输出:None = 拉起指令已发出;RunResult = 发不出去。

    ⚠ **不带 -k**:代理的子进程就是影刀本身,-k 会连它手上的任务一起杀掉
    (旧写法带 -k,是因为旧协议靠"重新启动"来触发运行;现在运行靠接口提交,
    启动只需要发生一次)。客户端已开着时再 kickstart,第二个实例会自己退出。
    """
    app = paths.yingdao_app()
    if not app.exists():
        return _fail(V_UNCONFIGURED, f"影刀主程序不在 {app}(装在别处就设 env YINGDAO_APP,"
                                     "再跑 launchd_install 重写代理)")
    target = f"gui/{os.getuid()}/{launchd.YINGDAO_LABEL}"
    try:
        r = subprocess.run(["launchctl", "kickstart", target],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:                                  # noqa: BLE001
        return _fail(V_DOWN, f"launchctl 调不动:{e}")
    if r.returncode != 0:
        # 最常见是代理没装/没在图形会话里 load
        return _fail(V_DOWN, f"launchctl kickstart {target} 退 {r.returncode}"
                             f"({(r.stderr or '').strip()[:200]})—— 多半是启动代理没装:"
                             "跑 launchd_install,并在图形会话里 load 那份 plist")
    logger.info("影刀客户端没开,已 kickstart %s", target)
    return None


def check_ready(uuid: str) -> RunResult | None:
    """输入:机器人 UUID → 输出:None = 可以提交;RunResult = 现在不能提交的原因。

    只读、不耗额度:探活(核对发现文件是活的那份)→ 登录 → 模式与队列。
    客户端刚起来时登录与渲染进程都要几秒到几十秒,调用方按 _RETRYABLE 决定再等。
    """
    try:
        yd.health()
    except yd.YingdaoUnreachable as e:
        return _fail(V_DOWN, str(e))
    except (yd.YingdaoNoResponse, yd.YingdaoApiError) as e:
        return _fail(V_NOT_READY, f"探活异常:{e}")
    try:
        yd.auth_current()
        st = yd.state()
    except yd.YingdaoApiError as e:
        if e.code == 4011:
            return _fail(V_NOT_LOGGED_IN, "影刀没登录(客户端「设置」里勾上启动后自动登录)")
        if e.code == 5010:
            return _fail(V_ASSISTANT, e.message)
        return _fail(V_NOT_READY, str(e))       # 令牌刚换、渲染进程还没起来等
    except (yd.YingdaoUnreachable, yd.YingdaoNoResponse) as e:
        return _fail(V_NOT_READY, str(e))
    if st.get("mode") != "console":
        return _fail(V_ASSISTANT, f"客户端处于「{st.get('currentModeDisplayName') or st.get('mode')}」,"
                                  "本地任务接口只在设计器模式下可用")
    if is_placeholder_state(st):
        return _fail(V_NOT_READY, "渲染进程还没回应(state 是占位结果)")
    task = st.get("task") or {}
    running = task.get("running")
    if running:
        who = ("上一轮的影刀还在跑" if running.get("appId") == uuid
               else f"在跑别的应用「{running.get('appName') or running.get('appId')}」")
        return _fail(V_BUSY, f"{who},状态 {running.get('status')}",
                     task_id=running.get("taskId"))
    if task.get("queueState") == "editing":
        return _fail(V_BUSY, "Studio 正在编辑应用(定时跑的时候别开设计器)")
    return None


def _wait_ready(uuid: str, deadline: float) -> RunResult | None:
    """输入:UUID + 就绪截止时刻 → 输出:None = 就绪;RunResult = 到点仍不能提交。"""
    kicked = False
    while True:
        res = check_ready(uuid)
        if res is None:
            return None
        if res.verdict == V_DOWN and not kicked:
            kicked = True
            k = _kickstart()
            if k is not None:
                return k
        if res.verdict not in _RETRYABLE or time.monotonic() >= deadline:
            if res.verdict == V_DOWN and kicked:
                return replace(res, detail="已 kickstart,到点仍没起来:" + res.detail)
            return res
        time.sleep(_READY_POLL_SEC)


def _submit(uuid: str, atype: str, request_id: str) -> str | RunResult | None:
    """输入:UUID + 应用类型 + 请求号 → 输出:taskId / RunResult(拒绝或结果不明)/
    None(**能证明没送达**,同一方法可以再提)。"""
    try:
        data = yd.submit_task(uuid, atype, request_id=request_id)
    except yd.YingdaoUnreachable:
        return None                                 # 连接都没建立
    except yd.YingdaoNoResponse as e:
        return _fail(V_UNKNOWN, str(e))
    except yd.YingdaoApiError as e:
        msg = e.message.lower()
        if e.http_status == 503 or (e.http_status == 403 and "token" in msg):
            return None         # 网关层拒绝(过载 / 客户端重启令牌已换),没进渲染进程
        if e.http_status != 200:
            return _fail(V_REJECTED, str(e))        # 400/413/415/404/500:没建任务
        if e.code == 4011:
            return _fail(V_NOT_LOGGED_IN, e.message)
        if e.code == 5010:
            return _fail(V_ASSISTANT, e.message)
        if e.code == 4091:
            if any(m in msg for m in _MSG_NOT_DELIVERED):
                return None
            if any(m in msg for m in _MSG_AMBIGUOUS):
                return _fail(V_UNKNOWN, e.message)
            if any(m in msg for m in _MSG_BUSY):
                return _fail(V_BUSY, e.message)
            if any(m in msg for m in _MSG_QUOTA):
                return _fail(V_QUOTA, e.message)
        if e.code == 5000:
            return _fail(V_REJECTED, f"云端预检失败(权限 / 应用已回收 / 网络;原因只在影刀"
                                     f"客户端日志里):{e.message}")
        return _fail(V_REJECTED, e.message)
    if data.get("accepted") and data.get("taskId"):
        return str(data["taskId"])
    # code 0 却没有 taskId:任务建没建说不清,按结果不明处理(不重提)
    return _fail(V_UNKNOWN, f"提交回应缺 taskId:{data!r}"[:300])


def _fresh_now(trigger_utc: datetime) -> bool:
    try:
        data = json.loads(paths.frontend_scrape_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return is_fresh(data, trigger_utc)


def _track(task_id: str, trigger_utc: datetime, deadline: float) -> RunResult:
    """输入:taskId + 触发时刻 + 截止时刻 → 输出:按终态与 latest.json 判定的结论。"""
    poll = _env_int("YINGDAO_POLL_INTERVAL", 15)
    last = "查不到"
    while True:
        try:
            t = yd.get_task(task_id)
            last = str(t.get("status") or "?")
        except yd.YingdaoApiError as e:
            if e.code == 4041:          # 客户端中途重启,跑着的任务没落库就没了
                if wait_fresh(trigger_utc, timeout=max(0, int(deadline - time.monotonic()))):
                    return RunResult(True, V_OK, "任务记录丢了但 latest.json 已更新", task_id)
                return _fail(V_LOST, "跟踪中客户端重启,任务查不到了", task_id)
            t, last = {}, f"查询出错 {e.message}"
        except (yd.YingdaoUnreachable, yd.YingdaoNoResponse) as e:
            t, last = {}, f"连不上客户端({e})"
        status = t.get("status")
        if status == "completed":
            if wait_fresh(trigger_utc, timeout=_GRACE_SEC):
                return RunResult(True, V_OK, "", task_id)
            return _fail(V_NO_OUTPUT, f"{paths.frontend_scrape_file()} 的 scraped_at 不晚于本次触发"
                                      " —— 核对影刀应用里写死的输出路径与 FRONTEND_SCRAPE_JSON"
                                      " 是不是同一个文件", task_id)
        if status == "failed":
            return _fail(V_FAILED, str(t.get("error") or "影刀没给原因"), task_id)
        if status == "canceled":
            return _fail(V_CANCELED, "任务被人工或别的程序停止", task_id)
        if time.monotonic() >= deadline:
            # 只报不停:停止接口是异步的、没有截止时间,而下一轮预检看得见它还在跑
            return _fail(V_TIMEOUT, f"到点仍是 {last},没有停它", task_id)
        time.sleep(poll)


def _reconcile(uuid: str, trigger_utc: datetime, deadline: float, why: str) -> RunResult:
    """输入:UUID + 触发时刻 + 截止 + 不明的原因 → 输出:对账后的结论。**绝不重提。**

    先看客户端正在跑的是不是我们这一单(提交前预检确认过当时没有我们的任务
    在跑,所以此刻在跑的那一单只能是这次提交建的);找不到就只看 latest.json。
    """
    poll = _env_int("YINGDAO_POLL_INTERVAL", 15)
    logger.warning("影刀提交结果不明(%s),转对账,不重提", why)
    while True:
        try:
            st = yd.state()
        except (yd.YingdaoUnreachable, yd.YingdaoNoResponse, yd.YingdaoApiError):
            st = {}
        running = {} if is_placeholder_state(st) else \
            ((st.get("task") or {}).get("running") or {})
        if running.get("appId") == uuid and running.get("sourceKind") == "api" \
                and running.get("taskId"):
            res = _track(str(running["taskId"]), trigger_utc, deadline)
            return replace(res, detail=("提交时" + why + ",对账找到了任务;" + res.detail).rstrip(";"))
        if _fresh_now(trigger_utc):
            return RunResult(True, V_OK, f"提交时{why},但 latest.json 已更新")
        if time.monotonic() >= deadline:
            return _fail(V_UNKNOWN, f"{why};到点既没找到任务也没等到新数据")
        time.sleep(poll)


def _version_note() -> str:
    try:
        v = str(yd.version().get("appVersion") or "")
    except (yd.YingdaoUnreachable, yd.YingdaoNoResponse, yd.YingdaoApiError):
        return ""
    if v and v not in yd.VERIFIED_VERSIONS:
        return (f"影刀 {v} 不在验证过的版本里({'/'.join(yd.VERIFIED_VERSIONS)}),"
                "接口若再变先跑 yingdao_probe")
    return ""


def run_robot(trigger_utc: datetime) -> RunResult:
    """输入:触发时刻(UTC,input.json 写完那一刻)→ 输出:RunResult。

    阻塞到出结论为止:就绪等待 ≤ YINGDAO_READY_SEC,提交成功后 ≤
    YINGDAO_TIMEOUT_SEC。任何失败只降级,不抛(接口不匹配除外,见下)。
    """
    uuid = robot_uuid()
    if not uuid:
        return _fail(V_UNCONFIGURED, "YINGDAO_ROBOT_UUID 未配置")
    try:
        atype = app_type()
    except ValueError as e:
        return _fail(V_UNCONFIGURED, str(e))
    request_id = f"walmartapi-{trigger_utc:%Y%m%dT%H%M%SZ}"
    try:
        ready_deadline = time.monotonic() + _env_int("YINGDAO_READY_SEC", 180)
        for attempt in range(1, _MAX_SUBMITS + 1):
            res = _wait_ready(uuid, ready_deadline)
            if res is not None:
                return res
            note = _version_note()
            submitted = time.monotonic()
            out = _submit(uuid, atype, request_id)
            run_deadline = submitted + _env_int("YINGDAO_TIMEOUT_SEC", 600)
            if isinstance(out, str):
                logger.info("影刀任务已提交:taskId=%s(%s,第 %d 次提交)", out, atype, attempt)
                return replace(_track(out, trigger_utc, run_deadline), note=note)
            if out is None:
                logger.warning("影刀提交没送达(第 %d 次),回到就绪等待后同一方法再提", attempt)
                time.sleep(_READY_POLL_SEC)
                continue
            if out.verdict == V_UNKNOWN:
                return replace(_reconcile(uuid, trigger_utc, run_deadline, out.detail), note=note)
            return out
        return _fail(V_NOT_READY, f"连提 {_MAX_SUBMITS} 次都没送达")
    except yd.YingdaoProtocolError as e:
        return _fail(V_PROTOCOL, str(e))


def is_fresh(data: dict, trigger_utc: datetime) -> bool:
    """输入:latest.json 内容 + 触发时刻(UTC)→ 输出:scraped_at 是否晚于触发。"""
    try:
        scraped = datetime.fromisoformat(str(data.get("scraped_at")))
        return scraped.astimezone(timezone.utc) > trigger_utc
    except (TypeError, ValueError):
        return False


def wait_fresh(trigger_utc: datetime, timeout: int) -> bool:
    """输入:触发时刻(UTC)+ 最多等几秒 → 输出:到点前是否等到新鲜的 latest.json。"""
    poll = max(1, min(_env_int("YINGDAO_POLL_INTERVAL", 15), timeout or 1))
    deadline = time.monotonic() + timeout
    while True:
        if _fresh_now(trigger_utc):     # 文件没出现/写到一半都算"还没",继续等
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
