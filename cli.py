#!/usr/bin/env python3
"""cli.py — 全项目唯一执行入口。

    python cli.py <workflow> [<workflow> ...] [-p key=value ...] [--dry-run]

统一负责(workflow 文件里不做这些):
  加载 <DATA_ROOT>/.env → flock 单实例锁 → 写 ops.runs 运行记录 → 执行 run(params)
  → 飞书通知(成功/失败都发)→ 退出码(0 成功 / 1 失败 / 3 已有实例在跑)。

**串联(2026-08-16 加,所有者定稿「订单链每个运行后都需要自动同步到飞书,
不要人来手动推」)**:workflow 位置参数可以给多个,按顺序跑,**前一个失败
就不跑后面的**:

    python cli.py order_sync order_audit order_center_push

⚠ 这是本项目实现"链"的**唯一**方式。铁律 1 禁止 workflow 互相 import ——
让 `order_sync` 结尾去调 `order_center_push` 就是把两条工作流焊死:
以后想单跑推送、想换推送目标、想在中间插一步,都得改 `order_sync` 的代码。
串联是**调度的事**,不是工作流的事;cli 本来就管锁/记录/通知,链只是把这
三件事各做 N 遍。

串联的语义(与单跑的差别只在这几条,其余完全一致):
  · 每一步各自拿自己的 flock 锁、各写一行 ops.runs、各写自己的日志文件;
  · 某一步拿不到锁 = 那个工作流真的正在跑,**停链**(退出码 3)——继续往下
    跑会让后面几步吃到半成品数据;
  · 飞书通知**整链一条**,不是一步一条(三步链每小时发三条会把群刷废);
  · 全部工作流名在**跑第一步之前**先验一遍,打错一个字不会跑到一半才发现。

  参数:`-p k=v` 发给**每一步**;要只给某一步用 `-p 工作流名:k=v`,例如
  `python cli.py order_sync order_center_push -p order_center_push:days=30`。

执行语义(所有者定稿 2026-08-16 走进生产时改;此前是"危险工作流缺省 dry-run,
真跑要 --execute"):**缺省即真跑**,危险工作流也一样;要空跑加 `--dry-run`。
理由:进了调度之后,"缺省 dry-run"这条防线只会伤到自己 —— launchd 里漏写一个
`--execute` 的后果是**那条链每天空转而且报成功**,比误跑更难发现。

⚠ 但「AI 改完代码必须先 dry-run,人眼确认输出后才真跑」这条**没有取消**,
只是从"默认值兜底"改成"纪律"。改完危险工作流的代码,第一次跑必须显式
`--dry-run`,输出人眼过一遍,再跑真的。--execute 保留为兼容别名(调度里
写了它也不会出错),但它现在是空操作。

launchd 定时、手动触发、未来的网页按钮和 MCP 工具,全部走这一条路径;
调用方身份用环境变量 WALMART_OPERATOR 标注(launchd / manual / web / mcp)。
"""

import argparse
import contextlib
import importlib
import logging
import sys
import traceback
from pathlib import Path

logger = logging.getLogger("cli")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cli.py", description="WalmartAPI-Contral 唯一执行入口")
    parser.add_argument("workflow", nargs="+",
                        help="workflows/ 下的工作流名(可给多个,按顺序串联)")
    parser.add_argument("-p", "--param", action="append", default=[],
                        metavar="key=value",
                        help="传给 run(params) 的参数,可重复;"
                             "串联时 `工作流名:key=value` 只发给那一步")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="空跑:只打印将做什么,不碰任何写接口")
    parser.add_argument("--execute", action="store_true",
                        help="(兼容保留,空操作)缺省即真跑;要空跑用 --dry-run")
    return parser.parse_args(argv)


