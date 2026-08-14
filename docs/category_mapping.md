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
| `audit.amazon_taxonomy` | 亚马逊类目全集(规范名/父子/是否叶子) | 28,495 node |
| `catalog.products` | 我们实际有货的类目(`browse_node_id` + 产品数) | 15,538 node |
| `audit.walmart_category_map` | 已有映射(高置信行) | 13,349 node |

三方 JOIN 命中率实测(`taxonomy_import` 预览**强制**先给这个数,不看命中率
就导入=重蹈 Best Sellers 那次覆辙):产品侧 82.2%,映射表侧 99.9%。

### 中间层节点缺失与补法

对账版 JSON 的 `meta.reconciled_tree_rows` 是 32,147,实际入库 28,495
(leaves 26,956 + unverified_new_nodes 1,539,verified_added_paths 0 条)。
当前用法只按叶子 node 查,不影响 ②a 直查;但**父链兜底做不了**
("叶子无映射就退一级找父类目的映射"这种)。

**先分清是文件缺还是解析器没读**:导入器只认 `leaves` /
`verified_added_paths` / `unverified_new_nodes` 三个段名,别的段以前会静默丢弃。
现在 `taxonomy_import` 预览会打印**文件构造体检**——顶层每个段的行数,
未解析的段直接标 ⚠,并把 `meta` 里的自报行数与实际可解析数对差:

```
python cli.py taxonomy_import -p file=~/Downloads/amazon_taxonomy_reconciled_20260814.json
```

**补法(零采集,推荐)**:中间层的 ID 和名字我们手里已经有,只是分在两列——
`products.browse_node_chain`(根→叶的 ID 链)与 `products.amazon_category`
(同一路径的名称串)。按叶子右对齐 zip 即可还原每一层的 node/名/父/路径:

```
python cli.py taxonomy_derive            # 预览:长度差分布 + 与已知树对拍
python cli.py taxonomy_derive -p apply=1
```

判据是**与已知树对拍的一致率**(叶位/中间位分开报);中间位低于 `min_agree`
(默认 0.9)直接拒绝写入——对不齐就是串位,写进去会污染类目树。补入行
`source='derived_products'`,只补树里没有的 node;`taxonomy_import` 重灌
**只删文件段的行**,反推补层留着(同 node 文件行覆盖它)。

覆盖面 = 我们有货的类目——正好是唯一需要走父链的那部分。要全树中间层,
仍需你的抓取脚本把非叶行也一并导出(`父节点ID`/`深度`/`L1~L8` 说明它本来就有)。

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
  (`catmap_suggest`,**未实现**)或人工;
- **C 桶** 2,759 node:产品带着这个 node,但类目树里根本没有 → 采集侧补抓
  (`python cli.py catmap_gap -p only=not_in_tree`);
- **D 桶** 12,386 node:亚马逊有、我们没货 → **不处理**(别浪费 LLM)。

## 3. 常用命令

```bash
python cli.py catalog_health                      # 体检:类目/ID/PT 覆盖面
python cli.py catmap_gap                          # 四桶缺口(按产品数降序)
python cli.py catmap_gap -p only=not_in_tree      # C 桶:给采集侧的补抓清单
python cli.py catmap_mine                         # 挖(票≥5 优势≥80%,只报不改)
python cli.py catmap_mine -p min_support=3 -p min_dominance=0.7
python cli.py catmap_mine -p promote=1            # 把 mined_trusted 写进映射表
python cli.py catmap_fix -p nodes=all_conflicts   # 冲突定点修正(危险,需 --execute)
python cli.py taxonomy_import -p file=<路径>      # 导类目树(预览强制先验 JOIN)
python cli.py taxonomy_derive                     # 反推中间层节点(零采集)
python cli.py node_backfill                       # 从存量快照回填 browse_node_id
python cli.py pt_backfill                         # 历史实证 PT 回填产品主档
```

## 4. 未决

- **映射编辑权威(飞书 vs PG)**:`catmap_mine -p promote=1` 直接写 PG;若最终
  定飞书为权威,已升级行需回补进「映射明细」表。所有者待类目树补抓后再定。
- **②段人工核对的载体**:目前只能 psql 看建议表,没有导出/回收工作流。
- **父链兜底本身还没接进审核链**:中间层补齐(`taxonomy_derive`)只是把
  数据备好,L1 里"叶子无映射 → 沿父链找已映射祖先"这一级还没加——
  加之前要先想清楚**退到第几级为止**(退到 L1 根类目等于瞎判)。
