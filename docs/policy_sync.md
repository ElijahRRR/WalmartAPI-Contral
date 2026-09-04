# 政策表官方同步方案(policy_sync)

> 所有者定稿(2026-09-01)两条:①**政策表以官方为准** —— 不推测、不以现存表为准,
> 数据库政策表与 LLM prompt 的政策段都必须从官方同步;②**先重塑政策表,再跑分类
> 对照**。本方案即第①条的落地;分工照旧:规划侧定稿本文,执行侧照 §七清单实现。
>
> ⚠ 本定稿曾**撤销** `docs/api_blueprint.md`「明确不做」里的政策页爬虫一条 ——
> **该撤销已被 §九/§十 收回**(2026-09-01):仓内**不实现爬虫**,蓝图该条保留并加注,
> 政策来源改为 refdata 转录件(`.claude/skills/policy-refresh` 流程)。

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
| `category_en` | ~~存量 37 行不改名~~ → **2026-09-02 所有者定稿:表内名一律改为官方拼写**(旧脚本跟随新流程;对上但拼写不同的行 dry-run 报「将改名」,真跑 UPDATE category_en,id 不变);新类别行用**官方拼写**插入,`id = max(id)+1` 起 |
| `full_policy` | **官方类别页正文全文**(html→纯文本,保留段落),每次同步全量刷新 |
| `official_url` | 官方页 URL,刷新 |
| `policy_updated_at` | 官方页 "Last Updated" 日期(修订 2026-09-01:解析不到**置 NULL**,原文存 `raw.last_updated_raw` 并在摘要与报告点名 —— **不拿抓取日顶替**,否则「官方没改过」与「我们没读懂日期」不可区分) |
| `synced_at` | now() |
| `raw` | 抓取元数据 jsonb:{fetched_at, http_status, page_title, last_updated_raw, content_sha256, chars} |
| `prohibited_items` / `conditional_items` / `preapproval_items` / `preapproval` / `legal_refs` / `category_zh` / `zh_seller_risk` / `zh_seller_notes` / `overall_status` | **一律不读不写**(修订 2026-09-01:这些是给人看的中文/人工列,英文要点句填进去会中英混列 —— 原「空时填要点句」条款作废);新行这些列留 NULL,进报告等人工。**overall_status 为本节初稿漏列补正**(2026-09-01 执行侧发现:实表第 9 列,中文总述,S4 现用 —— 同属人工区) |

**官方名 ↔ 表内名的对行**:归一化匹配(casefold + `&`↔`and` + 去逗号/括号后缀
`(Covered Goods)` + 空白折叠)自动对上;对不上的**不猜**,进 dry-run 报告的
「未对上清单」由所有者裁决(改名/新增/忽略)。官方页存在而表里没有 → 计划新增;
表里存在而官方页没有 → **不删行**,报告标「官方总览已不含该类别」待人工。

## 三、工作流 `workflows/policy_sync.py`(⚠ 抓取侧描述已被 §九/§十 取代:来源=refdata 转录件,无抓取;dry-run/upsert/隔离等行为条款仍有效)

- `DANGEROUS = True`(写判定数据;缺省即真跑是仓规,但**首次必须先 --dry-run**);
- `--dry-run`:抓全部页面,输出逐类别 diff —— 新增行清单 / 未对上清单 / 每行
  full_policy 的 sha 变化与字数变化 /
  官方已不含清单;**不写库**;
- 真跑:upsert 按 §二;摘要首行给「新增 N / 刷新 M / 未对上 K / 官方缺席 J」;
- 手动跑,不进调度(官方页低频变更;所有者按需重跑);
- 网络失败单类别隔离:一页抓不到不炸整轮,摘要点名缺席页,**缺席页对应行本轮
  不刷新**(绝不写空值覆盖)。

## 四、api 层 `api/policy_pages.py`(⚠ 本节整体已被 §九/§十 作废:不实现抓取,api 层无此模块;仅存历史)

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

## 六、测试(⚠ HTML 夹具条款已被 §九/§十 取代:解析对象是 refdata md,测试直接用仓内 42 份真实转录件;其余口径条款仍有效)

