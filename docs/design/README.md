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

## 2026-08-18 第三轮:采纳所有者提供的交互原型为主画板

所有者在 claude.ai/design 项目「前端设计方案讨论」(465557a5)里另做了一版
**单文件交互原型**(可点导航、13 视图、危险两步抽屉、按工作流的破坏面预设),
功能设计优于本仓此前的静态画板 —— 定稿采纳为画布主画板(打开即落它,fill 展开)。

- `console.ref.dc.html`:原型**原文存档**(从设计项目取回,未改动)。
- `merge_console.py`:合入脚本(幂等,从 ref 原文重建)。做三件事:
  ①内联 `industry-styles.css` 与 ds bundle 空壳(画布 CSP 下相对路径加载不了);
  ②修两处事实 —— 退出码 3 那轮**不写 ops.runs**;日报通知走**飞书应用直发**,
  没有 webhook(原型写的 webhook 403 是老口径);
  ③补原型缺的两个视图:**审核中心**(L0-L4 分层/pending 两来源/重审三通道)与
  **类目映射**(四桶缺口/置信度生命周期),沿用原型自己的组件语言。
- `Console.dc.html`:合入产物(生成物,改 merge_console.py 再重建,不手改)。
- 原 20 块静态画板退居第二、三页,作为业务域细节的参照。

### 换肤(同日追加):功能用原型的,配色用 erp-core

所有者定稿:交互原型的功能设计保留,配色换 erp-core。原型的颜色全走 CSS
变量,换肤收在 merge_console.py 的 2.5 步(token 级替换,组件形态不动):

- 主色:工业蓝 #2b5c8c → **zinc-900 黑 #18181B**(黑主按钮 / 黑激活导航),
  accent 阶映射 zinc 阶;侧栏白底、页面底 #FAFAFA、分隔线 #E4E4E7;
- 语义三组:红 #DC2626(50/700/200 一组)、琥珀 #B45309、绿 #047857;
  新立 --sev-mid(sky #0369A1,中间态虚线签 —— 原型用 accent 蓝,映射 zinc
  后会与「忙」的灰撞,故单列)与 --sev-ok-dot(emerald-500 圆点,店铺点阵);
- 字体:Barlow 三件套 → Inter + Noto Sans SC;ID/数字等宽 → JetBrains Mono;
- 深色主题同步 zinc 暗面映射(主按钮反白);方角 / 四角标记 / 斜纹危险钮保留。
