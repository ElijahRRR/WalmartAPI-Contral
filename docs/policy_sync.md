# 政策表官方同步方案(policy_sync)

> 所有者定稿(2026-09-01)两条:①**政策表以官方为准** —— 不推测、不以现存表为准,
> 数据库政策表与 LLM prompt 的政策段都必须从官方同步;②**先重塑政策表,再跑分类
> 对照**。本方案即第①条的落地;分工照旧:规划侧定稿本文,执行侧照 §七清单实现。
>
> ⚠ 本定稿**撤销** `docs/api_blueprint.md`「明确不做」里的
> 「marketplacelearn.walmart.com 政策页爬虫(类目映射 pipeline 归档不迁移)」一条
> (那是迁移期决定,如今政策表成为归类 join 与 L3 判定的权威源,必须可同步)。
> 执行侧同步修改蓝图并注明本定稿出处。

## 〇、事实基础(2026-09-01 实查)

- 官方总览页 `marketplacelearn.walmart.com/guides/Prohibited-products-policy:-overview`
  (页面标注 Last Updated: Jun 5, 2026)枚举 **43 个政策类别**,武器族嵌套在
  "Weapons" 父级下(Air Powered/BB/Toy Guns、Firearms、Firearm Accessories、
  Firearm Ammunition、Knives and Other Melee Weapons);
- 库内 `audit.walmart_prohibited_policy` 只有 **37 行**(audit_import 一次性搬迁,
  无同步链)—— **整个武器族缺失**,L3 的政策清单从未含武器政策,而生产报错里
  武器族拒绝真实存在(Firearms 18 / Knives 16 / Accessories 13 / Ammunition 3);
- 词形差实证(官方 vs 报错原文/表内):`Cosmetic Products` vs `Cosmetics Products`、
  `Plants and Seeds` vs `Plants & Seeds`、`Tobacco, E-Cigarettes and Vaping
  Products`(官方无牛津逗号)、`Knives and Other Melee Weapons`(Other 大写)、
  `Stamps and Tickets` vs `Stamps & Tickets`、Jewelry 行的 `(Covered Goods)` 后缀。

## 一、范围与红线

- **来源唯一 = 美国站** marketplacelearn 总览页 + 各类别页;`/ca/` 加拿大页不收。
  类别枚举**以总览页实际链接清单解析为准,不信任何计数**(含本文的 43)。
- **直连合规说明**:marketplacelearn 是公开帮助站,不是 Marketplace 卖家 API ——
  不涉店铺关联封号红线,不走店铺代理,普通 httpx 直连即可(超时/重试要有)。
- 本方案**只动政策表数据与同步工具**,不动归类引擎、不动 L3 代码。
  L3 的三档全文喂入是第三步的事;但见 §五的连带声明。

## 二、表写入口径(保守:刷新可再生列,人工列一律不碰)

| 列 | 口径 |
|---|---|
| `category_en` | **存量 37 行不改名**(audit_reason/L3 存量对齐挂在现值上,改名随 L3 提版批);新类别行用**官方拼写**插入,`id = max(id)+1` 起 |
| `full_policy` | **官方类别页正文全文**(html→纯文本,保留段落),每次同步全量刷新 |
| `official_url` | 官方页 URL,刷新 |
| `policy_updated_at` | 官方页 "Last Updated" 日期(解析不到用抓取日,并在 raw 里标注) |
| `synced_at` | now() |
| `raw` | 抓取元数据 jsonb:{fetched_at, http_status, page_title, last_updated_raw, content_sha256, chars} |
| `prohibited_items` / `conditional_items` / `preapproval_items` / `preapproval` / `legal_refs` / `category_zh` / `zh_seller_risk` / `zh_seller_notes` | **一律不读不写**(修订 2026-09-01:这些是给人看的中文/人工列,英文要点句填进去会中英混列 —— 原「空时填要点句」条款作废);新行这些列留 NULL,进报告等人工 |

**官方名 ↔ 表内名的对行**:归一化匹配(casefold + `&`↔`and` + 去逗号/括号后缀
`(Covered Goods)` + 空白折叠)自动对上;对不上的**不猜**,进 dry-run 报告的
「未对上清单」由所有者裁决(改名/新增/忽略)。官方页存在而表里没有 → 计划新增;
表里存在而官方页没有 → **不删行**,报告标「官方总览已不含该类别」待人工。

## 三、工作流 `workflows/policy_sync.py`

- `DANGEROUS = True`(写判定数据;缺省即真跑是仓规,但**首次必须先 --dry-run**);
- `--dry-run`:抓全部页面,输出逐类别 diff —— 新增行清单 / 未对上清单 / 每行
  full_policy 的 sha 变化与字数变化 /
  官方已不含清单;**不写库**;
- 真跑:upsert 按 §二;摘要首行给「新增 N / 刷新 M / 未对上 K / 官方缺席 J」;
- 手动跑,不进调度(官方页低频变更;所有者按需重跑);
- 网络失败单类别隔离:一页抓不到不炸整轮,摘要点名缺席页,**缺席页对应行本轮
  不刷新**(绝不写空值覆盖)。

## 四、api 层 `api/policy_pages.py`(外部接口适配,零业务判断)

- `fetch_overview() -> list[{name, url}]`:总览页链接清单解析(嵌套的 Weapons
  子项摊平收录;父级 "Weapons" 本身若无独立正文页则不成行);
