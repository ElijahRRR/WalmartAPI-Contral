# SKU 编码规则:货源隐匿 + 多源共存

> 状态:**计划待所有者批准,未动生产代码**(2026-09-01)。
> 所有者定稿:「沃尔玛侧通过 SKU 倒查产品来源,我不想让沃尔玛知道我的货源是
> 哪里来的」+ 多源共存(amz / 1688 / 自建)。SKU 里**不能看得出 ASIN**,
> 身份靠登记簿反查。
> 调研:仓库侧全量 grep(硬等号残留 / 模式提取调用点 / 登记时机),2026-09-01。

## 0. 一句话模型

**SKU = `<店铺代号>-<12 位随机不透明码>`,身份唯一出处 = `catalog.listing_sources`,
先登记再提交。** SKU 本身零信息:看不出 ASIN、看不出来源类型、看不出上架日期;
同一产品在两家店是两个毫无共性的码。所有来源(amz/跟卖/1688/自建)同一条规则。

## 1. 现状定稿(2026-09-01 摸底)

「sku ≠ asin」在**读方向**早已是既定事实(所有者 2026-08-11 定稿):
`services/sku_asin.extract_asin` 认三种形态(裸 ASIN / 旧三段式
`前缀-源头码-价格` / 纯数字 item id),`catalog.listing_sources` 登记簿也在
(「谁上架谁登记」,2026-08-07)。**写方向还是 sku = asin**:

| 层 | 现状 | 位置 |
|---|---|---|
| 生成 | `list_new` 四处直接拿 `r["asin"]` 当 SKU | list_new.py:252/257/599/1042 |
| 登记时机 | **提交成功之后**才 `listing_sources.register` | list_new.py:240-262 `_apply_submit_result` |
| 跟卖 | 自有规则 `PREFIX+YYYYMMDD+4 位序号`,B 列人工优先 | match_feed.make_sku / match_listing.py:170 |
| SQL 硬等号 | `p.asin = w.sku` 等 **5 处** | maintenance_intents.py:192/202/233/322;product_audit.py:407 |
| 模式提取 | `extract_asin(sku)` **7 个调用点**只看形态、不查登记簿 | order_lines/product_events/audit_rules/alloc_survey×2/blacklist×2 |
| 人看的表 | 上架表**没有 SKU 列**;在线产品总表有 sku 无来源码 | resources.LISTING_SHEET / ONLINE_ITEMS |

三条会静默出事的地方(规则一改立刻发作):
1. **登记在提交之后**:sku=asin 时映射可从 SKU 本身恢复,所以无害;换成不透明码
   后,提交成功与登记之间崩溃 = 沃尔玛上有一个谁也不知道是什么的 SKU,永远
   进不了任何自动链(catalog_sync 扫到它 → sources_backfill 按格式猜 → unknown)。
2. **5 处 SQL 硬等号**:新 SKU 的品在维护链/审核复审眼里**整体不存在**——不报错,
   只是永远不改价、不清零、不删。
3. **7 处模式提取**:订单行/事件/黑名单/分配的 asin 列对新 SKU 全部 NULL——
   消费方按 `asin IS NOT NULL` 过滤,这批货**退出销量维度**且看起来像"一单没卖"。

## 2. 约束与不做的事

- **目标只有一个:SKU 不透露来源。** 不含 ASIN、不含"像 ASIN 的 10 位码"、不含
  来源类型标记、不含 1688 offer id、不含日期(日期泄露上架节奏)。
- **不隐藏店铺代号**:那是我们内部的店名前缀,沃尔玛看不出含义;而且同一产品在
  两家店的码本来就是两次独立随机,不需要靠隐藏店铺段防关联。
- **不改存量 SKU**:沃尔玛 SKU 建后不可改。旧三段式(A109-B08QF9XLMH-02 这类)
  已经泄露了 ASIN,无法追回,不在本计划范围。存量与新规则**永久双轨并存**——
  所以读侧必须两种都认(登记簿优先,模式提取只给存量兜底)。
