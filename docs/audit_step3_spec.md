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
| **B 换喂 + 规范化 + 回放** | S4 换官方全文;user 段扩容;输出 schema 三段化;`audit_detail` 落库;理由映射去猜测;证据通道泛化;路由提示删除;`audit_replay` 回放工作流 | 生产机**不 pull**(见 §五) |
| **C 瘦身 + 清理** | L0 双输出(品牌文案扫描迁入)+ Made in USA 迁入;L2 = R1;删 R3 硬拒/R4/R5/R7/R8/R10 及其数据;删 `POLICY_LEGACY_NAMES` / `POLICY_ALIASES` / `to_official` / `_L3_NORMALIZE` / `_pt_to_policy` / 路由表 | 生产机 pull A+B+C → 按 §五 切换 |

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

### 3.1 system prompt(S1–S4,仍是单一连续静态前缀)

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
- 体量:S1+S3 约 6K 字符不变;S4 由现上界 ≈17.6K 字符变为 ≈199K 字符(42 篇
  `render_feed_text` 后实测 199,123;Content Standards 另加),≈ 5–5.5 万 token;
  deepseek-v4-flash 1M 上下文内,前缀缓存命中的硬前提(顺序固定、逐字节稳定)不变。

### 3.2 user 段

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

### 3.3 输出 schema 与解析

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
- `L3Result` 字段随 schema 改名;`raw` 仍不落库;pending 不写 `llm_cache`(不变)。

### 3.4 三段落库与投影

| 落点 | 判定结果 | 类别 | 具体内容 |
|---|---|---|---|
| `audit.audit_runs` | `verdict` / `l3_verdict` | `l3_reason_category`(列名不改,语义 = policy 枚举) | `l3_reason_text`(列名不改,语义 = detail) |
| `catalog.products` | `audit_status` | `audit_reason` := 类别(枚举 / `none` 不写,pass 与 pending 为 NULL) | **新列 `audit_detail text`** |
| `catalog.product_events`(`audit_rejected`) | event | `detail.reason`(键名不改,兼容 `audit_history_fold`) | 新键 `detail.detail` |
| 飞书上架表 | E 列(pass/reject/pending,不变) | F 列 = `【类别】具体内容`(一个单元格) | 同左 |

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

### 3.5 理由映射:`compute_final_reason` 收敛为查表

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

### 3.6 证据通道泛化

`summarize_l2_for_l3(l2)`(`services/audit_l3.py:549-598`,只读 L2)→
`summarize_evidence(outcome_partial)`:读 phase0 / l1 / l2 三层里 `penalty == 0` 的软 hit,
按 `rule_code` 查一张渲染表出一行;未登记的 rule_code 原样打 `* {rule_code}: {detail 摘要}`,
不丢。B 批渲染表 = 现五分支(R4 品牌 / R3 证书 / R5 商标 / R7 促销 / R8 敏感);C 批删到只剩
L0 品牌文案扫描一条。品牌词清单(`MAX_BRANDS=10`)从同一通道取。

### 3.7 路由提示:删除

`route_policy_hints` 与 `_CATEGORY_ROUTES`(31 键)/ `_PT_KEYWORD_ROUTES`(13 组裸子串)/
`_ALWAYS_INCLUDE` / `ROUTE_MAX_POLICIES` / `STATS["route_unresolved"]` / product_audit 摘要第 10 行
(`workflows/product_audit.py:1366-1372`)整体删除。理由:它是第二张手工维护的「类目 → 政策」
映射,而 §十.7 已定「政策类别 ≠ 类目」;换全文后 LLM 面前有全部 43 篇,提示只会把注意力
锁在 ≤5 篇上。回放评估(§3.8)顺带验证删了之后类别准确率没掉。
(若所有者要保留:改成只读官方名的常量表,不再走 `to_official`。见 §六。)

### 3.8 回放评估 `workflows/audit_replay.py`

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

### 3.9 成本与缓存

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

### 3.10 测试、文档、版本

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

## 四、C 批规格:瘦身 + 清理

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

## 五、切换手册(A+B+C 都合并后,一次做完)

```bash
git pull                                    # 看钟:18:10 之前 pull,当晚 audit_sheet 就用新链
python cli.py db_init                       # audit_detail 列 + replay_results 表(幂等)
python cli.py policy_sync --dry-run         # 预期 新增 2(id 43/44 内容族)/ 刷新 42 / 改名 0
python cli.py policy_sync
python cli.py audit_replay --dry-run        # 样本规模 + 预估成本
python cli.py audit_replay -p neg=600 -p pos=400   # 谷时段;看报告再决定下一步
python cli.py product_audit -p mode=stale -p limit=N   # 谷时段分晚跑;pending/rejected 走 mode=nonpass
```

- 回放报告不达标(§六第 5 条的线)→ 不跑 `mode=stale`,先修提示词/规则再回放;修改 = 再提版。
- 回滚 = `git revert` C/B 两批(A 的转录件无害);已被新版本盖章的行要再付一次重审。
- `error_reclass_report` 不受影响;`audit_sheet` 的 `limit=500` 在切换周可临时调低控成本。

## 六、待所有者裁决(默认值 = 我的建议;同意即按此开工)

1. **类别词表**:43 官方名 + `内部黑名单` / `类目准入` 两条非政策类别;pending 类别为空;
   零兜底(§二)。
2. **路由提示删除**(§3.7);备选:保留但改成官方名常量表。
3. **飞书 F 列**:`【类别】具体内容` 一格;备选:拆两列(运营在上架表加一列 + registry 加字段)。
4. **`catalog.products` 新列 `audit_detail`**(§3.4);备选:不加列、拼进 `audit_reason`
   (下游要再拆,不推荐)。
5. **回放集与验收线**:反例 600(按期望类别分层封顶)+ 正例 400;验收线建议 —— 正例误伤率
   不高于旧链、带类别反例的类别准确率作为主指标报出(先看数,不预设阈值)。
6. ~~Content Standards 范围~~ **已定(2026-09-02)**:所有者把两页都给了,两页都进(43 索引页 +
   44 规则页);43 的 H1 已由所有者确认,FAQ 段待补录。Overview 页链接的 21 个分类风格指南**不进**(所有者定稿 2026-09-02:每页按产品类型再挂深链,
   拿不完也放不下,是写内容的规范不是判违规的判据;将来做内容生产时参考)。
7. **描述截断 600 → 3000**(§3.2)。
8. **全量重审规模**:approved 存量全部走 `mode=stale`,还是只让 `audit_sheet` 按需重审
   (§10 原话:「规则存在,上架时对要上架的品起作用就够了」)—— 建议后者为主、`mode=stale`
   只跑近 90 天有动销的一批。

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
