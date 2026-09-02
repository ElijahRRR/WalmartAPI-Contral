# 商品审核链详细步骤(L0 → L1 → L2 → L3 → L4)

> 本文回答一件事:**一个 ASIN 从进入审核到落结论,中间到底发生了什么**。
> 每一层做什么、每条规则的判据来自哪张表/哪份文件、判不出来怎么办,逐条写清。
> 定稿日期 2026-08-20;**2026-09-02 第三步 B1 批**改写 §5(L3 换喂官方全文 +
> 输出三段化)与 §7(类别自报 + 理由映射查表),执行规格
> `docs/audit_step3_spec.md` §三。规则集版本以 `registry/resources.py` 的
> `AUDIT_RULES_VERSION` 为准(2026-09-02 核对为 `c.2026-09-02.2`)。
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
              verdict(pass / reject / pending)+ 类别 + 具体内容
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

**`mode=stale` 还带一道动销闸**(2026-09-02 B2,所有者定稿
`docs/audit_step3_spec.md` §六.8:「只跑近 90 天有动销的一批,不再全量重付」):
`-p active_days=N` 缺省 **90**,追加谓词「`orders.order_lines` 里近 N 天有非
`Cancelled` 的行」;`-p active_days=0` **显式**关掉这道闸(全量 approved,最贵)。
口径与 `services/alloc_survey._SQL_SALES` 同源(全仓动销只有那一份):窗口打
`order_date`、只排 `Cancelled`、关联用 **`asin` 列**(由 `order_asin_normalize`
按 `services/sku_asin` 规则补填,提不出的留 NULL 自然不算动销)——
⚠ 绝不拿 `sku` 当 asin:三段式订货号与纯数字 item id 直接等值永远查空,
表现是"这批产品全都没动销过",而且不报错。摘要**首行**点名圈的是哪一批。

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

每条硬拒规则在 `hit.detail["category"]` 里**自报类别**(2026-09-02 B1,§7.2):
黑名单三码 = `内部黑名单`(类目那条若行内 `walmart_policy` 能对到政策表则用
该政策)、商标符号 / 专利自述 / 品牌黑名单 = `Intellectual Property`。

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

两条 hit 的类别都是 `类目准入`(2026-09-02 B1 在 detail 的 `category` 里自报)。
此前写死的 `walmart_policy="Restricted/Illegal"` 是**猜的** —— 白名单拦下与那条
禁售政策没有关系 —— 已删。

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

**只对 L2 pass 的产品跑。** 2026-09-02 第三步 B1 批换喂:L3 面前不再是六列
中文人工摘要 + 代码猜出来的政策路由,而是 **44 篇沃尔玛官方英文政策全文** +
上游三层的确定性证据 + 本 PT 的准入要求。

判定**不动分数**(hit penalty 恒 0),只决定 verdict:
`reject` → 整品拒(stage=L3);`pending` → 待人工;`pass` → 交 L4(若开)。

### 5.1 提示词四段(S1 指令 / S2 类别枚举 / S3 分隔 / S4 政策全文)

| 段 | 内容 | 来源 |
|---|---|---|
| S1 | 指令:判据只认末尾原文、`policy` 逐字抄枚举、`detail` 的写法、品牌证据怎么判、本 PT 准入要求怎么判、严格 JSON | 代码字面量(`_S1`) |
| S2 | 候选类别清单 | `SELECT category_en … ORDER BY category_en` + `registry.resources.AUDIT_NONPOLICY_CATEGORIES` + `none` |
| S3 | 分隔段(自称的篇数 = S4 实际渲染出的篇数) | 代码字面量(`_S3`) |
| S4 | **44 篇官方英文全文**,每篇 `## 类别名` + 喂入版正文 | `SELECT id, category_en, full_policy … WHERE full_policy IS NOT NULL ORDER BY id`,经 `services/policy_feed.render_feed_text` 渲染 |

- **喂入版渲染件不落库**:库里存官方原文,喂 LLM 的版本渲染时派生(清洗只有
  一条路径,`docs/policy_sync.md` §十.3);剥掉链接 / 页内导览 / 免责声明 /
  页面 chrome,单行数据表转成「列名 + 条目清单」。
