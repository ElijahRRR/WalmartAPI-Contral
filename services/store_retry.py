"""店级「跑完别人再串行补试一遍」公共积木(所有者定稿 2026-08-26 重试标准①)。

标准全文四步(出处 docs/plan.md 2026-08-26 行):
  ① 并发/循环跑完全部店 → 失败店**串行**补试一遍(本模块);
  ② 补试仍失败 → **不炸整轮**:工作流照常报成功,摘要首行点名缺席店;
  ③ 下游店维工作流按水位避让缺席店(services/store_absence);
  ④ 链尾对缺席店逐店重跑完整链一次(cli.py),再失败即止。

「跑完别人再补试」与 #91 的「重试的等到完整跑完一轮再尝试」(所有者原话,
api/feeds submit_feed 头注)同源:失败时补打进的正是造成失败的那片拥堵,
整轮跑完之后管子已经空了。串行而非并发,同理 —— 第一轮既然证明了这个
时段/这批代理在抖,补试就没有理由再齐射一遍。
"""

import logging
import time

from api import _client

logger = logging.getLogger("services.store_retry")


def serial_second_pass(failures: list[tuple], attempt) -> tuple[list, list]:
    """输入:[(store_dict, 首轮异常)] + 单店执行函数 → 输出:(救回 [(store, 结果)], 仍失败 [(store, 异常)])。

    · StoreDeadError **不补试**:凭证死是确定性的,重试只会再死一次
      (problem_product_cleanup 二轮重试的同款判据);
    · 其余(代理故障/超时/泛化)各补试**一次**,店间串行,每店前
      `_client.backoff(0)` 抖动等待 —— 复用 #91 官方阶梯,不造第二套;
    · `attempt(store)` 必须是**第一轮同一个函数**(单一落地路径纪律:
      重试轮另写简化版,迟早漏掉一半落地动作 —— #91 在 _ok_result 上
      栽过这一跤后钉死的规矩)。
    """
    recovered, still = [], []
    for store, first_err in failures:
        name = store.get("name", "?") if isinstance(store, dict) else str(store)
        if isinstance(first_err, _client.StoreDeadError):
            still.append((store, first_err))
            continue
        time.sleep(_client.backoff(0))
        try:
            recovered.append((store, attempt(store)))
            logger.info("店级补试救回:%s(首轮:%s: %s)", name,
                        first_err.__class__.__name__, first_err)
        except Exception as e:                      # noqa: BLE001 —— 补试是最后一搏,
            still.append((store, e))                # 任何失败都只收进缺席名单,不再传播
            logger.warning("店级补试仍失败:%s: %s: %s", name,
                           e.__class__.__name__, e)
    return recovered, still


def classify(err: Exception) -> str:
    """输入:店级异常 → 输出:摘要用的归类词(凭证/代理/其他)。

    枚举而非 catch-all(CLAUDE.md:兜底触发条件明确;分类词进摘要首行,
    人要靠它决定去修凭证表还是去看代理商)。
    """
    if isinstance(err, _client.StoreDeadError):
        return "凭证"
    if isinstance(err, _client.PROXY_ERRORS):
        return "代理"
    return "其他"
