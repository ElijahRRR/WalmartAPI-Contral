"""影刀 RPA 衔接积木(daily_report 用;仅 macOS 生产机有效)。

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

logger = logging.getLogger("services.yingdao")


def _robot_uuid() -> str:
    return os.environ.get("YINGDAO_ROBOT_UUID", "").strip()


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