- **没有全文的行整条跳过并记账**(`audit_l3.missing_full_text()`,进 run 摘要):
  只剩一个 `## 标题` 的空壳等于没给判据,却会让 LLM 以为"这一类我看过了"。
  它仍在 S2 候选里 —— 那正是要人去补 `full_policy` 的信号。
  ⚠ 记的是**构建期状态**不是每轮计数:提示词一个进程只构造一次,而
  `reset_stats()` 每轮开头调 —— 放进 STATS 的净效果是"第一轮报了、后面每轮
  都报 0",而缺失一直都在。构建本身在 `_PROMPT_LOCK` 下做(并发 128 起跑时
  不加锁就是每个线程各渲染一遍 44 篇全文)。
- **篇数按真正渲染出来的段数填**,不是政策表行数,也不能数 `## ` ——
  官方正文自己带 `## Overview` 一类小标题(实测 251 个),数出来的是假数。
- 体量:S1+S3 约 2.6K 字符,S4 约 21 万字符 ⇒ 整段 system prompt ≈ 6 万 token。

**prompt 前缀缓存契约**:messages 恒为 `[system, user]`,system prompt 对**同一轮
的所有产品**逐字节相同,进程内只构造一次。任何把产品信息拼进 system 的写法都会
打散前缀缓存(命中率 ~95% → ~63%)。政策表一改(新增行 / 改名 / 刷新正文),
S2/S4 跟着变 —— 这是**设计如此**(政策表就是 L3 的判定输入):缓存的前提是
"每个产品都一样",不是"永远一样";变更后一次性重建属预期成本,同批递增
`AUDIT_RULES_VERSION`。

### 5.2 user 段:产品 + 上游证据 + 本 PT 准入要求

```
# 产品信息      ASIN / 标题 / 品牌字段 / Amazon 类目 / 沃尔玛 PT / Category
               (+ 飞书人工标注 pt_meta.notes,≤200 字符)
               五点描述(≤10 条)+ 长描述(≤3000 字符)
# 本 PT 的沃尔玛准入要求    walmart_pt_meta.requirements[:500](没有则整段不出)
# 上游证据                  L0/L1/L2 三层 penalty==0 的软 hit
# 待评估的品牌/商标词        同一通道里的品牌词,前 10 个
```

三处与 2026-09-02 之前不同:

- **长描述 600 → 3000 字符**(所有者定稿 §六.7):判违规靠的是正文,600 字
  砍掉的正是宣称最密的那一段;每品多 ≈1K token,与 6 万 token 前缀比可忽略。
- **删「原产国」行**:采集契约里根本没有这个值,给 LLM 一个恒空字段只会
  诱导它把"原产国未知"当证据。
- **新增「本 PT 的沃尔玛准入要求」**:R3 硬拒的替身。"这个类目要什么证"是
  事实(飞书维护),"**这个具体产品**要不要"是判断 —— 后者交 LLM,拿不准
  一律 pass(2026-08-21 咖啡桌教训的彻底版:不许按类目名连坐整类)。

### 5.3 上游证据通道(`summarize_evidence`)

读 **L0 / L1 / L2 三层**里 `penalty == 0` 的软 hit(扣了分的是硬拒,那种产品
根本进不了 L3),按 `rule_code` 查一张渲染表出一行:

| rule_code | 送什么 |
|---|---|
| `title_desc_blacklist`(R4) | 前 10 个命中品牌 + 原文片段 |
| `cat_requires_cert*`(R3) | `meta_requirements` / `hard_cert_fields` / `soft_cert_fields` |
| `trademark_live`(R5) | 前 10 个 USPTO mark |
| `content_promotional`(R7) | strong + allcaps 短语;只有软词时标「仅空洞形容词」 |
| `walmart_strict_sensitive`(R8) | subtypes + 命中短语 |
| `phase0_brand_mention`(C 批迁入 L0) | 前 10 个命中品牌 + 原文片段 |
| **未登记的任何 rule_code** | `* {rule_code}: {detail 摘要}` —— **不丢** |
| `audit_reason.NOT_A_REASON` 里的(`pt_dict_fallback` / `unmapped_amazon_path` / `l4_images_partial` / `l4_bad_schema`) | **不送** —— 见下 |

通道按 rule_code 查表,与证据出自哪一层无关 —— C 批把品牌文案扫描迁进 L0
时,判定链一个字都不用改。品牌词清单(≤10)出自同一通道。

