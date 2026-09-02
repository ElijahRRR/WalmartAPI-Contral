# 商品审核链详细步骤(L0 → L1 → L2 → L3 → L4)

> 本文回答一件事:**一个 ASIN 从进入审核到落结论,中间到底发生了什么**。
> 每一层做什么、每条规则的判据来自哪张表/哪份文件、判不出来怎么办,逐条写清。
> 定稿日期 2026-08-20;规则集版本以 `registry/resources.py` 的 `AUDIT_RULES_VERSION`
> 为准(2026-08-26 核对为 `c.2026-08-24.1`)。
>
> 代码入口:`python cli.py product_audit`(`workflows/product_audit.py`)
> 判定引擎:`services/audit_rules.audit_one`(门面)→ `audit_phase0` / `audit_l1_llm`
> / `audit_l2` / `audit_l3` / `audit_l4` → `audit_reason`(理由映射)

---

## 0. 一句话总览

```
候选行 → [历史结论短路] → L0 精准拦截 → L1 定类目(PT) → L2 规则打分
                                                     ↓ pass 才继续
                                              L3 语义(LLM)
                                                     ↓ pass 才继续
                                              L4 视觉(LLM,默认关)
                                                     ↓
                              verdict(pass / reject / pending)+ 政策理由
```

三条贯穿全链的纪律:

1. **判不了 ≠ 判过了。** 任何一层给不出确定答案,一律 `pending` 交人工,
   绝不默认放行(计划 10.2)。唯一例外是 L4:视觉端点故障时维持原结论
   (它只做增量拦截、默认关、只对已 pass 的产品跑,故障不该制造假阳)。
2. **一件事只有一处判据。** 同一个判断不许存在两份平行清单 —— 改一处漏一处
   而且**不报错**。2026-08-20 删掉的 R0/R2/L1-excluded 就是这条纪律的执行。
3. **兜底必须留痕。** 任何回落、跳过、失败都要记日志计数并进 run 摘要;
   静默常态化 = 主路径已坏没人知道。

**打分体系名存实亡**:起始 100 分,硬规则 -100,软规则恒 0,阈值 60。
所以净语义就是"**任一硬规则命中即拒**",分数只是留给人看的痕迹。

---

## 1. 进入审核之前:谁会被审

`workflows/product_audit.py` 的候选谓词(`_DEFAULT_CANDIDATE`,唯一出处):

| 产品当前状态 | 会不会被领 |
|---|---|
| 从没审过(`audit_status IS NULL`) | 会,**优先级最高** |
| `pending` | 会,但有 **1 天退避**(`mode=pending` 可绕过退避) |
| `approved` | 版本号落后当前 `AUDIT_RULES_VERSION` 的**会**(2026-08-24 起,`_DEFAULT_CANDIDATE` 第三支);版本一致的不会 |
| `rejected` | **不会**自动重审 |

要整批重审只有显式通道:`-p force_rerun=<目标版本号>`(库里版本 ≠ 该值的全部重审,
含 rejected)、`-p rerule=<rule_code>`(只翻某一条规则的历史命中)、`-p repts=1`
(飞书类目表判据在我判过之后变过的那批 rejected)、`-p mode=pending` /
`mode=nonpass`(非 approved 全量)/ `mode=stale`(approved 版本落后,必须全链)/
`mode=pass`、`mode=online`(只与 `stages=L0` 连用)。

排序契约:**没审过的永远先于重试的 pending** —— 否则 pending 存量一多,
新入库的产品会被饿死。

**缺数据同轮补采**:表里轮到审、库里却没有(或有行无标题=采集降级)的 ASIN,
推采集批次 → 轮询等采完 → 就地摄取 → **这一轮就判掉**,不等第二天。

**历史结论短路**(`mode=backfill`):先查 `audit.audit_runs` 有没有旧结论,
有就直接采用(零 LLM,detail 里带 `referenced_run_id`,不写新 run)。
谓词必须排除 `stage_stopped_at='SHORTCUT'` 的影子行。

---

## 2. L0 —— 精准前置拦截(`services/audit_phase0.py`)

**定位**:只处理"100% 确定不能上"的情况。不做文本级启发式 ——
标题里出现某个品牌词这类模糊信号交给 L2 R4 / L3。

**四条规则串行短路**:任一命中即整条流水线终止,`verdict=reject`、
`score_final` 硬写 0、`stage_stopped_at='L0'`,`audit_hits` 只落 **1 行**。

