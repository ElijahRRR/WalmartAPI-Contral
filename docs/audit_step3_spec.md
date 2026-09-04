# 审核链第三步执行规格(2026-09-02 草案,待所有者定稿)

> 依据:`docs/audit_pipeline.md` §10(瘦身定稿)、`docs/policy_sync.md` §十.3–7(喂入版口径 /
> 四修订)、2026-09-02 政策表真跑落地(42 行官方名 + 官方英文全文,报错侧政策名 join 100%)。
> 本文只写**怎么做**;为什么这么定见上面两处。定稿后按 §一 的批次派实现 + 对抗复核,
> 每批 `python -m pytest -q` 全绿 + 所有者验收后合并。
> 事实核对基准:2026-09-02 main(`58551fa`),file:line 以此为准。

## 〇、一句话

L3 从「读 6 列中文人工摘要 + 代码猜路由」改为「读 44 篇官方英文全文 + 上游**确定性**证据」,
输出统一为**判定结果 / 类别 / 具体内容**三段;L0/L2 只留确定性规则(黑名单、符号、白名单准入);
用后台报错记录做回放评估;`POLICY_LEGACY_NAMES` 一族与所有"关键词猜政策"的代码退役。

## 一、批次与顺序

| 批 | 内容 | 合并后生产动作 |
|---|---|---|
| **A 转录** | 内容族两页按 policy-refresh 纪律转录 en/zh 进 `refdata/policy_pages/`(**2026-09-02 已落地**:43 `Content standards: Overview` 所有者粘贴、44 `Product details policy` 公开页结构化数据 + 粘贴交叉核对);`policy-refresh` 技能补第二来源;喂入层补两条规则(图片整删、表尾空行不算数据行) | **不跑** `policy_sync`(跑了会再让 L3 缓存全量失效一次,白付);等 C 合并后随切换一起跑 |
| **B1 换喂 + 规范化**(**2026-09-02 已落地**) | S4 换官方全文;user 段扩容;输出 schema 三段化;`audit_detail` 落库;理由映射去猜测;证据通道泛化;路由提示删除 | 生产机**不 pull**(见 §五) |
| **B2 回放 + 重审面**(**2026-09-02 已落地**) | `audit_replay` 回放工作流;`mode=stale` 的 `active_days=90`;首条串行预热 | 同上 |
| **C 瘦身 + 清理**(**2026-09-03 已落地**) | L0 双输出(品牌文案扫描迁入)+ Made in USA 迁入;L2 = R1;删 R3 硬拒/R4/R5/R7/R8/R10 及其数据;删 `POLICY_LEGACY_NAMES` / `POLICY_ALIASES` / `to_official` / `_L3_NORMALIZE` / `_pt_to_policy` / 路由表 | 生产机 pull A+B+C → 按 §五 切换 |

顺序理由:§10「先换喂后删 R7/R8」;B、C 分两批是为了对抗复核可读,但**只切换一次**
(两批各自递增 `AUDIT_RULES_VERSION`,生产只看到最终版;pull 一次)。
⚠ `audit_sheet` 18:10 每天跑 `product_audit from_sheet=1`,`_DEFAULT_CANDIDATE` 含
「approved × 旧版本」(`workflows/product_audit.py:238-242`)—— 生产机一 pull 新版本,当晚
就会用新链重审上架表里的品。所以 pull 的时机就是切换的时机,不是"先拉下来放着"。

## 二、类别词表:全链唯一键的最终形态

「类别」= 判定落在哪一类,**只许两种来源、零推断**:

1. **官方政策类别名**:`audit.walmart_prohibited_policy.category_en` 实时集合(42 条禁售 +
   内容族 2 页 `Content standards: Overview` / `Product details policy`,共 44;S2 枚举、
   L3 白名单、落库、飞书全用**表内原拼写**);内容规则(促销宣称 / 真伪宣称 / 竞品独家 /
   非英文 / URL 等)全在 44 那页的四张「允许 / 禁止」表里,43 是索引页;
2. **非政策类别**(registry 常量 `AUDIT_NONPOLICY_CATEGORIES`,固定两条):
   - `内部黑名单` —— 卖家 / ASIN / 亚马逊类目黑名单命中(内部决策,不对应沃尔玛政策;
     现步 1.2 已把它们排除在政策映射外,`services/audit_reason.py:232-234`);
   - `类目准入` —— L2 R1 白名单拦下(`cat_access_blocked` / `cat_zh_blocked`)、L1 出版物硬拦
     (`publication_pt_forbidden`)、以及 L3 依据本 PT `requirements` 判"需证而无"的拒绝。

pass → `none`;pending → 类别为 NULL(具体内容写待定原因)。**没有 `General-Use Products`
兜底**:拒绝而无类别只可能是代码 bug,落 NULL + 计数 + warning,不许编一个。

每条硬拒规则**自带**类别(在规则 detail 里声明,不再事后推断):

| 规则 | 类别 | 出处 |
|---|---|---|
| `phase0_lark_blacklist_seller/asin` | `内部黑名单` | 固定 |
| `phase0_lark_blacklist_amazon_cat` / `phase0_forbidden_category` | 黑名单行自带 `walmart_policy` 且能 `policy_names.resolve` 到表 → 该政策;否则 `内部黑名单` | `catalog.amazon_cat_blacklist.walmart_policy`(`services/audit_phase0.py:142-143`) |
| `phase0_brand_blacklist` / `phase0_trademark_symbol` / `phase0_patent_claim` | `Intellectual Property` | 现状不变 |
| `phase0_made_in_usa`(C 批迁入) | `Product claims` | 官方第 29 节「Made in the USA」专段(`refdata/policy_pages/en/29-product-claims.md:39-49`),不再自造 `Made in USA claims` |
| `publication_pt_forbidden`(L1) | `类目准入` | 现步 4a 猜成 IP,改为按性质归类 |
| `cat_access_blocked` / `cat_zh_blocked`(L2 R1) | `类目准入` | 现 detail 写 `walmart_policy="Restricted/Illegal"` 是猜的(`services/audit_l2.py:166-269`),删 |
| L3 reject | LLM 输出的 `policy`(白名单校验) | §3.3 |

规则声明的政策名在 ctx 装配时对表 `resolve` 一次(`services/audit_rules.py:256-258` 装
`known_policies` 处),解析不到 = 启动即 `RuntimeError`(表被改名了,不许静默)。

## 三、B 批规格:L3 换喂 + 输出规范化 + 回放

> **落地状态(2026-09-02)**:§3.1–§3.7 与 §3.10 的测试/文档/版本部分 **B1 批
> 已落地**(判据版本 `c.2026-09-02.2`);§3.8 回放工作流、§3.9 的首条串行预热、
> §六.8 的 `mode=stale -p active_days` **B2 批已落地**(**不提版** ——
> B2 一个字都没动判定,提版只会让全库 approved 白重审一轮)。

### 3.1 system prompt(S1–S4,仍是单一连续静态前缀)—— **B1 已落地**

- **S4 = 官方英文全文**:`POLICY_ROWS_SQL` 改为 `SELECT id, category_en, full_policy FROM
  audit.walmart_prohibited_policy WHERE full_policy IS NOT NULL ORDER BY id`;每行渲染为
  `## {category_en}\n\n{policy_feed.render_feed_text(full_policy)}`(`services/policy_feed.py:85`
  首次接线;现 `format_full_policy_block` 六个中文人工列与 50/30/240/80 截断整体删除,
  `services/audit_l3.py:474-499`);`full_policy` 为 NULL 的行跳过并计数进摘要
  (不是空壳标题 —— 空壳给 LLM 等于没给)。
- **S2 枚举**不变(`REASON_CATEGORIES_SQL` ORDER BY category_en),追加两条非政策类别与
  `none`;`brand_misuse` 删(品牌误用归 `Intellectual Property`,由 §3.3 翻拒规则落地)。
- **S1 重写**(中文指令,要点):角色;判据只认下面的官方英文原文(训练记忆里的政策版本作废);
  `policy` 必须**逐字抄**枚举里的一项;`detail` 用中文、≤120 字、**引用触发的原文片段**
  (保留原语言)+ 触犯的条款要点;品牌证据的判法(提到 ≠ 卖的就是:兼容/适配/对比提及不是
  品牌误用);本 PT 准入要求的判法(先判"这个具体产品要不要这张证",要而 listing 无 → 拒,
  类别 = 覆盖它的政策,没有政策覆盖 → `类目准入`);输出严格 JSON。`{N}` 占位符保留,
  措辞改为「{N} 篇沃尔玛政策全文(Prohibited Products Policy 各类别 + 内容标准两页)」。
- 体量(**B1 实测**):S1+S3 = 2,372 + 181 字符(重写后比旧版 6K 短 —— 旧版
  一半篇幅是关键词清单与判定维度,换全文后由原文承担);整段 system prompt
  212,556 字符 ≈ 6.1 万 token(44 篇渲染件);deepseek-v4-flash 1M 上下文内,
  前缀缓存命中的硬前提(顺序固定、同轮逐字节稳定)不变。
- **篇数按真正渲染出来的段数填**(B1 实现修订):官方正文自己带 `## Overview`
  一类小标题(实测 251 个),按 `## ` 数出来的是假数;`policy_parts()` 返回
  列表,`len()` 即篇数。

### 3.2 user 段 —— **B1 已落地**

在现模板(`services/audit_l3.py:638-661`)上改四处:

- 长描述截断 `MAX_DESC_CHARS` 600 → 3000,五点全给(现 `MAX_BULLETS=5`,亚马逊本就 ≤5,
  但有多的照给);判违规靠的是正文,600 字砍掉的是宣称最密的部分;每品多 ≈1K token,
  与 5 万 token 前缀相比可忽略;
- 删「政策路由提示」行(§3.7);
- 「L2 规则引擎命中」段改为「上游证据」段(§3.6),并新增「本 PT 的沃尔玛准入要求」行:
  `ctx.pt_meta[pt]["requirements"][:500]`(`audit.walmart_pt_meta.requirements`),没有则不出行;
- 「待评估的品牌/商标词」段保留,来源改为上游证据里的品牌命中(B 批仍来自 L2 R4/R5 软 hit,
  C 批后来自 L0 软 hit;通道见 §3.6)。

`原产国` 行恒 `(空)`(采集契约无此值,`:637`)—— 本批不动,删这一行(给 LLM 一个恒空字段
只会诱导它把"原产国未知"当证据)。

### 3.3 输出 schema 与解析 —— **B1 已落地**

```json
{
  "verdict": "pass" | "reject",
  "policy": "<枚举之一,逐字;pass 时 'none'>",
  "detail": "<中文 ≤120 字:引用原文片段 + 条款要点;pass 时可空>",
  "brand_verdicts": [{"brand": "…", "is_real_brand": true, "evidence": "…"}],
  "confidence": "high" | "medium" | "low"
}
```

- 删 `signals`(全仓无消费者)、`reason_category` → `policy`、`reason_text` → `detail`、
  `blacklist_brand_verdict` → `brand_verdicts`、`llm_confidence` → `confidence`。