> ⚠ 这条通道的合同是"上游软证据一条不丢"。漏掉一条不会报错,只会让 L3 少看
> 一样东西 —— R7/R8 曾经整整两个月一个字都没进提示词,而 L2 的 detail 里
> 写着"L3 LLM 需判断宣称词是否有事实依据"。承诺了没送到,比不承诺更糟。

> ⚠ **过程留痕不是证据**(2026-09-02 复核补):`pt_dict_fallback`(类目靠
> 字典回落)、`unmapped_amazon_path`(映射表曾标注无对应 PT)记的是**我们
> 自己链路里发生了什么**,与产品违不违规无关 —— 送进去等于请 LLM 拿"内部
> 没把类目定准"当拒绝理由。判据唯一出处是 `services/audit_reason.NOT_A_REASON`
> (人话渲染那边用的是同一张表),别在通道里另列一份。

### 5.4 输出三段与解析

```json
{"verdict": "pass|reject",
 "policy": "<候选类别之一,逐字;pass 时 none>",
 "detail": "<中文 ≤120 字:原文片段 + 条款要点>",
 "brand_verdicts": [{"brand": "…", "is_real_brand": true, "evidence": "…"}],
 "confidence": "high|medium|low"}
```

解析顺序(`parse_l3_reply`),**零模糊归一化**:

1. 非 JSON / verdict 取值非法 → pending `llm_bad_json`(绝不默认放行);
2. **品牌翻拒**:任一 `brand_verdicts[].is_real_brand is True` 且 LLM 自述
   pass → 改判 reject + `Intellectual Property` + detail「未授权引用品牌名 X」
   (严格 `is True`,字符串 "true" 不算);
3. pass → `policy` 强制 `none`(pass 没有类别);
4. reject → `policy` 经 `services/policy_names.resolve` 对枚举解析,命中回
   **表内原拼写**;**对不上 → pending `llm_bad_policy` + 计数**;
5. reject 落 1 条 L3 hit:`rule_code = llm_<policy slug>`,detail 五键定序
   `{policy, detail, confidence, brand_verdicts, prompt_version}`。

> ⚠ 第 4 步是 B1 最要紧的一处删除。旧版认不出类别就降级猜 `intellectual
> property` —— 猜出来的类别会一路落库、进飞书 G 列、进申诉口径,而没有任何
> 东西会红。现在它转 pending,并且计数进 run 摘要:成批出现 = 提示词或政策表
> 出了问题,不是单品的事。

**失败语义与旧系统相反**:LLM 重试尽 / 坏 JSON / verdict 非法 / 类别对不上,
**一律 pending** —— 故障窗口漏审违规商品的代价远大于人工复核。
pending **不写 llm_cache**。

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

## 7. 类别与具体内容:reject 之后说什么(`services/audit_reason.py`)

结论是**三段**(2026-09-02 第三步 B1 定稿,`docs/audit_step3_spec.md` §二/§3.4):

| 段 | 是什么 | 落点 |
|---|---|---|
| 判定结果 | pass / reject / pending | `products.audit_status`、上架表 F 列 |
| **类别** | 判定落在哪一类 | `products.audit_reason`、上架表 G 列、事件 `detail.reason` |
| **具体内容** | 那一句话:原文片段 + 条款要点,或规则命中翻成的人话 | `products.audit_detail`、上架表 H 列、事件 `detail.detail` |

### 7.1 类别词表:只许两种来源、零推断

1. **官方政策类别名** —— `audit.walmart_prohibited_policy.category_en` 实时集合
   (44 条:42 类禁售 + 内容族两页),用**表内原拼写**;
2. **两条非政策类别**(`registry.resources.AUDIT_NONPOLICY_CATEGORIES`):
   - `内部黑名单` —— 卖家 / ASIN / 亚马逊类目黑名单命中(我们自己的决策,
     不对应任何沃尔玛政策);
   - `类目准入` —— 类目白名单拦下、出版物硬禁、以及 L3 判"需证而无"却没有
     任何政策覆盖它。

pass → 类别 NULL;pending → 类别 NULL(待定原因写在**具体内容**里)。

### 7.2 每条硬拒规则**自带**类别(在 detail 的 `category` 键里自报)