| 序 | 规则 | rule_code | 判据来源 | 怎么判 |
|---|---|---|---|---|
| 1a | 卖家黑名单 | `phase0_lark_blacklist_seller` | `catalog.seller_blacklist` | `seller_id` 精确等值 |
| 1b | ASIN 黑名单 | `phase0_lark_blacklist_asin` | `catalog.asin_blacklist` | ASIN 精确等值 |
| 1c | 亚马逊类目黑名单 | `phase0_forbidden_category`(顶级名命中)/ `phase0_lark_blacklist_amazon_cat`(其余) | `catalog.amazon_cat_blacklist` | 见下 |
| 2 | 商标符号 ®/™/℠/© | `phase0_trademark_symbol` | 正则 | 大写开头 ≥3 字符的词紧邻符号 |
| 3 | 文案自述专利 | `phase0_patent_claim` | 正则 | "patented / 专利保护"等;漆皮(patent leather)豁免 |
| 4 | 品牌黑名单 | `phase0_brand_blacklist` | `catalog.brand_blacklist` | `brand` 字段**精确等值**(不是子串) |

### 2.1 亚马逊类目黑名单的三种匹配(2026-08-20 定稿)

判据**全在库里**,代码里一个类目常量都没有。同一张表用 `match_type` 分三种,
按精确度依次判定:

| 顺序 | match_type | 判据 | 为什么需要它 |
|---|---|---|---|
| ① | `node_subtree` | 产品 `browse_node_chain`(根→叶 ID 链)里出现名单中的 `browse_node_id` ⇒ 拦**整棵子树** | **首选**。名单写「拼图」,`拼图 > 3-D 拼图` 自动跟着被拦;类目改名也不失效 |
| ② | `top_name` | 路径**第一段**(顶级类目名)归一化后等值 | 亚马逊顶级 browse node 不发 ID,只能按名字 |
| ③ | `path_exact` | 归一化完整路径等值 | 飞书镜像的历史行;**父级不覆盖子级**,所以排最后 |

ID 链**从叶往根**查,命中最细的那棵子树 —— detail 里给的才是"它属于哪一棵
被禁的树",而不是"它在某个大类下"。

归一化(`normalize_amazon_category`)要点:`->` 必须先于 `>` 替换;段内空白
**全删**;顶级名比较时空白与逗号一起删(`Video Games` ≡ `VideoGames`)。

⚠ **顶级类目的粒度是"筐"不是"品"**。往表里加一个顶级 = 把整筐杂货一起拒掉
且停在 L0。加之前先问:这个筐里**每一件**都该拒吗?
(2026-08-17 教训:`Health & Household` 被整拦,一包牛皮纸礼品袋因为
"药品/膳食补充剂 restricted" 被拒。)

---

## 3. L1 —— 定类目(把亚马逊产品映射到沃尔玛 PT)

**定位**:L1 只做一件事 —— **这个产品在沃尔玛属于哪个 Product Type**。
它**不判**"这个类目能不能做"(那是 L2 R1 的事),也不判需不需要证书。

> 2026-08-20 变更:提示词里原有的「禁售品类」清单与 `excluded_category_reason`
> 输出字段已删除。那是**让模型凭标题猜类目禁令**,与 R1 白名单(按 PT 查
> 沃尔玛准入事实)讲同一件事,而且猜的那份还压在事实前面。

### 3.1 五级解析(`audit_rules.resolve_pt` + `audit_l1_llm`)

按顺序,**先命中先用**:

| 级 | 来源 | `pt_source` | 置信 | 说明 |
|---|---|---|---|---|
| ① | 沃尔玛在架实证 `catalog.walmart_items.product_type` | `walmart_confirmed` | 高 | 沃尔玛自己认过的类目就是标准答案(跨店唯一才采信) |
| ①b | 产品行已知 PT `catalog.products.walmart_pt` | `historical_confirmed`(沃尔玛回执实证)/ `audit_cached`(我们自己推断的) | 高 / 中 | **按来源分道**:不分道就成了"LLM 猜一个 → 下轮以高置信实证复述",猜错会被自己反复确认 |
| ⓪ | 映射表哨兵「无对应 Walmart PT」 | — | — | **只留痕不判死**(0 分 hit):判不出来才该 pending,不该拒 |
| ②a | `browse_node_id` 直查映射表 | `map_node` | 高 | 名称会漂 ID 不会 |
| ②b | 完整亚马逊路径精确等值 | `map_direct` | 高 | 先过路径别名折叠(`catmap_align`),免得把已映射路径当缺口 |
| ③ | **候选召回 + LLM rerank** | `llm_direct` / `map_fallback` | LLM 自报 | 见下 |

