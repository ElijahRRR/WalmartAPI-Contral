# 批次 C OPEN 条目裁决(2026-08-13,移植合同)

裁决原则(优先级从高到低):①所有者批复/计划文档已定的照办;②保真优先——
已知缺陷照迁+测试钉住,双跑出数据后再议修复;③"纯历史重复只留一种,零兜底"
(CLAUDE.md);④拿不准且影响业务语义的,按推荐值实现+报告亮出待所有者追认。

## 模块组织(与计划 C3 单文件的偏差,声明在案)

- `api/llm_vision.py`(C2,豆包 ARK 客户端,纯接口适配)
- `services/audit_l1_llm.py`(L1 第三级:候选 SQL + rerank)
- `services/audit_l3.py`(L3 语义 + 政策路由内存表)
- `services/audit_l4.py`(L4 视觉编排:选图/下载/prompt/verdict 重算)
- 接线(C4)改 `services/audit_rules.py`(audit_one 扩 L3/L4)与
  `workflows/product_audit.py`(参数/摘要)——批次 B 文件,由主线自己做,不给移植代理
理由:三层并行移植零冲突;services 积木自由组织不违规范。

## L1(spec_c_l1_rerank.md)

| OPEN | 裁决 | 依据 |
|---|---|---|
| 1 seed 补跑 | **不补**,照迁(LLM 后不复查 seed) | 保真;双跑后再议 |
| 2 amazon_leaf 列 vs raw | **照抄 `raw->>'amazon_leaf'`** | 零召回风险;列非空率未实测 |
| 3 提示词 30/top15 与 [:20] 不一致 | **不改,照抄** | 提示词=跑了数月的生产版 |
| 4 置信度阈值 | **零阈值照迁** | 新阈值=新业务判断;报告亮出可调 |
| 5 无候选是否调 LLM | **不调,直接 pending** | 旧行为(自由判→unknown→事故 fallback)在新修正下终点同为 pending,省成本;报告亮出 |
| 6 出版物硬禁盖①② | **补上,全三级生效** | 这是批次 B 漏迁(旧系统全三级生效),修=对齐旧行为 |
| 7 L1 缓存 | **不开**(旧 L1 不开) | 保真;rerank 需随映射表更新 |
| 8 error_records 入①级 | **做**:`DISTINCT ON (asin) walmart_pt != 'default' ORDER BY recorded_at DESC` + pt_meta 闸;与 walmart_items 冲突时在架优先;`pt_source='walmart_error_confirmed'` | 批复 #10 原文点名历史报错数据 |
| 9 沉淀新字典表 | **本批不建**;audit_runs 已留痕 | 反哺=直接用实证源;新表=新设计,到批次 D 再议 |
| 10 哨兵级位置 | **实证①②优先,哨兵只在映射级生效**(与旧"哨兵最前"不同) | 批复 #10 实证最优先,所有者已定;差异会进校准报告 |
| 11 候选 SQL 批量化 | **本批不做**,照抄 per-ASIN | 只有到达第三级的产品才查;实测慢再议 |

## L3(spec_c_l3.md)

| OPEN | 裁决 | 依据 |
|---|---|---|
| 1 R7/R8 喂进 prompt | **不补,逐字迁**(R7/R8 证据不进 L3) | 保真;已知缺陷注明+钉测试 |
| 2 cert requirements 键名 bug | **照迁**(固定套话) | 同上 |
| 3 strong 升级链 | **不迁**(连同 should_use_strong_l3_model 的 L3 用途) | 生产 yaml 两链主节点同为 claude-sonnet-5,能力事实已死=纯历史重复;报告亮出待追认 |
| 4 政策路由 | **保留内存版两张表**,hint_line 照产 | 提示词逐字稳定>省 token;45 行常量 |
| 5 F1 rule_code | **沿用 `llm_chain_exhausted`** | 新旧 audit_hits 对账 |
| 6 非法 verdict | **→pending** | 10.2 绝不默认放行 |
| 7 is_real_brand 强制翻拒 | **照迁** | 旧生产行为;去掉=降拦截;钉测试 |
| 8 L3 pending 的 score_final | **保留 L2 分数**(PT-pending 仍 None) | 分数=实际算到哪层;两种 pending 语义不同 |
| 9 原产国行 | **行保留,值恒 `(空)`**(ProductInfo 不加字段) | 提示词字节稳定;采集契约无此值 |

## L4(spec_c_l4_vision.md)