- 解析(`parse_l3_reply`,`services/audit_l3.py:733-819` 重写):
  1. 非 JSON / verdict 非法 → pending `llm_bad_json`(不变);
  2. `policy` 经 `policy_names.resolve(policy, known ∪ 非政策类别)`(大小写/词形差容错)→
     命中回表内原拼写;**不命中 → pending `llm_bad_policy`**(现状是降级猜成
     `intellectual property`,`:762`,删);`_LEGACY_CATEGORY_MAP` 删;
  3. pass 强制 `policy='none'`;
  4. 翻拒规则保留:任一 `brand_verdicts[].is_real_brand is True` 且 verdict=pass →
     reject + `Intellectual Property` + detail 补「未授权引用品牌名 X」(确定性后处理,
     `:773-784` 逻辑不变);
  5. reject 落 1 条 L3 hit,`rule_code = "llm_" + slug(policy)`,detail 五键定序改为
     `{policy, detail, confidence, brand_verdicts, prompt_version}`(`prompt_version` =
     `AUDIT_RULES_VERSION`,给回放与 audit_why 对版本)。
     **⚠ 2026-09-03 已扩到七键**:末尾追加 `product_is` / `policy_quote`(§八)。
- `L3Result` 字段随 schema 改名;`raw` 仍不落库;pending 不写 `llm_cache`(不变)。

### 3.4 三段落库与投影 —— **B1 已落地**

| 落点 | 判定结果 | 类别 | 具体内容 |
|---|---|---|---|
| `audit.audit_runs` | `verdict` / `l3_verdict` | `l3_reason_category`(列名不改,语义 = policy 枚举) | `l3_reason_text`(列名不改,语义 = detail) |
| `catalog.products` | `audit_status` | `audit_reason` := 类别(枚举 / `none` 不写,pass 与 pending 为 NULL) | **新列 `audit_detail text`** |
| `catalog.product_events`(`audit_rejected`) | event | `detail.reason`(键名不改,兼容 `audit_history_fold`) | 新键 `detail.detail` |
| 飞书上架表(2026-09-02 新表头) | F 列 审核结果(pass/reject/pending) | G 列 类别 | H 列 具体内容 |

- `audit_detail` 的确定性来源:L3 拒 → `l3.detail`;规则拒 → `audit_reason.explain_hit(rule_code,
  detail)`(它本来就是"具体内容"形态,如 `商标符号(命中:XYZ®)`);pending → 现三条固定句
  (`services/audit_store.py:22-27`)搬到这里,`audit_reason` 置 NULL;pass → NULL。
  `human_reason`(`services/audit_reason.py:394-406`,「人话1;人话2 [政策:X]」)退役,
  `_RULE_CN` / `explain_hit` 保留(渲染存量 hit 与规则拒)。
- DDL:`refdata/schema.sql` 加 `ALTER TABLE catalog.products ADD COLUMN IF NOT EXISTS audit_detail text`;
  `docs/db_schema.md` 同步;`audit_store._PRODUCT_SQL` 与 `product_audit._ADOPT_SQL` 各多写一列;
  `audit_why` 打印三段;`audit_reason_backfill` 不扩(存量行随重审自然更新,不回填)。
- 存量结论不迁移:旧行 `audit_reason` 里的中文句子 / 旧政策名在被重审前原样留着,
  飞书投影按"有 `audit_detail` 用新格式,无则旧格式"渲染,不出现半新半旧。

### 3.5 理由映射:`compute_final_reason` 收敛为查表 —— **B1 已落地**

新顺序(首个命中即出,全部是**规则自报**或 **LLM 结构化输出**):

1. `verdict != 'reject'` → None;
2. `all_hits` 按 phase0 → l1 → l2 → l3 顺序,第一条 detail 带 `category` 的 hit → 该值
   (硬拒规则在 §二 表里各自声明,ctx 装配时已对表);
3. `l3.verdict == 'reject'` → `l3.policy`;
4. 都没有 → None + `STATS["reason_missing"]` + warning(bug 信号,不兜底)。

删除:步 1.2 内部黑名单特判(改为它们自带 `内部黑名单`)、步 2/1.5 的归一化(L3 输出已在解析层
对表)、步 3 L4 关键词猜测、4a–4g 全部(`_pt_to_policy` 十组旧缩写名、4d cert 分桶、
`'General-Use Products'` 兜底)、`_L3_NORMALIZE`、`_normalize_l3_cat`(含 `.title()` 与
未 strip 的已知缺陷)、`known_policies_check`(枚举在解析层已保证)。

### 3.6 证据通道泛化 —— **B1 已落地**

`summarize_l2_for_l3(l2)`(`services/audit_l3.py:549-598`,只读 L2)→
`summarize_evidence(outcome_partial)`:读 phase0 / l1 / l2 三层里 `penalty == 0` 的软 hit,
按 `rule_code` 查一张渲染表出一行;未登记的 rule_code 原样打 `* {rule_code}: {detail 摘要}`,
不丢。B 批渲染表 = 现五分支(R4 品牌 / R3 证书 / R5 商标 / R7 促销 / R8 敏感);C 批删到只剩
L0 品牌文案扫描一条。品牌词清单(`MAX_BRANDS=10`)从同一通道取。

### 3.7 路由提示:删除 —— **B1 已落地**

`route_policy_hints` 与 `_CATEGORY_ROUTES`(31 键)/ `_PT_KEYWORD_ROUTES`(13 组裸子串)/
`_ALWAYS_INCLUDE` / `ROUTE_MAX_POLICIES` / `STATS["route_unresolved"]` / product_audit 摘要第 10 行
(`workflows/product_audit.py:1366-1372`)整体删除。理由:它是第二张手工维护的「类目 → 政策」
映射,而 §十.7 已定「政策类别 ≠ 类目」;换全文后 LLM 面前有全部 43 篇,提示只会把注意力
锁在 ≤5 篇上。回放评估(§3.8)顺带验证删了之后类别准确率没掉。
(若所有者要保留:改成只读官方名的常量表,不再走 `to_official`。见 §六。)

### 3.8 回放评估 `workflows/audit_replay.py` —— **B2 已落地**

- 性质:`DANGEROUS = False`;只写自己的表 `audit.replay_results` 与报告文件
  `<DATA_ROOT>/reports/audit_replay.txt`;**不写** `catalog.products` / `audit_runs` / `audit_hits` /
  飞书 / 事件。`--dry-run` = 只抽样本、算规模与预估成本,零 LLM 调用;真跑才调 LLM。
- 单一实现:直接调 `services.audit_rules.audit_one`(与生产同一条链),不复制判定逻辑。
- 样本(参数 `neg=N` / `pos=M` / `seed`):
  - 反例 = 沃尔玛已裁决的下架品:`catalog.walmart_items.unpublished_reasons` 经
    `error_taxonomy.classify_reasons` 得主码 ∈ {POLICY, IP, CONTENT, BRAND, PROHIBITED_FINAL},
    按 `services/sku_asin` 规则关联 `catalog.products`(**不用裸 `sku = asin`**,
    `refdata/schema.sql:122-123` 已废该约定);期望 = reject,期望类别:POLICY → join 上的
    官方名;IP → `Intellectual Property`;CONTENT → 内容族两名之一(`Content standards: Overview` /
    `Product details policy`,命中任一即算对);BRAND / PROHIBITED_FINAL
    → 只比判定不比类别。按期望类别分层抽样,每类封顶;
  - 正例 = 在架在售品:`published_status='PUBLISHED'` 且 `missing_since IS NULL` 且
    `unpublished_reasons` 为空,随机抽;期望 = pass;
  - PT_WRONG / GATED 不进本集(前者是 L1 的题,后者沃尔玛裁决语义与我方"能不能做"不对齐)。
- 三方对照:沃尔玛裁决(参照,非金标 —— 申诉成功/自愈态存在)、旧链最近一次 `audit_runs`
  结论(历史,已落库,不必重跑旧代码)、新链本次输出。
- 指标:反例召回(判拒率)、带类别反例的类别准确率(枚举精确等值)与混淆表、正例误伤率、
  新旧链一致率、按 confidence 分层的错误率、pending 率、成本与耗时。
- 已知局限写进报告头:产品正文只有当前值(`catalog.products` 就地覆盖,`snapshots.raw` 已裁
  大文本,`refdata/schema.sql:60-63`),被拒后改过 listing 的品会失真;沃尔玛裁决时的政策版本
  与今天不同。
- DDL:`audit.replay_results(run_tag, asin, expected_verdict, expected_category, got_verdict,
  got_category, got_detail, stage_stopped_at, old_verdict, old_category, confidence, created_at)`,
  `PRIMARY KEY (run_tag, asin)`。

### 3.9 成本与缓存(首条串行预热 **B2 已落地**)

- 政策表或提示词模板任何一字变化 ⇒ `catalog.llm_cache`(purpose=audit_l3)全量未命中(键含整段
  messages,`services/llm_cache.py:28-44`);本批必然全量,只付一次 —— 所以 A 批不单独跑
  policy_sync,B/C 只切换一次。
- DeepSeek 前缀缓存按请求前缀命中:S1–S4 静态 ⇒ 除每批第一条外都命中。`product_audit` 并发
  128 起跑时前 ~128 条可能同时未命中 —— 加**首条串行预热**(L3 开且候选 >1 时,第一个产品
  同步跑完再开线程池),一行代码,省一批 miss 价。
- 单品口径(按 `registry.resources.LLM_PRICING` 现价,切换时用 `llm_cost.summarize` 实算):
  前缀 ≈5.5 万 token 走命中价,user 段 ≈1.5–3K token 走输入价,输出 ≤1.5K;命中态下
  每品约 0.01–0.02 元,谷时段减半;首条(未命中)约十几倍。
- 全量重审规模由所有者定(§六);`mode=stale` 有天然分页,`limit` 分晚跑,排北京 18:00–08:00。

### 3.10 测试、文档、版本 —— **B1 部分已落地**

- 测试(新增/改写):S4 接线(43 名全在、无 URL、按 id 序、同轮逐字节稳定)、S1 `{N}`
  填充、user 段(无路由行、有准入要求行、描述 3000 截断)、解析(新 schema、resolve 容错、
  未知 policy → pending、翻拒)、`compute_final_reason` 零兜底(无类别 → None + 计数)、
  证据通道跨层、`audit_store` 三列、飞书 F 列格式、`audit_replay` 只读守门(SQL 动词)、
  抽样与报告格式、成本估算。`tests/test_audit_l3.py` 里的路由测试、`_S1 == 6028` 字节钉子、
  `format_full_policy_block` 截断契约随代码删。
- 文档:`docs/audit_pipeline.md` §5 / §5.0 / §5.1 / §7 重写,§10 标「B 批已落地部分」;
  `docs/db_schema.md`(`audit_detail`、`replay_results`);`docs/policy_sync.md` §十.7 补
  「输出规范化落地口径」;README 工作流数 / 测试数。
- `AUDIT_RULES_VERSION` → `c.<合并日>.1`。

**B1 落地记录(2026-09-02,与本规格的差异逐条)**:

