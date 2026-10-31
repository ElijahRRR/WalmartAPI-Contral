"""yingdao_probe — 影刀本地任务接口体检(默认只读;仅 macOS 生产机有意义)。

用法:
  python cli.py yingdao_probe              # 只读:逐项看客户端能不能接任务 ← **先跑这个**
  python cli.py yingdao_probe -p run=1     # 真提交一次(用现有 input.json),跑到出结论
  python cli.py yingdao_probe -p run=1 --dry-run   # 只看会不会提交,不提交

为什么有它(2026-10-31):影刀 1.22.x 删掉了 `shadowbot:Run` 协议,日报改走
客户端本地任务接口(api/yingdao)。接口没有官方公开文档,契约读自官方安装包
代码 —— 端口、模式、登录、应用类型这些**只能在那台 Mac 上看**。本工作流把
日报提交前会检查的东西一项项摆出来,哪一项不对就停在哪一项。

**默认只读**:不拉起客户端、不提交任务、不写任何文件、不耗额度。
⚠ 绝不打印令牌,也不打印发现文件全文(官方同样要求)。
`-p run=1` 走的是**日报同一条路径**(services/yingdao.run_robot,双轨禁止),
⚠ 会真跑一次机器人,社区版/创业版账号**扣一次每日额度**;旧系统 8 点那次
调度还开着时别用(两边各跑一次互抢)。
"""

import json
import logging
from datetime import datetime, timezone

from api import yingdao as yd
from registry import paths
from services import params as P, yingdao

DANGEROUS = False       # 默认只读;真提交要显式 -p run=1

logger = logging.getLogger("workflows.yingdao_probe")


def _find_app(uuid: str) -> list[str]:
    """输入:机器人 UUID → 输出:在哪几种应用类型的列表里查得到它。"""
    found = []
    for t in yd.APP_TYPES:
        page, pages = 1, 1
        while page <= pages:
            data = yd.list_apps(t, page=page)
            if any(str(i.get("appId")) == uuid for i in data.get("items") or []):
                found.append(t)
                break
            pages = int(data.get("pages") or 1)
            page += 1
    return found


def _input_line() -> str:
    path = paths.yingdao_input_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"  input.json:读不到 {path}"
    return (f"  input.json:{data.get('count')} 店,生成于 {data.get('generated_at')}"
            f"({path})")


def run(params: dict) -> str:
    """输入:-p run=1(可选)→ 输出:逐项体检结果;run=1 时附一次真提交的结论。"""
    uuid = yingdao.robot_uuid()
    do_run = P.flag(params, "run")
    lines = ["影刀本地接口体检 + 真提交" if do_run else "影刀本地接口体检(只读)"]
    try:
        atype = yingdao.app_type()
    except ValueError as e:
        return "\n".join(lines + [f"  ✗ {e}"])
    lines.append(f"  机器人 UUID:{uuid or '✗ 未配置(env YINGDAO_ROBOT_UUID)'};"
                 f"应用类型 {atype}(env YINGDAO_APP_TYPE)")

    path = paths.yingdao_instance_file()
    if path.exists():
        mode = oct(path.stat().st_mode & 0o777)
        lines.append(f"  发现文件:{path}(权限 {mode})")
    else:
        return "\n".join(lines + [
            f"  ✗ 发现文件不存在:{path}",
            "    → 影刀没开,或版本低于 1.22(没有本地接口)。先手动打开影刀再跑一次;",
            f"      或 launchctl kickstart gui/$(id -u)/com.walmartapi.yingdao"])

    try:
        h = yd.health()
        lines.append(f"  ✓ 探活:端口 {h['port']},进程 {h['pid']},"
                     f"实例 {str(h['instanceId'])[:8]}…")
        v = yd.version()
        ver = str(v.get("appVersion") or "?")
        mark = "✓" if ver in yd.VERIFIED_VERSIONS else "⚠"
        lines.append(f"  {mark} 版本 {ver}(验证过:{'/'.join(yd.VERIFIED_VERSIONS)})")
    except (yd.YingdaoUnreachable, yd.YingdaoNoResponse, yd.YingdaoApiError,
            yd.YingdaoProtocolError) as e:
        return "\n".join(lines + [f"  ✗ 探活失败:{e}"])

    try:
        a = yd.auth_current()
        lines.append(f"  ✓ 已登录:{a.get('username') or a.get('displayName')},"
                     f"账号等级 {a.get('gradeName') or a.get('grade')}"
                     "(社区版/创业版每提交一次扣一次每日额度)")
    except yd.YingdaoApiError as e:
        lines.append(f"  ✗ 登录:{e.message}(code {e.code})")
    except (yd.YingdaoUnreachable, yd.YingdaoNoResponse) as e:
        lines.append(f"  ✗ 登录查询失败:{e}")

    try:
        st = yd.state()
        task = st.get("task") or {}
        running = task.get("running") or {}
        lines.append(
            f"  {'⚠' if yingdao.is_placeholder_state(st) else '✓'} 状态:模式 "
            f"{st.get('currentModeDisplayName') or st.get('mode')},队列 "
            f"{task.get('queueState')},在跑 "
            f"{(running.get('appName') or running.get('taskId')) if running else '无'}"
            + (" —— ⚠ 这是渲染进程没回应时的占位结果,不能当空闲"
               if yingdao.is_placeholder_state(st) else ""))
    except (yd.YingdaoUnreachable, yd.YingdaoNoResponse, yd.YingdaoApiError) as e:
        lines.append(f"  ✗ 状态查询失败:{e}")

    if uuid:
        try:
            found = _find_app(uuid)
            lines.append(f"  {'✓' if atype in found else '✗'} 应用列表里查到这个 UUID 的类型:"
                         f"{'/'.join(found) or '都查不到'}"
                         + ("" if atype in found or not found else
                            f" —— 把 YINGDAO_APP_TYPE 设成 {found[0]}"))
            d = yd.app_detail(uuid, atype)
            lines.append(f"  ✓ 应用详情({atype}):「{d.get('appName')}」版本 {d.get('version')},"
                         f"类型 {d.get('uiaType')},接口入参 {len(d.get('inputs') or [])} 个")
        except (yd.YingdaoUnreachable, yd.YingdaoNoResponse, yd.YingdaoApiError) as e:
            lines.append(f"  ✗ 应用核对失败:{e}")

    ready = yingdao.check_ready(uuid) if uuid else None
    lines.append("  结论:" + ("可以提交" if uuid and ready is None
                             else (ready.line() if ready else "UUID 未配置,不能提交")))

    if not do_run:
        return "\n".join(lines)
    lines.append(_input_line())
    if params.get("dry_run"):
        return "\n".join(lines + ["🧪 [DRY-RUN] 不提交。去掉 --dry-run 才真跑(扣一次额度)"])
    if not paths.yingdao_input_file().exists():
        return "\n".join(lines + ["  ✗ 没有 input.json,不提交(先跑 daily_report -p phase=kpi "
                                  "-p yingdao=input 生成清单)"])
    trigger = datetime.now(timezone.utc)
    logger.info("yingdao_probe 真提交一次影刀任务")
    res = yingdao.run_robot(trigger)
    return "\n".join(lines + [f"  真提交结论:{res.line()}"])