每一级解出 PT 后都要过 `pt_meta` 闸:**PT 不在 `audit.walmart_pt_meta` 就不直出**
(宁 pending 不假 pass —— 废弃 PT 会让 L2 的硬闸集体失明)。

### 3.2 第三级:七路候选 + rerank

前两级都解不出时,从 `audit.walmart_category_map` 七路召回候选 PT,
**返回顺序即优先级**(提示词只取前 20 条,截断砍的是尾巴):

1. exact / prefix / leaf(映射表精确匹配,必须最前)
2. `title_keyword`(关键词补充)
3. `title_literal`(标题字面词反查)
4. `ancestor_N`(沿类目树父链上溯,最近的已映射祖先的 PT)—— 祖先粗但方向对
5. `pt_dict`(直接搜 PT 字典,不经映射表)—— 前六路都依赖映射表有这个类目

七路**全空**时不直接 pending,改走**两阶段开放判定**:先让 LLM 选沃尔玛大类,
再在该大类的全部 PT 里挑(所有者定稿 2026-08-14)。

候选非空但 LLM 全否掉(`unknown`)时,给**一次二次机会**:把候选面换成
两阶段开放判定再判一次。只在 `unknown` 分支重试 —— LLM 失败/坏 JSON 是链路
故障,换候选面治不了,重试只是白烧一次调用。

### 3.3 L1 的两条出口

| 情况 | 结果 |
|---|---|
| 解出 PT | 带着 PT 进 L2 |
| 解不出(rerank unknown / LLM 失败 / 坏 JSON / 无候选) | `verdict=pending`,`stage_stopped_at='L1'`,`score_final=None`,**隔天重试** |

### 3.4 L1 唯一保留的硬拦:出版物

`publication_pt_forbidden`(-100):PT ∈ {Books, Manuals & Guides, Comics,
Sheet Music, Autographed Collectibles}。

依据是实证不是猜:`walmart_error_records` 9 天日报里这 5 类的 E(知产)
占比 96–100%。`Planners`(5.8%)/`Record Books`(19%)**不含** —— 误伤高。

> 这条**不是**类目准入判断,所以没有随 excluded 链一起删。

---

## 4. L2 —— 规则引擎(`services/audit_l2.py`)

起始 100 分 → 先叠加 L1 的扣分 → 七条规则**按固定顺序全跑、不短路**
(命中 -100 后后面照跑,证据要收全)→ 下界 -1000。

**三条会扣分**(硬规则:R1 / R3 硬分支 / R10),**四条恒 0 分**(软证据,只为喂 L3)。

| 规则 | rule_code | penalty | 判据来源 |
|---|---|---|---|
| **R1 类目准入** | `cat_access_blocked` / `cat_zh_blocked` | **-100** | `audit.walmart_pt_meta` 的 `access_state` + `zh_can_do` |
| R1(判不了) | `cat_gate_pt_unknown` / `cat_gate_pt_not_in_meta` | 0 → **pending** | 同上 |
| **R3 类目需证书** | `cat_requires_cert_hard` | **-100** | `walmart_pt_meta.requirements`(**唯一判据**,2026-08-21 收敛) |
| R3(软) | `cat_requires_cert_soft` | 0 | 同上 |
| R4 品牌黑名单扫文案 | `title_desc_blacklist` | 0 | `catalog.brand_blacklist`(Aho-Corasick) |
| R5 USPTO 在效商标 | `trademark_live` | 0 | uspto 库 `brand_nice_class`(**默认关**) |
| R7 促销宣称 | `content_promotional` | 0 | 代码内短语表 |
| R8 敏感/严格合规 | `walmart_strict_sensitive` | 0 | 代码内词表 |
| **R10 Made in USA 声明** | `made_in_usa_claim` | **-100** | 代码内正则(扫 title + 全部五点 + 长描述;`not made in` 否定式排除) |

> **R0 与 R2 已于 2026-08-20 删除**,详见第 6 节。
> R6(`blacklist_compatible_for`)2026-04 删除,误伤率 90%,改由 L3 判。
> R9(®/™)已前移到 L0。