- `AUDIT_RULES_VERSION` = **`c.2026-09-02.2`**(当天已被改名批用掉 `.1`);
- 测试 2,638 收集(2,615 跑 + 23 跳过)全绿;S4 接线的断言打在**真实转录件**上
  (44 名全在、无 URL、每段首行是类别名、同轮逐字节稳定、缺全文计数);
- **S1 比规格估的 6K 短**(2,372 字符):旧 S1 有一半篇幅是关键词清单与六个判定
  维度(儿童 CPC 词表、冒犯性清单、整机电器判法),换全文后这些判据由官方原文
  承担,再抄一份就是第二处判据。**唯一保留的一句**是「整机电器/NRTL」的形态提示
  —— 它 2026-08-21 从 L2 迁上来时是「先补后删、无真空期」的那个补,归进
  「本 PT 准入要求怎么判」那一节(拿不准一律 pass 的口径逐字保留);
- **篇数不能数 `## `**:官方正文自己带 `## Overview` 一类小标题(实测 251 个),
  改为按 `policy_parts()` 返回的段数填(见 §3.1);
- **`explain_hit` 的 hit_val 键表摘掉 `category`**:那个键现在是规则自报的**类别**,
  留着会把专利自述那条渲染成「文案自述专利保护(…;命中:Intellectual Property)」
  —— 把类别名说成命中的原文,人照着去搜根本搜不到;
- **`human_reason` 退役后的替身是 `explain_hits(hits)`**(同一份渲染,只是不再拖
  「[政策:X]」尾巴):飞书 H 列渲染**存量老行**要它 —— 老行没有 `audit_detail`,
  不兜底的话几十万行会在表上一夜变成空白;
- **已知缺口(三条硬拒规则不在 §二 的自报表里)**:
  ① `cat_requires_cert_hard`(R3 硬拒)—— C 批降为证据;
  ② `made_in_usa_claim`(R10)—— C 批迁进 L0 并带 `Product claims`;
  ③ `l4_vision_violation`(L4 视觉,penalty -100)—— **§二 没给它类别**:
     "图上有什么"该映到哪条政策要所有者裁决,B1 **不替它编一个**;L4 默认关,
     面很小。
  这三条拒掉的产品走「类别 NULL + `reason_missing` 计数」那一路。**这是有意的**
  (§一:B、C 只切换一次,生产机等 C 合并后再 pull);验收信号 =
  **C 批合并后、L4 关闭时该计数应回到 0**。

**B2 落地记录(2026-09-02,与本规格的差异逐条)**:

- **不提版**:`AUDIT_RULES_VERSION` 仍是 `c.2026-09-02.2`。B2 一个字都没动判定
  (回放只读、`active_days` 只筛候选、预热只改发请求的次序),提版会让全库
  approved 白重审一轮 —— 版本号是判据的身份,不是"改过代码"的流水号;
- **抽样在库里做**(规格没写机制):`ORDER BY md5(sku || seed) LIMIT` 是伪随机
  且同 seed 恒定,进 Python 的行数因此**封顶**(`_POOL_MAX = 50,000`)。
  下架原因表几十万行带长文本,全拉回来是 2026-08-21 那次 OOM 的同款走法。
  代价是"池里没抽到"的类别进不了样本 —— 所以**漏斗四道全部计数进报告**
  (扫描 → 主码在集 → sku 提得出 asin → 库里有产品行),哪一道吃掉最多一眼可见;
- **内容族两名互认落在 `registry.resources.AUDIT_CONTENT_POLICIES`**:期望类别
  记规范名(43 索引页),判在 43 或 44 都算对(`category_ok`)。报错正文只说
  "内容不合规",不说是索引页还是明细页;
- **POLICY 的两种"没类别"分开计数**:抽不出政策名是**常态**(生产 3,363 条,
  `extract_policy` 头注)、抽出了但 join 不上才是**政策表缺口**。都不进本集,
  但混成一个数会让人以为政策表烂了;
- **报告头三条局限**(规格只点了两条):第三条「沃尔玛裁决是参照不是金标」
  在规格的「三方对照」那行里,读数的人最容易忽略的恰恰是它,一并写进报告头;
- **判定失败的行照样落库**(`got_verdict` 为 NULL):一条判炸了记下来继续 ——
  整轮停掉的话前面几百条已付费的 LLM 结果一起白付;"这条判不出来"本身也是
  回放结果,漏掉它样本量就对不上;
- **`old_category` 只有走过 L3 的老行才有值**(它是 `audit_runs.l3_reason_category`,
  规则拒的老行这一列是空的)—— 所以报告只拿旧链比**判定**(误伤率、一致率),
  不拿它算类别准确率;
- **第三处写面:`catalog.llm_cache`**(判定链自己写,L3 判完缓存出参)。那是缓存
  不是结论,而且与生产共用一份(回放付过的钱随后真重审直接命中),但头注与
  README 都写明 —— 一条自称"只写两处"的工作流,漏说第三处就是在骗读它的人;
- **两处共用件下沉**:首条串行预热 → `services/audit_pool.submit_chunk`(生产判定
  与回放发的是同一段前缀);审核输入行的形状 → `services/audit_rules.
  PRODUCT_ROW_COLUMNS/_FROM`(回放喂进去的产品正文必须与生产**同一份**,少一个
  `seller_id` 就等于卖家闸在回放里恒不命中,而两边结论看着都正常);
  并发缺省/上限 → `registry.resources.AUDIT_WORKERS_DEFAULT/MAX`(两条工作流同一个数);
- **报告文件名登记在 `registry/paths.audit_replay_report()`**(铁律 3:切换手册
  与所有者按这个路径找报告,改名得只有一处);
- **`active_days` 的校验前置**到 `_pick_where` 顶部并**只与 `mode=stale` 连用**
  (配错 mode 直接抛):静默忽略的话人以为只判了有动销的、实际整批重付,
  而摘要长得一模一样。摘要**首行**点名「近 N 天有动销」/「不限动销」。

**B2 对抗复核修订(2026-09-02,ACCEPT-WITH-FIXES 逐条)**:

1. **正例必须干净到 asin 级**:`_POS_SQL` 的 `NOT EXISTS` 是 **sku 级**,而身份是
   asin 级(A 店订货号在架、B 店订货号被拒,两行 sku 不相等,SQL 比不出来)。
   改为:排除**整个反例池**的 asin(不只是抽中的那批)+ 排除
   `rejected_asins()`(下架侧 sku 全量经 `services/sku_asin` 折成 asin)。
   漏斗分档记账。⚠ 判据面**不封顶**:抽样面抽不到就是没抽到,判据面漏一行
   就是把被拒的品当成好品去算误伤率;
2. **旧链基线不许含新链自己的行**:`audit.audit_runs` 补 `audit_version` 列
   (`audit_store` 落库时盖当前版本),基线 = 最近一次
   `audit_version IS DISTINCT FROM <当前版本>`(NULL 算旧)。没有它,跑过一轮
   `mode=stale` 之后就是新链跟新链比;报告头逐字写明这条口径;
3. **底线判在共同子集**:新链分母原是全部正例、旧链分母是"其中有旧结论的",
   两批产品不一样,比出来的"新链更好"可能只是因为另一批本来更干净。
   现在共同子集上新旧并排(底线判这里),全库水位另行单列;
4. **内容族两页进 `check_rule_policies`**:与 `AUDIT_IP_POLICY` 同一道装配期
   守门(对不上 RuntimeError,报错点名该改哪个常量);回放的期望类别改经
   `policy_names.resolve` 取**表内原拼写**,不把常量原样吐出来;
5. **样本身份 = `run_tag`**(见 §五):同 tag 有行就重放那一批,不重新抽样;
6. **判定前先提交**:抽样/打标的事务不许挂满整轮判定(几十分钟
   idle in transaction 按住老快照,vacuum 清不掉生产链这期间的死行);
7. **`limit_per_category=0` 报错**而不是静默落回缺省(`or` 把 0 和"没传"当成
   同一件事);
8. **`_CANDIDATE_SQL` 只 format 自己的尾段**:列清单是 `audit_rules` 的文本,
   那边哪天多一个 `{` 就会把 `product_audit` 炸在 KeyError,而吃同一份文本的
   `audit_replay` 一点事没有 —— 两边都看不出来的耦合。

## 四、C 批规格:瘦身 + 清理 —— **2026-09-03 已落地**

> **落地状态**:§4.1 / §4.2 / §4.3 全部实现,判据版本
> `AUDIT_RULES_VERSION = c.2026-09-03.1`;与本规格的差异逐条见本节末尾的
> 「C 落地记录」。测试 2,687 收集(2,664 跑 + 23 跳过)全绿。

### 4.1 L0 双输出

- `Phase0Result` 增 `evidence: list[RuleHit]`;`check()` 契约:硬命中 → `blocked=True` 立即返回
  (不变);全部硬规则未中 → 跑软规则,软 hit 进 `evidence`,`blocked=False` 继续。
  `stage_stopped_at` 语义不变;`audit_hits` 一次 run 可落多条 L0 行(硬 1 + 软 n)。
- 品牌黑名单文案扫描(R4 迁入):`rule_code = "phase0_brand_mention"`,penalty 0,
  detail `{matches:[{brand, matched_phrase}], count, note}`;数据源仍是 `catalog.brand_blacklist`
  一份、Aho-Corasick 自动机在 ctx 装配一处构建(`services/audit_rules.py:122,250`,从 L2 ctx
  字段挪到 L0 使用);词边界 / 中文紧邻即边界 / 自品牌精确豁免 / 同品牌只报一次的逻辑逐字随迁
  (`services/audit_l2.py:608-659`)。
- Made in USA(R10 迁入):`rule_code = "phase0_made_in_usa"`,硬拒,正则与否定式排除逐字随迁
  (`services/audit_l2.py:1166-1206`),类别 `Product claims`,扫描面 = 标题 + 全部五点 + 长描述(不变)。
- 顺序:卖家/ASIN/类目黑名单 → 商标符号 → 专利自述 → Made in USA → 品牌精确等值(硬);
  然后品牌文案扫描(软)。

### 4.2 L2 = R1

- `evaluate` 只剩 `_rule_category_gate`;删 `_rule_cat_requires_cert`(硬软两支)、
  `_rule_title_desc_blacklist`、`_rule_trademark_live` + `_R5_SQL` + `load_nice_mapping` +
  `refdata/audit/pt_nice_class.yaml` + `registry/paths.audit_seed_file` 里的登记、
  `_rule_content_promotional` + 两张宣称表 + 全大写噪声表、`_rule_walmart_strict_sensitive` +
  `_R8_SENSITIVE_PATTERNS`、`_rule_made_in_usa`(已迁 L0)、`_infer_walmart_policy` + 四张
  字面量表(`services/audit_l2.py:314-450`)。
- R3 的替身已在 B 批:本 PT `requirements` 行随产品进 L3(§3.2)。
- `product_audit` 的 `r5` 参数、`r5_on` 强制单线程、R5 查询失败摘要行随删;`_KNOWN_PARAMS` 同步。
- `AuditContext` 删 `uspto` / `ac_automaton`(挪到 L0 字段名)等字段。