| OPEN | 裁决 | 依据 |
|---|---|---|
| 1 ARK env 名 | `ARK_API_KEY` / `ARK_BASE_URL`(默认旧值 ark.cn-beijing…/api/v3)/ `ARK_VISION_MODEL`(默认 doubao-seed-1-6-flash-250615);api/llm_vision.py 直读 env(api/llm.py DEEPSEEK_API_KEY 同款先例) | 计划 A4:视觉不进 LLM_PURPOSE_ENV |
| 2 图片源 | ~~snapshots 最新 slow.images~~ **评审 P0 修正:主源 = catalog.products.slow->'images'**(采集契约 v1 slow 是顶层必填段,product_ingest 落进 products.slow;snapshots.raw 是裁剪载荷不保证内嵌 slow,按它取 = L4 恒无图整层空转)。快照 raw.slow.images 降为计数兜底。不套新鲜度门槛不变 | 契约 scraper_migration_brief.md:100/105;amz_source 同源先例 |
| 3 索引口径 | l4_issues 每条 issue **补写 `image_url`,保留 `image_index`(原序口径)** | 新增键不删旧键,消费方兼容 |
| 4 L4 缓存 | **不开**(旧不开;base64 键不可行) | 保真 |
| 5 trust_env | **用 httpx 默认**(生产 Mac 无代理,行为等同) | 简化 |
| 6 图片体积上限 | **不加上限**,日志记总字节 | 保真;实测后再议 |
| 7 confidence 大小写 | **照迁严格比较**(known defect 钉测试) | 双跑口径 |
| 8 失败路径 | **全部 →pass 维持原结论 + hit/日志计数告警**(F1 无图同此) | L4=增量视觉拦截、默认关、仅 pass 产品;故障不该制造假阳(旧仓 :349 理由成立);10.2 修正条文点名的是 L3。**报告亮出待所有者追认** |
| 9 部分图失败 | **成功子集继续判**,detail 记 fetched/total;0 张成功走无图路径 | 保真+可见性 |
| 10 JSON 提取 | **复用 api/llm.py _extract_json**,抛错走 F 路径;三级解析不迁 | 等价实现已有 |

## 客户端(spec_c_llm_client.md)

| OPEN | 裁决 | 依据 |
|---|---|---|
| 1 thinking disable | **本批不动 chat_json 请求体**;问所有者生产 DEEPSEEK_MODEL 实际值,若 v4-flash 家族再加显式 disable(影响 listing 链,单独小提交) | 影响面超批次 C |
| 2 thinking 进缓存键 | 随 OPEN-1,引入时一并进键 | 尚无 thinking 参数 |
| 3 L4 失败语义 | 同 L4-OPEN-8:→pass+告警,待追认 | |
| 4 缓存清理器 | **本批不做**,验收时带 llm_cache 体量数据重议 | 计划原话 |
| 5 L1 缓存 | 不开(同 L1-7) | |
| 6 usage 表 | **C5 本批跳过**(计划标"可选,所有者到时定");验收报告给成本实测,所有者要记账再定表名 | |
| 7/8 strong 链与触发器 | strong 链不迁(同 L3-3);**aggressive_offensive 触发器只随 L4 迁**(addon 提示词段 + medium 放宽),L3 用途不迁 | L4 保真需要它 |
| 9 9.4% JSON 失败率常量 | 不沿用,C3 验收实测 | 网关时代数据 |

## 全局

- LLM 失败语义(10.2 定稿,L1/L3 统一):api/llm.py 重试尽抛异常 → 调用方
  产 pending(L1:pt 解不出口径;L3:verdict='pending' + llm_chain_exhausted hit,
  score 保留 L2 值);**坏 JSON/非法 verdict 同 pending。L4 除外(见 L4-8)。**
- L3 verdict='skip' 语义保留(audit_runs.l3_verdict:未到 L3 层时 'skip')。
- 进入 L3 条件照旧编排:L2 出 pass(score≥60)才进;L3 reject 即终;
  L4 仅 outcome.verdict=='pass' 且 -p l4=on。
- AUDIT_RULES_VERSION 本批 bump 至 c.*(接线完成时)。

## L1 候选面收口(2026-08-14 全库扫完后的实证追加)

**背景**:全库判完后 pending 存量 2,016 条,所有者裁决"真的都不合适,那也不行"
——unknown 不等于这产品没类目,得给二次机会。三轮修改后 2,016 → 1,061 → 383。

三条实证结论,写在这里是因为它们都不是从设计推出来的,是数据打出来的:

1. **`unknown` 与链路故障必须分开处置**。`rerank_ex` 返回原因,只有
   `unknown`(候选给了但 LLM 全否)才换候选面重判;`llm_failed`/`bad_json`
   是链路故障,换候选面治不了,重试只是白烧一次调用。救回率 40.6% → 51.1%。

2. **⚠ 候选列表的顺序是契约,不是实现细节**。`build_user_prompt` 只喂前
   `_PROMPT_CAND_CUT` 条,所以 `candidates()` 的"顺序即优先级"是全链前提。
   `open_candidates` 二阶段一度按 PT 名字母序交出整个大类的 PT(Home 891 个),
   截断后只有 A 开头的 20 个能进提示词,`Cookware Sets` 这种永远见不到 LLM
   ——**"LLM 说都不合适"其实是没给它看**。任何新增候选来源必须自己排好序,
   且取数条数与 `_PROMPT_CAND_CUT` 同源(取多了白查,取少了白白缩窄候选面)。

3. **类目路径不能只取叶子**。`... > Cookware > Pots` 的叶子是 `Pots`,而 PT
   字典里那条叫 `Cookware Sets`——最有用的词在倒数第二段。改成从叶子往回取
   (跳过 Amazon 一级大类,那层泛到能命中上百个 PT)后,第七路 pt_dict 的
   选中率 1.9% → 25.8%,是这三条里贡献最大的一条。

**收口口径(所有者可随时推翻)**:pending 383 条(全库 0.03%)转人工队列,
不再投工程。其中 257 条(67%)是 `amazon_category` 为空,LLM 只能凭标题裸判
——这是**采集侧字段契约问题**,判定链这边已经治不动;39 条七路+开放两阶段
全空,两轮纹丝不动,是真的一点可用信息都没有。
**代码不变时不要重跑 `mode=pending`**:输入相同、temperature 0.1,结果一样,
纯烧配额。要再往下压必须先改逻辑。