- `fetch_policy_page(url) -> {title, last_updated, text, html_sha, chars}`:
  正文抽取用标准库(html.parser),**不新增第三方依赖**;剥导航/页脚,保正文段落;
- 超时/重试与仓内 httpx 惯例一致(timeout 明确、5xx 指数退避 ≤3 次);
- 解析失败 fail-loud(返回错误给 workflow 计入缺席),不静默给空文本。

## 五、连带声明(真跑的后果,所有者知情项)

1. **政策表内容变化 = L3 判定输入变化**:S4 政策段直接 `SELECT ... ORDER BY id`
   全表渲染 —— 新增武器族行会立刻进入 L3 提示词(这正是目的:武器盲区闭合)。
   因此**真跑同批需手动递增 `AUDIT_RULES_VERSION`**(registry/resources.py,
   版本注释写明「政策表官方同步 v1:补武器族 N 行 + 全文刷新」),前缀缓存一次性
   重建属预期成本。政策表至今没有变更→重审的通道,这是已登记的开放问题,本方案
   不解决、只不再让它变得更糟(版本递增让重审通道 rerule/stale 可用)。
2. 新行在 S4 先以「标题 + 要点句」形态出现(三档全文喂入随第三步 L3 批);
3. 归类对照报告的政策名 join 在表重塑 + 词形归一后预期接近 100%,**所有者重跑
   `error_reclass_report` 的时点 = 政策表真跑之后**(先重塑再跑分类,定稿顺序)。

## 六、测试

- 解析器用**本地 HTML 夹具**(执行侧抓总览页 + 2 个类别页,裁剪到正文骨架
  <50KB/页存 `tests/fixtures/policy_pages/`,标注抓取日期与 URL;测试不打真网);
- 对行归一化(&↔and/逗号/括号后缀/大小写)逐词形差断言(§〇清单全覆盖);
- upsert 口径测试:人工列不覆盖 / prohibited_items 非空不动 / 缺席页不刷新 /
  不删行 —— 全部用假连接钉死;
- dry-run 零写库守门。

## 七、执行侧清单

1. `api/policy_pages.py`(§四);
2. `workflows/policy_sync.py`(§三,upsert SQL 走 registry/db);
3. `tests/test_policy_sync.py` + HTML 夹具(§六);
4. `docs/api_blueprint.md`:「明确不做」撤销该条,登记 policy_pages 两函数
   (非 Marketplace API,单列一小节,注明本定稿出处与日期);
5. `docs/db_schema.md`:walmart_prohibited_policy 行注补「policy_sync 同步列 /
   人工列」分界;
6. README 工作流计数 +1、测试计数同步;
7. `python -m pytest -q` 全绿;不做 git 操作;冲突停下记录。

## 八、修订(2026-09-01 所有者定稿):语言原则 + 表格人机分区重设计

**旧格式的病**:旧清洗把政策压成中文摘要喂 L3,而产品文案是英文 —— 跨语言、有翻译
损耗、还是二手信息。所有者定稿:**不按原格式来,重新设计**。

### 8.1 语言原则(全链适用)

> **给 LLM 的 = 官方英文原文;给人的 = 中文。两区永不混写。**

- L3 的政策输入 = PG 英文区的 `full_policy`(官方全文,第三步 L3 批接线);
  现 S4 喂的中文摘要列(overall_status/zh_risk/prohibited_items 截断)**整体退出
  LLM 输入**,降级为人看的留档;
- policy_sync 只维护英文机器区六列(category_en/full_policy/official_url/
  policy_updated_at/synced_at/raw),对中文/人工八列**不读不写**(§二已改)。

### 8.2 飞书表『沃尔玛禁售政策』两区设计(v2,待所有者确认列布局并建表)

**机器投影区(程序写,人只读)** —— PG 英文区 → 飞书:
| 列 | 内容 |
|---|---|
| A | 官方类别名(EN,category_en) |
| B | 官方页 Last Updated |
| C | 我方同步时间 |
| D | 全文字数(变化 = 官方改了政策的信号) |
| E | 官方页链接 |

(full_policy 全文不进表格 —— 飞书单元格不适合几千词长文,全文只在 PG,人看点 E 列链接)

**人工区(运营写,回同步 PG 中文列)** —— 飞书 → PG:
| 列 | 内容 | 落 PG 列 |
|---|---|---|
| F | 中文类别名 | category_zh |
| G | 风险等级(红/黄/绿:这类我们能不能碰) | zh_seller_risk |
| H | 中文速览(一句话:要什么证/哪些细分做不了) | zh_seller_notes(或新列,建表时定) |
| I | 运营备注(案例/处置口径) | 与 H 合并或另列,建表时定 |

数据流定稿:
```
官方页面 ──policy_sync──► PG 英文区 ──投影──► 飞书 A-E(人只读)
运营填飞书 F-I ──回同步──► PG 中文区(人区权威在飞书,类目表同款模式)
L3 只读 PG 英文区(full_policy 全文);中文列永不进 prompt
```

v2 前置:所有者建飞书表并给 token/table id → registry 登记(表与字段常量)→
投影与回同步两条通道(走 api/feishu 标准通道、限额 95%、字段引用常量)。
v1(官方→PG 英文区)不依赖 v2,先行落地 —— L3 换喂英文全文只需要 PG。