### 4.3 退役

- `registry.resources.POLICY_LEGACY_NAMES`、`services/policy_names.to_official` + `resolve` 第 4 级、
  `services/error_taxonomy.POLICY_ALIASES` / `alias_gaps`、`workflows/error_reclass_report._alias_notes`
  及相关测试;`resolve` 保留 1–3 级(精确 / casefold / 词形键)。
- `policy_sync` 的「经旧名认领」一级删除;「疑似改名对」保留为改名的人工入口。
- 存量 `audit_hits.rule_code`(`title_desc_blacklist` / `cat_requires_cert_*` / `trademark_live` /
  `content_promotional` / `walmart_strict_sensitive` / `made_in_usa_claim`)保留在 `_RULE_CN`
  与证据渲染表里(只渲染旧行,新链不再产生);`rerule=` 参数对它们仍可用。
- 文档:`docs/audit_pipeline.md` §2 / §4 / §8.5 重写,§10 标全部落地;`docs/conventions.md`
  若有 R5/R7/R8 的规则引用同步;`services/audit_l2.py` 模块头「六条规则」措辞随之消失。
- `AUDIT_RULES_VERSION` 再递增。

**C 落地记录(2026-09-03,与本规格的差异逐条)**:

- `AUDIT_RULES_VERSION` = **`c.2026-09-03.1`**;
- ⚠ **缓存口径改正**(复核指出,初版写的"提示词未动 ⇒ 缓存不受影响"是错的):
  system prompt 确实一字未动,但 `llm_cache.cache_key` 哈希的是**整段 messages
  (system + user)**(`services/llm_cache.py:28-44`),而 C 批**动了 user 段**:
  「上游证据」里的品牌行换了措辞(`* 标题/描述命中黑名单(R4, 共N个…)` →
  `* 文案提到黑名单品牌(共N个…)`),R3 软 / R5 / R7 / R8 四类证据行整体消失
  —— 原来只有这几类证据的产品,那一段现在塌成一行「(上游无证据)」。
  于是缓存分成两半:
    · **本来就没有任何软证据的产品**:user 段逐字节不变 ⇒ 照旧命中,零成本;
    · **有品牌命中、或曾有 R3 软/R5/R7/R8 证据的产品**:未命中 ⇒ 按 L3 全价
      重付(谷时段减半)。
  与 B1 那次"全库全量未命中"**不是一回事**:这次是**按产品分的部分未命中**,
  账要按"有多少产品带软证据"估,不能按 0 估;
- **ctx 字段随判定方改名** `ac_automaton` → `brand_mention_automaton`
  (规格只说"挪到 L0 使用"):字段名带着已删规则的编号,下一个人会照着去找 R4;
  自动机仍在 `audit_rules._build_automaton` 一处构建。**`r4_source` 不改名**
  —— 它是 TRO 命中接线与事件账本的既有口径(`audit_store.tro_hits` 的形参、
  测试、店铺事件),改它属于另一件事,注释里写明"旧称 R4 键";
- **证据通道要读第二个槽**(规格没点名,差点漏接):`summarize_evidence`
  原先只读每层的 `hits`,而 L0 的软证据落在**新加的 `evidence` 槽**里 ——
  只读 `hits` 的话品牌命中一条都送不到 L3,提示词照样漂亮、没有任何东西会红
  (「承诺了没送到」的第二次)。改为每层读两个槽,并补一条**端到端**用例
  (真跑 `audit_phase0.check` → 证据行 → user 段那两处);
- **TRO 命中接线跟着搬**(规格没点名):`audit_store._r4_brands` 原先写死
  "从 `outcome.l2` 里找 `title_desc_blacklist`",规则迁进 L0 后它会**静默归零**
  —— TRO 从此再也不报警,而摘要照样漂亮。改为 `_mentioned_brands`:按
  rule_code 读 `all_hits`,与证据出自哪一层无关;
- **`made_in_usa_claim` 补进 `_RULE_CN`**:它 2026-08-24 上线时就没登记过中文名
  (存量老行一直渲染成裸 rule_code),迁走之后更没人管,一并补上;
- **`registry/paths.audit_seed_file` 保留、只改登记**:规格写的是"删 registry/paths
  里的登记"。函数本身留着 —— `refdata/audit/` 目录仍在(`l3_keywords.yaml` 是
  参考资料,代码从不加载),而铁律 3 要求取那个目录的路径必须经它;docstring
  改成"当前零消费方 + 三份种子各自何时下线";
- **`registry/db.uspto_conn` / `uspto_dsn` 保留**(本仓已无消费方,README 外部
  资源行与两处 docstring 都写明):库与外部灌库链路都还在,§10 定稿写的是
  "将来需要按新流程重建"。删掉它等于把重建的入口也删了;
- **`policy_join` 的两处行为差**(旧名别名表退役的连带,都不是 bug):
  ① 老 run 里 `l3_reason_category` 装着旧缩写名的行,`_adopt_history` 从此
  解析不到 → 类别留空 + 计数(采用历史本来就不重判);
  ② 报错正文里的语义缩写 join 不上 → 进「政策表缺口」清单(那正是它该待的地方);
- **`policy_sync` 少一句提示**:表里同时留着「旧缩写名 + 官方名」两行时,旧行
  只进「官方已不含」(不删行、零写),不再带「该名已被 id N 占用」——
  「疑似改名对」比的是「未对上的官方页」×「官方已不含的行」,而这一轮官方页
  已经对上了别的行,配不成对。**代价写在测试里**
  (`test_a_legacy_row_next_to_its_official_row_is_left_alone_and_listed`),
  换掉的是一张永远不会再被验证的历史映射表;
- **`test_error_taxonomy` 删掉「改名前那张 30 行近似表」上的 join 基线**:
  那一半量的是别名桥本身,桥拆了它就只会把退役的东西焊回来;官方表那一半
  (16/19 → 19/19)原样保留。

**C 对抗复核修订(2026-09-03,ACCEPT-WITH-FIXES 逐条)**:

1. **缓存账算错了**:见上条「缓存口径改正」—— 初版按"提示词没动"写成零成本,
   实际是**按产品分的部分未命中**(带软证据的全价重付)。三处措辞同改:
   `registry.resources` 的版本变更块、本节、§五的排量提示;
