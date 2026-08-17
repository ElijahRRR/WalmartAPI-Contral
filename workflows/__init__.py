"""workflows — 业务工作流层:每文件一条工作流,只暴露 run(params) -> 结果摘要。

约定(CLAUDE.md 工程规范):
- 不含 argparse,不自行处理调度/通知/锁/运行记录——那些是 cli.py 的事。
- 危险工作流(提交 feed、DELETE_ITEM、清库存等)在模块顶层声明 DANGEROUS = True;
  **缺省即真跑**(2026-08-16 定稿):`params["execute"]` 只有用户显式 `--dry-run`
  时才为 False。run() 必须在 execute=False 时只打印"将对哪些 SKU 做什么",
  不碰任何写接口。⚠ 旧口径是"缺省 dry-run、真跑加 --execute",改的理由见
  cli.py 头注:进了调度之后,漏写 --execute 会让那条链**每天空转而且报成功**,
  比误跑更难发现。`--execute` 保留为空操作别名,调度里写了也不会错。
- DANGEROUS=False 的工作流 `execute` 恒为 True;它们若也想支持空跑,读的是
  `params["dry_run"]`(cli 单独透传)——只看 execute 的话 `--dry-run` 对它们
  完全无效,而且不报错。
- 依赖规则:workflows 可 import services / api / registry,严禁被任何层 import。
"""