- 解析器用**本地 HTML 夹具**(执行侧抓总览页 + 2 个类别页,裁剪到正文骨架
  <50KB/页存 `tests/fixtures/policy_pages/`,标注抓取日期与 URL;测试不打真网);
- 对行归一化(&↔and/逗号/括号后缀/大小写)逐词形差断言(§〇清单全覆盖);
- upsert 口径测试:人工列不覆盖 / prohibited_items 非空不动 / 缺席页不刷新 /
  不删行 —— 全部用假连接钉死;
- dry-run 零写库守门。

## 七、执行侧清单(⚠ 第 1、3 项抓取侧内容已被 §九/§十 取代;实际交付以 §十.6 落地记录为准)

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

**2026-09-02 所有者定稿:飞书回同步通道取消。** 人工区 F-H 只存在于飞书(运营在飞书看、在飞书写),
不回写 PG;PG 的中文人工列冻结为遗留(S4 换喂英文全文后无程序消费者)。
只保留单向投影 PG → 飞书 A-E。v2 前置:所有者建飞书表并给 token/table id → registry
登记(表与字段常量)→ 投影通道(走 api/feishu 标准通道、限额 95%、字段引用常量)。
v1(官方→PG 英文区)不依赖 v2,先行落地 —— L3 换喂英文全文只需要 PG。

## 九、再修订(2026-09-01 所有者定稿):脚本抓取作废,改子代理逐页转录

1. **不用脚本抓取**(§三/§四/§六/§七 的 policy_sync 工作流与 api/policy_pages
   实现整体暂缓作废)—— html.parser 抓的是拍平的纯文本,结构(标题/列表/表格)
   会丢,与官方页面不是同样的东西;
2. 改由 **workflow 派 opus5 子代理逐页访问官方类别页**,按官方结构忠实转录成
   markdown(不翻译/不概括/不增删),汇总成**一份 md 文件交所有者审阅**;
3. **入库与飞书表设计,待所有者审完 md 再定**(§二/§八.2 相应暂缓);
4. §八.2 人工区的「风险等级」列**取消** —— 抓的本来就是禁售政策,分级无意义。

## 十、三修订(2026-09-01 所有者定稿):流程固化 + 喂入版口径

1. **流程固化为仓库 skill** `.claude/skills/policy-refresh/`(总览核对 → 子代理逐页转录 →
   独立校验 → 所有者审阅 → 中文翻译 → 入库 → 飞书投影);本轮转录产物入仓
   `refdata/policy_pages/en|zh/`(42+42 节;en = 权威判据源,zh = 人读译本)。
   今后同步 = 重跑 skill 更新 refdata,**git diff 即政策变更审计记录**。
2. **入库来源改为 refdata 转录件**(§三/§四的抓取侧作废不复活):policy_sync 工作流
   读 `refdata/policy_pages/en/*.md`,解析头部四行(类别名/URL/官方 Last Updated/抓取日)
   与正文,按 §二 口径 upsert;`raw` 列记 {source: "refdata", file, content_sha256, chars}。
3. **喂入版口径(定稿)**:S4 喂给 LLM 的政策文本从 `full_policy`(en 全文)**渲染时派生**,
   不另落库,剥除:超链接 URL(保留链接文字)、「In this guide」页内导览、Notes 免责声明、
   头部四行、页面 chrome 行;表格做「列名 → 条目清单」的机械无损变换
   (Prohibited/Allowed with restriction/Allowed 各列转为带列名标题的条目列表,
   免 LLM 解析 `<br>` 单元格)。派生函数为纯函数、进测试;规则变更 = L3 输入变更,
   与政策表内容变更同待遇(递增 AUDIT_RULES_VERSION)。
4. **链接不进提示词**(所有者定稿):URL 对判定零贡献、徒耗 token;链接只在 PG(官方页
   official_url 列)与飞书 E 列供人点看。
