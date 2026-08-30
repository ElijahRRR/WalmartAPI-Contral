"""store_watch — 店铺事件账本的**消费端**:高危扫描 → 飞书推送 → 标已推。

用法:
  python cli.py store_watch --dry-run      # 只扫描列出:不比对治理、不推送、不标记 ← 先跑这个
  python cli.py store_watch                # 一整轮:治理快照 diff → 扫高危 → 推 → 标
  python cli.py store_watch -p seed=1      # **首次上线专用**:不推送直接标记(吞掉历史存量)
  python cli.py store_watch -p hours=72 -p limit=100
  python cli.py store_watch -p severity=mid   # 临时看中危;⚠ 别写进调度(mid 每天几十条)

定稿依据(2026-08-30 所有者需求「店铺维度病历 + TRO 封店预警」):
  账本 `ops.store_events` 只追加,**推送是它唯一的"叫醒人"出口**。写入方
  (daily_report / product_audit / order_audit / store_config / 五条运营链)一律
  只落行不发通知 —— 谁发谁就得各自实现一套去重与限流,而且同一次封店会从三个
  地方各响一次。本工作流是那一个出口:每小时扫一遍未推送的高危,一轮一条消息。

DANGEROUS = False 的依据:不碰沃尔玛任何写接口;写库只有两处 ——
  ① `notified_at` 打标(幂等,只写 NULL 行);② 治理快照 diff 产出的事件
  (只追加,同一份配置比两次不会多产)。整轮可重跑。

四个陷阱(每一条都对应一种"预警看起来在跑,其实没人被叫醒"):

  ① **DANGEROUS=False ⇒ cli 把 execute 恒置真**(它没有沃尔玛写接口可关),
     `--dry-run` 是单独透传的那个键。只看 execute 的话 `--dry-run` 对本工作流
     完全无效而且不报错 —— 照 skill_export/maintenance_scan 的写法自己认。
  ② **推送失败绝不标记**:notify 返回 False(webhook 没配 / 被拒)时整轮不动
     `notified_at`,摘要明说"未发出,下轮重试"。标了就等于把这些事件永久
     埋掉 —— 账本只追加,没有第二条补给线会再提醒一次(daily_report 的
     `_phase_push` 同款纪律:摘要是人眼闸门,不许自我美化)。
  ③ **时间窗是双刃**:`hours=48` 挡的是首轮上线被几个月历史高危刷屏,代价是
     **超窗还没推出去的高危就永远推不出去了**。所以窗口外的滞留数必须单独
     报(恒 0 不打,>0 就是有漏网的)。首次上线用 `-p seed=1` 把存量一次标掉,
     之后窗口只用来兜"推送连着失败几天"这种情况。
  ④ **一轮一条消息,不是一事件一条**:49 家店同时被封时,逐条发会把飞书刷到
     没人看。明细行进同一条消息;超过 `limit` 的在摘要里报"还有 N 条待推",
     下一轮(每小时)接着推。

TRO 组合(`store_events.tro_signature`):同店同日出现「封店」+「资金冻结」两条
high 是 TRO 冻结的典型形状 —— 两条事件照记两条,这里只负责认出组合、把话说重。
"""

import logging

from api import feishu
from registry import db
from services import store_config, store_events

DANGEROUS = False

logger = logging.getLogger("workflows.store_watch")

#: severity 码 → 人话(推送文案里不出现英文码)
_SEV_WORD = {"high": "高危", "mid": "中危", "info": "普通"}

_DEFAULTS = {"severity": "high", "hours": 48, "limit": 50}


def _flag(v) -> bool:
    return str(v or "").lower() in {"1", "true", "yes"}


def _pos_int(params: dict, key: str) -> int:
    """输入:params + 键 → 输出:正整数(缺省见 _DEFAULTS;非法直接抛)。

    抛而不是回落缺省:`-p hours=48h` 这种写错的值若被静默当成 48,人会以为
    自己改的窗口生效了,而它一直是缺省值 —— 参数错要在第一时间看见。
    """
    raw = params.get(key)
    if raw in (None, ""):
        return _DEFAULTS[key]
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{key} 要正整数,收到:{raw!r}") from None
    if n <= 0:
        raise ValueError(f"{key} 要正整数,收到:{n}")
    return n


# ── 第一段:治理快照 diff ────────────────────────────────────────────────────

def _phase_config(conn, execute: bool) -> tuple[int, str, str | None]:
    """输入:连接 + 是否真跑 → 输出:(本轮治理事件数, 摘要行, 告警行或 None)。

    ⚠ dry-run **整段跳过**:比对本身会写事件行并推进 ops.cursors 里的快照,
    而 dry-run 的约定是不写任何东西。跳过零损失 —— 快照没被推进,下一次真跑
    比的还是同一版,同样的变化一条都不会丢。

    飞书读不到时 `check_and_record` 自己已经守住了"不产事件不覆盖快照"
    (纪律 ②),这里只负责把它的警告端到摘要上。
    """
    if not execute:
        return 0, "治理快照:dry-run 未比对(比对会写事件与快照,留给真跑)", None
    events, warn = store_config.check_and_record(conn)
    if warn:
        return 0, "治理快照:本轮未比对", warn
    highs = sum(1 for e in events if e["severity"] == "high")
    if not events:
        return 0, "治理快照:无变更", None
    line = f"治理快照:{len(events)} 条变更"
    if highs:
        line += f"(其中 high {highs} 条,本轮随高危一起推)"
    line += ";".join([""] + [store_events.brief(e) for e in events[:5]])
    if len(events) > 5:
        line += f" …另 {len(events) - 5} 条"
    return len(events), line, None