- **不做可逆码**(HMAC/加密):可逆意味着"拿到密钥就能批量倒推",而登记簿反查
  本来就要做,可逆性没有第二个消费者。随机码 + 登记簿是最简且最强的组合。
- 沃尔玛约束(**待所有者机器上按本地 MP_ITEM spec 核验**,§8):SKU 上限 50 字符、
  按 seller 唯一、不可变;字母数字与 `-` 安全。本规则最长 6+1+12 = 19。

## 3. 编码规则

```
<店铺代号>-<不透明码>
A085-K7QM2X9RT4WB
```

**店铺代号**:店铺凭证表新增一列「SKU前缀」(registry 登记字段常量),人填、
程序直读,`[A-Z0-9]{2,6}`、全店唯一。**不从店名推导**:谭总12 / 82杨乾良 这类
店名没有 ASCII 前缀可取,推导 = 猜。**fail-closed**:没填的店 `list_new`
整店跳过并在摘要点名(与「维护仓库」认不出整店跳过同一条口径)——
宁可这店今天不上,不能悄悄用一个临时前缀上一批然后改不了。

**不透明码**:12 位,字母表 `23456789ABCDEFGHJKMNPQRSTVWXYZ`(30 符号,剔除
0/O、1/I/L、U 防抄错),`secrets` 随机;空间 30^12 ≈ 5.3×10^17,撞库概率可忽略,
真撞上由登记簿主键拒绝后重抽。**为什么 12 位不是 10 位**:10 位字母数字正是
ASIN 的长度,任何"像不像 ASIN"的启发式(沃尔玛的、我们自己的 `_PLAIN` 正则)
都以此为特征;12 位与所有存量形态都对不上,`extract_asin` 对它必然返 None,
调用方于是走登记簿——形态本身就是分流器。

**同一条规则覆盖全部来源**:amz / 跟卖 / 1688 / 自建。跟卖保留「B 列人工优先」,
自动续号改走本规则(旧 `PREFIX+日期+序号` 停用——它泄露上架日期与批量节奏)。

**没有序号段**:同店同产品重上(SKU_LOCKED / 删后重上)= 再抽一个码。登记簿允许
同 (店铺, 来源类型, 来源码) 对应多个 SKU;"现役"那个 = 仍在 `walmart_items`
且未缺席的那个,不在编码里表达。

## 4. 身份:登记簿升级

- `catalog.listing_sources` 加索引 `(store, source_type, source_key)`(反查用)。
  表结构不变:它已经是「SKU → 来源」的正表,缺的只是反向索引。
- 新积木 `services/sku_codec.py`:
  - `mint(conn, store, prefix, source_type, source_key, workflow) -> sku`:
    抽码 → INSERT 登记簿 → 主键撞了重抽(最多 5 次,超过抛错——那说明字母表
    或随机源坏了,不是运气)。**抽码与登记是一个函数**,不存在"抽了没登记"。
  - `is_opaque(sku) -> bool`:形态判定(前缀-12 位码),给清洗/分类用。
- `services/sku_asin` 加 `resolve(conn, store, sku)` / `resolve_many(conn, pairs)`:
  **登记簿优先**(source_type=amz → source_key;其它来源 → None,它们本来就
  没有 ASIN),查不到再走 `extract_asin` 模式提取(**只为存量兜底**)。
  规则写死在 docstring:模式提取是存量的遗产,新码永远走登记簿。
- SQL 收口(5 处):`p.asin = w.sku` → `p.asin = ls.source_key`。`_SQL_AMZ_JOIN`
  已经 JOIN 了 `listing_sources ls … source_type='amz'`,只是比对时没用它——
  改的是一个等号右边。变体偏移删除、连续无货删除、product_audit mode=pass 同款。

## 5. 上架链:先登记再提交

`list_new` / `match_listing` 的顺序改成:**领号 → 抽码登记(mint)→ 组载荷 → 提交**。
CLAUDE.md 铁律「防重状态先落库再调接口」在这里的落地。