5. **政策段连续且挨在一起**(所有者定稿):全部政策渲染为提示词中**单一连续块**、
   固定排序(ORDER BY id),产品信息只出现在其后 —— 这同时是前缀缓存命中的硬前提
   (缓存经济账:政策前缀 ~5.5 万 token,命中价下 DS ≈0.0076 元/条、GLM ≈0.0154 元/条)。
6. **执行侧落地记录(2026-09-01,经对抗复核后定稿)**:
   - 报告口径:「未对上 K」= 计划新增 N + 歧义扣留(表内两行归一化同名/两官方页指向同一行,
     一行不动等人裁决),常态 K==N;「官方已不含」与「未对上」间做疑似改名配对提示;
     解析失败单列一节,不混入「官方已不含」;
   - 归一化在 §二 四条之外补两条:去撇号、削词尾单复数 s(否则 Cosmetic↔Cosmetics 词形差
     打不平);已用测试钉死 42 个官方名两两归一化不碰撞;
   - **首轮真跑前置**:生产表存量 7 行用旧缩写名(Drugs & Paraphernalia、Electronics & RF、
     Auto & Motor Vehicles、Textiles & Apparel、Military & Law Enforcement、
     Ride-Ons & Micromobility、Tobacco & Vaping 一族),按「不猜」口径对不上官方全称,
     直接真跑会产生**同概念双行**并污染 S2 候选 —— 所有者必须先按 dry-run
     「未对上清单」逐条裁决(改名/新增/忽略;改名随本批 AUDIT_RULES_VERSION 提版一起做)
     —— **2026-09-02 修订:改为系统化改名**(表内名 := 官方名,见 §十.7),不再逐条裁决;
   - 真跑连带后果三条(摘要强制提醒):①AUDIT_RULES_VERSION 手动递增;②新增行人工列
     全 NULL,S4 现渲染为空壳标题待运营补中文;③`services/audit_l3.py` S1/S3 提示词
     硬写「37 条」将与实际行数不符,是否修改随第三步 L3 批由所有者定;
     **⚠ 旧口径已作废(2026-09-02),留档勿照做**:①的版本号**已由改名批递增**至
     `c.2026-09-02.1`(再手动提一版 = 白白触发第二轮全量重审);③的「37 条」已
     **动态化**(S1/S3 按实时政策条数渲染,不再有对不上的字面量),连带后果因此
     只剩两条 —— 现行口径见 §十.7 落地记录与 `workflows/policy_sync.py` 摘要;
   - 喂入版渲染件(`services/policy_feed.render_feed_text`)已落地进测试,
     **S4 接线随第三步 L3 批**,当前 S4 仍读中文人工列。