# 参数值里混进了本该独立成词的开关。成因几乎总是**分隔符不是普通空格**
# ——从聊天/网页复制命令时容易带上不换行空格(U+00A0)等,shell 不把它当
# 分词符,于是 `-p k=v --execute` 整串进了 v。
# ⚠ **只报错,绝不"帮你"把它解释成开关**:那等于让一个粘贴事故把 dry-run
#    变成真跑。危险工作流的 --execute 必须是人显式敲进去的。
_FLAGS = ("--execute", "-p", "--param", "-h", "--help", "--dry-run")


def _build_params(pairs: list[str], steps: list[str]) -> dict[str, dict]:
    """输入:-p 原文 + 本次要跑的工作流名 → 输出:{工作流名: 该步的 params}。

    `k=v` 发给每一步;`步骤名:k=v` 只发给那一步(串联时 order_center_push
    要 days=30 而 order_sync 不认识这个键,共用一份会把无关参数塞满每一步的
    ops.runs 记录,排障时分不清哪个参数是给谁的)。

    ⚠ 前缀只在**冒号前那段恰好是本次某个工作流名**时才生效 —— 否则值里带
    冒号的普通参数(URL、时间)会被误切。
    """
    out: dict[str, dict] = {s: {} for s in steps}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"参数格式错误(应为 key=value): {item}")
        k, _, v = item.partition("=")
        k, v = k.strip(), v.strip()
        # split() 按任意空白切,包括 U+00A0 —— 正是要抓的那种
        stuck = [w for w in v.split() if w in _FLAGS]
        if stuck:
            raise SystemExit(
                f"参数值里粘进了开关 {' '.join(stuck)}:\n"
                f"    -p {k}={v}\n"
                f"  多半是路径与开关之间那个空格不是普通空格(从聊天/网页复制\n"
                f"  常带不换行空格)。把该处空格重敲一遍,或给值加引号:\n"
                f"    -p \"{k}={v.split()[0]}\" {' '.join(stuck)}\n"
                f"  ⚠ 本命令**没有执行**——不会替你把它当成开关,免得一次粘贴\n"
                f"    事故把 dry-run 变成真跑。")
        scope, sep, rest = k.partition(":")
        if sep and scope in out:
            out[scope][rest.strip()] = v
        else:
            for st in out:
                out[st][k] = v
    return out


class _NotOnScreen(logging.Filter):
    """带 `file_only=True` 的记录只进日志文件,不上终端。

    摘要要**同时**满足两个需求:终端上干干净净出现一次(人在看),日志文件里
    留全文(事后查"那次到底输出了什么",这是唯一能回答的地方)。少了过滤器
    就只能二选一 —— 要么终端刷两遍,要么日志里没有摘要。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


def dup_env_keys(text: str) -> dict[str, list[str]]:
    """输入:.env 全文 → 输出:{重复出现的键: [每次的值]}(纯函数,可测)。

    ⚠ dotenv **顺序读取、后者覆盖前者,且不报重复**。生产实证 2026-08-17:
    所有者的 .env 里 FEISHU_RISK_PT_WIKI_TOKEN 等四个键各出现两次,
    第二次是模板留下的空值,于是那四张表全部读成"未登记" ——
    值明明就在文件里,肉眼看过去也在,报错却说没配。这种错自己找不到。
    """
    seen: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        if k:
            seen.setdefault(k, []).append(v.strip())
    return {k: vs for k, vs in seen.items() if len(vs) > 1}


def _warn_dup_env(env_file: Path) -> None:
    """.env 有重复键就吼一声 —— 尤其是"后面那个是空值"的情况。"""
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    for k, vs in dup_env_keys(text).items():
        blanked = (not vs[-1]) and any(vs[:-1])
        msg = ("⚠ .env 里 %s 出现 %d 次,**最后一次是空值,把前面的覆盖了**"
               "(dotenv 后者覆盖前者且不报重复)——删掉那行空的"
               if blanked else "⚠ .env 里 %s 出现 %d 次,最后一次生效")
        print(msg % (k, len(vs)), file=sys.stderr)


def _setup_logging(logs_dir: Path) -> None:
    """根 logger 只配一次(stderr);每步的文件 handler 由 _log_to 挂/摘。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    screen = logging.StreamHandler(sys.stderr)
    screen.addFilter(_NotOnScreen())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[screen],
    )
    # ⚠ basicConfig 在"根 logger 已有 handler"时是**静默空操作**(某个 import
    # 抢先配过就会这样)。级别单独钉一遍 —— 否则根还停在 WARNING,
    # 每步的 INFO 日志在到达文件 handler 之前就被过滤掉,日志文件是空的而且不报错。
    logging.getLogger().setLevel(logging.INFO)


