"""店级「跑完别人再串行补试一遍」公共积木(所有者定稿 2026-08-26 重试标准①)。

标准全文四步(规则条目在 CLAUDE.md 工程规范,展开全文在 docs/conventions.md §四):
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
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def fan_out(store_list: list[dict], attempt, workers: int,
            log_label: str = "") -> tuple[list, list, list, str]:
    """输入:店铺列表 + 单店执行函数 → 输出:(结果, 凭证死店名, 缺席, 规模闸说明)。

    缺席 = `[(店名, 归类词)]`(归类词唯一出处 `diagnose`);规模闸说明为空串
    或止损原文(调用方必须放进摘要,见 serial_second_pass 第四条)。

    标准①②的落地骨架(所有者定稿 2026-08-26,展开全文 docs/conventions.md §四):
    跨店并发 → StoreDeadError 当场归 dead → 其余异常先收着**不判生死** →
    跑完别人再 `serial_second_pass` 串行补试一遍 → 仍失败的按 `diagnose`
    分流(凭证失效归 dead 口径,其余归 absent)。

    · `attempt(store)` 必须是**第一轮同一个函数**:补试跑的就是它,不另写
      简化版(单一落地路径纪律,见 serial_second_pass 第三条);
    · `log_label` 只进日志(如「同步」/「售后同步」),不进任何摘要/通知;
    · 本函数只回答「谁成功、谁凭证死、谁缺席」。**零店完成闸、摘要首行拼装、
      gate_note 落哪一行、缺席店后续怎么避让,全部留在调用方** —— 那些各件
      本来就不同(catalog_sync 有 strict 闸、returns_sync 的缺口靠下轮窗口
      覆盖)。

    收编自 catalog_sync 与 returns_sync 两处逐字相同的实现,语义以
    catalog_sync 现行代码为准。⚠ **不收编** settlement_sync/perf_problems/
    daily_report:那三处只 diagnose、故意没有串行补试,折进来等于给它们加
    重试(行为变更,不是重构)。
    """
    results: list = []
    dead: list[str] = []
    to_retry: list[tuple] = []
    absent: list[tuple[str, str]] = []          # (店名, 归类词:代理/其他/凭证)
    gate_note = ""
    if not store_list:      # 不开 0 个线程;「零店完成不许报成功」是调用方的闸
        return results, dead, absent, gate_note
    by_name = {s["name"]: s for s in store_list}
    with ThreadPoolExecutor(max_workers=min(workers, len(store_list))) as pool:
        futures = {pool.submit(attempt, s): s["name"] for s in store_list}
        for f in as_completed(futures):
            name = futures[f]
            try:
                results.append(f.result())
            except _client.StoreDeadError as e:
                # 凭证死是确定性的:跳过全店,不补试(重试只会再死一次)
                logger.error("%s", e)       # 异常自带店名,不重复拼
                dead.append(name)
            except Exception as e:
                # 代理故障(StoreProxyError/socksio)与泛化异常都先收着 ——
                # 标准①(所有者定稿 2026-08-26):跑完别人再串行补试,
                # 此刻不判生死。08-26 13:00 的事故正是在这里把两家店的
                # SOCKS 报错直接判成 failed → 整轮 raise → 八步链全停
                logger.exception("店铺 %s %s失败(待串行补试): %s",
                                 name, log_label, e)
                to_retry.append((by_name[name], e))

    # 标准①:失败店串行补试一遍。单一落地路径 —— 补试跑的就是第一轮
    # 同一个 attempt,不另写简化版
    if to_retry:
        recovered, still, gate_note = serial_second_pass(
            to_retry, attempt, total_stores=len(store_list))
        results.extend(r for _s, r in recovered)
        for s, e in still:
            cls = diagnose(e)
            if cls == "凭证失效":                    # 补试中才暴露的凭证死,归 dead 口径
                dead.append(s["name"])
            else:
                absent.append((s["name"], cls))
    return results, dead, absent, gate_note
