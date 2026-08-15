"""影刀 RPA 衔接积木(daily_report 用;仅 macOS 生产机有效)。

衔接是**两个文件**,不经飞书(所有者定稿 2026-08-15,新影刀应用):
    本仓 write_input() → input.json → 影刀读它决定抓哪些卖家页
    → 影刀写 latest.json → 本仓 wait_fresh()/读取回填
旧路径「先把总览写飞书 → 影刀读飞书拿 sellerId」同期删除,不留双轨。
反转的理由是**新鲜度是被保证的而不是碰巧的**:input.json 由 spawn 它的
那一次运行在 spawn 前几秒写出,影刀物理上读不到隔夜的清单;若让影刀自己
连库,它在任何时刻都能读到任意一天的数据,而 cli.py 的 flock 管不到它。

旧系统实证规则全部照搬(docs/legacy_survey.md #daily_report):
- spawn 用 shadowbot:Run?robot-uuid=<uuid> 协议 URL 非阻塞启动(macOS `open`);
  应用必须已在「我获取的应用」跑过一次(首次有授权弹窗),路径 /Applications/影刀.app
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
from services import stores

logger = logging.getLogger("services.yingdao")


def _robot_uuid() -> str:
    return os.environ.get("YINGDAO_ROBOT_UUID", "").strip()


# input.json 每行对影刀公开的字段。**store 是主键不是装饰**:sellerId 可能为空
# 或变更,而 latest.json 现仍按 sellerId 回键(格式不动,一次只改一端)。
_INPUT_FIELDS = ("store", "seller_id", "store_status", "payment_status")

# 只放行 sellerStatus 为此值的店(所有者定稿 2026-08-15)。非 ACTIVE 的店前台
# 页面本就没什么可看,抓它纯耗时间和风控额度。
_ACTIVE = "ACTIVE"

_DROP_REASONS = ("no_seller_id", "inactive", "dup_seller_id")


def write_input(rows: list[dict]) -> tuple[int, dict[str, int]]:
    """输入:当轮已入库的 KPI 行 → 输出:(写出店数, {丢弃原因: 店数})。

    整文件覆盖写,只含本轮真跑到的店——不追加、不与上一版合并,所以不会重复。

    三道过滤,**每道都单独计数**(丢弃静默常态化 = 少抓一半没人知道):
    - `no_seller_id` 空 sellerId。⚠ A147 事故防线:影刀拿它拼出
      https://www.walmart.com/seller//cp/shopall(路径中段为空)会崩掉整条
      RPA 循环,**后续店铺全被跳过**——不是少抓一家,是少抓一半。
    - `inactive` store_status 非 ACTIVE。
    - `dup_seller_id` 同一 sellerId 出现多次(店铺凭证表有重复行时会发生),
      抓两遍同一个页面纯属浪费,后者覆盖前者也看不出来。

    被丢掉的店**照常入库**,只是不参与前台抓取;它们当天的销售状态会留空
    (卖家名称有跨日延续兜底)。

    原子写(tmp + rename):影刀随时可能在读,写一半被读到会让它解析失败。
    latest.json 那侧本仓早就在防这件事(读到半截文件继续等),这边同样欠不得。
    """
    path = paths.yingdao_input_file()
    out: list[dict] = []
    seen: set[str] = set()
    drops = dict.fromkeys(_DROP_REASONS, 0)
    for r in rows:
        sid = str(r.get("seller_id") or "").strip()
        if not sid:
            drops["no_seller_id"] += 1
        elif str(r.get("store_status") or "").strip().upper() != _ACTIVE:
            drops["inactive"] += 1
        elif sid in seen:
            drops["dup_seller_id"] += 1
        else:
            seen.add(sid)
            out.append({f: ("" if r.get(f) is None else str(r[f]))
                        for f in _INPUT_FIELDS})
    out.sort(key=lambda r: stores.sort_key(r["store"]))     # 与看板同一店铺序
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "count": len(out), "stores": out}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    logger.info("影刀输入:%d 店写出 %s,丢弃 %s", len(out), path, drops)
    return len(out), drops


def spawn() -> bool:
    """输入:无 → 输出:是否成功发出启动指令(非阻塞,不代表 RPA 跑完)。"""
    uuid = _robot_uuid()
    if not uuid:
        logger.warning("YINGDAO_ROBOT_UUID 未配置,跳过影刀启动")
        return False
    try:
        subprocess.run(["open", f"shadowbot:Run?robot-uuid={uuid}"],
                       check=True, timeout=15, capture_output=True)
        return True
    except Exception as e:
        logger.warning("影刀启动失败(需 macOS + /Applications/影刀.app): %s", e)
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
