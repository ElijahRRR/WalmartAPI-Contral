# 设计画布源文件(生成物,进 git)

前端设计画布「WalmartAPI 运营台设计稿」的画板源码。与 `skills/` 同一纪律:
**这些 .dc.html 与 canvas.json 是 `gen.py` 渲染出来的生成物,不要手改** ——
改 `gen.py` 再重新生成(`python3 gen.py`,在本目录跑)。

- 每个 `*.dc.html` = 画布上的一块画板(第一轮 8 块:设计系统总览 / 总览首页 /
  工作流触发三联 / 产品详情 / 订单待人工 / 上架闸门漏斗)。
- `canvas.json` = 画板布局(按使用流排,不按重要性)。
- 设计词汇逐值取自旧仓 erp-core(`/workspace/erpapi` 的
  `erp-core/handoff-design/project/`),定稿见 `docs/frontend_brief.md` 第六节。
- 画布本体发布为 claude.ai Artifact,由会话内 `/design` 重新播种更新;
  若所有者在画布 GUI 里改过并保存,先从画布导出(--extract)再回灌,
  **不要**直接用本目录旧文件覆盖别人的改动。