@contextlib.contextmanager
def _log_to(workflow: str, logs_dir: Path):
    """每步日志各进各的文件(logs/<workflow>.log)——串联不改这条约定。

    人排障是按工作流名去 logs/ 下找的;串联要是写进一个合并文件,
    `logs/order_center_push.log` 里就再也看不到定时跑的那些轮次了。
    """
    h = logging.FileHandler(logs_dir / f"{workflow}.log", encoding="utf-8")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(h)
        h.close()


def _record_start(workflow: str, params: dict, operator: str):
    """写 ops.runs 起始行,返回 run_id;数据库不可用只告警,不阻断工作流。"""
    try:
        from psycopg.types.json import Jsonb

        from registry import db
        with db.pg_conn() as conn:
            row = conn.execute(
                "INSERT INTO ops.runs (workflow, params, started_at, status, operator) "
                "VALUES (%s, %s, now(), 'running', %s) RETURNING id",
                (workflow, Jsonb(params), operator),
            ).fetchone()
            return row[0]
    except Exception as e:
        logger.warning("ops.runs 起始记录写入失败(不阻断): %s", e)
        return None


def _record_finish(run_id, status: str, summary: str) -> None:
    if run_id is None:
        return
    try:
        from registry import db
        with db.pg_conn() as conn:
            conn.execute(
                "UPDATE ops.runs SET finished_at = now(), status = %s, summary = %s WHERE id = %s",
                (status, summary[:4000], run_id),
            )
    except Exception as e:
        logger.warning("ops.runs 结束记录写入失败(不阻断): %s", e)


_ERR_MAX = 1200          # 飞书消息不宜过长;超长只截尾并标明


def _err_brief(e: BaseException) -> str:
    """输入:异常对象 → 输出:发飞书用的错误正文(**整条消息,不是最后一行**)。

    ⚠ 2026-08-17 生产实见:此前取 `traceback.format_exc().splitlines()[-1]`,
    catalog_sync 一家店 400 时飞书收到的整条通知是

        ❌ catalog_sync 失败
        For more information check: https://developer.mozilla.org/…/400)

    —— 整条消息里最没用的那一行。原因是取"最后一行"只对**单行**异常成立
    (那时最后一行正好是 `类型: 消息`);而本项目的失败摘要恰恰是多行的:
    **当时的** catalog_sync 是「有店失败 → 整体判失败,通知带明细」(该语义
    已于 2026-08-26 被店级重试标准替换成"缺席不炸整轮";多行失败摘要如今
    出自零店闸/strict 闸),那份明细全长在前面几行,被这一刀切光了。
    httpx 的错误自身又是两行、第二行是 MDN 链接,于是越是需要细节的失败,
    通知越是只剩一句废话。

    多行异常(RuntimeError("摘要\\n明细…"))发全文;超长截尾并标明。
    """
    body = str(e).strip() or e.__class__.__name__
    if len(body) > _ERR_MAX:
        body = body[:_ERR_MAX] + f"\n…(已截断,全文见日志 {e.__class__.__name__})"
    return body


def _notify(text: str) -> None:
    try:
        from api import feishu
        feishu.notify(text)
    except Exception as e:
        logger.warning("飞书通知失败(不阻断): %s", e)