两条硬规则共用一道闸:**上游已判死**(任一 L1 hit `penalty<0`,如出版物硬禁)
⇒ 整条规则不参与 —— 既不重复扣分,也不会把一条确定的拒降级成 pending。

### 4.1 R1:类目准入 —— **唯一的类目判据**

一个 PT 必须**同时满足**两条才允许继续:

1. `access_state ∈ {普通商品, 附条件允许}`
2. `zh_can_do == '是'` 或以 `'需评估'` 开头

任一不满足 → -100 → reject。两条都不满足时**只报更上游的** `cat_access_blocked`。

这张表由 `pt_spec_sync` 按沃尔玛官方 MP_ITEM spec 重建、飞书维护:
spec 的顶层必填字段 → 推"要什么认证" → 推"中国搬运做不做得了"。
**以后维护类目准入只需要维护这一张白名单。**

#### 判不了的两种情况(2026-08-20 P0 修复)

| 情况 | 此前 | 现在 |
|---|---|---|
| PT 空 / `unknown` / `(unknown)` | `return []` ⇒ 100 分**放行** | `cat_gate_pt_unknown`,penalty 0,**整条结论转 pending** |
| PT 不在 `walmart_pt_meta` | 记一条 warning 后**放行** | `cat_gate_pt_not_in_meta`,同上 |

为什么必须改:白名单是唯一的类目判据,而"查不到这个 PT"被当成了
"这个 PT 没问题"。

**说实话:当前接线下这两条走不到。** `resolve_pt` 末尾有一道同款 pt_meta 闸
(解出来的 PT 不在表里就丢弃),PT 解不出会先在 L1 转 pending,所以 L2 收到的
PT 必定在表里。这两个分支是**防御网**,不是在补一个正在漏货的洞。
留着的理由只有一条:白名单成了唯一判据之后,"查不到就放行"这个默认值本身
不能存在 —— 上游任何一道闸被放松,后果就是**静默满分放行且不报错**。
默认值要站在安全那一侧。

**不扣分是有意的**:扣分等于"证据确凿地拒",而事实是**没有证据**。
优先级 `reject > pending > pass` —— 有确定答案时不许降级成待定。

落库理由分三种(重试口径不同):

| stage | 理由 | 重刷有用吗 |
|---|---|---|
| L1 | 待类目判定(候选/rerank 均解不出) | 有用,隔天重试 |
| **L2** | **PT 不在类目准入明细,判不了(待补 walmart_pt_meta)** | **没用**,要等明细补行(`pt_spec_sync`) |
| L3 | LLM 全链路故障,待人工复核 | 有用 |

### 4.2 R3:类目需证书(硬/软两分支)

**两个**分支互斥、依次判定(硬优先):

| 分支 | 条件 | rule_code | penalty |
|---|---|---|---|
| A | `walmart_pt_meta.requirements` 命中**硬**认证关键词 | `cat_requires_cert_hard` | -100 |
| B | requirements 只命中**软**关键词 | `cat_requires_cert_soft` | 0 |

硬词:UL / ETL / CSA / NRTL / FCC / FDA(食品·药品·510(k))/ MoCRA / EPA(FIFRA)
/ CPSIA / CPC / GCC / AAFCO / NSF / ATF / DEA。
软词:SDS / ASTM / ANSI / ISO / RoHS / Prop 65 / 警告标签 / 测试报告。

### ⚠ 2026-08-21 下线了两条链(所有者定稿)

原本还有两个分支读 `audit.walmart_pt_spec`(硬 `has_real_cert` / 软
`has_soft_cert`),外加一个 NRTL **整机/小件分类器**(PT 名含
`replacement`/`parts`/`accessor` 强制判小件,否则保守判整机)。整套下线,理由两条,
都是实证:

1. **那张表是死快照。** `audit.walmart_pt_spec` 是批次 A 从旧审核库整表搬来的,
   `pt_spec_sync` 重建过但从没进过调度 —— 库里 `real_cert_fields` 存的还是**原始
   spec 字段名**(`has_nrtl_listing_certification`),而重建写进去的是认证**名称**
   (`NRTL 认证(UL/ETL/CSA)`)。两者口径还相反:旧数据判**硬**,清洗判**需评估**。
2. **整机 vs 小件代码判不了。** 生产实见:一张**实木咖啡桌**被判「整机电器,
   必须 NRTL 认证, 搬运做不了」—— 因为 `Coffee Tables` 的官方 spec 里带着
   `has_nrtl_listing_certification`(那是给带 USB 口的电动升降桌准备的字段),
   而分类器拿 PT 名猜,咖啡桌不含 `parts`/`accessor` ⇒ 保守判整机。同一个类目下
   整机与非电产品本来就是混着的。

