"""店级「跑完别人再串行补试一遍」公共积木(所有者定稿 2026-08-26 重试标准①)。

标准全文四步(定稿全文在 CLAUDE.md 工程规范「店维工作流的失败处理」条):
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
import re
import time

from api import _client

logger = logging.getLogger("services.store_retry")


def serial_second_pass(failures: list[tuple], attempt,
                       total_stores: int | None = None
                       ) -> tuple[list, list, str]:
    """输入:[(store_dict, 首轮异常)] + 单店执行函数(+总店数)→ 输出:(救回, 仍失败, 规模闸说明)。

    · StoreDeadError **不补试**:凭证死是确定性的,重试只会再死一次
      (problem_product_cleanup 二轮重试的同款判据);
    · 其余(代理故障/超时/泛化)各补试**一次**,店间串行,每店前
      `_client.backoff(0)` 抖动等待 —— 复用 #91 官方阶梯,不造第二套;
    · `attempt(store)` 必须是**第一轮同一个函数**(单一落地路径纪律:
      重试轮另写简化版,迟早漏掉一半落地动作 —— #91 在 _ok_result 上
      栽过这一跤后钉死的规矩);
    · **规模闸**(2026-08-26 对抗校验):失败店数超过 max(3, 总数//5) 判
      系统性故障(代理商区域挂了/网络出口出事)—— 串行补试只会把故障时长
      按店数放大(每小时的链会拖过整点、下一轮拿不到锁),此时一家都不补,
      全部按仍失败返回并给出说明(第三个返回值,调用方必须放进摘要)。
      调用方给不出总数时闸不生效(闸是止损优化,不是正确性前提)。
    """
    if total_stores and len(failures) > max(3, int(total_stores) // 5):
        note = (f"⚠ 失败 {len(failures)}/{total_stores} 店超过补试规模闸"
                f"(max(3, 总数//5)),疑似系统性故障(代理商/网络出口),"
                f"本轮不逐店补试 —— 修好根因后手动重跑")
        logger.warning("%s", note)
        return [], list(failures), note
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
    return recovered, still, ""


# 「代理无效」的判据:代理服务器**明确拒绝**了我们的接入(认证失败/需要
# 认证),这是配置错,改凭证表的代理账号密码才能好。消息样式来自
# httpcore/socksio 源码实况("Invalid username/password"、认证协商失败、
# HTTP 代理 407)。判不准的一律归「代理波动」而不是「无效」——
# 无效是叫人去改配置,指错路比不指更糟。
_PROXY_DEAD_MARKS = ("username/password", "auth", "407")


def diagnose(err: Exception) -> str:
    """输入:店级异常 → 输出:摘要用的归类词(一眼指路,所有者要求 2026-08-26)。

    词表(枚举,别往里加 catch-all 新词;每个词都对应一条不同的处置路):
      凭证失效   StoreDeadError:沃尔玛拒了这套凭证 → 修凭证表
      代理无效   代理服务器拒绝认证/要求认证 → 修凭证表的代理账号密码
      代理波动   SOCKS/隧道层其他故障(Malformed reply/断线/握手失败)
                 → 找代理商;补试与链尾重赛常能自愈
      沃尔玛NNN  沃尔玛端点回了 HTTP NNN(429=配额、5xx=沃尔玛侧故障)
                 → 看配额/等沃尔玛,与本地无关
      网络未达   请求经 api 层自动重试后仍没打到(状态码 None)
                 → 网络/代理链路,看该店代理与出口
      其他       以上都不是 → 看该工作流日志全文

    ⚠ 「沃尔玛NNN/网络未达」靠匹配 **api 层自家的报错格式**(items.py 等的
    "返回 {status}"/"返回 None"),改那边的文案要同步这里(有测试钉住两端)。
    分诊只影响**报告**,不影响重试行为(补试与否只看 StoreDeadError 类型)。
    """
    if isinstance(err, _client.StoreDeadError):
        return "凭证失效"
    if isinstance(err, _client.PROXY_ERRORS):
        msg = str(err).lower()
        if any(mark in msg for mark in _PROXY_DEAD_MARKS):
            return "代理无效"
        return "代理波动"
    text = str(err)
    m = re.search(r"返回 (\d{3})", text)
    if m:
        return f"沃尔玛{m.group(1)}"
    if "返回 None" in text:
        return "网络未达"
    return "其他"