def _load_modules(names: list[str]):
    """输入:工作流名 → 输出:{名: 模块};任一个不存在直接 SystemExit(2)。

    ⚠ **在跑第一步之前把全部名字验一遍**:串联里打错第三个名字,一步步 import
    会让前两步(可能是破坏性的)先跑完,然后整链报"未知工作流"。
    """
    mods = {}
    for name in names:
        try:
            mods[name] = importlib.import_module(f"workflows.{name}")
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(name):
                avail = sorted(
                    p.stem for p in (Path(__file__).parent / "workflows").glob("*.py")
                    if not p.stem.startswith("_"))
                print(f"未知工作流: {name}\n可用: {', '.join(avail)}",
                      file=sys.stderr)
                raise SystemExit(2) from None
            raise
    return mods


def _run_step(name: str, module, params: dict, dry_run: bool, operator: str,
              logs_dir: Path) -> tuple[str, str]:
    """输入:一步的名字/模块/参数 → 输出:(status, summary)。

    status ∈ success / failed / locked。锁、ops.runs、日志文件都在这一层,
    单跑与串联走的是同一段代码 —— 串联不是"另一种跑法",只是跑 N 次。
    """
    from services import runlock

    # 本地副本:此前就地改 per_step 的 dict(设 execute、pop lock_wait),
    # 链尾重赛复制到的是被消费过的版本 —— lock_wait 静默丢失,配了等锁的
    # 步骤在重赛里撞锁即退(2026-08-26 对抗校验)。原 dict 谁都不动。
    params = dict(params)

    dangerous = bool(getattr(module, "DANGEROUS", False))
    # 缺省即真跑(所有者定稿 2026-08-16):调度里漏写 --execute 会让整条链每天
    # 空转而且报成功,比误跑更难发现。空跑改为显式 --dry-run。
    # 非危险工作流本来就恒真,不受 --dry-run 影响(它们没有写接口可关)。
    params["execute"] = (not dry_run) if dangerous else True
    # ⚠ dry_run 单独透传:扫描类工作流 DANGEROUS=False(不碰沃尔玛写接口),
    # 但它们**会写建议表**。按"AI 改完代码先 dry-run"的纪律,人会对着扫描件
    # 敲 --dry-run —— 只看 execute 的话那个开关对它们完全无效,而且不报错。
    params.setdefault("dry_run", bool(dry_run))
    mode = "" if not dangerous else ("[DRY-RUN] " if dry_run else "[EXECUTE] ")
    if dangerous and dry_run:
        print(f"🧪 [DRY-RUN] {name} 本次只打印将做什么,不碰写接口")

    # lock_wait 是 cli 保留参数,不透传给 run()(2026-08-19 所有者定稿):
    # 只给调度里显式配了它的那一步用(product_chain 的 product_ingest 撞
    # order_audit 借锁的日常档期);缺省 0 = 立刻退 3,老语义一字不变
    try:
        lock_wait = float(params.pop("lock_wait", 0) or 0)
    except (TypeError, ValueError):
        lock_wait = 0.0

    with runlock.hold(name, wait_secs=lock_wait) as got:
        if not got:
            extra = f"(等锁 {lock_wait:.0f}s 超时)" if lock_wait else ""
            print(f"⚠ {name} 已有实例在运行(flock 占用){extra},本次退出",
                  file=sys.stderr)
            return "locked", f"{name}:已有实例在运行{extra},未执行"
        with _log_to(name, logs_dir):
            run_id = _record_start(name, params, operator)
            try:
                summary = module.run(params)
                summary = str(summary) if summary is not None else "(无摘要)"
            except Exception as e:
                err = traceback.format_exc()
                logger.error("workflow %s 失败:\n%s", name, err)
                _record_finish(run_id, "failed", err[-2000:])
                return "failed", f"{mode}{name} 失败\n{_err_brief(e)}"
            # ★ **硬拒不是成功**。全仓十几处「前提不成立就别跑」的早退都是
            # 普通的 `return "⛔ …"`而不抛异常 —— 那是对的，它不是崩溃，
            # 不该打印 traceback。但原来 cli 原样记 status='success' 并发 ✅，
            # 后果是 **ops.runs 对这几条工作流失去判别力**：
            #   实测 2026-08-16 日志：`workflow alloc_plan 成功: ⛔ 限额表读不到…`
            #   紧跟 `✅ [DRY-RUN] alloc_plan 成功` —— 飞书告警里「什么都没干」
            #   和「分配了 2.8 万条」长得一模一样。
            # 且串联模式说好了「前一个失败就不跑后面」，而拒跑计成成功
            # 会让整条链带着“前提没满足”一路跑到底。
            # 约定：摘要以 ⛔ 开头 = 前提不成立、**什么都没做**。
            if summary.lstrip().startswith(REFUSED_MARK):
                logger.error("workflow %s 硬拒:\n%s", name, summary,
                             extra={"file_only": True})
                print(summary)
                _record_finish(run_id, "refused", summary)
                return "refused", f"{mode}{name} 未执行(前提不成立)\n{summary}"
            # 摘要在终端上只出现一次(下面那句 print);全文进日志文件备查
            logger.info("workflow %s 成功:\n%s", name, summary,
                        extra={"file_only": True})
            print(summary)
            _record_finish(run_id, "success", summary)
            return "success", f"{mode}{name} 成功\n{summary}"


