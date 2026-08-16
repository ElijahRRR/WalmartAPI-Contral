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

    with runlock.hold(name) as got:
        if not got:
            print(f"⚠ {name} 已有实例在运行(flock 占用),本次退出",
                  file=sys.stderr)
            return "locked", f"{name}:已有实例在运行,未执行"
        with _log_to(name, logs_dir):
            run_id = _record_start(name, params, operator)
            try:
                summary = module.run(params)
                summary = str(summary) if summary is not None else "(无摘要)"
            except Exception:
                err = traceback.format_exc()
                logger.error("workflow %s 失败:\n%s", name, err)
                _record_finish(run_id, "failed", err[-2000:])
                return "failed", f"{mode}{name} 失败\n{err.strip().splitlines()[-1]}"
            # 摘要在终端上只出现一次(下面那句 print);全文进日志文件备查
            logger.info("workflow %s 成功:\n%s", name, summary,
                        extra={"file_only": True})
            print(summary)
            _record_finish(run_id, "success", summary)
            return "success", f"{mode}{name} 成功\n{summary}"


_ICON = {"success": "✅", "failed": "❌", "locked": "⚠"}
_EXIT = {"success": 0, "failed": 1, "locked": 3}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    steps = list(args.workflow)
    per_step = _build_params(args.param, steps)

    # .env 先于一切业务 import 加载,registry 各函数 call-time 求值即可拿到
    from dotenv import load_dotenv

    from registry import paths
    load_dotenv(paths.env_file(), override=False)

    # cli 只自建它直接需要的 logs/locks;完整 DATA_ROOT 初始化走 init_data_root 工作流
    logs_dir = paths.logs_dir()
    _setup_logging(logs_dir)
    modules = _load_modules(steps)

    import os
    operator = os.environ.get("WALMART_OPERATOR", "manual")

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
    head = "✅" if worst == "success" else _ICON.get(worst, "❌")
    chain = " → ".join(steps)
    body = "\n".join(f"{_ICON.get(s, '⏭')} {t}" for _, s, t in results)
    _notify(f"{head} 链 [{chain}]\n{body}")
    return _EXIT.get(worst, 1)


if __name__ == "__main__":
    sys.exit(main())
