# 类目映射维护(ID 锚定)

> 亚马逊类目 → 沃尔玛 PT 的映射表怎么建、怎么补、谁来核。所有者定稿
> 2026-08-14。审核链 L1 的 ②a/②b 级直接吃这张表,映射错=整类目产品判错。

## 0. 为什么以 browse_node_id 为键

Amazon 的 URL slug、商品面包屑、Best Sellers 导航是**三套不完全一致的名称**,
同一个类目在三处能写成三个样子(实测:`Home Décor` vs `Home Décor Products`、
`Outdoor Power Tools` vs `Mowers & Outdoor Power Tools`)。**名字会漂,ID 不会。**
所以映射表、建议表、产品主档一律以 `browse_node_id` 为锚:

- 采集侧零改动——ID 一直在抓也在传,只是没进 `slow` 段,在 `raw.category_ids`
  里幸存(逗号链,根→叶,最后一段即最细类目);详见 `docs/scraper_migration_brief.md`。
- `node_backfill` 从已存快照就地回填,生产已落 **111.3 万 ASIN,零重采**。
- 路径对齐(`catmap_align`)降级为**无 ID 老行的兜底**,不再是主路径。

## 1. 三张表(全部以 node_id 连接)

| 表 | 是什么 | 现量 |
|---|---|---|
| `audit.amazon_taxonomy` | 亚马逊类目节点主档(规范名/是否叶子) | 28,495 node(重导后应达 32,147) |
| `audit.amazon_node_paths` | 父子路径关系(DAG,多路径各一行) | 待重导 |
| `catalog.products` | 我们实际有货的类目(`browse_node_id` + 产品数) | 15,538 node |
| `audit.walmart_category_map` | 已有映射(高置信行) | 13,349 node |

三方 JOIN 命中率实测(`taxonomy_import` 预览**强制**先给这个数,不看命中率
就导入=重蹈 Best Sellers 那次覆辙):产品侧 82.2%,映射表侧 99.9%。

### 中间层节点:文件本来就有,是解析器漏读(2026-08-14 更正)

对账版 JSON 里 `leaves` 26,956 行、`nodes` **32,147 行(全量树)**。首版导入器
写死了三个段名(`leaves` / `verified_added_paths` / `unverified_new_nodes`),
`nodes` 段**被静默丢弃**,入库只剩 28,495——当时误判成"文件只发了叶子"。

两处改:

1. **按内容认段,不按段名**:顶层任何 list、行里带 `browse_node_id` 就解析,
   段名陌生也收,并在预览的「文件构造体检」里标出来(每段行数 + 是否解析 +
   `meta` 自报数与实际可解析数的差)。写死段名 = 上游多给的东西静默丢掉。
2. 重跑一次导入即可把中间层收进来:
   ```
   python cli.py taxonomy_import -p file=<对账版 JSON>          # 先看体检
   python cli.py taxonomy_import -p file=<对账版 JSON> -p apply=1
   ```

### browse tree 是 DAG,不是树

同一个 node 可以挂在**多个父**下、有**多条完整路径**(`Belts` 同时在男装配件 /
汽车皮带 / 工业传动下)。按 `browse_node_id` 简单去重会静默丢掉多路径关系,
父链回退就会退到错误的祖先。所以落**两张表**:

| 表 | 键 | 存什么 |
|---|---|---|
| `audit.amazon_taxonomy` | `node_id` | **节点级**属性:名称、是否叶子。路径/父/深度列是**代表路径**的取值,给展示和 1:1 JOIN 用,**不是唯一真相** |
| `audit.amazon_node_paths` | `(node_id, parent_node_id, full_path)` | **路径级**关系:每个挂载点一行,一条都不合并。`parent_node_id=''` 表示根级(PK 不收 NULL) |

要走父链一律查 `amazon_node_paths`。导入与反推两侧都按这个口径写:
`taxonomy_import` 的路径行不去重,`taxonomy_derive` 的 (父, 完整路径) **成对**
计票(拆成两个 Counter 各取多数票会拼出一条从没出现过的父路径组合)。

### 树外 node 的补法(`taxonomy_derive`,零采集)

中间层归文件管之后,反推只剩一个职责:补**任何一版类目树里都没有的 node**
(C 桶,产品带着这个 ID)。数据我们手里已经有,只是分在两列——
`products.browse_node_chain`(根→叶 ID 链)与 `products.amazon_category`
(同一路径的名称串),按叶子右对齐 zip 即可还原 node/名/父/路径:

```
python cli.py taxonomy_derive            # 预览:长度差分布 + 与已知树对拍
python cli.py taxonomy_derive -p apply=1
```

判据是**与已知树对拍的名称一致率**(叶位/中间位分开报);中间位低于
`min_agree`(默认 0.9)拒绝写入——对不齐就是串位。补入行标
`source='derived_products'`;`taxonomy_import` 重灌**只删文件来源的行**,
反推层留着(同 node 由文件行覆盖)。

### 正式下发数据规格(所有者定稿 2026-08-14)

| 数据集 | 键 | 用途 |
|---|---|---|
| `amazon_all_nodes` | `browse_node_id` | 节点主档:名称、是否叶子。**只有这里按 ID 唯一** |
| `amazon_node_paths` | `browse_node_id + parent_node_id + full_path` | 父子路径关系,多路径各一行 |
| `amazon_leaf_nodes` | `browse_node_id` | 叶子集。逻辑上可由 `all_nodes.is_leaf` 推出,单独下发当**对账校验位**用 |
| `amazon_to_walmart_mapping` | — | 映射快照 |

⚠ **L1 根类目必须给真实 `browse_node_id`**。现在对账版里 39 个根类目行既无
`browse_node_id` 也无 `父节点ID`,URL 是 Best Sellers 的 slug 链接(不带
`node=`),文件用 `L1_<slug>` 当根标识——**根的数字 ID 在文件里任何地方都取不到**,
父链走到倒数第二层就断。产品侧的 `browse_node_chain` 里有真实根 ID
(Home & Kitchen=1055398、Tools & Home Improvement=228013…),现由
`taxonomy_derive` 反推补入;正式下发应由文件直接给。

⚠ 映射这一项要先定**方向**再下发:现在 `catmap_mine -p promote=1` 直接写 PG,
PG 是事实上的权威;若映射随类目树一起下发并重灌,挖出来的行会被文件冲掉。
这就是「映射编辑权威(飞书 vs PG)」那条未决——下发前必须先定。

## 2. 三段式维护顺序(所有者规划)

### ① 挖已有实证(零 LLM,`catmap_mine`)

同一 node 下多个产品的实证 PT 压倒性指向同一个 → 可信映射。
判据是**优势度不是全票**(首跑教训:要求 100% 一致会把 1,321 个类目误判成分流;
回填 PT 来自删除历史+报错日报,同一类目历史上被挂过几个 PT 很正常)。

已跑:

| 档位 | 可信(已升级) | 待核 | 分流 | 与旧映射冲突 |
|---|---|---|---|---|
| 票≥5 优势≥80% | 224 ✅ | 190 | 1,131 | 70(已全修) |
| 票≥5 优势≥70% | 141 ✅ | 224 | 969 | 38(已全修) |
| 票≥3 优势≥70% | +116 ✅ | — | — | — |

映射覆盖率 61.5% → 62.9% → 63.7% → **64.4%**(产品侧 15,538 node 已映射 10,011)。
(末行 +116 是按映射表 node 数 13,233→13,349 反推的净增,该轮输出未留档。)
冲突行由 `catmap_fix` 定点修正:旧行降级为'低'并留痕(**不删**,旧映射是历史证据),
新行带 node_id 以 `mined_conflict_fix` 插入。

**这一段是常态化重跑,不是一次性**:A 桶还剩 1,740 node / 16.6 万件"有实证 PT
却没映射",瓶颈是实证太稀(Golf Cart Accessories 4,925 件产品只有 128 件有 PT)。
每跑一轮 `product_audit`,`products.walmart_pt` 就多一批,下一轮 `catmap_mine`
就能多挖出一批——自增强回路。

### ② 数据少 / 有分流 → 人工核对

- **待核** 224 node:支持 2~4 票、优势达标(所有者原话"只有两三个产品映射了,需要核对");
- **分流** 969 node:优势度真 <70%,多 PT 混杂;
- 都落在 `audit.category_map_suggestions`(带 `browse_node_id` / 代表路径 /
  支持数 / `pt_distribution` 全分布)。

### ③ 从无到有(零实证)

- **B 桶** 3,787 node / 44,781 件:有产品、无映射、无任何实证 PT → LLM
  (`catmap_suggest`,已实现但**仍按路径为键**,尚未跟着锚线改成 node 键;
  产出是建议表里的死数据,升级通道等编辑权威定稿)或人工;
