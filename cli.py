#!/usr/bin/env python3
"""cli.py — 全项目唯一执行入口。

    python cli.py <workflow> [-p key=value ...] [--execute]

统一负责(workflow 文件里不做这些):
  加载 <DATA_ROOT>/.env → flock 单实例锁 → 写 ops.runs 运行记录 → 执行 run(params)
  → 飞书通知(成功/失败都发)→ 退出码(0 成功 / 1 失败 / 3 已有实例在跑)。

危险工作流强制 dry-run:模块声明 DANGEROUS=True 时,params["execute"] 只有
显式传 --execute 才为 True;缺省 dry-run,run() 只打印将做什么,不碰写接口。
AI 改完代码必须先 dry-run,人眼确认输出后才允许 --execute(安全铁律)。

launchd 定时、手动触发、未来的网页按钮和 MCP 工具,全部走这一条路径;
调用方身份用环境变量 WALMART_OPERATOR 标注(launchd / manual / web / mcp)。
"""

import argparse
import importlib
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cli")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cli.py", description="WalmartAPI-Contral 唯一执行入口")
    parser.add_argument("workflow", help="workflows/ 下的工作流名,如 ping_stores")
    parser.add_argument("-p", "--param", action="append", default=[],
                        metavar="key=value", help="传给 run(params) 的参数,可重复")
    parser.add_argument("--execute", action="store_true",
                        help="危险工作流真跑开关;缺省 dry-run 只打印将做什么")
    return parser.parse_args(argv)


# 参数值里混进了本该独立成词的开关。成因几乎总是**分隔符不是普通空格**
# ——从聊天/网页复制命令时容易带上不换行空格(U+00A0)等,shell 不把它当
# 分词符,于是 `-p k=v --execute` 整串进了 v。
# ⚠ **只报错,绝不"帮你"把它解释成开关**:那等于让一个粘贴事故把 dry-run
#    变成真跑。危险工作流的 --execute 必须是人显式敲进去的。
_FLAGS = ("--execute", "-p", "--param", "-h", "--help")


def _build_params(pairs: list[str]) -> dict:
    params = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"参数格式错误(应为 key=value): {item}")
        k, _, v = item.partition("=")
        v = v.strip()
        # split() 按任意空白切,包括 U+00A0 —— 正是要抓的那种
        stuck = [w for w in v.split() if w in _FLAGS]
        if stuck:
            raise SystemExit(
                f"参数值里粘进了开关 {' '.join(stuck)}:\n"
                f"    -p {k.strip()}={v}\n"
                f"  多半是路径与开关之间那个空格不是普通空格(从聊天/网页复制\n"
                f"  常带不换行空格)。把该处空格重敲一遍,或给值加引号:\n"
                f"    -p \"{k.strip()}={v.split()[0]}\" {' '.join(stuck)}\n"
                f"  ⚠ 本命令**没有执行**——不会替你把它当成开关,免得一次粘贴\n"
                f"    事故把 dry-run 变成真跑。")
        params[k.strip()] = v
    return params


def _setup_logging(workflow: str, logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / f"{workflow}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stderr)],
    )


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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    params = _build_params(args.param)

    # .env 先于一切业务 import 加载,registry 各函数 call-time 求值即可拿到
    from dotenv import load_dotenv

    from registry import paths
    load_dotenv(paths.env_file(), override=False)

    # cli 只自建它直接需要的 logs/locks;完整 DATA_ROOT 初始化走 init_data_root 工作流
    _setup_logging(args.workflow, paths.logs_dir())

    from services import runlock
    lock = runlock.acquire(args.workflow)     # 句柄必须留着:GC 掉即释放锁
    if lock is None:
        print(f"⚠ {args.workflow} 已有实例在运行(flock 占用),本次退出", file=sys.stderr)
        return 3

    try:
        module = importlib.import_module(f"workflows.{args.workflow}")
    except ModuleNotFoundError as e:
        if e.name and e.name.endswith(args.workflow):
            avail = sorted(p.stem for p in (Path(__file__).parent / "workflows").glob("*.py")
                           if not p.stem.startswith("_"))
            print(f"未知工作流: {args.workflow}\n可用: {', '.join(avail)}", file=sys.stderr)
            return 2
        raise

    dangerous = bool(getattr(module, "DANGEROUS", False))
    params["execute"] = args.execute if dangerous else True
    if dangerous and not args.execute:
        print(f"🧪 [DRY-RUN] {args.workflow} 为危险工作流,本次只打印将做什么;"
              f"真跑请加 --execute")

    import os
    operator = os.environ.get("WALMART_OPERATOR", "manual")
    run_id = _record_start(args.workflow, params, operator)
    mode = "" if not dangerous else ("[EXECUTE] " if args.execute else "[DRY-RUN] ")

    try:
        summary = module.run(params)
        summary = str(summary) if summary is not None else "(无摘要)"
    except Exception:
        err = traceback.format_exc()
        logger.error("workflow %s 失败:\n%s", args.workflow, err)
        _record_finish(run_id, "failed", err[-2000:])
        _notify(f"❌ {mode}{args.workflow} 失败\n{err.strip().splitlines()[-1]}")
        return 1

    logger.info("workflow %s 成功: %s", args.workflow, summary)
    print(summary)
    _record_finish(run_id, "success", summary)
    _notify(f"✅ {mode}{args.workflow} 成功\n{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
