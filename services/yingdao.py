"""影刀 RPA 衔接积木(daily_report 用;仅 macOS 生产机有效)。

衔接是**两个文件**,不经飞书(所有者定稿 2026-08-15,新影刀应用):
    本仓 write_input() → input.json → 影刀读它决定抓哪些卖家页
    → 影刀写 latest.json → 本仓 wait_fresh()/读取回填
旧路径「先把总览写飞书 → 影刀读飞书拿 sellerId」同期删除,不留双轨。
反转的理由是**新鲜度是被保证的而不是碰巧的**:input.json 由 spawn 它的
那一次运行在 spawn 前几秒写出,影刀物理上读不到隔夜的清单;若让影刀自己
连库,它在任何时刻都能读到任意一天的数据,而 cli.py 的 flock 管不到它。

旧系统实证规则全部照搬(docs/legacy_survey.md #daily_report):
- spawn 用 shadowbot:Run?robot-uuid=<uuid> 协议 URL 非阻塞启动,**拿影刀主
  程序绝对路径直启**(registry.paths.yingdao_app(),env YINGDAO_APP 覆盖)。
  ⚠ 不走 `open <协议URL>`:调度沙箱里 open 要经 Launch Services 分发,被沙箱
  边界拦下退 1(2026-08-24 生产实证);旧 walmart-kpi-daily 直启主程序是
  日志验证过的路。应用必须已在「我获取的应用」跑过一次(首次有授权弹窗)
- 新鲜度校验:latest.json 的 scraped_at 必须晚于本次触发时刻,旧数据继续等
  (这同时是防重:影刀已在跑时绝不能再 spawn,两次互抢会让校验反复失败到超时)
- 超时 600s / 轮询 15s(env YINGDAO_TIMEOUT_SEC / YINGDAO_POLL_INTERVAL 可调);
  超时不是错误——调用方降级用旧数据(卖家名称已有跨日延续兜底)
- latest.json 路径经 registry.paths.frontend_scrape_file()(影刀应用内部写死
  自己的输出路径,env FRONTEND_SCRAPE_JSON 必须指到同一文件)

⚠ 并跑纪律:旧系统 8 点调度也会 spawn 影刀。新系统开 yingdao=1 之前必须
先停旧 walmart-kpi-daily,严禁两边同天各 spawn 一次。
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

from registry import paths
from services import kpi, stores

logger = logging.getLogger("services.yingdao")


def _robot_uuid() -> str:
    return os.environ.get("YINGDAO_ROBOT_UUID", "").strip()


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


def spawn() -> bool:
    """输入:无 → 输出:是否成功发出启动指令(非阻塞,不代表 RPA 跑完)。

    直启主程序、协议 URL 作 argv,与旧 walmart-kpi-daily 同款(旧日志实证
    能拉起 Runner 窗口)。**不做 `open` 兜底**:两条启动路只会让"哪条路
    在跑"变成猜谜,沙箱里 open 恒失败,兜它等于每天多一次注定失败的调用。
    Popen 不 wait:影刀是独立应用,跑完与否由 wait_fresh() 按 latest.json
    的新鲜度判,不看进程退出码。
    """
    uuid = _robot_uuid()
    if not uuid:
        logger.warning("YINGDAO_ROBOT_UUID 未配置,跳过影刀启动")
        return False
    app = paths.yingdao_app()
    if not app.exists():
        logger.warning("影刀主程序不存在:%s(装在别处就设 env YINGDAO_APP)", app)
        return False
    try:
        subprocess.Popen([str(app), f"shadowbot:Run?robot-uuid={uuid}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logger.warning("影刀启动失败: %s", e)
        return False


def is_fresh(data: dict, trigger_utc: datetime) -> bool:
    """输入:latest.json 内容 + 触发时刻(UTC)→ 输出:scraped_at 是否晚于触发。"""
    try:
        scraped = datetime.fromisoformat(str(data.get("scraped_at")))
        return scraped.astimezone(timezone.utc) > trigger_utc
    except (TypeError, ValueError):
        return False


def wait_fresh(trigger_utc: datetime) -> bool:
    """输入:触发时刻(UTC)→ 输出:超时前是否等到新鲜的 latest.json。"""
    timeout = int(os.environ.get("YINGDAO_TIMEOUT_SEC", "600"))
    poll = int(os.environ.get("YINGDAO_POLL_INTERVAL", "15"))
    path = paths.frontend_scrape_file()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if is_fresh(data, trigger_utc):
                return True
        except (OSError, ValueError):
            pass        # 文件没出现/写到一半,继续等(旧系统 :1108 同款防御)
        time.sleep(poll)
    logger.warning("影刀 %ds 内未产出新鲜数据,降级沿用旧值(卖家名称有跨日延续)",
                   timeout)
    return False