- **C 桶** 2,759 node:产品带着这个 node,但类目树里根本没有 → 采集侧补抓
  (`python cli.py catmap_gap -p only=not_in_tree`);
- **D 桶** 12,386 node:亚马逊有、我们没货 → **不处理**(别浪费 LLM)。

### ⛔ B 桶不排期(所有者 2026-08-17 裁决)

`catmap_gap` 现在会把缺口里的产品按 `stage_stopped_at` 摊开。生产实测:

| | 件数 | 停在 L0 | 结论 |
|---|---|---|---|
| ① 产品**自己**有实证 PT | 113,496 | 6,231(5%) | **不靠映射表**:`resolve_pt` 的实证级(walmart_items / 产品主档)直接直出,映射表是第 ② 级轮不到 —— "停在 L1 只有 1 件"就是铁证。这批数字**不是补映射的收益** |
| ② 产品**自己**无实证 PT | 84,414 | **70,714(84%)** | 这批才真靠映射表;但 84% 停在 L0(黑名单/禁售大类/®™),审核第一层就短路,压根不查类目 —— 补映射白补 |

⇒ 补 B 桶的真实收益 = ②里非 L0 的 **约 1.37 万件** + 未来新品,不是"十几万件"。

所有者裁决原话:**"所以不用补。以后真用上了自然会审核。"** 依据是审核链本身
不缺兜底 —— 映射解不出会进 L1 的 LLM 候选路径,只是慢些贵些,不会判不了。
因此 **`catmap_suggest` 批量补映射不排期**;`catmap_mine`(零 LLM、挖实证)
不受此裁决影响,照常跑。

⚠ 注意这两处的 A/B 与 ①/② **不是同一根轴**:上面四桶按 node 分(该 node 下
有没有**任何一个**产品带实证 PT),这张表按**产品自己**分,所以 180,166 与
113,496 对不上是正常的,不是 bug。

## 3. 重导类目树的执行顺序

```bash
git pull
python cli.py db_init                        # 建 amazon_node_paths(幂等)
python cli.py taxonomy_import -p file=<对账版 JSON>            # 先看体检
python cli.py taxonomy_import -p file=<对账版 JSON> -p apply=1
python cli.py taxonomy_derive                # 预览:只补树外 node
python cli.py taxonomy_derive -p apply=1
python cli.py catmap_gap                     # 验收:树 node 数 / C 桶是否缩小
```

预览要盯三处:① `nodes` 段有没有被解析(体检行里应出现,行数 32,147);
② 路径关系条数与其中的多父挂载数;③ 产品侧/映射表侧 JOIN 命中率不低于上次
(82.2% / 99.9%)。`taxonomy_derive` 要盯**中间位对拍一致率**,低于 90% 会拒绝写入。

## 4. 常用命令

```bash
python cli.py catalog_health                      # 体检:类目/ID/PT 覆盖面
python cli.py catmap_gap                          # 四桶缺口(按产品数降序)
python cli.py catmap_gap -p only=not_in_tree      # C 桶:给采集侧的补抓清单
python cli.py catmap_mine                         # 挖(票≥5 优势≥80%,只报不改)
python cli.py catmap_mine -p min_support=3 -p min_dominance=0.7
python cli.py catmap_mine -p promote=1            # 把 mined_trusted 写进映射表
python cli.py catmap_fix -p nodes=all_conflicts   # 冲突定点修正(危险,需 --execute)
python cli.py taxonomy_import -p file=<路径>      # 导类目树(预览强制先验 JOIN)
python cli.py taxonomy_derive                     # 反推树外 node(零采集)
python cli.py node_backfill                       # 从存量快照回填 browse_node_id
python cli.py pt_backfill                         # 历史实证 PT 回填产品主档
```

## 5. 未决

- **映射编辑权威(飞书 vs PG)**:`catmap_mine -p promote=1` 直接写 PG;若最终
  定飞书为权威,已升级行需回补进「映射明细」表。所有者待类目树补抓后再定。
- **②段人工核对的载体**:目前只能 psql 看建议表,没有导出/回收工作流。
- **父链兜底本身还没接进审核链**:中间层补齐(`taxonomy_derive`)只是把
  数据备好,L1 里"叶子无映射 → 沿父链找已映射祖先"这一级还没加——
  加之前要先想清楚**退到第几级为止**(退到 L1 根类目等于瞎判)。