| 规则 | 类别 |
|---|---|
| `phase0_lark_blacklist_seller` / `_asin` | `内部黑名单` |
| `phase0_lark_blacklist_amazon_cat` / `phase0_forbidden_category` | 黑名单行的 `walmart_policy` 能 `policy_names.resolve` 到政策表 → 该政策;否则 `内部黑名单` |
| `phase0_brand_blacklist` / `phase0_trademark_symbol` / `phase0_patent_claim` | `Intellectual Property` |
| `publication_pt_forbidden`(L1) | `类目准入` |
| `cat_access_blocked` / `cat_zh_blocked`(L2 R1) | `类目准入` |
| L3 reject | LLM 输出的 `policy`(解析层已对枚举校验) |

规则代码里写死的政策名只有一个(`registry.resources.AUDIT_IP_POLICY`),
`audit_rules.load_context` 装配时对表解析一次:解析不到、或与表内拼写不同,
**启动即 RuntimeError**(表改名而代码没跟上时,静默的后果是那几条硬拒一直往
库里写一个表里不存在的类别名,而三处口径对不上都不会红)。

### 7.3 `compute_final_reason` = 查表,四步

| 步 | 判据 | 结果 |
|---|---|---|
| 0 | 不是 reject | None |
| 1 | all_hits(phase0 → l1 → l2 → l3 序)第一条 detail 带 `category` 的 | 该值 |
| 2 | l3 判 reject | `l3.policy` |
| 3 | 都没有 | **None** + `STATS["reason_missing"]` + warning |

**没有兜底**。判拒而没有类别只可能是代码 bug(某条硬拒规则忘了自报),
落 NULL + 计数 + warning,让它自己现形 —— 而不是编一个
`General-Use Products` 出来(所有者 2026-08-16:「理由是 General-Use
Products,这是什么意思」)。

> ⚠ **已知缺口:三条硬拒规则还没自报类别**
>
> | 规则 | 现状 | 去向 |
> |---|---|---|
> | `cat_requires_cert_hard`(L2 R3 硬拒) | 拒了但没类别 | C 批降为证据(本 PT requirements 进 L3) |
> | `made_in_usa_claim`(L2 R10) | 同上 | C 批迁进 L0,带 `Product claims` |
> | `l4_vision_violation`(L4 视觉,-100) | 同上 | **§二 的类别表没有它**:"图上有什么"映到哪条政策要所有者裁决,**不许替它编一个**;L4 默认关(`-p l4=on` 才跑),面很小 |
>
> 在这三条被处理之前,它们拒掉的产品走第 3 步:类别 NULL + `reason_missing`
> 计数。**这是有意的**(§10:B、C 只切换一次,生产机等 C 合并后再 pull),
> 验收信号 = **C 批合并后、L4 关闭时该计数应回到 0**。

2026-09-02 B1 **删掉**的九步推断(别照着旧文档写回来):步 1/1.5 读
`walmart_policy`、步 1.2 内部黑名单特判、步 2 的 `_normalize_l3_cat` 归一化
(L3 输出在解析层就对表了)、步 3 的 L4 issue 关键词猜测、步 4a–4g
(`_pt_to_policy` 十组裸子串 / cert 分桶 / `General-Use Products` 兜底)、
`known_policies_check`。政策名归一化从此只有 `services/policy_names` 一处实现。

### 7.4 具体内容怎么来(`audit_store.conclusion_detail`)

- reject + L3 判的 → `l3.detail`(LLM 给的中文一句:原文片段 + 条款要点);
  **LLM 没给(或只回了空白)时** → `违反「<类别>」(LLM 未引用原文片段)`;
- reject + 规则判的 → **判死那条 hit** 的 `explain_hit`(它本来就是"具体内容"
  形态,如 `商标符号(命中:XYZ®)`);
- ⚠ **判拒这一格永远不留空**:留空的后果不是"少一格" —— 飞书 H 列会走老行
  兜底渲染,把 `llm_alcohol` 这种规则码原样打给运营看(`_RULE_CN` 里没有
  `llm_*` 条目,也不该有:它是随政策名生成的);
- pending → 三句固定句之一(按停在哪一层分,重试口径不同:L1 类目解不出
  隔天重试有意义 / L2 要等 `walmart_pt_meta` 补行,重刷无用 / L3 LLM 故障
  重试有意义);
- pass → NULL。

`explain_hit`(一条 hit)与 `explain_hits`(最多三条,取代退役的
`human_reason`)保留:飞书上架表 H 列渲染**存量老行**(还没有 `audit_detail`
的那些)靠它 —— 不兜底的话,几十万行会在表上一夜变成空白,看起来像"审核把
理由弄丢了"。

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

### 8.5 关于那 44 篇沃尔玛官方政策