7. **四修订(2026-09-02 所有者定稿):官方政策类别名 = 全链唯一键**
   - **政策类别 ≠ 类目**:政策表按产品类别(如 Animals)组织,其禁品散布在家居/户外/宠物等
     多个类目下,「类目 → 政策」的映射(旧 `_L3_NORMALIZE` 思路)不成立也不保留。
     **旧脚本跟随新流程变动**,不是新流程迁就旧脚本;
   - 落地口径:policy_sync 真跑把**所有对上但拼写不同的行改为官方拼写**(dry-run 报
     「将改名」清单,含别名表命中的缩写行如 Drugs & Paraphernalia → Drugs and Drug
     Paraphernalia;id 不变),真正未对上的才需所有者裁决新增/忽略;同批跟随修改依赖
     旧拼写的代码与数据:`services/audit_reason._L3_NORMALIZE`(改为官方 42 名)、
     `services/error_taxonomy.POLICY_ALIASES`(改名后退役)、测试 KNOWN_POLICIES、
     `services/audit_l3.py` 提示词「37 条」字面量与「逐字节相同」契约(契约退役);
     同批递增 AUDIT_RULES_VERSION;
   - **评估方法定稿**:用后台已有报错记录做回放 —— 让 LLM 跑一遍已知沃尔玛裁决的产品,
     拿输出与沃尔玛的报错结果比对(判定 + 政策类别两维);需含通过样本(测误伤),
     沃尔玛裁决作参照而非金标(申诉成功/自愈态存在);
   - **L3 输出规范化**(现状杂乱):审核输出与最终结果统一为「判定结果 / 政策类别
     (限定官方 42 名枚举)/ 具体内容」三段;结构化枚举输出后不再需要任何模糊归一化。
     **落地设计待政策表完成后再议**;
   - **飞书回同步通道取消**(§八.2 已改)。
   - **执行侧落地记录(2026-09-02,本批已实现)**:
     · `registry/resources.POLICY_LEGACY_NAMES` = 仓内**唯一一份**「表内旧名 → 官方名」
       映射(先收 §十.6 那 7 条;所有者 dry-run 发现新拼写差时在这里追加,**不许另起第二张表**);
       仅供过渡期 —— ⚠ **2026-09-03 C 批已连同 `POLICY_ALIASES` / `to_official` /
       policy_sync 的旧名对行一级整体删除**(改名 2026-09-02 真跑落地,表内就是官方
       拼写,映射表指向的是表里已不存在的旧名)。今后的改名走**人工入口**:报告的
       「疑似改名对」把「未对上」与「官方已不含」里同概念的两条并排点名,由人裁决;
     · `policy_sync` 两级对行(`norm_category` 词形 + 旧名精确等值;**旧名那一级
       2026-09-03 C 批删除,只剩词形**)+ 「将改名」清单 +
       独立 `_RENAME_SQL`(同一事务、机器列 upsert **之前**、只 SET `category_en`、id 不变)+
       「改名冲突」扣留(目标名已被另一行占用 → 不改名也不刷新);摘要首行加「改名 R」;
       「疑似改名对」保留 —— C 批之后它是**改名的唯一人工入口**(此前提示的是
       "还没进映射表的拼写差");
       ⚠ **两级不是 if/else**(2026-09-02 补批修正):旧名那一级必须照查,哪怕词形已经
       命中了别的行 —— 否则"旧名认领成功、但目标官方名已被表内另一行占用"(表里同时
       有 `Drugs & Paraphernalia` 与 `Drugs and Drug Paraphernalia`)那一行谁也没点到,
       会掉进「官方已不含」,报成"官方删了这个类别",而真相是**一对同概念双行等着合并**
       (判反的方向:人会去动库删行)。现在它落「改名冲突」、计入 touched、零写库;
       「两行登记同一旧名」同样归「改名冲突」,且那张官方页**不新增**(在一对待合并的
       行旁边再添第三行是把问题变三倍)。报告小节标题相应改为「未对上/不敢动」;
       ⚠ **2026-09-03 C 批**:旧名那一级退役后,这两种冲突场景不再可能发生
       (词形命中天然同键,表里真有两行撞键走的是「不敢动」)。留下的差别一条:
       表里同时留着「旧缩写名 + 官方名」两行时,旧行只进「官方已不含」(不删行、
       零写),不再带「该名已被 id N 占用」那句提示 —— 代价是报告少一句话,
       换掉的是一张永远不会再被验证的历史映射表;
     · `error_taxonomy.POLICY_ALIASES` 改为从 `POLICY_LEGACY_NAMES` 反向派生(不再手写)
       —— ⚠ 2026-09-03 C 批连同 `alias_gaps` 与报告头的「别名表健康」两行删除,
       `policy_join` 只走词形归一(语义缩写对不上 = 进「政策表缺口」清单);
     · `audit_reason._L3_NORMALIZE` 删掉 20 条政策名(只留 brand_misuse / content standards
       两族**非政策伪类目**),`_normalize_l3_cat(cat, known=())` 改为对实时 `category_en`
       集合解析、命中回**表内原拼写**(解析规则走 `policy_names.resolve`,不在本文件
       另写一份);`compute_final_reason` 增 `known` 入参,两个调用点传
       `ctx.known_policies` —— 与 `audit_l3.valid_reason_categories` 同源同拼写;
     · `audit_l3` 的 S1/S3「37 条」改为 `{N}` 占位符 + 渲染时按实时政策条数填(**除这一个
       token 外两段一字节未动**,测试用"填回 37"逐字节证明);「逐字节相同」契约退役为
       "同一轮内逐字节相同";
     · `AUDIT_RULES_VERSION` → `c.2026-09-02.1`(**首跑无需再手动递增**,摘要已改口径);
     · **成本口径(真跑摘要已点名)**:政策表是 S2/S4 的唯一数据源 ⇒ L3 的 system prompt
       逐字节变化 ⇒ `catalog.llm_cache`(purpose=`audit_l3`)的存量**全量未命中**
       —— 缓存键含整段 messages(`services/llm_cache.cache_key`),不是"少省一点",
       是一条都不命中;**与本批要求的全量重审叠加 = 那批产品全额重付**
       (DeepSeek 前缀缓存同样一次性重建,按 miss 价另算)。口径与
       `registry/resources.LLM_CACHE_ANCHOR` 那段同源:大批重审排北京时间
       18:00–次日 08:00 谷时段,单价减半;
     · **`services/policy_names.py`(2026-09-02 补批)= 政策名归一化与旧名翻译的唯一模块**:
       `norm_category`(从 policy_sync 原样搬入,规则不变)/ `to_official`(查
       `POLICY_LEGACY_NAMES` 精确等值)/ `resolve(name, known)`(精确 → casefold →
       词形键 → 旧名翻译后重试,命中回**表内原拼写**,认不出给 None)。
       改名前后同一份代码都活,**写死旧缩写名的地方不必跟着改名批改字面量**;
       只 import `re` 与 `registry`(铁律 1:services 不许 import workflows);
       ⚠ **2026-09-03 C 批**:`to_official` 与 `resolve` 的第 4 级删除,只剩
       1–3 级(精确 / casefold / 词形键),模块从此只 import `re`。语义缩写
       **解析不到就是解析不到** —— 那是正确答案,它要人裁决是改名还是新增;
     · 跟着改走它的四处:`policy_sync`(import 而非自带一份)、
       `audit_l3.route_policy_hints`(两张路由表的 29 个政策名逐个 resolve ——
       旧写法 `c in known` 改名后**静默丢 7 条**政策提示)、
       `audit_reason._normalize_l3_cat` 与**步 1.5**(`audit_l2._infer_walmart_policy`
       那批旧缩写名的唯一出口)、`error_taxonomy._norm_key`;
       ⚠ 前三处**已随 2026-09-02 第三步 B1 批退役**(路由表整删、理由映射收敛为
       查表),`resolve` 现在的消费方是 `audit_l3.parse_l3_reply`(L3 答出的类别
       对枚举)、`audit_phase0`(黑名单行的 `walmart_policy` 对表)、
       `audit_rules.check_rule_policies`(装配期守门)与 `error_taxonomy`;
     · 遗留(改名批不动,已在代码里标注;**①②已随 B1 消化**,见下条):
       ① `audit_reason._pt_to_policy`(步 4c)与 4d cert 分桶里写死的旧缩写名 ——
          那两步是 PT/类目关键词**兜底推断**,输入不是政策名,对表反而会把"推断"
          伪装成"表里查到的";改名后它们落在表外(只多几条 warning 计数,判定不变),
          改法随「L3 输出规范化」一起定 → **B1 整段删除**(理由映射收敛为查表);
       ② `audit_l3` 路由表里 `Pet Products` 与 `Jewelry/Precious Metals` 两条 ——
          官方 42 名里根本没有这两个类别名(官方是 `Pet Foods, Supplements,
          Medicines and Other Products`;Jewelry 那条是旧仓自造的斜杠写法),
          归一化也打不平。**改名前就是死的**,不是改名批改坏的 → **B1 整张路由表
          删除**(政策类别 ≠ 类目,换全文后提示只会把注意力锁在 ≤5 篇上);
       ③ `audit_l2._infer_walmart_policy` 的常量仍是旧缩写名 —— **不逐条改**;
          B1 后理由映射不再读 `walmart_policy` → **C 批(2026-09-03)随 R3/R5/R7/R8
          整条删除**,四张字面量表一起走(政策名不许由类目/PT 名/认证词推断)。

     · **「L3 输出规范化」落地口径(2026-09-02 第三步 B1 批,规格
       `docs/audit_step3_spec.md` §二/§3.3/§3.4/§3.5)** —— §十.7 第四条
       「审核输出与最终结果统一为三段」的落纸:

       · **类别词表**:官方 `category_en` 实时集合(44)+ registry 常量
         `AUDIT_NONPOLICY_CATEGORIES`(`内部黑名单` / `类目准入`)+ pass 的
         `none`。**零兜底**:判拒而没有类别 = 代码 bug,落 NULL + 计数 +
         warning,不许再有 `General-Use Products` 那样的兜底值;
       · **来源只有两处**:硬拒规则在 `hit.detail["category"]` 里**自报**
         (§二 表),L3 在结构化输出的 `policy` 里给。规则代码里写死的政策名
         只剩一个(`resources.AUDIT_IP_POLICY`),`audit_rules.load_context`
         装配时对表解析一次 —— 解析不到或拼写不同**启动即 RuntimeError**;
       · **解析层对表**:`parse_l3_reply` 用 `policy_names.resolve(policy,
         枚举)` 回表内原拼写;**对不上 → pending `llm_bad_policy` + 计数**
         (旧版降级猜 `intellectual property`,已删)。于是下游一层归一化都
         不需要:`audit_reason._normalize_l3_cat` / `_L3_NORMALIZE` /
         `known_policies_check` 随之退役;
       · **落库三段**:`catalog.products` 新增 `audit_detail`;`audit_reason`
         专放类别(pass 与 pending 为 NULL);`audit_runs.l3_reason_category`
         / `l3_reason_text` **列名不改、语义 = 类别 / 具体内容**;
         `product_events.audit_rejected` 的 `detail.reason` 键名不改,新增
         `detail.detail`;飞书上架表 F/G/H 三列对齐同一口径(老行按有没有
         `audit_detail` 决定用新格式还是老格式渲染);
       · **提示词侧**:S4 改喂 `full_policy` 的 `render_feed_text` 渲染件
         (人工中文列不再进提示词);全文为空的行整条跳过并计数;S2 枚举
         删 `brand_misuse`(品牌误用归 IP,由确定性翻拒规则落地)。
     · **已解决(与上一版记录相反,勿照旧引用)**:`error_taxonomy._norm_key` 已归并为
       `policy_names.norm_category`(不再是"只折叠空白 + casefold"),于是
       `Plants & Seeds` / 牛津逗号 Tobacco / 无 `(Covered Goods)` 的 Jewelry 三种报错
       写法改名后**都 join 得上**(语料 19 个 distinct 政策名:改名前 16/19、
       改名后 19/19;旧手写实现是 15/19 与 16/19,测试钉住"只许升不许降")。
       连带:`alias_gaps()` 在目标态报的是 **5 条**不是 7 条 —— `Auto & Motor Vehicles`
       与 `Textiles & Apparel` 只差 `&`↔`and`,归一化已经够用,别名本身多余。

8. **内容族两页入表(2026-09-02,A 批)**:`refdata/policy_pages/en/` 从 42 份增至 44 份 ——
   43 `Content standards: Overview`(登录墙,所有者粘贴;H1 已确认,FAQ 段待补录)与 44
   `Product details policy`(公开页,页面结构化数据渲染 + 粘贴交叉核对)。它们不是
   Prohibited Products Policy 类别,是沃尔玛「violates Walmart's content policy」/「unverified
   authenticity claims」两类下架原因所指页面;进同一张表、同一条 S4 块、同一个类别枚举
   (理由与 L3 用法见 `docs/audit_step3_spec.md` §一 / §二)。**入库动作推迟到第三步 B/C 批
   切换时一起跑**(现在跑一次 = L3 缓存白白再失效一次)。喂入层随之补两条机械规则:
   `![alt](url)` 图片整行删(alt 只是文件名)、表尾整行空单元格不算数据行。