所有者原话:「**代码只判定确定性的**,这种很明显不确定,应该交给 LLM 看这个产品
是不是整机电器,而不是让代码从类目看是不是整机。所以,旧的死快照不要了,死代码
也不要了,**以飞书源为准,以后我们只更新这个**」。

**先补后删,无真空期**:「整机电器」这一判定同批移入 **L3 判定维度 6**
(`audit_l3._S1`)——由 LLM 看 title/bullets/description 判产品本身带不带电、
是不是成品,**默认放行、拿不准 pass**。删掉的 `cat_requires_cert_small_part`
在存量 `audit_hits` 里仍有,理由渲染保留兼容。

`audit.walmart_pt_spec` 这张表**不删**:`pt_spec_sync` 仍写它,`audit_why` /
`pt_census` 仍查它做诊断 —— 只是审核链不再拿它当判据。

> **2026-08-20 P0 修复:裸子串 → 词边界。**
> 此前是 `kw in requirements`,`"ul" in "fda regulation"` 为真 ——
> **任何**写了 regulation 的类目都会被打成硬认证「UL 认证」并 -100 硬拒。
> `"iso" in "poison control"`、`"atf" in "platform"`、`"dea" in "idea"` 同理。
> 这是纯误伤:拒得理直气壮,理由却是从别的词里抠出来的两个字母。
> 现在纯 ASCII 关键词走词边界正则,中文/混排关键词(如「fda 食品」)仍走子串
> —— 中文没有词边界概念,`\b` 夹在两个汉字之间永不成立。

### 4.3 R4:品牌黑名单扫文案(证据)

扫 `title + 全部五点 + 长描述`,Aho-Corasick 自动机命中后**手动检查词边界**。
自品牌豁免是**精确等值**;同一个品牌只报第一次。penalty 恒 0 ——
命中的词是真品牌还是通用英文词,交 L3 判。

> **2026-08-20 修复:中文紧邻不算边界。**
> 原判据 `c.isalnum()` 对汉字返回 True,于是「耐克运动鞋」里的「耐克」
> 左右都被判成词内字符 —— **中文品牌一条都拦不住**,而且不报错。
> 中日韩与全角字符不写分词空格,紧邻即边界。带音标的拉丁字母(café 的 é)
> 仍按词内字符处理,免得切出 "Caf" 这种假前缀。

### 4.4 R5:USPTO 在效商标(证据,默认关)

标题/描述里提取"疑似商标"候选词(大写开头、总长 ≥4、过 stopword),
按 `walmart_category` 映射出的 Nice Class 过滤,查 uspto 库的 LIVE 商标。

默认关的理由:`brand_nice_class` 覆盖率只有 ~2.6 万 / 1400 万。
连续失败 5 次自动关停本轮并在摘要亮出计数(不许静默 fail)。

### 4.5 R7:促销宣称(证据)

扫 `title + 前 3 条五点`(全大写连跑只扫 title):

- **strong**:无据的最高级/排名宣称(`premium quality` / `#1` / `best seller`
  / `military grade` / `FDA approved` / `100% guaranteed` …)
- **soft**:空洞形容词(`high quality` / `unbeatable` / `factory direct` …)
- **allcaps**:连续 3+ 个全大写词(噪声 token 如 USB/LED/ROHS 不凑数)

> **2026-08-20 修复两处:**
> ① 只命中 soft 短语时**整条 hit 被丢掉**,而 detail 里写着"L3 LLM 需判断"
> —— 承诺了没送到,比不承诺更糟。现在照样落账,`soft_only=True` 标出份量。
> ② 噪声表里的 `"RoHS"` 因为比较用 `t.upper()` **永远匹配不上**,
> 单字母项 `L/M/S` 因为正则要求 ≥2 字符**证明不可达**。整表统一大写、删死条目。

### 4.6 R8:敏感 / 严格合规(证据)

迎合沃尔玛**实际**审核尺度(即使政策文本未明列):敏感文化日 / 政治 / 宗教
/ 武器 / 成人 / 卡通 IP。penalty 0,detail 带 `subtypes` 与命中短语,交 L3 定夺。

---

## 5. L3 —— 语义审核(LLM,`services/audit_l3.py`)