2. **`_scan_brand_mentions` 不许 getattr 兜底**:`AuditContext` 把
   `brand_mention_automaton` 声明成无默认值的必填项,兜底只会让"字段再改一次名"
   变成**静默无证据 + TRO 从此不报警**。改为属性直取(`None` 仍表示"词表为空,
   没有词可扫"),补一条用例钉死"少字段必须 AttributeError";
3. **`compute_final_reason` 补扣分闸**:C 批让 penalty=0 的证据行排在
   `all_hits` **最前面**,而原实现"第一条带 `category` 的就用它"没有扣分闸 ——
   给软证据顺手加个 `category` 就能把整条产品的类别劫走,而判定结果一字不变、
   没有任何东西会红。改为 `if cat and h.penalty < 0`(L3 的类别在结构化输出里,
   不走这条路,不受影响),补回归用例;
4. **两处注释指着已删的代码**:`audit_phase0._check_brand` 的「留给 L2 R4」改指
   同层的 `_scan_brand_mentions`;`pt_admission` 的「L2 `_infer_walmart_policy`
   → L3 上下文」改指规则自报类别(§二)与 L3;
5. **README 的 policy_sync 行不再写死版本号**(`c.2026-09-02.1`)—— 那个数由
   工作流摘要按实时 `AUDIT_RULES_VERSION` 渲染,文档里写死就是第二份会漂的真相。

## 五、切换手册(A+B+C 都合并后,一次做完)

> **B2 已落地**:下面 `audit_replay` 与 `mode=stale -p active_days` 两条命令
> 现在真的存在(工作流 `workflows/audit_replay.py`;报告落
> `paths.audit_replay_report()` = `<DATA_ROOT>/reports/audit_replay.txt`)。
> 顺序不变 —— `db_init` 建 `replay_results` 在前,`policy_sync` 刷政策表在中间
> (回放的期望类别要 join 它),回放在 `mode=stale` **之前**:报告不达标就别开
> 那条最贵的。

```bash
git pull                                    # 看钟:18:10 之前 pull,当晚 audit_sheet 就用新链
python cli.py db_init                       # audit_detail 列 + audit_runs.audit_version 列 + replay_results 表(幂等)
python cli.py policy_sync --dry-run         # 预期 新增 2(id 43/44 内容族)/ 刷新 42 / 改名 0
python cli.py policy_sync
python cli.py audit_replay --dry-run        # 样本规模 + 预估成本
python cli.py audit_replay -p neg=600 -p pos=400   # 谷时段;看报告再决定下一步
python cli.py product_audit -p mode=stale -p active_days=90 -p limit=N   # 近 90 天有动销的一批,谷时段分晚跑;pending/rejected 走 mode=nonpass
                                            # ⚠ limit **缺省不限量**(2026-09-03):这里给 N 是为了分晚跑;不给就一轮判完
                                            # ⚠ 带软证据的产品缓存未命中要全价重付(见下条),别按"零成本"排量
```

- 回放报告不达标(§六第 5 条的线:**正例误伤率不高于旧链**)→ 不跑 `mode=stale`,
  先修提示词/规则再回放;修改 = 再提版。报告里那一行会自己说话
  (达标写「底线达标」,不达标写「⚠ 新链误伤高于旧链……别开 mode=stale」)。
- 回放的**样本身份是 `run_tag`,不是 `seed`**(2026-09-02 B2 复核修订):
  某个 tag 在 `audit.replay_results` 里已经有行,就**重放那一批 asin**(期望值
  原样取回、结果原地覆盖),不重新抽样;换一批样本 = 换 tag。
  理由:`seed` 只保证"同一份候选面上抽同一批",而候选面 `catalog.walmart_items`
  每天被 `catalog_sync` 重写 —— `ORDER BY md5(sku || seed) LIMIT` 的窗口跟着天天变,
  隔天"同 seed 对比"已经不是同一批产品,而两份报告长得一模一样。
  ⇒ 改完提示词的正确姿势:`python cli.py audit_replay -p tag=<上一次那个 tag>`。
- **旧链基线排掉新链自己写的行**:`audit.audit_runs` 2026-09-02 B2 补了
  `audit_version` 列(`services/audit_store` 落库时盖当前 `AUDIT_RULES_VERSION`),
  回放取的是每个 asin 最近一次 `audit_version IS DISTINCT FROM <当前版本>` 的行
  (NULL = 存量老行,算旧链)。没有这道谓词的话,**跑过一轮 `mode=stale` 之后**
  基线就变成新链自己的结论 —— 自己跟自己比,而数字看着完全正常。
- 回放会写 `catalog.llm_cache`(判定链自己写)—— 那**不是**浪费:紧接着的
  `mode=stale` 重审命中同一批缓存,回放的钱等于预付了一部分。
- **本次重审的缓存账不是零**(2026-09-03 C 批复核修正):C 批没动 system prompt,
  但**动了 user 段**(证据行的措辞与条目),而缓存键哈希的是整段 messages ⇒
  **带软证据的产品一律未命中、按全价重付**(带品牌命中的,以及从前吃 R3 软 /
  R5 / R7 / R8 那几类证据的);**完全没有软证据的产品照旧命中**。
  估算 `mode=stale` 那一晚的账时按这两半分开算 —— 按"提示词没动所以不花钱"
  排量,会在谷时段跑到一半才发现钱不够。
- **`limit` 缺省不限量**(所有者定稿 2026-09-03,与本批同分支):此前缺省 500,而
  `from_sheet` 会把它顶成 ASIN 总数 —— 摘要写着「只判 500 个」、实际把待审的全判了。
  现在口径只有一条:**不给就不限量,给了就真截断**。切换那几晚要控成本就显式给
  `-p limit=N`,拿不准规模先 `--dry-run`(摘要报「共 N 个」)。
- 回滚 = `git revert` C/B 两批(A 的转录件无害);已被新版本盖章的行要再付一次重审。
- `error_reclass_report` 不受影响;`audit_sheet` **缺省不限量**(2026-09-03),切换周要控成本就在调度表给它显式加 `-p limit=N`。

## 六、所有者裁决(2026-09-02 定稿,八项全部落定)

1. **类别词表**:44 官方名 + `内部黑名单` / `类目准入`;pending 类别为空;零兜底(§二)。
2. **路由提示整体删除**(§3.7)。
3. **飞书上架表**:所有者已改表头为 21 列 `店铺 / ASIN / SKU / walmart上架标题 /
   walmart_product_type / 审核结果 / 类别 / 具体内容 / 审核日期 / amz价格 / 库存 / walmart价格 /
   是否上架 / 上架feedid / 上架日期 / 未上架理由 / 上架结果 / 报错 / feed查询日期 / 登记日期 /
   查询编码`;审核域 = D~I 六列,G 类别、H 具体内容分列。**热修已接线**(同日,PR #109):
   registry 列序 + `services/listing_sheet` 全部区间**按表头名推导字母**(与 maint_sheet 同路,
   所有者定稿「统一为按表头名定位写表」)+ 读表前核验第 1 行表头(对不上即停并点名列)+
   审核投影六列;热修期 G = `audit_reason` 现值、H = 人话;B 批改为 G = 类别枚举、
   H = `audit_detail`;T/U 运营域脚本不写。
4. **`catalog.products` 新增 `audit_detail`**,`audit_reason` 专放类别(§3.4)。
5. **回放集**反例 600 + 正例 400,先看数不预设阈值;底线 = 正例误伤率不高于旧链(§3.8)。
6. **内容族**:44 `Product details policy` 进提示词(所有者明示);43 索引页已进表随之进
   (1.2K 字符);21 个分类风格指南不进。
7. **长描述截断 600 → 3000**(§3.2)。
8. **重审规模**:以 `audit_sheet` 按需重审为主;`mode=stale` 只跑近 90 天有动销的一批 ——
   B 批给 `mode=stale` 加 `active_days=N`(缺省 90)参数,按订单表近 N 天有订单行的 SKU
   过滤候选(表名按 `docs/db_schema.md` 定),不再全量重付;§五 手册随之改。

## 七、事实依据(本文引用的现状,已核到 file:line)

- S4 只读中文人工列、`full_policy` 未用、`render_feed_text` 只在测试:`services/audit_l3.py:434-499`,
  `services/policy_feed.py:85`,`docs/db_schema.md:744`;
- 提示词体量:`_S1` 6028 字符(`tests/test_audit_l3.py:138` 钉死)、`_S3` 147、S4 上界
  42×(50+30+240+80+标题);42 篇全文 `parse_policy_file` 合计 268,996 字符,
  `render_feed_text` 后 199,123;
- 输出六键与解析降级:`services/audit_l3.py:396-411, 686-819`;`signals` / `llm_confidence`
  仓内无消费者(grep services/ workflows/);
- 落库:`services/audit_store.py:29-60, 172-215`;`audit_runs` 无 `final_reason_category` 列
  (`services/audit_models.py:166-169`);飞书 F 列 `workflows/product_audit.py:746-765`;
- 理由映射九步与兜底:`services/audit_reason.py:8-28, 87-182, 185-300`;
- 路由两表 29 个政策名字面量:`services/audit_l3.py:80-152`;`route_policy_hints` `:191-243`;
- L2 七条规则:`services/audit_l2.py:1243-1251`;R7/R8 只扫标题 + 前 3 条五点(`:926-928, 1119-1121`);
  R4 扫 `searchable_text` 全量;R10 `:1166-1206`;`_infer_walmart_policy` `:314-450`;
- L0 串行短路、`Phase0Result.hits ≤ 1`:`services/audit_phase0.py:9-11, 368-396`,
  `services/audit_models.py:91-101`;
- `walmart_pt_meta.requirements` 是 R3 的唯一判据源:`services/audit_l2.py:453-465`;
- Content Standards 页登录墙:匿名抓取 `pageDataErrorCode=204`、`pageData=None`,与
  General-Use Products 页同形;公开导航「Item content, imagery, and media」下 8 页无此页;
  Product details policy 页公开(pageData 31.8K 字符)—— 2026-09-02 实测;
- Made in the USA 专段:`refdata/policy_pages/en/29-product-claims.md:39-49`;
- 回放素材与局限:三面报错列(`catalog.walmart_items.unpublished_reasons` /
  `catalog.product_events.detail->>'reasons'` / `ops.feed_item_errors.description`),
  `audit.walmart_error_records`(无增量同步,`docs/db_schema.md:742`);产品正文无历史
  (`services/product_ingest.py:42-67`,`refdata/schema.sql:60-63`);`sku=asin` 裸等值仍在
  `workflows/product_audit.py:409-411` 与 `refdata/schema.sql:527`,规则唯一出处 `services/sku_asin.py`;
- 缓存键含整段 messages:`services/llm_cache.py:28-44`;调度:`registry/schedule.py:167-203`。

---

## 八、人工验收回改与验收集(2026-09-03,版本 `c.2026-09-03.2`)

C 批合并后,所有者拿 11 个在架 ASIN 跑 `product_audit -p asins=… --dry-run`,
**逐个人工看过产品页**给出判断。下面 8 条是那次人工判定的原话归纳,
**它就是验收集**:提示词以后怎么改,都要拿这 8 条重跑对一遍。

### 8.1 验收集(人工判定 = 基准,不是新链的结论)

| ASIN | 人工判定 | 人工给的理由 |
|---|---|---|
| `B0F21HVDDN` | **pass** | 正常柜子;USB 充电端口与 2 个电源插座只是这个柜子的**配置**,不是整机电器 |
| `B0DFY42VDL` | **pass** | 正常产品 |
| `B0F6YH7ZX4` | **pass** | 同 `B0F21HVDDN`,正常柜子 |
| `B0FBX3JR3Y` | **reject** | 明确是儿童产品 |
| `B0DSCQJ698` | **reject** | 明确是儿童产品 |
| `B0GYNRCZ9F` | **reject** | 未授权引用品牌名 `abba` |
| `B0015XDK30` | **reject** | 标题含 ®/™ 商标符号(政策:Intellectual Property) |
| `B0C1V8SVHZ` | ~~reject~~ → **pass**(2026-09-03 所有者复看后更正) | 初判依据是「沃尔玛后台显示要儿童证书」;复看产品(西部牛仔图案装饰门垫,17×30 吋,rubberwood)后判定**不需要儿童证书** —— 沃尔玛之所以提示,多半是标题结尾那句 `Teen Girls Gifts`。**pass 才是对的** |

⚠ **旧链的结论不是基准**。这次人工复核推翻的正是旧链:旧链拿 NRTL/CPC 把
柜子、斗柜整片拒掉,人工看下来是**误拒**。同理,"内部黑名单命中"也不算新链
更好 —— 黑名单是死代码、数据在不断补,拿它当分数是自己给自己送分。

### 8.2 这次改了什么(只动 `audit_l3._S1`,规则代码与词表一个字没动)

判错的根子不是"少了某一类产品的特判",是**框架缺了两块**(所有者原话:
"这样子又变成了专属某一些产品的判断……这些东西在政策中都有写,他应该可以
直接判定情况")。所以补的是框架,不是产品清单:

1. **先定「本体」** —— 政策判的是这件商品**本身是什么**,不判它的部件 /
   兼容对象 / 使用场景 / 图案装饰。带 USB 口与插座的柜子本体是家具;
   "for iPad" 的收纳架本体是收纳架;反过来 "Kids Picnic Table" 本体**就是**
   儿童家具(面向谁做的是商品定义的一部分,不是"场景提及")。
   → 修 `B0F21HVDDN` / `B0F6YH7ZX4` 的误拒,同时把 `B0FBX3JR3Y` /
   `B0DSCQJ698` 拒到位。
2. **命中从两类扩到三类,新增 C. 附条件允许** —— 官方政策页有三态,提示词
   原来只写了两态(禁 / 不禁),把中间那列「Allowed with restriction」整块
   漏了。44 篇里 **23 篇**有这一列。本卖家的条件是固定的(搬运模式、无证书、
   无授权、拿不到预审批)⇒ 对**本体**提出证书 / 预审批 / 注册 / 检测报告
   要求的,一概满足不了 → reject;针对部件的 → pass;证据不足 → pass。
   → 覆盖「本体确实要证书」那一类。
   ⚠ **`B0C1V8SVHZ` 不属于这一类**:所有者复看后判它 pass(见 8.1 更正)
   —— 门垫不是儿童产品,后台提示多半是标题里 `Teen Girls Gifts` 引起的,
   不是本体要证。
   两条护栏照旧写死:别把「附条件允许」读成「允许」;不许按类目名连坐整类。
3. **品牌段自洽 + 只输出真品牌** —— `evidence` 写"暗示兼容性"却标
   `is_real_brand: true`,整件商品会被翻成知产侵权;现在列表里**只许**放判成
   真品牌的词,判 false 的一个都不写(所有者原话:"其他假品牌无需输出")。
   → 保住 `B0GYNRCZ9F`(真·未授权引用 `abba`)这一类,同时不让兼容词误伤。

### 8.3 输出加了排查面(所有者:"输出有明细方便我们排查问题")

L3 输出**三段 → 五段**,新增两键,**都不参与判定**:

- `product_is`:本体一句话,**pass 也要填**。判错时先看它 —— 本体认错和条款
  引错是两种病,药也不同;
- `policy_quote`:触发判定的**那一句政策原文逐字**。类别对了不等于条款对了;
  抄不出原句 = 手里根本没条款。

落点:
- **reject** → 随 hit.detail 落 `audit.audit_hits`(七键定序,后两键接在末尾),
  `workflows/audit_why` 把 detail 原样摊开,不必改渲染;
- **pass 不落 hit** → 只有 `services.audit_l3` 那一行 INFO(屏幕 +
  `logs/<workflow>.log`,dry-run 也打)。**误放行只能从这行看出来**;
- 取不到就是 `None`:老 `llm_cache` 行没有这两个键,**缺排查信息不是坏 JSON**,
  照旧解析出结论,绝不因此 pending。

### 8.4 验收怎么跑

```bash
# 提示词逐字节变了 ⇒ purpose=audit_l3 存量缓存全量未命中,这 11 条全额重付
python cli.py product_audit -p asins=B0F21HVDDN,B0DFY42VDL,B0F6YH7ZX4,\
B0FBX3JR3Y,B0DSCQJ698,B0GYNRCZ9F,B0015XDK30,B0C1V8SVHZ --dry-run
```

对照 8.1 那张表逐条看;`--dry-run` 一个字都不写库,结论看摘要与
`logs/product_audit.log` 里的 `L3 <asin> …` 行(本体 / 类别 / 原句 / 具体内容 /
真品牌全在那一行)。**对不上的不要靠加特判去凑** —— 那正是这次被否掉的改法。

### 8.5 第一次验收跑的实测(2026-09-03,`c.2026-09-03.2`,空跑 8 条)

`L3 system prompt 214320 字符(缺全文 0 篇)`;候选 8 / 判定 8(过 5 / 拒 3 / 待定 0);
L3 判 7 条(`B0015XDK30` 在 **L0 商标符号硬拒**就停了,没进 L3)。**8 条对上 7 条**(`B0C1V8SVHZ` 那条按 8.1 更正后的基准算,新链判 pass 是对的):

| ASIN | 人工 | 新链 | 新链给的本体 | |
|---|---|---|---|---|
| `B0F21HVDDN` | pass | pass | 带USB口和插座的角落收纳柜(家具) | ✅ |
| `B0DFY42VDL` | pass | pass | 带金属框架和织物抽屉的白色收纳柜(家具) | ✅ |
| `B0F6YH7ZX4` | pass | pass | 带电源插座和LED灯的九斗柜(家具) | ✅ |
| `B0FBX3JR3Y` | reject | reject `Children's Products` | 儿童感官吊椅(秋千椅) | ✅ |
| `B0DSCQJ698` | reject | reject `Children's Products` | 面向儿童的户外野餐桌(儿童家具) | ✅ |
| `B0015XDK30` | reject | reject(L0 商标符号) | —(没进 L3) | ✅ |
| `B0GYNRCZ9F` | reject(未授权引用 `abba`) | **pass** | 户外庭院遮阳伞(庭院家具配件) | ❌ **`.2` 造成的回归** |
| `B0C1V8SVHZ` | **pass**(基准已更正,见 8.1) | pass | 印有西部牛仔图案的装饰门垫(家居用品) | ✅ |

**「本体」这一维当场生效**:三个柜子的本体都被认成家具(USB 口 / 插座 / LED
只当配置),`.1` 那类"带电部件 ⇒ 整机电器 ⇒ 要 NRTL"的误拒没有再出现;
两条儿童品都拒到 `Children's Products`,`policy_quote` 抄回了 CPC 那句原文。

两条当时判"未达",**结局完全不同**:

- **`B0GYNRCZ9F` 是 `.2` 自己弄出来的回归** —— 所有者要的是「假品牌无需
  **输出**」,`.2` 顺手写成了「无需逐个**判**」;判定动作一松模型整段跳过,
  而当时列的三条真品牌信号(型号 / 100% Authentic / 自称授权)**都不覆盖最
  常见的那一种**:品牌名直接当自己的商品名写在标题开头。`.3` 补「品牌位」、
  `.4` 再把顺序写死(先定本体再判品牌),**不是给 `abba` 开特例**;
- **`B0C1V8SVHZ` 不是没接住,是基准原本记错了。** 所有者复看产品后更正
  (8.1):西部牛仔图案的装饰门垫,17×30 吋,rubberwood —— **不需要儿童
  证书**;沃尔玛后台之所以提示,多半是标题结尾那句 `Teen Girls Gifts`。
  **新链判 pass 是对的。**
  这条留个教训:「沃尔玛后台要证」是**运营现象**,不等于「这件商品按政策
  要证」。拿现象当基准,会把判据往「按类目名连坐整类」上拽 —— 那正是
  2026-08-20 删 R3 时否掉的东西。基准只认**看过产品之后的人工判定**。

### 8.6 第二轮反馈:品牌这一维两处收口(`c.2026-09-03.4`)

所有者看完 8.5 又指出两件事,都不是"某个产品判错",是维度问题:

**① 判定顺序:先定本体,再判品牌。**
原来品牌段自成一节、不引用本体,于是「USB 口可给 iPad 充电」里的 `iPad`
被当成那只**收纳柜**的真品牌,整件商品翻成知产侵权(旧链实测就是这么拒的:
`L3 verdict override: pass→reject, is_real_brand=true: ['ipad']`)。
现在每个词只问一句 —— **它是不是「这件商品本身」的牌子?** 并给两条对照:

- 本体是**角落收纳柜**,`iPad` 在「USB 口可给 iPad 充电」里 → 别人的牌子 → false;
- 本体是**庭院遮阳伞**,标题开头 `ABBA PATIO … Umbrella` → 自己的牌子 → true。

守门测试连**节的先后**一起钉(`s1.index("# 先定") < s1.index("# 品牌证据怎么判")`)
—— 顺序本身是判据的一部分。

**② L0 品牌黑名单扫描收窄成只扫标题。**
`audit_phase0._scan_brand_mentions` 从 `title + 全部五点 + 长描述` 改成**只有
标题**;`ProductInfo.searchable_text` 随之删除(它最后一个消费者就是这里)。
收窄不是"少查了":词表 4.2 万条里混着 corner / life / wooden / better / side /
time 这类通用词,扫描述等于把送进 L3 的品牌词清单**灌满噪声**,而那份清单
**≤10 个** —— 真正长在品牌位上的那个词反而挤不进去。代价说清:**只在描述/
五点里出现的品牌从此不进证据**,所有者认这笔账。

⚠ **L0 商标符号规则不动**(照旧扫标题 + 五点前 5 条 + 描述前 1000 字符):
® / ™ 是强信号,没有噪声问题。两条 L0 规则的扫描面**故意不同**,别顺手统一
—— 有一条守门测试专门拦这件事。

### 8.7 验收通过:`c.2026-09-03.4` 对上 8 / 8(2026-09-03 生产机空跑)

`product_audit(c.2026-09-03.4)`,`L3 system prompt 214888 字符(缺全文 0 篇)`;
候选 8 / 判定 8(过 4 / 拒 4);L3 判 7(`B0015XDK30` 停在 L0 商标符号硬拒)。

| ASIN | 基准 | 本轮 | 新链给的本体 |
|---|---|---|---|
| `B0F21HVDDN` | pass | pass,**真品牌=无** | 带USB口和插座的角落收纳柜(家具) |
| `B0DFY42VDL` | pass | pass | 白色布艺抽屉柜(家具) |
| `B0F6YH7ZX4` | pass | pass | 带电源插座和LED灯的九斗柜(家具) |
| `B0C1V8SVHZ` | pass(8.1 更正) | pass | 印有文字图案的化纤门垫(家居地垫) |
| `B0FBX3JR3Y` | reject | reject `Children's Products` | 儿童感官吊椅(秋千椅) |
| `B0DSCQJ698` | reject | reject `Children's Products` | 面向儿童的户外野餐桌(儿童家具) |
| `B0GYNRCZ9F` | reject | reject `Intellectual Property`,**真品牌=['abba']** | 户外庭院遮阳伞 |
| `B0015XDK30` | reject | reject(L0 商标符号,未进 L3) | —— |

两条要记住的读数经验:

- **`iPad` 那条确实是顺序问题**。同一个产品,`.2` 判 pass 但旧链翻拒(`ipad` 被
  当成收纳柜的真品牌);`.4` 把「先定本体再判品牌」写死之后 `真品牌=无` ——
  本体先定成"收纳柜",`iPad` 自然落到"别人的牌子"那边。
- **带 `verdict override` 字样的拒,理由不是模型写的**。`B0GYNRCZ9F` 日志里先出
  `L3 verdict override: pass→reject, is_real_brand=true: ['abba']` —— 模型自己
  输出的是 pass,是代码按合同(任一 `is_real_brand is True` ⇒ 整品 reject +
  `Intellectual Property`)翻的确定性后处理。所以那条的 `policy_quote` 恒为空、
  `detail` 是固定句式「未授权引用品牌名 X」。**看日志时别把它当模型的判断。**

### 8.8 首次回放实测(`v4首测`,反例 200 / 正例 100 目标 → 实取 114 / 100)

`c.2026-09-03.4`,墙钟 9s,$0.08(**缓存命中 97%** —— 214 条一批,正好验证了
§5.1 那条「命中率 ≈ (N−k)/N」的算术:N=136 次调用、k≈4)。

读数**全在一个陷阱上**,记在这里:

| 指标 | 报告首版给的数 | 真实含义 |
|---|---|---|
| 反例召回 | 78/114 = 68.4% | **虚高**。78 条全部是 L0 `内部黑名单` 拦下的 |
| 判据召回 | (首版没报) | **0/36** —— 真正走到判据的反例,一条都没判拒 |
| 类别准确率 | 0/67 = 0.0% | 67 条判拒里 **64 条得到 `内部黑名单`**,压根没走到政策判定 |
| 正例误伤 | 新链 3/100 = 旧链 3/100 | 底线达标(三条都出自 L3:两条品牌翻拒 + 一条儿童品) |

**根因是评估口径,不是判据**:反例取自沃尔玛拒过的品,而**「拒了就拉黑」是既有
流程** —— 那些 ASIN 早就躺在 `catalog` 黑名单三表里(ASIN 黑名单 73,918 条),
L0 认出来当场硬拒,判据一步都没走。算术对得上:214 条样本 − 136 次 L3 调用
= 78 条没进 L3,恰好等于召回数。

**已修**:`report()` 把两笔账拆开(守门测试
`test_report_把内部黑名单命中与判据召回分开报`)——
① 反例召回下面加「判据召回(扣掉黑名单命中)」并标明"要看判据行不行只能看这个数";
② 类别准确率加第二个数「真正由判据给出类别的 N 条里 x/N」。
一条黑名单命中都没有时这两行不出(不给读的人加噪声)。

⚠ **下一步要查的是那 36 条**:走到了判据、沃尔玛拒过、而我们判 pass。
报告全文 `▍判定不符逐条` 里有逐条的「沃尔玛原文 / 我们的具体内容」。
在看清这 36 条之前,**不要动提示词** —— 局限①(产品正文只有当前值,被拒后
改过 listing 的会失真)与局限③(沃尔玛裁决不是金标)都可能吃掉其中一部分。

### 8.9 修品牌误伤:命中的是「词」,不是「品牌」(`c.2026-09-03.5`)

`.4` 之后 `audit_replay`(400 正例)实测 **正例误伤 16/398 = 4.0% > 旧链
11/398 = 2.8%**,所有者定稿 §六.5 的底线不达标。逐条看**全是品牌翻拒**:

`better`+`trio`(皂液分配器)/ `smith`(胶水加速剂)/ `southern`(橱柜拉手)/
`serene`(水槽格栅)/ `Essex`(圣经索引标签)/ `velcro` / `milwaukee`

所有者点破了根因:「**这样子的词在标题里应该可以看出来它到底是品牌还是不是
品牌**」,并给了那条标题:

> **Bob Smith Industries** BSI-151H Insta-Set Super Glue Accelerator …

命中的词是 `smith`,而标题里的品牌是 **Bob Smith Industries** —— **另一家公司,
只是名字里碰巧含这个词**。黑名单收的是**单词**,标题里的品牌常是**多词完整名**;
我们把片段当成品牌递过去问,等于问错了问题。`southern` / `serene` / `Essex`
全是同一种。

**两处改(都不是特判)**:

1. **L0 命中带回上下文窗口**(`audit_phase0._BRAND_CTX = 40`):
   `matches[].context` 存命中词前后 40 字符的标题原文,证据行优先渲染它。
   只递 `smith` 这个词,L3 根本没法判"它属于哪个完整品牌名"。
   ⚠ 库里存量命中行没有这个键 → 退回 `matched_phrase`,不炸。
2. **S1 加「词 → 完整品牌名 → 是不是同一个牌子」这一问**,排在原有两问
   (用在别人身上 / 当成自己的名字)**之前** —— 先认出品牌名,才谈得上它是
   用在谁身上。三条对照写死:`Bob Smith Industries ≠ smith` /
   `Milwaukee M18 = milwaukee` / `Better Drying` 里的 `better` 压根没当名字用。

⚠ 提示词与 user 段的证据行都变了 ⇒ `audit_l3` 存量缓存**全量未命中**。

**验收线**:同 tag 重放,**正例误伤降到旧链 2.8% 或以下**才算修好;
在那之前**不要跑 `blacklist_route`** —— 放出来的 4 万条会立刻落到判据上,
而判据正在**过度拒绝**,等于让一批本来该放行的品换个地方再被拒一次。

⚠ 这句原文写的是"等于拿真金白银去试",**说重了,更正在此**:路由本身
(删黑名单行)不触发任何审核、不触发任何上架,一分钱不花;这 4 万条真要
重审也是 $28 上下(闲时价,§5.1 的算术)。**要拦的是结论错,不是钱。**

### 8.10 验收线达标:`c.2026-09-03.5` 重放 `路由后`(2026-09-03 生产机)

`audit_replay -p tag=路由后`,墙钟 26s,**缓存命中 96%**,$0.52/千条。

| 指标 | `.4`(§8.9 那次) | `.5`(本轮) | |
|---|---|---|---|
| **正例误伤(共同子集 398)** | 新链 **16/398 = 4.0%** vs 旧链 11/398 = 2.8% | 新链 **11/398 = 2.8%** vs 旧链 11/398 = 2.8% | ✅ **底线达标** |
| 正例误伤(全部 400) | —— | 12/400 = 3.0% | |
| 反例召回 | 78/114 = 68.4% | 207/284 = 72.9% | |
| **判据召回**(扣掉黑名单命中) | 0/36 → 后续 16.5% | **14/91 = 15.4%** | ⚠ 略降 |
| 类别准确率 | 0/67 | 2/177 = 1.1%(真正由判据给类别的 9 条里 **2/9 = 22.2%**) | |
| 新旧一致率 | —— | 567/682 = 83.1% | |

**§8.9 那两处改是对的,且不是给某个词开特例**:`.4` 里翻拒的
`smith` / `southern` / `serene` / `Essex` / `better` / `trio` **全部消失** ——
`Bob Smith Industries` 的胶水加速剂、三腔皂液分配器、圣经索引标签现在都判 pass。
剩下的 `verdict override` 只有两条:`milwaukee`(LED 工作灯)与
`velcro`(遮阳篷替换顶布),**两个都像是真品牌**,不当误伤看。

⚠ **两处副作用要盯着,别当成纯赚**:

- `B0FH2CYMGW`(儿童床架)从 reject **翻成 pass**。品牌这一维松了之后,
  召回也跟着松 —— 这条是儿童品,翻错方向了;
- `B0BVXXNTF6` 的 `confidence` 掉到 `medium`;
- **判据召回 16.5% → 15.4%**,方向是往下的。误伤与召回本来就在跷跷板两头,
  §六.5 的底线只钉了误伤这一头,所以这次算过 —— 但下一轮改提示词时,
  **要拿判据召回一起看**,别把误伤一路压到判据什么都不拒。

**结论:`blacklist_route` 的前置闸(§8.9 验收线)已解除。** 存量路由按
`docs/error_taxonomy.md` §十二的裁决执行。

### 8.11 `B0FH2CYMGW` 漏判:缺口在「缺证即禁」这一半(`c.2026-09-03.6`)

§8.10 记的那条副作用查清了,**是框架缺口,不是采样噪声,也不是这个产品特殊**。

诊断只花了一行日志(§8.3 加 `product_is` 就是为了这个):

```
L3 B0FH2CYMGW pass(缓存) 本体='儿童床架(家具)' 类别=none 原句=None 具体=None 真品牌=无 置信=high
```

**本体认对了**——「儿童床架(家具)」——然后仍然判 pass,而且置信 high。
所以病不在「先定本体」那一节,在**本体定完之后没有一条路通向 reject**。

#### 缺的是什么

`.4` 补的 C 类(附条件允许)把触发面绑在了官方页的**「Allowed with restriction」
那一列**上。而实测:44 篇里**只有 23 篇有这一列**,另外 21 篇只有
`Prohibited | Allowed` 两列 —— 儿童产品那篇正是其中之一。它把同一件事写在
**Prohibited 列**,并且拴在「缺文件」上:

> Listing or offering for sale any children's product on Walmart's Marketplace
> **that doesn't have a valid CPC** and corresponding third-party test report…

> Sellers may list children's products on Walmart's Marketplace **only if** each
> product is accompanied by a valid CPC…

这个形状三类命中一条都接不住:

| | 为什么接不住 |
|---|---|
| A. 品类整体禁售 | 政策没有整体禁儿童产品,禁的是「没有 CPC 的」 |
| B. 用途/特征/年龄段 | B 要求「在产品原文里找到那个特征的证据」,而这里的"特征"是**缺一份文件** —— listing 里永远看不到 |
| C. 附条件允许 | 触发词是 `Allowed with restriction` / `must be certified` 那一族,**不含**「写在 Prohibited 列、以缺证为条件」这一种 |

同一形状在语料里是**普遍的**,不止儿童产品:通用消费品(GCC)、
代步/微出行(CPC + 电池检测)、PFAS(合格证)、珠宝(卖家须获批)、
电子/射频(FCC 授权)全是这么写的。

#### 改了什么(C 类重写,仍然不是产品清单)

1. **两种写法并列,判法相同** —— ①「Allowed with restriction」那一列;
   ② **写在 Prohibited 列、拴在「缺这份文件」上**(`that doesn't have a
   valid …` / `only if accompanied by …` / `without a valid …` /
   `unless … approved`)。点明第二种**最容易漏判**:读起来像在说"允许",
   主句其实是禁令。
2. **不要去 listing 里找"没有证书"的证据** —— 缺失在文案里永远看不到;
   条件满不满足开头那句(搬运模式、无任何证书认证)已经定死了。
3. **⚠ 刹车,与 ① 同等重要**:政策若把**适用范围**推给末尾原文之外的清单
   → **判不出 → pass**。这是「判据只有末尾原文」的直接推论:
   **那张表不在你手里,就不许猜谁在表上。**

第 3 条不是可选项。通用消费品那篇要求 GCC,适用面是"受监管的消费品",
具体哪些要查一份**不在我们语料里**的监管目录 —— 没有这条刹车,模型会拿
"受监管消费品"连坐掉整个家居品类,§8.9 那种误伤会以更大的规模回来。

两个方向各举一例(**这是在教怎么读适用范围条款,不是产品名单**):

- 政策**把适用判据写进了自己正文** ——「主要为 12 岁及以下儿童设计或面向
  他们的消费品」。拿着 listing 就能判,本体落进去 → reject。
  并补一句反向护栏:**别因为"这看起来只是件普通家具"就放过 —— 本体前面
  已经定成儿童用品了,定了又不用,等于白定**;
- 政策**把适用范围指向原文之外的清单** → 判不出 → pass。

守门测试两条:`test_S1_缺证即禁_写在Prohibited列的那一半也要判`(钉第二种
写法 + 刹车 + 反向护栏 + 两条老护栏不被吃掉 + 节的先后)、
`test_S1_适用范围两个方向都举了例_不是产品清单`(钉住"两个方向都在",
防止以后只留下会拒的那一半)。

⚠ `_S1` 5,288 → 6,170 字符 ⇒ `audit_l3` 存量缓存**全量未命中**,重放要全额重付。

**验收线**(与 §8.9 同一条,再加一条):

1. 8 条人工验收集仍 8/8(§8.1 / §8.4);
2. `B0FH2CYMGW` 回到 **reject `Children's Products`**;
3. 同 tag 重放,**正例误伤仍 ≤ 旧链 2.8%** —— 这一改扩大了 reject 面,
   误伤是它最可能的代价,必须复测;
4. 判据召回**应当上行**(15.4% 是改前的基线)。

⚠ **顺序建议:先跑存量路由,再上 `.6`。** 两件事互不依赖,但路由只改变
**反例**能不能走到判据(黑名单命中挡掉的那一批),几乎不动正例误伤;
而 `.6` 两头都动。先路由 → 判据召回第一次有统计效力 → 再拿 `.6` 去比,
才读得出这一改到底加回了多少召回。反过来做,两件事混在一个读数里分不开。

### 8.12 度量失效:`判据召回 15.4%` 分的不是「判据」(2026-09-03)

所有者问「重新思考 LLM 判定不准确的原因」。五路独立复核加一路唱反调,
结论先说最刺眼的一条:**那个 15.4% 分的根本不是判据。**

#### 分道靠的是一次字符串等值

```python
_MEMORY_CATEGORY = resources.AUDIT_CAT_INTERNAL_BLACKLIST     # "内部黑名单"
judged_neg = [r for r in neg if r["got_category"] != _MEMORY_CATEGORY]
```

而**上游硬拒自报的就是真政策名**,一律 `!= 内部黑名单`,于是全部混进
「判据」的分子与分母 —— 它们**没有一条读过那 44 篇原文**:

| 出处 | 自报类别 |
|---|---|
| `audit_phase0.py:454` 品牌黑名单(生产两万余条 TRO 词) | `Intellectual Property` |
| `:270` 商标符号 ®/™ | `Intellectual Property` |
| `:315` 专利自述 | `Intellectual Property` |
| `:375` Made in USA | `Product claims` |
| `:169-176` 亚马逊类目黑名单,`walmart_policy` **能 join 上时 category 被改写成那条真政策名** | 真政策名 |
| `audit_l1_llm.py:129` / `audit_l2.py:179,199` | `类目准入` |

⚠ 类目黑名单那条最刺眼:**同一张表**,一部分行进记忆桶、一部分行进判据桶,
分界线竟是「黑名单行里有没有填对政策名」。

**反方向也漏**:`AUDIT_NONPOLICY_CATEGORIES` 被喂进了 L3 自己的候选枚举与
白名单(`audit_l3.py:368 / :743`),**走完 44 篇全文**却答 `内部黑名单` 的行
会被当成记忆,从分子分母里一起踢掉 ⇒ **分母偏小**。

仓库自己早就写着这件事(`services/audit_rules.py:146-149`):
「`AUDIT_IP_POLICY` —— 品牌黑名单 / 商标符号 / 专利自述三条硬拒 + L3 的
品牌翻拒,**四处判同一个 IP**」。

#### 改了什么

新增纯函数 `evidence_kind(row)`,分道只认两样**事实**(都早就落库):

1. `stage_stopped_at` —— 停在 L0/L1/L2(判据之前)或 L4(之后)的,不是判据;
2. `confidence` —— 「没走 L3 为 NULL」(`schema.sql:1811`),它是"到过判据"的
   唯一可靠标记。⚠ **L3 判 pass 时 `stage_stopped_at` 是 `None` 不是 `'L3'`**
   (`audit_rules.py:499-516`),只看 stage 会把判据放行的行整批丢掉。

L3 内部再分一次:**品牌翻拒是确定性后处理,不是模型的政策判断**。两者同为
`stage_stopped_at='L3'`、同为 `Intellectual Property`,只有 detail 的固定句式
能分 —— 常量提到 `audit_l3.BRAND_OVERRIDE_PREFIX`,并在那里写明**它是接口
不是文案**,改串 = 改评估口径。

报告现在把三桶**全摊开**(规则与记忆 / 品牌翻拒 / 政策判据),各报条数与判拒数,
不藏任何一桶。判据召回只认第三桶。

#### 不用重跑就能看到真数

判定链一个字节没动 ⇒ **误伤在数学上不可能变**;而 `audit.replay_results` 里
`stage_stopped_at` / `confidence` / `got_detail` 早就落库,**一条 SQL 就能把
上一轮的 14 拆开**:

```sql
SELECT CASE WHEN stage_stopped_at IN ('L0','L1','L2','L4') THEN 'L0/L1/L2 规则与记忆'
            WHEN confidence IS NULL                        THEN 'L0/L1/L2 规则与记忆'
            WHEN got_detail LIKE '未授权引用品牌名 %'       THEN 'L3 品牌翻拒'
            ELSE 'L3 政策判据' END AS 结论是谁给的,
       count(*) AS 反例数,
       count(*) FILTER (WHERE got_verdict = 'reject') AS 判拒
FROM audit.replay_results
WHERE run_tag = '路由后' AND expected_verdict = 'reject' AND got_verdict IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;
```

⚠ **预期是数字更难看**,那正是这次修正的价值。

#### 还有四条同向的口径问题(未修,记账)

1. **分母里混着「判据根本没机会跑」的行** —— L1 类目解不出、L2 判不了、
   pending、判定抛异常。本仓第一条纪律就是「判不了 ≠ 判过了」,这里破了;
2. **分母里 38%(107/284)不携带可判信息** —— `BRAND` 是账号权限、
   `PROHIBITED_FINAL` 不给理由,`label()` 对这两类返回期望类别 `None`
   (只比判定),它们进得了分母却几乎不可能进分子;
3. **理论上限不是 100%** —— 44 篇按「拿 listing 能不能判」分:约 20 篇白送、
   6 篇文案类、13 篇需外部数据 ⇒ **上限约 70–75%**;
4. **样本量不够下结论** —— 91 条的 95% 置信区间是 [9.4%, 24.2%];
   「类别准确率 2/9」的区间是 **[6.3%, 54.7%]** —— 这个数**不含任何信息**,
   不该拿来做任何判断。

⚠ 还有一条会自己变的:`blacklist_route` 一跑,91 这个分母就被**整个重定义**
(那批 ASIN 不再被 L0 拦下,会落到判据上)。所以**路由之后的第一轮回放是新基线**,
不要拿它跟路由前的 15.4% 比大小。

### 8.13 正例口径换成「在线够久 + 从未被报错」(所有者 2026-09-04)

所有者两句话推翻了原来的评估口径:

> 「不应该主要和旧链对比,**旧链并不准确,准确的只有沃尔玛的报错提示,
> 特别是带政策的那部分**」
> 「可以拿一些**在线时间长、且从未被沃尔玛报错**的产品当作正例,
> 这个算比较有说服力的」

两句都对,而且指向同一件事:**证据分级**。

#### 为什么旧链不能当主对照

旧链不是独立事实,它是**另一版我们自己写的判据**。§8.1 那次人工复核推翻的
正是它 —— 它拿 NRTL/CPC 把柜子、斗柜整片拒掉,所有者看完产品页判的是误拒。
拿一个已知会误拒的东西当基准,「持平」只说明**错得一样多**。

而沃尔玛写 `Prohibited Products Policy: <类别>` 的那部分是**真正的外部事实**,
且它给的类别与审核链吃的是**同一张枚举**(`walmart_prohibited_policy.category_en`)
⇒ 可以逐 ASIN 对**判定 + 条款**两件事。这是全套证据里唯一有条款粒度的。

#### 证据分级(反例三档、正例一档)

| 侧 | 档 | 证据 | 强度 |
|---|---|---|---|
| 反例 | `POLICY` 且抽出政策名并 join 上表 | 沃尔玛**写明**了裁决与条款 | **金标** |
| 反例 | `IP` / `CONTENT` | 裁决是沃尔玛的,**类别是我们按码推的** | 推断 |
| 反例 | `BRAND` / `PROHIBITED_FINAL` | 有裁决、无条款 | 只比判定 |
| 正例 | 在线够久且从未被报错 | 沃尔玛**沉默** —— 见下 | 弱,但可加强 |

#### 正例:曝光时长是把沉默变成证据的那个量

原口径「在架在售 + 当前无下架原因」有两个洞:

1. **新上架的品几乎不含信息** —— 昨天才上架的 listing "没被拒",是因为沃尔玛
   根本还没看过它。所有者说的「在线时间长」正是补这一个:一个品在架 N 天没被摘,
   说明它真的趟过了沃尔玛的巡检;
2. **`unpublished_reasons` 是就地覆盖的当前值** —— 半年前被拒、后来改好的品在
   那一列上看着干干净净,而它恰恰是最该被拒的那类。

**已改**(`workflows/audit_replay`):

- `_POS_SQL` 加在线天数闸:`min(created_at)` 跨店取最早那条还活着的行,
  `HAVING now() - min ≥ pos_days`;参数 `-p pos_days=N`,**缺省 180 天**;
- `rejected_asins()` 并上 `audit.walmart_error_records` 的**历史**账本
  (那张表天生带 asin,不用过 sku_asin);
- 报告新增「正例口径」一行:干净在架总量、够天数的、入选品在线中位/最长天数,
  漏斗里单列「曾被沃尔玛报错过 N」。

⚠ **`created_at` 是我们第一次同步到这行的时间,不是沃尔玛的上架时间** ——
它是在线时长的**下界**(新接的店会让老 listing 看着"年轻")。下界正是要的:
宁可把老品当新品排掉,不可把新品当老品放进来。
⚠ **自检**:够天数的不到干净在架的 2% 时,报告会点名 —— 那多半说明
`created_at` 挤在整表重建那天,「在线 N 天」是假的,这一轮的误伤别拿来下结论。

#### 已知代价与未决

- **换口径 ⇒ 要新 tag**:同 tag 重放读 `replay_results` 既有行,不重新抽样。
  所以新正例集必须换 tag,**误伤与 `路由后` 那一轮不再直接可比** —— 这是
  换掉弱基准必然付的一次;
- **选择性偏差**:活得久的品天然偏"无争议"(素色家具、家居杂货),
  在这一集上的低误伤可能低估全目录的误伤。报告给出年龄中位/最长,便于看偏斜;
- **`catalog.product_events` 的历史下架事件还没并进判据面**(键是 sku/asin 混装,
  要过一次 sku_asin),记为缺口;
- **反例侧的三档分离还没做** —— `label()` 现在把金标与推断揉成「带类别 N 条」,
  而 `audit_replay.py` 里 `_code` 在建样本时就被丢掉、`replay_results` 也没存
  沃尔玛原文与主码 ⇒ **金标子集连事后重建都做不到**。这一档要落库才能算,
  待所有者定主指标水位后一并做。

### 8.14 首跑 `金标基线`:正例 0 条,以及两处我自己的显示缺陷(2026-09-04)

`audit_replay -p tag=金标基线 -p pos_days=180` 实测:**正例扫描 0 条**。
干净在架 68,947 个 sku,**没有一个** `min(created_at)` 早于 180 天前。

#### 根因多半不是"整表重建",是这张表本身就没那么老

`created_at` 是**我们第一次同步到这行**的时间。本仓是 2026 年才开始的重构迁移
(`docs/plan.md`),`catalog.walmart_items` 是新建表 —— 它的历史上限就是我们
开始同步的那天,与沃尔玛的真实上架时间无关。180 天闸大过了表的年龄本身。

⚠ 所以自检那句「多半是整表重建过」**指错了方向**,已改:现在先查
「全库最老的是多少天」,再按两档分别说 ——
- **一条都没过**:闸比全库最老的还大 → 调闸(报告直接给出 `N 要 ≤ <最老天数>`);
- **过了但只剩零头**:`created_at` 挤在某一天 → 数据本身可疑,**调闸也救不回可信度**。

两档的下一步完全不同,合成一句话就没法照着做。

#### 两处显示缺陷(都是我写的,已修)

1. **空集伪装成"在线 0 天"**:`age_med` / `age_max` 在空集时给 `0`,报告于是写
   「入选品在线**中位 0 天 / 最长 0 天**」—— 看着像"入选的都是当天上架的",
   实际是**一条都没入选**。改成 `None`,并且只在真有入选品时才打这一行;
2. **底线指标整个缺席却不吼**:要了 400 条正例、实际 0 条,报告照常打印每一节,
   「正例误伤」那栏显示 `0/0 = —`,一眼扫过去像跑完了。现在**顶到首行点名**
   (与 `store_absence`「摘要首行点名」同一条纪律)。

#### 这一轮反例侧的读数(正例缺席不影响它)

| 指标 | 数 |
|---|---|
| 反例召回 | 206/282 = 73.0% |
| ├ L0/L1/L2 规则与记忆 | 201 条,判拒 201 |
| ├ L3 品牌翻拒(确定性后处理) | 3 条,判拒 3 |
| └ **L3 政策判据** | **78 条,判拒 2 ⇒ 判据召回 2.6%** |
| 真正由判据给出类别的 | **1 条**,1/1 |

与 `路由后` 那一轮(2/79 = 2.5%)几乎一致 —— **换正例口径不影响反例侧**,
两轮互相印证:**判据本身的召回就在 2.5% 上下**,不是抽样波动。

⚠ `按 L3 置信分层 93.8% 判定不符` 这一行本轮**没有意义**:正例为 0 ⇒ 分母
只剩走到 L3 的反例,而那批本来就是判据没拦住的。分层表在正例缺席时应当整节不出,
记为待修。