#: 摘要以它开头 = 工作流自己判定「前提不成立,什么都没做」。
#: 工作流写 `return "⛔ …"` 就行,不必招异常。
REFUSED_MARK = "⛔"

_ICON = {"success": "✅", "failed": "❌", "refused": "⛔", "locked": "⚠"}
#: 硬拒与失败同一个退出码:两者对调用方的意义一致 —— **活没干成**,
#: `&&` 串起来的后续步骤都不该再跑。区分在 ops.runs.status 与通知图标上。
_EXIT = {"success": 0, "failed": 1, "refused": 1, "locked": 3}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    steps = list(args.workflow)
    per_step = _build_params(args.param, steps)

    # .env 先于一切业务 import 加载,registry 各函数 call-time 求值即可拿到
    from dotenv import load_dotenv

    from registry import paths
    load_dotenv(paths.env_file(), override=False)
    _warn_dup_env(paths.env_file())

    # cli 只自建它直接需要的 logs/locks;完整 DATA_ROOT 初始化走 init_data_root 工作流
    logs_dir = paths.logs_dir()
    _setup_logging(logs_dir)
    modules = _load_modules(steps)

    import os
    operator = os.environ.get("WALMART_OPERATOR", "manual")
    from datetime import datetime, timezone
    chain_started = datetime.now(timezone.utc)   # 链尾重赛的缺席判据锚点

    results: list[tuple[str, str, str]] = []        # (名, status, 文案)
    for i, name in enumerate(steps):
        status, text = _run_step(name, modules[name], per_step[name],
                                 args.dry_run, operator, logs_dir)
        results.append((name, status, text))
        if status != "success":
            # 停链:后面几步的输入是这一步的输出,继续跑 = 拿半成品数据干活
            # (order_sync 没拉全就推飞书 → 表里少一批订单,而且没人报错)
            for later in steps[i + 1:]:
                results.append((later, "skipped",
                                f"{later}:上游 {name} 未成功,未执行"))
            break

    if len(steps) == 1:
        # 单跑:通知形态与串联前**逐字一致**(人和告警规则都认这个格式)
        _notify(f"{_ICON[results[0][1]]} {results[0][2]}")
        return _EXIT[results[0][1]]

    # 串联:整链一条通知。三步链每小时发三条会把群刷废,而且看不出这几条
    # 属于同一次运行
    worst = next((s for _, s, _ in results if s != "success"), "success")
    text = _chain_text(steps, results, worst)
    # 链尾缺席店重赛(店级重试标准④,所有者定稿 2026-08-26):主链全部成功
    # 且链里含 catalog_sync(缺席判据锚定目录水位)时才有意义;主链没跑完
    # 就重赛是拿半成品数据干活,不做。
    if worst == "success" and "catalog_sync" in steps:
        replay = _replay_absent(steps, modules, per_step, args.dry_run,
                                operator, logs_dir, chain_started)
        if replay:
            text += "\n" + "\n".join(replay)
    _notify(text)
    return _EXIT.get(worst, 1)