**只对 L2 pass 的产品跑。** 补 L2 硬规则抓不到的问题,提示词里是**六个判定维度**:

| # | 维度 | 判什么 |
|---|---|---|
| 1 | 品牌真伪 | R4 命中的词是真品牌还是通用英文词 |
| 2 | 冒犯性内容 | 色情 / 仇恨象征 / 仿真武器 |
| 3 | 知识产权 | 商标 / 版权 IP / 专利 / Trade Dress / 肖像权 |
| 4 | 品牌字段伪装 | brand 填 Unbranded 但文案暗示大牌 |
| 5 | 儿童产品 CPC 兜底 | L1 类目漏判时从文案兜 |
| **6** | **整机电器 / NRTL**(2026-08-21 新增) | 产品**本身**带不带电、是不是成品 |

**维度 6 是从 L2 移上来的**,不是新加的判定 —— L2 原来按 PT 名猜整机/小件的
分类器同日下线(见 §4 R3 那节)。移上来的理由是所有者定的:「代码只判定确定性
的,这种很明显不确定」。**默认放行、拿不准 pass** —— 绝大多数产品不带电,宁可
漏一个也不要重蹈"按类目名连坐整类"的覆辙。

判定**不动分数**(hit penalty 恒 0),只决定 verdict:
`reject` → 整品拒(stage=L3);`pending` → 待人工;`pass` → 交 L4(若开)。

**prompt 前缀缓存契约**:messages 恒为 `[system, user]`,system prompt 对**同一轮
的所有产品**逐字节相同(政策静态段 + 政策压缩块 —— 候选块与压缩块都由政策表
**全部**行实时渲染),进程内只构造一次。任何把产品信息拼进 system 的写法都会
打散前缀缓存,命中率从 ~95% 掉回 ~63%。

⚠ **2026-09-02 契约退役**(`docs/policy_sync.md` §十.7):"对所有产品逐字节相同"
的实际含义一直是"同一轮内相同" —— 政策表一改(新增行 / 改名 / 刷新正文),
提示词就跟着变,这是**设计如此**(政策表就是 L3 的判定输入)。提示词里那句
硬写的「37 条」同批改为按实时条数渲染(库里早就不是 37 行,自称的数目与紧随
其后的清单对不上)。政策表变更后前缀缓存一次性重建属预期成本,同批递增
`AUDIT_RULES_VERSION`。

**失败语义与旧系统相反**:LLM 重试尽 / 坏 JSON / verdict 取值非法,
**一律 pending**(旧仓是 pass)—— 故障窗口漏审违规商品的代价远大于人工复核。
pending **不写 llm_cache**。

### 5.0 政策路由:哪几条政策会被点名送进 user 段

`route_policy_hints(walmart_category, walmart_pt, known=…)` 用两张内存表
(`_CATEGORY_ROUTES` 等值 + `_PT_KEYWORD_ROUTES` 裸子串)挑 ≤5 条最相关政策,
永远以 `Intellectual Property` + `Offensive Content` 打头(截断也挤不掉)。
**这只是提示**,不是判据白名单 —— 白名单是全表(`valid_reason_categories`)。

两张路由表里写的是**旧仓搬迁时的缩写名**(`Electronics & RF` 一族),而政策表
2026-09-02 起用官方拼写。所以每个条目都过一次
`services/policy_names.resolve(名字, known)` 再返回:精确 → casefold → 词形
(`&`↔`and` / 逗号 / 括号后缀 / 单复数)→ 旧名映射,**命中回表内原拼写**。
改名前后同一张路由表都活,不必跟着改名批改字面量。

> ⚠ 旧写法是 `c in known` 直接过滤 —— 改名后会**静默丢掉 7 条**政策提示:
> 提示词照旧漂亮、判定照旧返回,只是 L3 少看了 7 类政策。没有任何东西会红。

**两条本来就是死的**(不是改名改坏的,官方 42 名里根本没有这两个类别名):

| 路由表写的 | 政策表里的实际情况 |
|---|---|
| `Pet Products` | 官方是 `Pet Foods, Supplements, Medicines and Other Products`,归一化打不平 |
| `Jewelry/Precious Metals` | 旧仓自造的斜杠写法,官方那条是 `Jewelry, Watches, … (Covered Goods)` |