- 提交失败/被拒的行,登记簿里那个 SKU 留着(沃尔玛上从未存在,无害);下次
  重上再抽一个新码。不复用——复用要先判"上次到底成没成",那是又一套状态机。
- 载荷里 `sku` 字段、回执按 sku 找回行、UPC 池 `mark_used`、事件记录:全部
  从 `r["asin"]` 改成 `r["_sku"]`(预备期挂到行上)。**这些点一处漏了就是**
  「SKU 是新码、事件记的是 ASIN」这种对不上的账,所以要有测试钉住"载荷 sku
  ≠ asin 且 = 登记簿里那个"。
- 上架表加一列「SKU」(所有者建列,registry 登记),提交时回写——运营看到
  ASIN 就要能看到它在沃尔玛叫什么;在线产品总表投影加「来源码」列
  (从登记簿 JOIN),反过来看 SKU 也能对到 ASIN。

## 6. 消费方收口(7 处模式提取)

| 调用点 | 手里有什么 | 改法 |
|---|---|---|
| order_lines(订单落库) | store + conn | `resolve_many` 批量,落库当场填 asin |
| product_events.record_many | store + conn | 同上 |
| audit_rules(同 ASIN 跨店多 PT) | cur | 改查登记簿 JOIN |
| alloc_survey ×2 | items 含 store,cur 在手 | `resolve_many` |
| blacklist ×2 | conn | `resolve_many` |

原则:**登记簿优先、模式兜底、提不出留 NULL 绝不猜**(与 A1.5 定稿口径一致)。
两条清洗工作流(`sku_normalize` / `order_asin_normalize`)换成同一个 `resolve_many`,
存量行里凡是登记簿能查到的一并补齐。

## 7. 批次

**批次 0|读侧就绪(零行为变化)**:codec + 反查索引 + `resolve` + 5 处 SQL 收口 +
7 处消费方收口 + 两条清洗工作流接 `resolve_many`。存量 SKU 走的路一个字节不变
(登记簿里存量行 source_key 就是当年按格式回填的 asin,等号右边换成它结果相同)。
测试钉:三种存量形态经 `resolve` 结果与 `extract_asin` 逐字相同;不透明码经
`extract_asin` 必返 None、经 `resolve` 能查到。

**批次 1|写侧切换**:店铺凭证表「SKU前缀」列 + registry 常量 + fail-closed;
`list_new` / `match_listing` 改「抽码登记 → 提交」;上架表「SKU」列回写;
在线产品总表「来源码」列。**试点顺序是硬约束**:
1. 一家店填「SKU前缀」,其它店不填(它们整店跳过,摘要点名——这本身就是
   fail-closed 的验收);
2. `list_new --dry-run` 看该店载荷里 `sku` 是不透明码、其它店是"未配前缀跳过";
3. 真上 1 个品 → `catalog_sync -p store=<店>` → 在线产品总表看到新 SKU 与来源码;
4. **`maintenance_scan -p preview=1 -p store=<店>` 必须能看见这个品**——这是
   批次 0 的 SQL 收口有没有做对的唯一实测;
5. 该品出一单后查 `orders.order_lines.asin` 有值;
6. 通过后其它店逐个填前缀。

**批次 2|顺手的简化(另议,不在本计划内)**:`sku_locked_heal` 目前 RETIRE →
24h 冷却 → 清列重上同一 SKU;有了"重上 = 新码",冷却可能整个不需要。
但 RETIRE 对锁死 offer 的处置官方无明文,按本仓纪律不按推断编码,留待实测。

## 8. 待核验 / 待所有者决定

- [ ] 沃尔玛 SKU 字符集与上限:所有者机器上 `<DATA_ROOT>/specs/MP_ITEM/<版本>` 的
      Orderable.sku 定义(本容器没有 spec)。
- [ ] 店铺凭证表建「SKU前缀」列、上架表建「SKU」列——所有者建列后我登记常量。
- [ ] 各店前缀取值(A085 / L001 这类可沿用店名前缀;谭总系与数字开头店要另定)。
- [ ] 跟卖旧续号规则停用是否影响运营现有习惯(B 列人工优先不变)。