- **8 个 `map_ambiguous` node 等人工裁剪**:映射表自己挂着多条高置信且 PT
  不同的行,这 8 个 node 的 ②级直出**已经失明**(直出闸要求恰好一个高置信
  PT),且全程无人报错。`catmap_mine` 只报不写,裁剪走 `catmap_fix`。
  已向所有者提议做个"两边摊开供选"的小工具,**未获批,不要擅自动手**。
- **9 个准入漏了的汽配 PT 待所有者填飞书**:`pt_census` 已把清单落到
  `pt_待补准入明细.csv`(Category/PTG 按同路径兄弟或名字最近邻给了建议,
  弱依据的已标注)。所有者在飞书「沃尔玛类目表」补齐**准入状态 / 中国卖家
  可做**两列后跑 `python cli.py risk_sync` 生效。当前 `pt_census` 已与官方
  MP_ITEM spec 对齐到 6,951,只剩这 9 条。

## 置信度生命周期:机器自动降档 + LLM 兜底 + 自动升档(2026-08-14 定稿)

所有者裁决:"有争议的可以算作中等(有真实 ASIN 的类目映射,但数量不多)或者
低等可信度(凭空推测),然后对于这种中等低等可信度的,把候选交给 LLM 去判就
可以了" + "如果满足条件,跑一轮数据库也可以升档"。

这把类目映射的长期维护从**人工逐条看冲突**,换成**证据驱动的档位自动流转**。

### 两半机制

**消费端(早就在跑,零改动)**:
  · 审核链两个 ②级直出闸硬筛 `confidence='高'`(audit_rules.py:162/175);
  · 候选召回几路**不筛档位**,只拿它排序(audit_l1_llm._CONF_ORDER 高→中→低)。
⇒ 高 = 直出不经 LLM;中/低 = 只进候选,LLM 拿它当参考自己判。
**弱证据永远绕不过 LLM**——这是这套设计成立的前提。

**供给端(2026-08-14 补上)**:`catmap_mine` 原来只把 `mined_trusted` 以 '高'
写进映射表,另外三个桶**只报不写**,等于机制有消费端没有供给端。现在:

| 桶 | 判据 | 档位 |
|---|---|---|
| `mined_trusted` | ≥min_support 票且优势度达标 | **高** |
| `mined_review` | 2~4 票、优势度达标(有真实 ASIN,数量不多) | **中** |
| `map_conflict` | 实证与旧映射相左(旧行不动,实证 PT 另插一条) | **中** |
| `mined_mixed` | 票分流,首选 PT 是多数派而非共识 | **低** |
| `map_ambiguous` | 映射表**自己**挂着多条 PT 不同的高置信行 | 只报不写 |

### 升档自动,降档不自动

重跑 `catmap_mine -p promote=1`,数据攒够了就 低→中→高 自动往上走
(`plan_promotions` 纯函数算新增/升档/跳过,SQL 的 `WHERE 新档位 > 旧档位`
是并发下的第二道闸)。目标档位不高于现档位的一律跳过,**重跑幂等**。

**为什么不自动降档**:
  · 证据可能只是**暂时**变薄(pt_source 回填没跑完、本轮只挖了子集),
    据此把高置信行降成中,②级直出立刻对整个类目失效,而没人会发现;
  · 高置信行可能是**人工**定的(catmap_fix),机器不该拿一轮统计推翻人的判断。
降档走 `catmap_fix`(人工圈定 node,旧行降 '低' 并留 notes,新行插 '高')。

### ⚠ 顺带修掉的一处潜伏 bug

`_IN_MAP_NODE_SQL` 原写法 `CASE WHEN count(DISTINCT pt)=1 THEN min(pt) END`,
让"已有两条互相矛盾的高置信行"的 key 返回 NULL;调用方 `if k` 只过滤键、
值为 None 就当成**没映射过** ⇒ 该 key 会被当新映射挖出来,promote 时再插
第三条高置信行。它同时让 ②级直出对这个 key 永久失明(直出闸要求恰好一个
高置信 PT),而全程无人报错。**加上自动升档后这个 bug 会主动造成伤害**,
所以现在把"没映射"与"映射了但自相矛盾"分开,后者单列 `map_ambiguous` 桶,
只报不写,并在摘要里给出人工裁剪的 SQL。