它们每次命中记 **warning + 计数**(同名一轮只警告一次,计数逐次累加),
进 `product_audit` 摘要的「⚠ L3 政策路由解析不到 N 次」那行 —— 旧写法只记
debug,等于没人看得见。改法(补映射 / 改路由表 / 枚举化)随
`docs/policy_sync.md` §十.7 的「L3 输出规范化」一起定。

### 5.1 L2 证据怎么送进 L3

`summarize_l2_for_l3` 把 L2 的软 hit 摘成几行文本进 user 段:

| L2 hit | 送什么 |
|---|---|
| `title_desc_blacklist` | 前 10 个命中品牌 + 原文片段 |
| `cat_requires_cert_*` | `meta_requirements` / `hard_cert_fields` / `soft_cert_fields` |
| `trademark_live` | 前 10 个 USPTO mark |
| `content_promotional` | strong + allcaps 短语;只有软词时标「仅空洞形容词」 |
| `walmart_strict_sensitive` | subtypes + 命中短语 |

> **2026-08-20 补上两处丢证据**(此前是"照迁旧缺陷"):
> ① R7/R8 **原先完全没有分支**,一个字都进不了提示词 —— L2 在 detail 里
> 写着"L3 LLM 需判断宣称词是否有事实依据",L3 却只能自己从原文重看一遍。
> ② cert 分支取的是 `detail['requirements']`,这个键在三种 cert hit 里
> **一个都不存在**(真实键是 `meta_requirements` / `hard_cert_fields` /
> `soft_cert_fields`),于是前两档永远退化成一句固定套话。

---

## 6. L4 —— 视觉审核(LLM 多图,默认关)

**只对当前 verdict 仍为 pass 的产品跑**,且**只能把 pass 翻成 reject**
(它根本不对 reject 产品运行)。开关:`-p l4=on`。

图片来源:主源 `catalog.products.slow -> 'images'`(采集契约 v1 里 slow 是顶层必填段,
product_ingest 落进来;首图=主图);主源无图才回落最新一条含图快照的
`raw.slow.images[]` 并记一条兜底告警。不套 24h 新鲜度门槛(图不像价格易变)。

判定确定性:温度必须 0.0;verdict **由本地 `image_issues` 重算,模型自报的
verdict 字段完全忽略** —— 否则同一个 ASIN 一次 pass 一次 reject,无法闭环。

**失败语义是全链唯一的例外**:五条失败路径(无图 / 全部下载失败 / 调用失败
/ 坏 JSON / schema 违约)一律 **维持 pass** + 一条 penalty=0 的告警 hit + 日志。
**L4 绝不产出 pending。**

L4 不动分数,所以 L4 reject 的产品落库分数通常仍 ≥60 = "分数够但被视觉拦下"。

---

## 7. 理由映射:reject 之后说人话(`services/audit_reason.py`)

`verdict=reject` 时算一个 **Walmart 政策表的 `category_en`**(条数随表,
2026-09-02 起是官方 42 类),按顺序:

| 步 | 判据 | 结果 |
|---|---|---|
| 0 | 不是 reject | None |
| 1 | hit 是 `phase0_forbidden_category` 且 detail 有 `walmart_policy` | 该 policy 原样 |
| 1.2 | 黑名单中心三码(asin/seller/amazon_cat) | **None**(内部决策,不对应任何沃尔玛政策) |
| 2 | L3 判 reject 且给了 reason_category | 归一化值(**随表**:`policy_names.resolve` 对实时 `category_en` 集合解析,命中回表内原拼写;2026-09-02 §十.7) |
| 1.5 | 其余带 `walmart_policy` 的 hit | 该 policy **对表解析后**的表内拼写(解析不到才原样) |
| 3 | L4 判 reject | 按 issue 文本 → Offensive Content / Intellectual Property |
| 4a | `publication_pt_forbidden` | Intellectual Property |
| 4c | PT 关键词十组 | 对应政策 |
| 4d | cert 两码(`cat_requires_cert_hard` / `_soft`;`_small_part` 2026-08-21 已下线)→ 按 `walmart_category` 分桶 | 对应政策 |
| 4e/4f | 商标 / 黑名单品牌 | Intellectual Property |
| 4g | 全不中 | General-Use Products |

步 2 与步 1.5 都走 `services/policy_names.resolve(名字, ctx.known_policies)`
(仓内唯一一份政策名归一化),命中回**表内原拼写**:

- 步 2 吃的是 L3 答出的 `reason_category`;
- 步 1.5 是 **`services/audit_l2._infer_walmart_policy` 那批写死的旧缩写名**
  (`Military & Law Enforcement` / `Electronics & RF` / `Drugs & Paraphernalia`
  …)进 `final_reason_category` 的**唯一出口** —— 在这一处解析就够了,
  不逐条改 audit_l2 的常量(那是 §十.7「L3 输出规范化」那一步的事)。

落在政策表之外时**只记 warning 不改判**(兜底触发必须留痕)。已知会落在表外的:
`Restricted/Illegal` / `Jewelry/Precious Metals` / `.title()` 变形值,以及
2026-09-02 表改官方拼写之后,**4c/4d 两步**里写死的旧缩写名(`Military & Law
Enforcement` 一族)—— 那两步是 PT/类目关键词**兜底推断**,输入根本不是政策名,
对表反而会把"推断"伪装成"表里查到的";改法随 §十.7 的「L3 输出规范化」一起定。

---

## 8. 2026-08-20 的收敛:为什么删掉 R0 / R2 / L1 excluded

### 8.1 删了什么

| 被删 | 原来是什么 | 体量 |
|---|---|---|
| **L2 R0** | 代码常量:8 个 `walmart_category` 硬禁(Vehicles / Electronics / Fashion / Food & Beverage / Health & Personal Care / Beauty / Baby / Automotive) | 8 条 |
| **L2 R2** | `refdata/audit/forbidden_categories_zh_seller.yaml` 的 `mega_forbidden_categories`:按 PT 名关键词 / PT 精确等值 / `walmart_category` 前缀 | 18 组 |
| **L1 excluded** | 同一个 yaml 的 `excluded_categories`:按亚马逊路径段 / PT 子串 / 标题词判 3C·服饰·汽配·带电禁售 | 13 条 |
| LLM 侧 | 提示词里的「禁售品类」整节 + `excluded_category_reason` 输出字段 | — |

`forbidden_categories_zh_seller.yaml` 整份文件已删除。

### 8.2 为什么

这三份清单和 **R1 类目准入白名单讲的是同一件事** —— 一个类目能不能做。

- 四份判据各自维护,**改一处漏三处**,而且漏了**不报错**;
- 三份黑名单都是**猜**的(按类目名前缀、PT 名里的词、亚马逊路径段名),
  白名单是**查**的(沃尔玛官方 spec 的必填字段 + 沃尔玛侧准入事实);
- 猜的那几份还压在事实前面 —— R0 按 `walmart_category` 前缀拦,
  `Camera` 这类词按词边界会把相机包/贴膜/镜头盖一起拦
  (词在 PT 里 ≠ 产品是那个东西)。

所有者定稿:「**以后我只要维护这个沃尔玛类目白名单即可**」。

### 8.3 顺序很重要:先补白名单,再删黑名单

**中间不能有真空期。** 实际执行顺序:

1. `pt_spec_sync` 按本地官方 MP_ITEM spec 重建类目准入明细 → 白名单补齐 ✅
2. R1 的两条静默放行改判 pending(否则白名单查不到就等于放行)✅
3. R3 裸子串修掉(否则误伤会在删掉黑名单后更显眼)✅
4. 才删 R0 / R2 / L1 excluded / yaml ✅

### 8.4 影响面

- 曾被 R0/R2/excluded 拦下、而白名单放行的产品 → 会翻成 **pass**;
- 曾因 R3 裸子串误判"要 UL 认证"的 → 翻案;
- PT 不在准入明细的 → 从"静默 pass"变成 **pending 待人工**(这批要盯);
- 中文品牌命中 R4 的证据 → 现在才真的收得到。

全量重审:`python cli.py product_audit -p force_rerun=c.2026-08-18.1`

### 8.5 关于那 46 条沃尔玛禁售政策

**不接进类目判定。** 那 46 条是**按产品**写的概述,不是按类目写的:
同一个 PT 底下既有能卖的也有不能卖的(`Garden & Patio` 下既有园艺耙也有
禁售的活体种苗),PT 级套政策必然误杀整类。
政策的落点在**产品级**:L2 `_infer_walmart_policy` 推出政策标签 → 给 L3 当上下文。

---

## 9. 排查一条结论:`audit_why`

```bash
python cli.py audit_why -p asins=B0XXXXXXXX
```

只读,输出这个 ASIN 停在哪一层、命中了哪些 rule_code、每条的判据来自
**哪张表哪一列**。看到不认识的 rule_code 先查 `workflows/audit_why.py`
里的判据出处表。