#: 链尾重赛的规模闸:今日缺席超过这个数 = 系统性故障(代理商区域挂了/
#: 网络出口出事),逐店重赛只会把工作量按店数放大、把破坏步骤拖进无人时段
#: —— 止损点名,让人去修根因(2026-08-26 对抗校验定稿)。
REPLAY_MAX_STORES = 5


def _replay_absent(steps, modules, per_step, dry_run, operator, logs_dir,
                   since) -> list[str]:
    """输入:主链步骤与参数 + 本轮起点 → 输出:重赛结果行(无缺席店返回 [])。

    店级重试标准④(所有者定稿 2026-08-26):主链跑完后,按目录水位
    (services/store_absence,与调度顺序无关)找出本轮缺席的店,对每家把
    链内**声明 SUPPORTS_STORE 的步骤**带 store=X 逐店重跑一次;某步失败即
    终止该店的重赛(上游语义与主链一致),**再失败即止,不循环** ——
    README 的「失败不要自动重跑」禁的是盲目整链重跑,这里是设计内的、
    逐店限定、单次的重赛。全局步骤(sources_backfill/product_refresh/
    product_audit)主链已全量跑过、不因单店缺席而陈旧,跳过。
    防重不在这一层:各工作流自己的闸(feed_log 在途 / claim 只取 suggested /
    dedupe 20h / cap_destructive 按日记账)照常起作用,重赛不绕任何入口。
    每步照常拿 flock、写 ops.runs、进各自日志 —— 与主链同一段 _run_step。

    四道闸(2026-08-26 对抗校验加,一道都不能少):
      · **单店链不重赛**:主链任何一步带了 store= 范围参数,水位判据看的
        却是全船 —— 会把"没被本次范围覆盖"误判成"缺席",对全船跑破坏步骤;
      · **长期缺席不重赛**(split_stale):凭证死三天的店天天重赛=天天 ❌;
      · **规模闸**:今日缺席 > REPLAY_MAX_STORES 判系统性故障,点名不重赛;
      · **水位复核**:步骤全绿不等于救回,重赛后水位仍没跨过链起点的照实说。
    """
    from registry import db
    from services import store_absence
    if any("store" in (per_step.get(n) or {}) for n in steps):
        return ["—— 缺席店重赛跳过:主链带了 store= 范围参数"
                "(水位判据是全船的,单店链重赛会误伤其余店铺)——"]
    try:
        with db.pg_conn() as conn:
            recent, chronic = store_absence.split_stale(conn, since=since)
    except Exception as e:
        logger.warning("缺席店探测失败,跳过链尾重赛: %s", e)
        return [f"—— 缺席店重赛跳过:探测失败({e.__class__.__name__}),"
                f"见 cli 日志 ——"]
    lines: list[str] = []
    if chronic:
        lines.append(f"⚠ 长期缺席 {len(chronic)} 店(落后船队 >"
                     f"{store_absence.CHRONIC_LAG_HOURS}h,不逐日重赛):"
                     f"{','.join(chronic)} —— 修凭证/代理,或去凭证表取消「启用」")
    if not recent:
        return lines
    if len(recent) > REPLAY_MAX_STORES:
        lines.append(f"⚠ 今日缺席 {len(recent)} 店 > 规模闸 {REPLAY_MAX_STORES}"
                     f",疑似系统性故障(代理商/网络出口),不逐店重赛:"
                     f"{','.join(recent)} —— 修好根因后手动重跑整链")
        return lines
    replayable = [n for n in steps
                  if getattr(modules[n], "SUPPORTS_STORE", False)]
    skipped = [n for n in steps if n not in replayable]
    lines.append(f"—— 缺席店重赛:{len(recent)} 店,逐店一次、再失败即止"
                 f"(全局步骤跳过:{','.join(skipped) or '无'})——")
    from services import notify_fmt as nf
    per_store_lines: dict[str, list[str]] = {}
    failed_at: dict[str, tuple] = {}
    for store in recent:
        got: list[str] = []
        for name in replayable:
            params = dict(per_step[name])
            params["store"] = store
            status, text = _run_step(name, modules[name], params, dry_run,
                                     operator, logs_dir)
            if status != "success":
                # 失败铺开全文(_chain_text 同款纪律):人要能从通知里直接
                # 看出该修凭证表还是找代理商,而不是去翻 ops.runs
                failed_at[store] = (name, status, text)
                break
            # 成功步骤压一行:重赛跑的是 DANGEROUS 步骤,发了多少 feed
            # 不能只剩一个 ✅(「✅ 救回」读起来像补了个同步)
            got.append(f"   · {nf.first_line_of(text)}")
        per_store_lines[store] = got
    # 水位复核:步骤退出码全绿 ≠ 数据真回来了(例:该店返回 0 商品,
    # upsert 一行不写、水位不前进)—— 按事实说话,不发假 ✅
    try:
        with db.pg_conn() as conn:
            still = set(store_absence.stale_stores(conn, since=since))
    except Exception:
        still = set()
    for store in recent:
        if store in failed_at:
            name, status, text = failed_at[store]
            body = "\n".join(f"   {ln}" for ln in str(text).splitlines()
                             if ln.strip())
            lines.append(f"❌ {store}:仍缺席 —— 重赛卡在 {name}({status});"
                         f"今天到此为止,明天整链自然再试\n{body}")
        elif store in still:
            lines.append(f"⚠ {store}:重赛步骤全成但目录水位未推进"
                         f"(在线 0 商品店?),仍按缺席处理")
            lines.extend(per_store_lines[store])
        else:
            lines.append(f"✅ {store}:救回")
            lines.extend(per_store_lines[store])
    return lines