**不接进类目判定。** 政策是**按产品**写的,不是按类目写的:同一个 PT 底下
既有能卖的也有不能卖的(`Garden & Patio` 下既有园艺耙也有禁售的活体种苗),
PT 级套政策必然误杀整类。

政策的落点在**产品级**:2026-09-02 第三步 B1 起,**44 篇官方英文全文整段进
L3 的 system prompt**(§5.1),由 LLM 拿产品正文与原文条款逐条核对 ——
不再由 L2 的 `_infer_walmart_policy` 用类目关键词推一个政策标签当上下文
(那张表 C 批随 R3/R7/R8 一起删)。

---

## 9. 排查一条结论:`audit_why`

```bash
python cli.py audit_why -p asins=B0XXXXXXXX
```

只读,输出这个 ASIN 停在哪一层、命中了哪些 rule_code、每条的判据来自
**哪张表哪一列**。看到不认识的 rule_code 先查 `workflows/audit_why.py`
里的判据出处表。

结论那几行按**三段**打印(2026-09-02 B1):`结论 <判定> 类别 <类别>` +
`具体内容 <那一句>`。老行(B1 之前审的)`audit_detail` 是 NULL,打出来就是
`None` —— 那是"这一行还没被新链重审过"的样子,不是查询坏了。

---

## 10. 2026-09-02 所有者定稿:审核链瘦身(随第三步 L3 批实施)

背景:L3 换喂官方英文政策全文(`refdata/policy_pages/en`,44 篇)之后,L2 里
"硬代码代 LLM 判语义"的规则失去存在理由;黑名单能力只许有一处实现。

**进度(2026-09-02)**:A 批(内容族两页转录)、**B1 批**(见下表「已落地」列与
§5 / §7)与 **B2 批**(回放工作流 `audit_replay`、`mode=stale` 的 `active_days`、
首条串行预热)**已落地**;C 批(L0 双输出 / L2 只剩 R1 / 删 R3 硬拒、R4、R5、
R7、R8、R10 与 `POLICY_LEGACY_NAMES` 一族)未做。
⚠ **B2 不提版**:它一个字都没动判定(回放只读、`active_days` 只筛候选、预热只改
发请求的次序),提版会让全库 approved 白重审一轮。
⚠ **B 与 C 只切换一次**:两批各自递增 `AUDIT_RULES_VERSION`,生产机等 C 合并
后再 `git pull` —— pull 的时机就是切换的时机(`audit_sheet` 18:10 当晚就会用
新链重审上架表里的品)。定稿:

| 规则 | 处置 | 说明 |
|---|---|---|
| R1 类目准入 | **保留,成为 L2 的全部** | 拿 L1 的 PT 查白名单 `access_state` + `zh_can_do`;PT 查不到 → pending(判不了 ≠ 判过了) |
| R3 类目需证书 | 硬拒分支**降为证据**(C 批);**替身已在 B1 落地** | 该 PT 的 `requirements` 一行随产品送 L3(§5.2),由 LLM 判"这个具体产品要不要这张证"(2026-08-21 咖啡桌教训的彻底版);硬闸只留白名单 `zh_can_do`。⚠ B1 → C 之间 R3 硬拒仍在,而它**没有自报类别** ⇒ 那批 reject 的类别落 NULL + `reason_missing` 计数(§7.3 已知缺口) |
| R4 品牌黑名单扫文案 | **移入 L0,双输出** | 黑名单能力只在 L0 一处实现、一份数据:`brand` 字段精确等值 → 硬拒终止(现规则 4);标题/五点/描述扫到黑名单词 → **0 分证据不终止**,送 L3 由 LLM 判是品牌还是"兼容/提及"(R6 误伤 90% 的教训:提到 ≠ 卖的就是)。词边界 / 中文紧邻即边界 / 自品牌精确豁免 / 同品牌只报一次的逻辑随迁 |
| R5 USPTO 在效商标 | **删除** | 默认关、覆盖率 2.6 万/1400 万,死重;将来需要按新流程重建 |
| R7 促销宣称 | **退役,前置条件先满足** | 其判据(#1 / best seller / premium quality / FDA approved)属沃尔玛 **Content Standards**,不在 42 条禁售政策内 —— 先用 policy-refresh 流程把 Content Standards 页转录进 L3 前缀,再删 R7(先补后删,无真空期) |
| R8 敏感/严格合规词表 | **删除** | Offensive Content 政策全文(第 25 节,8 个子域)+ 武器族已进表,LLM 读原文判 |
| R10 Made in USA 声明 | **移入 L0** | 确定性正则(含 `not made in` 否定式排除),与"专利自述"同类,命中即硬拒 |

瘦身后的链路:

```
L0  库黑名单精确拦截(卖家/ASIN/亚马逊类目/品牌)+ 商标符号 + 专利自述 + Made in USA → 硬拒即终止
    + 品牌黑名单文案扫描 → 0 分证据,不终止
L1  定 PT(是什么;LLM 只在候选 rerank 出场)
L2  = R1 白名单准入(能不能;代码查表,确定性)
L3  产品全文 + 44 篇政策英文全文(含内容族两页)+ 本 PT 的 requirements 行 + L0 品牌证据
    → 判定结果 / 类别(官方类别名 + 内部黑名单 / 类目准入)/ 具体内容
```

L3 那一行 **B1 已落地**(§5);L0 的双输出与 L2 的瘦身留给 C 批。

工程要点(执行规格:`docs/audit_step3_spec.md` §三,所有者八项定稿 §六):

- ✅ **B1 已落地** —— S4 换官方英文全文、S2 枚举补两条非政策类别、S1 重写、
  user 段扩容(描述 3000 / 五点全给 / 本 PT 准入要求 / 删原产国与路由提示)、
  输出三段化与解析零猜测、类别由规则自报、`compute_final_reason` 收敛为查表、
  `catalog.products.audit_detail` 新列与飞书 G/H 分列、政策路由整体删除;
- ✅ **B1 已落地** —— 证据进 L3 的通道泛化为「读取上游**所有阶段**的软 hit」
  (`summarize_evidence(phase0, l1, l2)`,按 rule_code 查渲染表,未登记的也不丢);
- ✅ **B2 已落地** —— `audit_replay` 回放工作流(拿沃尔玛裁决考这条链:反例召回 /
  类别准确率 + 混淆表 / **正例误伤新旧并排**(所有者底线:新链不高于旧链)/ 新旧
  一致率 / 按置信分层错误率 / pending 分层 / 成本与耗时;只写 `audit.replay_results`
  与 `<DATA_ROOT>/reports/audit_replay.txt`;样本身份 = `run_tag`,同 tag 重放
  同一批;旧链基线按新列 `audit_runs.audit_version` 排掉新链自己写的行)、
  `mode=stale` 的 `active_days=90`(§1)、首条串行预热(`services/audit_pool`,
  省一批前缀缓存 miss);
- ⬜ C 批 —— L0 契约从「命中即终止」改为「**硬命中终止、软命中带证据前行**」
  (`audit_hits` 可落多行;`stage_stopped_at` 语义不变,只有硬拒才停);
  R4/R10 迁入 L0、L2 只剩 R1、删 R3 硬拒 / R5 / R7 / R8 与
  `POLICY_LEGACY_NAMES` / `to_official` / `POLICY_ALIASES`;
- ⬜ **类别缺口三条**(§7.3):`cat_requires_cert_hard` / `made_in_usa_claim`
  随 C 批消化;`l4_vision_violation` 的类别**等所有者裁决**("图上有什么"
  映到哪条政策),在那之前 L4 判拒一律类别 NULL + 计数。验收信号:
  **C 批合并后、L4 关闭时 `reason_missing` 应回到 0**;
- 存量 `audit_hits` 的 rule_code(`title_desc_blacklist` / `cat_requires_cert_*` / `content_promotional`
  / `walmart_strict_sensitive` …)保留兼容渲染,新链不再产生;
- **顺序:先换喂(L3 读英文全文 + Content Standards),后删 R7/R8**;`AUDIT_RULES_VERSION` 同批递增;
- 落地后用报错回放评估集(已知沃尔玛裁决的产品 + 通过样本)比对瘦身前后的一致率;
- **不做**:把整张 PT 表放进 LLM 知识库让它"一次判类目 + 能不能做" —— 查白名单是对
  LLM 刚选出的 PT 做确定性查表,代码做;"是什么"与"能不能"合判会污染分类
  (2026-08-20 删 L1 禁售清单的同一理由);知识库是检索不是查表,捞不到会猜,
  「PT 不在表 → pending」的安全默认值也会丢。给 LLM 的是本 PT 的那一行,不是整张表。