# ── 第二段:扫描 → 推送文案 ─────────────────────────────────────────────────

def _headline(rows: list[dict], tro: list[str], severity: str) -> str:
    """输入:本轮扫到的行 + TRO 命中店 + 级别 → 输出:结论主体(不含前缀图标)。"""
    stores = {r["store"] for r in rows if r.get("store")}
    n_global = sum(1 for r in rows if not r.get("store"))
    body = (f"{len(stores)} 店 {len(rows) - n_global} 条"
            f"{_SEV_WORD.get(severity, severity)}")
    if n_global:
        # 全局行(TRO 品牌源头)不属于任何一家店,店数里数不到它 ——
        # 不点出来的话"0 店 1 条"看着像 bug。⚠ 它也**不能算进店铺那个数**:
        # 算进去就成了"1 店 3 条,另全局 1 条"(3 里已经含着那 1 条),
        # 人按这两个数去库里对,永远对不上
        body += f",全局 {n_global} 条"
    if tro:
        body += f"({len(tro)} 店疑似 TRO 封店)"
    return body


def _detail_lines(rows: list[dict], tro: list[str]) -> list[str]:
    """输入:本轮扫到的行 + TRO 命中店 → 输出:结论行之后的明细行。"""
    lines = []
    if tro:
        lines.append(f"🚨 疑似 TRO 封店:{'、'.join(tro)}(封店 + 资金冻结同日出现)")
    return lines + [f"· {store_events.brief(r)}" for r in rows]


# ── 一轮 ────────────────────────────────────────────────────────────────────

def run(params: dict) -> str:
    """输入:params(severity/hours/limit/seed)→ 输出:本轮摘要(第一行结论)。"""
    # 陷阱 ①:DANGEROUS=False 时 cli 的 execute 恒真,--dry-run 得自己认
    execute = bool(params.get("execute")) and not params.get("dry_run")
    seed = _flag(params.get("seed"))
    severity = str(params.get("severity") or _DEFAULTS["severity"]).strip()
    if severity not in store_events.SEVERITIES:
        return (f"severity 只接受 {'/'.join(store_events.SEVERITIES)},"
                f"收到:{severity!r}")
    hours = _pos_int(params, "hours")
    limit = _pos_int(params, "limit")
    word = _SEV_WORD.get(severity, severity)

    tail: list[str] = []        # 结论行之后的行(明细、滞留、治理、警告)
    with db.pg_conn() as conn:
        _n_gov, gov_line, gov_warn = _phase_config(conn, execute)
        # 治理段刚落的 high 事件,下面这一扫就扫得到(同一事务,同一轮推出去)
        rows = store_events.scan_unnotified(conn, severity, hours, limit)
        n_window, n_stale = store_events.unnotified_counts(conn, severity, hours)
        tro = store_events.tro_stores(rows)

        if not rows:
            head = f"店铺预警:无待推送{word}(窗口 {hours}h)"
        else:
            body = _headline(rows, tro, severity)
            tail += _detail_lines(rows, tro)
            ids = [r["id"] for r in rows]
            if not execute:
                head = f"🧪 [DRY-RUN] 店铺预警:{body} —— 未推送未标记"
            elif seed:
                # seed 反过来:不推送直接标记。首次上线把存量吞掉,免得第一条
                # 消息是几百条几个月前的历史(而真正今天那条埋在里面)
                n = store_events.mark_notified(conn, ids)
                head = (f"店铺预警:{body} —— seed 标记 {n} 条"
                        f"(不推送,首次上线吞存量)")
            elif feishu.notify("\n".join([f"🚨 店铺预警:{body}"]
                                         + _detail_lines(rows, tro))):
                n = store_events.mark_notified(conn, ids)
                head = f"🚨 店铺预警:{body} —— 已推送并标记 {n} 条"
            else:
                # 陷阱 ②:一条都不标,下一轮原样重来
                logger.error("飞书未发出,本轮 %d 条%s不标记(下轮重试)",
                             len(rows), word)
                head = (f"⚠ 店铺预警:{body} —— **未发出**"
                        f"(飞书未配置或推送被拒),本轮不标记,下轮重试")
            remaining = max(0, n_window - len(rows))
            if remaining:
                tail.append(f"⚠ 还有 {remaining} 条待推(本轮 limit={limit}),"
                            f"下一轮接着推")

    if n_stale:
        # 陷阱 ③:窗口外滞留恒 0 不打,>0 说明有漏网的,人该去看为什么
        tail.append(f"⚠ 窗口外滞留 {n_stale} 条未推送{word}(早于 {hours}h,"
                    f"本轮扫不到)—— 确认原因后用 -p hours=N 放宽窗口补推")
    tail.append(gov_line)
    if gov_warn:
        tail.append(gov_warn)
    return "\n".join([head] + tail)