def _chain_text(steps: list[str], results: list[tuple], worst: str) -> str:
    """输入:步骤名 + [(名, 状态, 摘要)] + 最坏状态 → 输出:整链那一条通知正文。

    排版规范见 `services/notify_fmt` 头注。这里落地的是第 5 条:
    **成功的步骤压成一行(它自己摘要的第一行),失败/跳过的给全文。**

    ⚠ 为什么不是全都给全文(2026-08-17 所有者要求整理通知形态):产品线七步
    每步都是一段密集中文,拼起来是二十来行没有层次的文字 —— 人第三天就不看了,
    而**不看的通知等于没有通知**。成功的那些只需要一行证明它跑了、跑出多少;
    真正要读的是失败那一步的明细,所以只有它铺开。
    全文并没有丢:`ops.runs.summary` 每步一行整篇存着,日志也在。

    单跑的通知形态**逐字不动**(上面那个分支)—— 人和告警规则都认那个格式。
    """
    from services import notify_fmt as nf
    icon = "✅" if worst == "success" else _ICON.get(worst, "❌")
    lines = [f"{icon} 链 [{' → '.join(steps)}]"]
    for name, status, text in results:
        mark = _ICON.get(status, "⏭")
        if status == "success":
            lines.append(f"{mark} {nf.first_line_of(text)}")
        else:
            # 失败/跳过:整段铺开(缩进两格,与折叠行区分开)
            body = "\n".join(f"   {ln}" for ln in str(text).splitlines()
                             if ln.strip())
            lines.append(f"{mark} {name}\n{body}" if body else f"{mark} {name}")
    if worst != "success":
        lines.append("(成功步骤只显示首行;全文见 ops.runs 与各步日志)")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
