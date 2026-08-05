"""workflows — 业务工作流层:每文件一条工作流,只暴露 run(params) -> 结果摘要。

约定(CLAUDE.md 工程规范):
- 不含 argparse,不自行处理调度/通知/锁/运行记录——那些是 cli.py 的事。
- 危险工作流(提交 feed、DELETE_ITEM、清库存等)在模块顶层声明 DANGEROUS = True;
  cli.py 据此强制 dry-run:params["execute"] 只有用户显式 --execute 才为 True,
  run() 必须在 execute=False 时只打印"将对哪些 SKU 做什么",不碰任何写接口。
- 依赖规则:workflows 可 import services / api / registry,严禁被任何层 import。
"""
