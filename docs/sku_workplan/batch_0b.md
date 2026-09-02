## 0b|订单/事件/黑名单/审核侧收口 + 飞书 ASIN 列(零行为变化,一处例外)

> ## ⚠ 所有者定稿覆盖(2026-09-02,优先级高于下文任何 item)
> 1. **上架表 SKU 列在 R 列,不是 V**:所有者已建 R「SKU」,原 R~U(real_title /
>    real_pt / real_upc / upc_match,全仓无代码读写)顺延为 S~V。`LISTING_SHEET.columns`
>    在 `feed_check_date` 之后**插入** `sku`(第 18 位,不是末尾追加);`_COLS` 仍改 22
>    (读 A~V);写函数只写 `R{r}`;所有 item 文本里的 `V{r}` / 「V 列」一律读作 `R{r}` /
>    「R 列」;acceptance 里 `A1:V1` 表头核验保留(应看到第 18 格 = SKU)。
> 2. **飞书列名统一叫「来源码」**:销售订单表、售后订单表新增字段是「来源码」(不是
>    「ASIN」),值仍取 `order_lines.asin`;在线产品总表「来源码」在 **Q 列**(第 17 列),
>    元组元素名 `source_key`。四列所有者均已建好,飞书列接线 PR 不再等建列。
> 3. **来源字母定稿**:`SKU_SOURCE_LETTERS = {"amz": "A", "match": "B", "1688": "C", "self": "H"}`,
>    批次 0a 落 registry 时直接填值,不再留空。
>

**目标**:把「从 SKU 认 ASIN」这件事在订单链、事件账本、黑名单链(含**回填/重建**那条,原工作包漏列)、审核判定链、两条清洗工作流、sources_backfill 七处全部改成「登记簿 catalog.listing_sources 优先、形态提取只作存量兜底」;并把飞书 ORDER_SALES / ORDER_RETURNS 加「ASIN」列、ONLINE_PRODUCTS_SHEET 加 source_key 列(拆成第二个 PR,在所有者建完列之后再合)。存量数据(sku=asin / 三段式 / 纯数字 / PHUMWMT)走的路必须与今天逐字节相同;切到 12 位不透明码后这七处仍然认得出产品。本批次不产生任何 DDL、不新增任何形态判据常量(不透明码字母表唯一之家是批次 0a 的 services/sku_codec),唯一的判定口径变化见 decision D-0b-2。

**零行为变化**:否

除 D-0b-2 一处显式决策点外为零行为变化,理由逐条:①登记簿存量行的 source_key 就是 sources_backfill/list_new 回填的 asin,`resolve_many` 对 amz 行返回(归一并经 is_standard_asin 校验后的)source_key、其余一律回落 `extract_asin`,对今天库里的三种存量形态输出与 `extract_asin` 逐字相同(由 0a 的契约测试与本批次 test_sku_guard.py::test_legacy_shapes_resolve_identically 双向钉住);②`resolve_pairs` 的纯数字倒查改成**两级**(先 (店,item_id) 再全局),第二级 SQL 文本与今天的 `_ITEMID_SQL` 逐字相同、且对第一级没解出的**全部**剩余对(含 store 为 NULL 的平台级事件)都跑 —— 所以今天补得上的行改后一行不少(原工作包把这一跳收窄到店维度、并对 store 为 NULL 的对整条跳过,那是**少补**,是回归,已按审查意见改掉);③形态计数 asin/wrapped/numeric/other/numeric_resolved 五档仍按 **distinct sku** 计(与今天逐字同口径),新增的三档 pairs/registry_differs/numeric_cross_store 按组合计并在 docstring 里写死单位,`samples` 对 sku 去重后取前 5(避免同一 sku 跨店重复占样本位);④`_FILL_SQL` 加 store 维度只改「怎么定位行」不改「填什么值」:待洗集合由 `SELECT DISTINCT store, sku` 枚举了每一个 (店,sku) 组合,更新到的行集合与今天按裸 sku 匹配完全相同;`IS NOT DISTINCT FROM` 是为了不漏掉 product_events 的 store=NULL 行;⑤order_lines.asin 的写入从 extract 时点挪到 upsert 时点,`_ASIN_GUARD = COALESCE(EXCLUDED.asin, t.asin)` 不变,算不出仍留 NULL;⑥product_events 平台级事件(store 为空:product_ingest / audit_store.event_row / product_audit 补采 / audit_history_fold 直插)保持 extract_asin 原路 —— 注意 cleanup_history_import **带 store**,走登记簿腿(D-0b-7 的事实描述已按审查意见订正);⑦blacklist 的 `or sku` 原文兜底保持不变(只加日志计数);`_LATEST_CTE` 改经登记簿取键后,存量三种形态的键与今天逐字相同(amz 行 source_key==sku、非 amz 行 source_key 为 NULL 回落 e.asin/e.sku);⑧sources_backfill 的写路由(ASIN 形态→amz,其余→unknown)一字不改,只在「疑似新码漏登记」桶非空时追加一行摘要,该桶今天恒为 0 ⇒ 摘要逐字相同;⑨飞书三处加列全部推到第二个 PR,在所有者建完列之后才合 —— 因此既没有 `_adapt_rows` 每轮刷 WARNING 的窗口期,也不存在 registry 17 列而投影 SQL 16 列的中间态。**前提**:本论证②①两条都依赖「登记簿里没有 source_key ≠ sku 的 amz 行」。生产库里可能有:refdata/schema.sql:232-233 的存量回填正则 `^B0[A-Z0-9]{8}` **右端没有锚点**且写 `left(sku, 10)`,所以 `B0XXXXXXXX-2` 这类重上后缀 SKU 会被登记成 amz 且 source_key='B0XXXXXXXX'。这批行今天 extract_asin 提不出、asin 恒 NULL,改后会被登记簿解出 —— 是修复方向,但**是行为变化**。故体检⑤⑥升格为**合并硬闸**(必须为 0),数据/正则的修复归 0a(见 depends_on)。

### 改动清单

#### 0b-01 · `services/sku_asin.py` · 58-66(_ITEMID_SQL 及其上方 4 行注释)、69-100(resolve_skus)

**改动**:把批量清洗入口从「按 sku 列表」改成「按 (店, sku) 对」,并把纯数字倒查改成两级(先店内、后全局兜底)。

① 删 `resolve_skus(conn, skus)`,新增:
```python
def resolve_pairs(conn, pairs: list[tuple[str | None, str]]) -> tuple[dict, dict]:
```
docstring 首行:「输入:连接 + [(店铺, sku)] → 输出:({(店铺,sku): asin}, 计数)」。第二段必须写死计数单位(混单位是这个函数最容易被误读的地方):`asin`/`wrapped`/`numeric`/`other`/`numeric_resolved` 五档按 **distinct sku** 计(与改前逐字同口径);`pairs`/`registry_differs`/`numeric_cross_store` 三档按 **(店,sku) 组合** 计。

② 实现固定四步,同函数内不许拆:
  (a) `pairs = list(dict.fromkeys(pairs))`(去重保序);对 `dict.fromkeys(k for _s, k in pairs)` 逐个 `classify` 记前四档;`buckets["pairs"] = len(pairs)`。
  (b) `reg = sku_asin.resolve_many(conn, pairs)`(0a 提供;登记簿 amz 行给 source_key,其余一律回落 `extract_asin`),`mapping.update(reg)`;
      `buckets["registry_differs"] = sum(1 for (_s, k), a in reg.items() if a != extract_asin(k))`
      —— 这一档就是「登记簿给出的答案与形态提取不同」的组合数,**上线前它必须是 0**(非 0 = 体检⑤那批 source_key ≠ sku 的行,见 risks 第 1 条)。
  (c) 纯数字倒查**两级**,先店内后全局:
      `still = [(s, k) for (s, k) in pairs if (s, k) not in mapping and classify(k) == "numeric"]`
      · 第一级:对 `s` 非空的对,按新 `_ITEMID_STORE_SQL` 查 `(store, item_id) → 订货号`,再对 `[(store, 订货号)]` 调**一次** `resolve_many`(**不是 `extract_asin`** —— 切码后反查出的订货号本身就是不透明码,必须再过一次登记簿);
      · 第二级:对第一级仍没解出的**全部**剩余对(**含 `s` 为 None 的平台级事件行**),按 `_ITEMID_SQL`(文本一字不改)按 item_id 全局反查,再走 `extract_asin`(与改前逐字相同)。
      命中记 `numeric_resolved`(按 distinct sku);**只**由第二级救回的另记 `numeric_cross_store`。
  (d) `return mapping, dict(buckets)`。

③ 新增常量,紧挨 `_ITEMID_SQL` 上方:
```
_ITEMID_STORE_SQL = """
SELECT DISTINCT store, item_id, sku FROM catalog.walmart_items
WHERE store = ANY(%s) AND item_id = ANY(%s)
"""
```
④ `_ITEMID_SQL`(63-66)**一个字不改** —— 它就是第二级,保持它是「今天补得上的行改后一行不少」的字面凭据。58-62 那段收编注释保留,末尾补一句:「2026-09-xx 拆两级:第一级带 store(切码后同一 item_id 反查出的订货号还要再过店维登记簿),第二级仍是这条全局 SQL —— **它是既有行为,不许删**:今天订单行落在 T2 店、item_id 只在 T1 店的 walmart_items 里有行时,靠的就是它。」

**为什么**:两条清洗工作流的 `_FILL_SQL` 要带 store(sku_plan §1「问 1」的两条硬约束之一),store 必须从取数那一刻就带着,否则工作流会自己拼一份 (store, sku) 逻辑 —— 那正是 `test_rules_are_not_reimplemented_here` 要拦的。

**采纳审查意见(第三位审查者 blocker「resolve_pairs 是严格的少补」)**:原稿把倒查收窄到店维度、并对 store 为 None 的对整条跳过。实测 services/sku_asin.py:63-66 的 `_ITEMID_SQL` 不带 store、`resolve_skus` 的映射键是裸 sku、两条 `_FILL_SQL` 也只按 sku 匹配 —— 今天任意一家店的 walmart_items 有该 item_id,**所有店**的行都补得上,product_events 的 store=NULL 行也补得上。收窄它是行为变化(而且是让 order_lines.asin / product_events.asin 变空的那个方向,静默)。故改成两级:店内优先(切码后不串味),全局兜底(保住今天的覆盖面),差额单列 `numeric_cross_store` 报出来给所有者看。

第三跳用 `resolve_many` 而不是 `extract_asin`,否则不透明码在纯数字这条路上永远解不出来。

**测试**:
- tests/test_sku_asin.py::test_resolve_skus_two_hops_and_leaves_the_rest_alone(改名 test_resolve_pairs_two_hops_and_leaves_the_rest_alone;夹具 _Cur/_Conn 改喂 (store, sku) 对;断言 `conn.cur.args == (["102460018738", "998877665544"],)` 在 store 全为 None 时**必须一字不变**——这就是第二级仍是老 SQL 的证据)
- tests/test_sku_asin.py::test_resolve_skus_skips_the_lookup_when_nothing_is_numeric(改名 …_pairs_…;断言仍是「一条 SQL 都不该发」)
- tests/test_sku_asin.py::test_numeric_itemid_hop_prefers_the_store_scoped_row(新;两家店同 item_id 各自反查出自己那行,不串味)
- tests/test_sku_asin.py::test_numeric_itemid_hop_falls_back_to_the_global_lookup(新;订单行在 T2、walmart_items 只有 T1 那行 ⇒ 仍解得出,且计入 numeric_cross_store)
- tests/test_sku_asin.py::test_numeric_itemid_hop_still_runs_for_store_null_pairs(新;store=None 的对必须走第二级,不许跳过)
- tests/test_sku_asin.py::test_numeric_itemid_hop_goes_through_the_registry_again(新;第一级反查出的订货号是不透明码时仍解得出 asin)
- tests/test_sku_asin.py::test_bucket_units_are_documented_and_stable(新;asin/wrapped/numeric/other 按 distinct sku 计——同一 sku 在 3 家店,四档数字与单店时相同)
- tests/test_order_asin_normalize.py::test_normalize_agrees_with_the_event_ledger_cleaner(改调 resolve_pairs;`is` 同一函数对象的断言保留)

**验收**:python -m pytest -q tests/test_sku_asin.py tests/test_order_asin_normalize.py tests/test_sku_normalize.py

#### 0b-02 · `services/sku_asin.py` · 103-110(samples)

**改动**:签名改 `def samples(pairs: list[tuple[str | None, str]], buckets: dict) -> dict`;内部**先对 sku 去重再取前 5**:
```python
    def _five(kind):
        seen, out = set(), []
        for _st, s in pairs:
            if classify(s) == kind and s not in seen:
                seen.add(s); out.append(s)
                if len(out) == 5:
                    break
        return out
    return {k: _five(k) for k in ("numeric", "other") if buckets.get(k)}
```
返回值形状(只报 numeric/other 两桶、各前 5 个样本、样本只放 sku 串不放 store)一字不改 —— 摘要是给人认新形态的,带上店名反而更难读,同一个 sku 出现五次更是把样本位全占了。

**为什么**:两个调用方(sku_normalize / order_asin_normalize)传的是同一个待洗集合,samples 与 resolve_pairs 必须吃同一个入参形状,否则工作流要自己再摊一次列表 —— 又是一处工作流里的规则重实现。**采纳审查意见(第三位审查者 major)**:原稿只说「classify 取 pair 的第二元」,那样同一个 sku 跨 5 家店就会把前 5 个样本位全占满,而这个样本列表的全部作用就是「新形态先进其他桶带样本报出来,人认了再扩规则」——重复样本等于把它废掉。

**测试**:
- tests/test_sku_asin.py::test_samples_only_reports_the_buckets_a_human_has_to_look_at(改传 pairs;其余断言一字不改)
- tests/test_sku_asin.py::test_samples_dedupes_a_sku_that_spans_stores(新;同一 numeric sku 在 3 家店 ⇒ 样本里只出现一次)

**验收**:python -m pytest -q tests/test_sku_asin.py

#### 0b-03 · `services/sku_asin.py` · 1-16(模块 docstring 末尾)

**改动**:模块 docstring 末尾追加一段「三个入口的分工」,逐字写死(不加代码):
```
三个批量/单条入口,分工不许混(2026-09-xx 定):
  · resolve(conn, store, sku)      单条壳,只给零星调用点(services 内部)
  · resolve_many(conn, pairs)      **纯登记簿 + 形态兜底**的批量反查;
                                   services 内部消费方(order_lines /
                                   product_events / blacklist)用它
  · resolve_pairs(conn, pairs)     **清洗类工作流的唯一批量入口**,在
                                   resolve_many 之上多一跳纯数字 item_id
                                   倒查(要查 walmart_items),并产出形态计数
workflows/ 只准用 resolve_pairs;直接用 resolve_many / 拼 listing_sources
的 SQL 由 tests/test_sku_guard.py 拦(守门白名单)。
```

**为什么**:**采纳审查意见(第二位审查者 minor「SKU→ASIN 反查出现三个入口,调用方得自己挑」)**:0a 交付 resolve/resolve_many、0b 加 resolve_pairs,而 resolve_pairs 内部又调 resolve_many,两个批量入口语义高度重叠。守门只禁 workflows 用 resolve_many,没说 services 内部谁该用哪个 —— 不写死分工,下一个人会在 services 里随手挑一个,两条路各自演化。这是纯注释,零行为变化。

**测试**:
- tests/test_sku_guard.py::test_only_cleaner_workflows_call_resolve_pairs(新;workflows/ 下出现 resolve_pairs 的文件只允许 sku_normalize.py 与 order_asin_normalize.py)

**验收**:python -m pytest -q tests/test_sku_guard.py

#### 0b-04 · `workflows/sku_normalize.py` · 33-41(_DISTINCT_SQL / _FILL_SQL)

**改动**:两条 SQL 加 store 维度,文本定死为:
```
_DISTINCT_SQL = """
SELECT DISTINCT store, sku FROM catalog.product_events WHERE asin IS NULL
"""

_FILL_SQL = """
UPDATE catalog.product_events e SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS store, unnest(%s::text[]) AS sku,
             unnest(%s::text[]) AS asin) m
WHERE e.sku = m.sku AND e.store IS NOT DISTINCT FROM m.store AND e.asin IS NULL
"""
```
`IS NOT DISTINCT FROM` 是硬要求,不许写成 `=`:product_events.store 对平台级事件(product_ingest / audit_store.event_row / product_audit 补采 / audit_history_fold)是 NULL,`=` 会把这批行全部漏掉,而且不报错。`asin IS NULL` 两处必须原样保留(幂等的根据)。

**为什么**:同一串 sku 在两家店切码后指向不同产品;不带 store 的 UPDATE 会把 A 店的 asin 写到 B 店的事件行上。这是 sku_plan §1「问 1」点名的两条硬约束之一。覆盖面不变的论证:待洗集合由 `SELECT DISTINCT store, sku` 枚举了每一个组合,所以按 (店,sku) 定位到的行集合与今天按裸 sku 定位完全相同。

**测试**:
- tests/test_sku_normalize.py::test_fill_sql_carries_store_and_tolerates_null_store(新增在**既有**文件里——tests/test_sku_normalize.py 已存在,原工作包写「新建该测试文件」是错的;断言 `IS NOT DISTINCT FROM` 在 _FILL_SQL 文本里、`asin IS NULL` 仍在两条 SQL 里、`DISTINCT store, sku` 在 _DISTINCT_SQL 里)
- tests/test_sku_guard.py::test_both_cleaners_fill_sql_carry_store(守门;两条工作流的 _FILL_SQL 文本都必须含 store 与 IS NOT DISTINCT FROM)

**验收**:python -m pytest -q tests/test_sku_normalize.py;python cli.py sku_normalize   # 预览零写入

#### 0b-05 · `workflows/sku_normalize.py` · 13-17(模块 docstring 清洗路径)、44-77(run)

**改动**:① 50 行 `skus = [r[0] for r in cur.fetchall()]` → `pairs = [(s, k) for s, k in cur.fetchall()]`;51 行 `if not skus` → `if not pairs`。
② 53 行 `sku_asin.resolve_skus(conn, skus)` → `sku_asin.resolve_pairs(conn, pairs)`;55 行 `sku_asin.samples(skus, buckets)` → `sku_asin.samples(pairs, buckets)`。
③ 摘要口径:先算三个数,再拼文案(**摘要里的「个 sku」一律用 sku 级数字**,以便与改前逐字对得上):
```python
        n_sku = len({k for _s, k in pairs})
        n_sku_ok = len({k for _s, k in mapping})
```
  · 58 行预览行改 `f"SKU 清洗预览:待洗 {n_sku} 个不同 sku / {len(pairs)} 个 (店,sku) 组合,形态 {shape};可解析 {n_sku_ok} 个 sku、{len(mapping)} 个组合"`(既有断言 `"待洗 4 个"`/`"可解析 3 个"` 仍是子串,保持绿);
  · 74 行改 `f"SKU 清洗:{n_sku_ok}/{n_sku} 个 sku 解析成功,补填事件 {n} 行"`(既有断言 `"3/4 个 sku 解析成功"` 保持绿);
  · 70 行 `unresolved = n_sku - n_sku_ok`。
④ 67-68 行的填充实参从两数组改三数组,**必须先固定 keys 再摊**:
```python
            keys = sorted(mapping, key=lambda p: (p[0] or "", p[1]))
            cur.execute(_FILL_SQL, ([s for s, _ in keys],
                                    [k for _, k in keys],
                                    [mapping[p] for p in keys]))
```
(现行 68 行 `(list(mapping), [mapping[s] for s in mapping])` 靠 dict 迭代序稳定;加到三个数组后这种写法一眼看不出对不对。`key=` 里 `p[0] or ""` 是因为 store 可能是 None,`sorted` 对混了 None 的元组会 TypeError。)
⑤ 模块 docstring 13-17 行的清洗路径三条前面补一条 ⓪:「登记簿 catalog.listing_sources 按 (店, sku) 反查 source_key(切码后唯一通路);②的倒查分两级:先 (店, item_id),查不到再按 item_id 全局查一次(**后者是既有行为,保住跨店补齐的覆盖面**)」。

**为什么**:取数带 store 之后,映射键、填充实参、摘要口径必须同步换,漏一处就是「解析出来了但填不进去」的静默失败。keys 固定 + 显式排序 key 是防呆:三个平行数组一旦错位,填进去的是别人的 asin,而且不报错;None store 不给排序 key 会当场 TypeError。摘要用 sku 级数字是**采纳审查意见(第三位审查者 major「摘要口径从不同 sku 变成组合后可解析率/覆盖率全部改数却没有测试钉住」)**:两个数并列报出来,既保住了与改前的可比性,也让 (店,sku) 组合数第一次可见。

**测试**:
- tests/test_sku_normalize.py::test_preview_profiles_without_writing(既有;夹具 _Cur 的 `DISTINCT sku` 分支改返回 `[(None, s) for s in ...]`;两条既有断言不改)
- tests/test_sku_normalize.py::test_apply_fills_only_resolved(既有;`skus, asins = conn.filled` 改 `stores, skus, asins = conn.filled`,新增断言三数组等长且逐位对齐)
- tests/test_sku_normalize.py::test_no_pending_rows(既有,不改)
- tests/test_sku_normalize.py::test_fill_args_are_three_aligned_arrays(新;mapping 里混 None store 与真 store,断言 sorted 不炸且逐位对齐)

**验收**:python -m pytest -q tests/test_sku_normalize.py;python cli.py sku_normalize(预览);再 python cli.py sku_normalize -p apply=1(所有者机器,幂等;第二次跑应报「无待洗行」或只补新增)

#### 0b-06 · `workflows/order_asin_normalize.py` · 52-61(_DISTINCT_SQL / _FILL_SQL)

**改动**:同 0b-04 口径:
```
_DISTINCT_SQL = """
SELECT DISTINCT store, sku FROM orders.order_lines
WHERE asin IS NULL AND sku IS NOT NULL AND btrim(sku) <> ''
"""

_FILL_SQL = """
UPDATE orders.order_lines o SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS store, unnest(%s::text[]) AS sku,
             unnest(%s::text[]) AS asin) m
WHERE o.sku = m.sku AND o.store IS NOT DISTINCT FROM m.store AND o.asin IS NULL
"""
```
`btrim(sku) <> ''` 与 `asin IS NULL`(两处)必须原样保留(既有 test_sql_only_touches_null_rows 钉着幂等)。

**为什么**:同 0b-04。订单行的 store 理论上非空,但 `IS NOT DISTINCT FROM` 与 `=` 在非空时等价、在脏数据上更安全,两条清洗工作流写法必须一致(它们有一条 test_normalize_agrees_with_the_event_ledger_cleaner 的对拍测试)。

**测试**:
- tests/test_order_asin_normalize.py::test_sql_only_touches_null_rows(既有,106-111 行;扩:再断言 `DISTINCT store, sku` 与 `IS NOT DISTINCT FROM` 在文本里)
- tests/test_sku_guard.py::test_both_cleaners_fill_sql_carry_store

**验收**:python -m pytest -q tests/test_order_asin_normalize.py;python cli.py order_asin_normalize   # 预览零写入

#### 0b-07 · `workflows/order_asin_normalize.py` · 13-18 与 20-25(模块 docstring)、70-128(run)

**改动**:与 0b-05 完全同款改法:
① 88 行 `skus = [r[0] for r in cur.fetchall()]` → `pairs = [(s, k) for s, k in cur.fetchall()]`;89 行 `if not skus` 同步。
② 92 行 `resolve_skus(conn, skus)` → `resolve_pairs(conn, pairs)`;94 行 `samples(skus, buckets)` → `samples(pairs, buckets)`。
③ 95-97 行:
```python
        n_sku = len({k for _s, k in pairs})
        n_sku_ok = len({k for _s, k in mapping})
        rate = len(mapping) / len(pairs) if pairs else 0.0
        head = (f"待洗 {n_sku} 个不同 sku / {len(pairs)} 个 (店,sku) 组合,"
                f"形态 {shape};可解析 {n_sku_ok} 个 sku、"
                f"{len(mapping)} 个组合({rate:.1%})")
```
④ 106-113 行分块:`keys = sorted(mapping, key=lambda p: (p[0] or "", p[1]))`,`chunk = keys[i:i + _BATCH]`,`cur.execute(_FILL_SQL, ([s for s, _ in chunk], [k for _, k in chunk], [mapping[p] for p in chunk]))`。`_COVERAGE_SQL` 与 114-115 行不动。
⑤ 117 行 `unresolved = n_sku - n_sku_ok`(125 行文案「解析不了 N 个 sku」因此仍是 sku 级,既有断言 `"解析不了 2 个 sku"` 保持绿)。
⑥ 模块 docstring:13-18 行清洗路径补 ⓪ 登记簿那一跳(文字同 0b-05⑤);20-25 行「提不出的留 NULL 不猜」那段末尾补一句:「切码后登记簿是主路、形态提取只是存量兜底 —— 两者都空才留 NULL」;31-32 行「纯数字 item_id 形态……写入路径上做不了」保留,后面补「(倒查分两级:先 (店,item_id),再按 item_id 全局兜底一次)」。

**为什么**:同 0b-05。docstring 要改是因为它现在写的是「规则唯一出处 services/sku_asin,这里不重复实现」,新增的登记簿那一跳仍在 services 里,但读的人得知道主路换了、倒查变两级了。

**测试**:
- tests/test_order_asin_normalize.py::test_resolves_the_four_sku_forms(既有 56-71 行;改调 resolve_pairs,夹具 _Cur 的 `SELECT DISTINCT sku` 分支改 `SELECT DISTINCT store, sku` 且返回 (None, s),`FROM catalog.walmart_items` 分支加 store 列)
- tests/test_order_asin_normalize.py::test_preview_writes_nothing(既有 88-93 行;`"可解析 1 个(50.0%)"` 改成新文案里的对应子串)
- tests/test_order_asin_normalize.py::test_apply_fills_only_resolved(既有 96-103 行;改三数组断言)
- tests/test_order_asin_normalize.py::test_unresolvable_stays_null_and_is_never_backfilled_with_raw_sku(既有 74-85 行,不变,必须仍绿)
- tests/test_order_asin_normalize.py::test_missing_column_says_run_db_init_not_a_traceback(既有 193-219 行;`_Boom.execute` 里的 `"SELECT DISTINCT sku"` 判断要跟着改成 `"SELECT DISTINCT store, sku"`,否则这条会假绿)

**验收**:python -m pytest -q tests/test_order_asin_normalize.py;python cli.py order_asin_normalize;再 -p apply=1(所有者机器)

#### 0b-08 · `tests/test_order_asin_normalize.py` · 114-126(test_rules_are_not_reimplemented_here)

**改动**:124 行 `"sku_asin.resolve_skus" in src` → `"sku_asin.resolve_pairs" in src`;125 行 `"_ITEMID_SQL" not in src` 保留;121 行 `"re.compile" not in src and "import re" not in src` 保留;新增两条:`"listing_sources" not in src`(登记簿那一跳也不许在工作流里出现)与 `"resolve_many" not in src`(工作流只准用批量入口,不许自己逐条查)。144 行 `oan.sku_asin.resolve_skus is sn.sku_asin.resolve_skus` → `resolve_pairs`。

**为什么**:这条守门测试是本批次「规则不在工作流里重实现」的**就地**执法点(跨文件的结构性断言统一去 tests/test_sku_guard.py,见 0b-23)。登记簿那一跳是新加的规则,必须一起被它管住,否则下一个人会在 sku_normalize 里直接写一条 JOIN listing_sources 的 SQL —— 两份规则,漏改一份不报错。

**测试**:
- tests/test_order_asin_normalize.py::test_rules_are_not_reimplemented_here(本条即测试)
- tests/test_order_asin_normalize.py::test_normalize_agrees_with_the_event_ledger_cleaner(144 行同步改名)

**验收**:python -m pytest -q tests/test_order_asin_normalize.py -k "reimplemented or agrees"

#### 0b-09 · `services/order_lines.py` · 166-169(extract_order_lines 的 asin 键 + 其上三行注释)

**改动**:删掉 166-169 这四行(三行注释 + `"asin": sku_asin.extract_asin(sku),`),`extract_order_lines` 从此**不产出 asin 键**(`_upsert` 在 416 行做 `{c: r.get(c) for c in cols}`,缺键取 None,列仍在载荷里)。原地留一行注释:
```python
            # asin 不在这里算 —— 它要查登记簿(要连接),统一由
            # upsert_order_lines 落库当场补(唯一实现路径,见 _fill_asins)
```
函数 docstring 首行不变(仍是纯函数,现在更纯了)。**顶部 27 行 `from services import sku_asin` 不删** —— 0b-10 的 `_fill_asins` 还要用。

**为什么**:登记簿反查需要连接,而 `extract_order_lines` 是刻意保持的纯函数(店铺名 + 单个订单 dict → 行列表),给它塞一个 conn 会让 order_sync 的并发取数路径(workflows/order_sync.py:36-42 `_persist` 在拿连接**之前**就 extract 完了)和一堆测试跟着动。把这一跳挪到唯一的写入口 `upsert_order_lines`,既拿到了连接,又天然做成批量一次查(order_sync 是每店一批,一批一条 SELECT)。

**测试**:
- tests/test_order_lines.py::test_extract_order_lines_no_longer_computes_asin(新;断言产出行里没有 asin 键)
- tests/test_order_asin_normalize.py::test_order_sync_fills_asin_at_write_time(既有 151-168 行,**会红**:它断言 `ol.extract_order_lines(...)[0]["asin"] == "B0GXX75JN5"`。改写成「落库当场填」的新形状:构造 _FakeConn 跑 `ol.upsert_order_lines`,断言 executemany 的行里 asin == B0GXX75JN5;测试名与 docstring 保持不变——它守的语义没变,变的只是在哪一步填)

**验收**:python -m pytest -q tests/test_order_lines.py tests/test_order_asin_normalize.py

#### 0b-10 · `services/order_lines.py` · 374-375(_ASIN_GUARD 及其注释)、451-458(upsert_order_lines)

**改动**:① 新增私有积木,紧挨 `upsert_order_lines`(451 行)上方:
```python
def _fill_asins(conn, rows: list[dict]) -> int:
    """输入:连接 + order_lines 行 → 输出:补出 asin 的行数(规则唯一出处 services/sku_asin)。"""
```
实现:一次 `sku_asin.resolve_many(conn, [(r.get("store"), r.get("sku")) for r in rows if r.get("sku")])`,逐行写 `r["asin"] = m.get((r.get("store"), r.get("sku")))`(解不出写 None,**不拿 sku 原文兜底** —— 订单链的口径是「提不出留 NULL」,与 order_asin_normalize docstring 20-25 行所有者 2026-08-15 的定稿一致)。空 rows 直接 `return 0`,不开游标。
② `upsert_order_lines` 的 453 行(raw 序列化循环)**之前**插一行 `_fill_asins(conn, rows)`。
③ 374 行注释「算不出就别覆盖:order_sync 拿纯数字 sku 提不出 asin,而扫尾工作流查库能填出来」改成:「算不出就别覆盖:order_sync 对**纯数字 item_id 形态**仍提不出 asin(那一跳要按 (店,item_id) 查 walmart_items,写入路径上做不了),由 order_asin_normalize 扫尾 —— 登记簿接上以后这条守卫**照样不能拆**」。

**为什么**:sku_plan §3.3 的 `services/order_lines.py:169` 一条:切码后 `extract_asin(不透明码)` 恒为 None ⇒ `order_lines.asin` 恒 NULL ⇒ 产品分的销量/退货率维度整个退出(分配引擎最强的信号没了,而且不报错)。放在唯一写入口里做,保证 order_sync 这条主路(workflows/order_sync.py:42 是唯一调用点)一定经过它;order_history_import 走自己的 `_INSERT`(:63-67,只导旧数据、sku 一定是存量形态),按 sku_plan §3.3「不改的」名单保持不动。注释③要改是因为「纯数字」这条剩余缺口是本批次**唯一**没被登记簿补掉的口子,不说清楚会有人以为 COALESCE 守卫可以拆了。

**测试**:
- tests/test_order_lines.py::test_upsert_fills_asin_from_the_registry(新;登记簿有 amz 行 → 填 source_key)
- tests/test_order_lines.py::test_upsert_leaves_asin_null_when_unresolvable(新;解不出留 None,绝不写 sku 原文)
- tests/test_order_lines.py::test_upsert_asin_guard_still_coalesces(新;`ol._ASIN_GUARD == "COALESCE(EXCLUDED.asin, t.asin)"` 且该表达式出现在生成的 SQL 里)
- tests/test_order_lines.py::test_fill_asins_is_one_query_per_batch(新;_FakeCursor 计 execute 次数 == 1,防逐行往返)
- tests/test_order_lines.py::test_upserts_build_conflict_sql / test_upsert_skips_write_when_nothing_changed / test_upsert_phone_all_zero_never_overwrites_real_number(既有 296/314/333 行;`_FakeCursor` 需补 `fetchall()` 返回 [] 才能接住新增的 SELECT,且 `conn.cur.calls[0]` 要改成取 executemany 那一条——现在 calls[0] 是 SELECT)

**验收**:python -m pytest -q tests/test_order_lines.py;上线后体检:psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) FILTER (WHERE asin IS NULL) AS 空, count(*) AS 总 FROM orders.order_lines WHERE order_date >= now() - interval '7 days'"(空值占比不得比改前上升)

#### 0b-11 · `services/product_events.py` · 86(import)、145-173(record_many),重点 160-167

**改动**:① 86 行 `from services.sku_asin import extract_asin` 改 `from services import sku_asin`(全文件其余 `extract_asin` 引用同步加前缀 —— 实测本文件只有 167 行一处)。**必须改成模块级 import**,否则测试无法 monkeypatch。
② `record_many` 在 156-159 行的 `bad = ...` 校验之后、160 行 `with conn.cursor()` 之前插:
```python
    # asin 两条路:带 store 的走登记簿(切码后唯一通路);store 为空的**平台级
    # 事件**没有店维度可查(登记簿主键是 (store, sku)),保持形态提取 —— 它们的
    # sku 本来就是 asin,extract_asin 恒等返回。平台级来源共四处:
    #   services/product_ingest.py:266(product_ingested)
    #   services/audit_store.py:196-215 event_row(audit_passed/rejected)
    #   workflows/product_audit.py:617(audit_backfill 补采)
    #   workflows/audit_history_fold.py:74-87(**直插 SQL,绕过本函数**,asin 列直填)
    # ⚠ cleanup_history_import **带 store**(services/cleanup_history.py:75),
    #   走登记簿腿,不在平台级之列。两条路都提不出存 NULL,消费方 coalesce(asin, sku)。
    asin_of = sku_asin.resolve_many(
        conn, [(r["store"], r["sku"]) for r in rows if r.get("store")])
```
③ 165-166 行那两行旧注释删掉(内容并进上面那段);167 行 `extract_asin(r["sku"])` 改:
```python
              (asin_of.get((r["store"], r["sku"])) if r.get("store")
               else sku_asin.extract_asin(r["sku"])),
```

**为什么**:sku_plan §3.3 的 `services/product_events.py:167` 一条:事件身份退化成随机码后,同一产品跨店/重上不再归并,`product_risk` 四个视图的 `coalesce(asin, sku)` 全部退化成按码分叉,时间线断成一段一段。

**采纳审查意见(第三位审查者 major,D-0b-7 事实前提写错)**:实测 services/cleanup_history.py:64/75 把 store 原样带进 record_many,所以 41.7 万行历史导入走的是**登记簿腿**,不是形态腿;原稿把它列进「平台级三个来源」并据此说「零风险、不查库」是错的。改正后:平台级来源是 product_ingest / audit_store.event_row / product_audit:617 三处 + audit_history_fold 直插 SQL 第四处(**第一位审查者 missing 项**,实测 workflows/audit_history_fold.py:74-87 `INSERT ... SELECT asin, asin, NULL, ...` 绕过 record_many)。这四处必须在注释里点名,否则守门白名单的理由与事实不符。

**测试**:
- tests/test_product_events.py::test_record_many_serializes_detail(既有 30-43 行;`conn.sqls[0]` 改 `conn.sqls[-1]` —— 第一行有 store=T1,INSERT 之前多一条登记簿 SELECT)
- tests/test_product_events.py::test_record_many_resolves_asin_via_registry_when_store_present(新)
- tests/test_product_events.py::test_record_many_falls_back_to_shape_for_platform_events(新;store 缺省的行仍按形态提取,且**不发 SELECT**)
- tests/test_product_events.py::test_record_many_issues_one_lookup_per_call(新;防批量灌账时逐行往返)
- tests/test_sku_asin.py::test_record_many_autofills_asin_column(既有 46-70 行;行里没有 store ⇒ 走形态腿,断言不改,但要确认 resolve_many 对空 pairs 不开游标——见 depends_on)
- tests/test_cleanup_history.py::test_record_many_carries_occurred_at(既有 158-176 行;行里没有 store ⇒ 不发 SELECT,夹具无 execute/fetchall 也不会炸;**这条是「空 pairs 不开游标」契约的现成守门**)

**验收**:python -m pytest -q tests/test_product_events.py tests/test_product_events_registry.py tests/test_cleanup_history.py tests/test_sku_asin.py

#### 0b-12 · `services/blacklist.py` · 51(import)、87-105(record_asins),重点 91-92 与 99

**改动**:① 51 行 `from services.sku_asin import extract_asin` 改 `from services import sku_asin`(其余引用同步加前缀:99、157 两处)。
② `record_asins` 在 93 行 `added = 0` **之前**插一次批量反查:
```python
    asin_of = sku_asin.resolve_many(
        conn, [(it.get("store"), it["sku"]) for it in items
               if it.get("category") in PERMANENT])
    fell_back: list[str] = []
```
③ 99 行 `asin = extract_asin(it["sku"]) or it["sku"]` 改:
```python
            asin = asin_of.get((it.get("store"), it["sku"]))
            if not asin:
                asin = it["sku"]
                fell_back.append(it["sku"])
```
④ 兜底可见化(conventions §六 真兜底三要件之「触发必须记日志计数」),105 行 `return added` 之前:
```python
    if fell_back:
        logger.warning("ASIN 黑名单:%d 个 sku 登记簿与形态都解不出,按订货号原文"
                       "入键(键不标准,按 ASIN 的重上架拦截对它们不生效):%s",
                       len(fell_back), fell_back[:5])
```
⑤ 91-92 行 docstring 那段改成:「黑名单键 = 登记簿反查出的 source_key(切码后唯一通路),查不到回落形态提取,再不行用订货号原文兜底并**告警计数**,宁可键不标准也不丢行(口径见 D-0b-1);订货号原文永远存 src_sku 溯源。」

**为什么**:sku_plan §3.3 的 `services/blacklist.py:99` 一条,后果是「黑名单键被灌随机码 ⇒ list_new 的黑名单闸拦不住违禁品」——钱和合规双输。`or sku` 原文兜底按 D-0b-1 默认**保持现状**,但今天它是完全静默的:2026-08-11 那次「2,702 个 C/E 品牌 0 命中」正是这种静默的产物,加一条计数告警让下次不用等三个月才发现。

**测试**:
- tests/test_blacklist.py::test_record_asins_key_is_cleaned_asin(既有 116-123 行;夹具 _Cur 的 execute 遇到 `FROM catalog.listing_sources` 会落到 else 分支返回空行 ⇒ resolve_many 回落 extract_asin ⇒ 断言不变、无需改夹具;**跑一遍确认**)
- tests/test_blacklist.py::test_record_asins_key_comes_from_the_registry_first(新;monkeypatch `bl.sku_asin.resolve_many` 给出与形态提取不同的值,断言取登记簿)
- tests/test_blacklist.py::test_record_asins_raw_fallback_is_counted_and_logged(新;caplog 里必须有那条 warning 且带计数)
- tests/test_blacklist.py 其余 15 条用例必须逐条仍绿(入选口径 BCEFGK / DO NOTHING / biz_cn / 来源列格式 / 品牌收集七条 / 尾段隔离两条 / 在途 48h)

**验收**:python -m pytest -q tests/test_blacklist.py tests/test_problem_scan.py tests/test_blacklist_push.py tests/test_feed_track.py

#### 0b-13 · `services/blacklist.py` · 135-179(collect_brands),重点 144-145、155-157、161、171、176

**改动**:① 144-145 行 `cands` 的键与 176 行 `_MARK_SQL` 实参 `sku` **保持不变**(D-0b-3);在 144 行上方加两行注释:
```python
    # 去重键是 sku 不是 asin:切码后 sku 全局唯一 ⇒ 同一 ASIN 在两家店各收一次
    # 品牌(渠道表/闸门表都 DO NOTHING 幂等,只多一次查库);改成 asin 会改变
    # 存量三段式行的去重粒度,不在本批次范围(见 D-0b-3)。
    # ⚠ 折叠后的 it["store"] 只是**任意一家**(同 sku 多店时后写覆盖先写),
    #   只准用于溯源列,**不得当查询键** —— 下面按「该 sku 出现过的全部店」反查。
```
② 155-157 行改成两段式(155-156 行那条「2026-08-11 生产实证 2,702 个 C/E 品牌 0 命中」的注释**保留**,前面补一句「登记簿反查是主路,形态提取只是存量兜底」):
```python
        stores_of: dict[str, list] = {}
        for it in items:
            if it.get("category") in BRAND_CATEGORIES:
                stores_of.setdefault(it["sku"], []).append(it.get("store"))
        for sku in stores_of:                      # 定序:与 items 顺序无关
            stores_of[sku] = sorted(set(stores_of[sku]),
                                    key=lambda s: (s is None, s or ""))
        resolved = sku_asin.resolve_many(
            conn, [(st, sku) for sku in todo for st in stores_of.get(sku, [])])
        asin_of = {sku: next((resolved[(st, sku)] for st in stores_of.get(sku, [])
                              if (st, sku) in resolved), None) or sku
                   for sku in todo}
```
③ 161 行 `brand_of.get(asin_of[sku])`、171 行 `asin_of[sku]`(溯源列)一字不改 —— 它们读的就是这个字典。

**为什么**:sku_plan §3.3 的 `services/blacklist.py:157` 一条:品牌收集按 `catalog.products.asin` 查,键错 ⇒ 0 命中 ⇒ 每轮空转、品牌黑名单永远长不出来,而摘要里只会显示 `no_brand` 这个看起来很正常的数字。这正是 2026-08-11 那次事故的同一个形状。

**采纳审查意见(第三位审查者 minor)**:原稿写 `resolve_many(conn, [(it.get("store"), sku) for sku, it in todo.items()])`,而 144 行 `cands = {it["sku"]: it}` 已经把同 sku 多店折叠成任意一家 —— 查登记簿用的是「谁活下来」,结果依赖 items 顺序。存量上两家店的 amz 行 source_key 都等于 sku,结果碰巧相同,但那是巧合,守门测试也钉不住。改成按该 sku 出现过的**全部店**排序后逐个试、取第一个解出的:与顺序无关。跨店给出不同 source_key 在现实里不可能(存量 source_key==sku;批次 2 之后 sku 全局唯一 ⇒ 至多一家店),所以「取第一个」不会掩盖矛盾。

**测试**:
- tests/test_blacklist.py::test_collect_brands_looks_up_products_by_cleaned_asin(既有 177-184 行;断言不变,跑一遍确认夹具够用)
- tests/test_blacklist.py::test_collect_brands_uses_registry_source_key(新;monkeypatch resolve_many 给出与形态不同的值)
- tests/test_blacklist.py::test_collect_brands_dedupe_key_is_still_the_sku(新;守门 D-0b-3,断言 conn.marked 里存的是 sku 原文)
- tests/test_blacklist.py::test_collect_brands_resolution_does_not_depend_on_which_store_survived_the_collapse(新;同一 sku 在 T1 登记为 amz、T2 未登记,items 两种顺序都必须得到同一个 brand 键)

**验收**:python -m pytest -q tests/test_blacklist.py tests/test_brand_scrape.py

#### 0b-14 · `services/blacklist.py` · 205-215(_LATEST_CTE 及其上方 205-206 行注释)、243-249(backfill_counts)

**改动**:**原工作包完全漏了这一处**(sku_plan §3.3 也没列),补:
① 205-215 行 `_LATEST_CTE` 改成经登记簿取键,文本定死为:
```
-- 身份 = coalesce(登记簿 source_key, asin, sku):登记簿是切码后的唯一通路,
-- 其次是 record_many 清洗出的标准码,再不行才用订货号原文兜底(口径与
-- record_asins 一致,见 D-0b-1)。多个订货号(不同店同一产品)归并到同一
-- asin,最新类别看**产品级**全局最新。
-- ⚠ LEFT JOIN 且限 source_type='amz':非 amz 行的 source_key 是 GTIN/offer_id,
--   拿它当 ASIN 黑名单键是错的类型(与 0a 全部收口点的身份表达式同形)。
_LATEST_CTE = """
WITH ev AS (
    SELECT e.*, coalesce(ls.source_key, e.asin, e.sku) AS ident
    FROM catalog.product_events e
    LEFT JOIN catalog.listing_sources ls
           ON ls.store = e.store AND ls.sku = e.sku AND ls.source_type = 'amz'
    WHERE e.event = 'problem_categorized'),
latest AS (
    SELECT DISTINCT ON (ident) ident AS asin,
           sku, store, occurred_at,
           detail->>'category' AS cat, detail->>'reason' AS reason
    FROM ev
    ORDER BY ident, occurred_at DESC)
"""
```
(`_BACKFILL_COUNT_SQL`:217、`_BACKFILL_ASIN_SQL`:224、`_CHANNEL_COUNT_SQL`:298 三个消费方都拼在 `_LATEST_CTE` 之后,列名 `asin/sku/store/occurred_at/cat/reason` 一个不少、语义不变,**它们的 SQL 文本一个字不改**。)
② `backfill_counts`(243-249)的计数 SQL 加一档**只读**告警计数,返回 dict 加 `opaque`:
```
SELECT ..., count(*) FILTER (WHERE asin ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$'
                              AND asin ~ '[A-Z]') AS opaque
```
字符类**不许手写字面量**:用 f-string 从 `services.sku_codec` 的字母表常量拼进来(与 0a-24 把事件码常量拼进 SQL 同款纪律)。`rebuild_asin_blacklist`(260-267)与 `backfill_from_events`(252-257)的写路径**一字不改**。
③ workflows/blacklist_push.py 的两处预览摘要(121-124、156-159)在 `c['opaque']` 非空时各追加一句:「⚠ 其中 N 个键形如不透明码(登记簿查不到 ⇒ 拦不住任何东西),见 D-0b-1」。零时不追加 ⇒ 摘要逐字相同。

**为什么**:**采纳审查意见(第一位审查者 missing 项,已实测证实)**:`_LATEST_CTE`(:209)按 `coalesce(asin, sku)` 从时间线取键,`_BACKFILL_ASIN_SQL`(:224-237)把它 INSERT 进 `catalog.asin_blacklist`(asin 是唯一键),`rebuild_asin_blacklist`(:260-267)先 `DELETE` 再整表重灌。切码后凡是 `product_events.asin` 为空的行,coalesce 会回落成 12 位不透明码 ⇒ **随机码被写成黑名单键**,与 §3.3 点名的三大最危险失效之一「黑名单键被灌随机码 ⇒ list_new 的黑名单闸拦不住违禁品」完全同形,而且 rebuild 是一次性把好键换成坏键。0b-12 只收口了实时那条腿,回填/重建这条腿不收口等于白改。

零行为变化论证:存量 amz 行 source_key==sku ⇒ coalesce 第一项就等于今天的第二/三项;非 amz 行 JOIN 不上(或 source_key IS NULL)⇒ 回落 e.asin/e.sku,与今天逐字相同。唯一会不同的是体检⑤那批 source_key ≠ sku 的行 —— 它是合并硬闸(必须为 0)。`opaque` 只计数不过滤:过滤会丢行,那是 D-0b-1 要拍板的口径,不混进零行为变化批次。

**测试**:
- tests/test_blacklist.py::test_latest_cte_takes_the_key_from_the_registry_first(新;SQL 文本断言 `LEFT JOIN catalog.listing_sources`、`source_type = 'amz'`、`coalesce(ls.source_key, e.asin, e.sku)` 三者在文本里)
- tests/test_blacklist.py::test_latest_cte_consumers_are_unchanged(新;`_BACKFILL_ASIN_SQL` 去掉 _LATEST_CTE 前缀后的文本、`_BACKFILL_COUNT_SQL` 同法、`_CHANNEL_COUNT_SQL` 同法,三者逐字与改前相同——把改前文本硬编码进测试)
- tests/test_blacklist.py::test_opaque_key_alphabet_comes_from_sku_codec(新;断言 `_BACKFILL_COUNT_SQL` 里的字符类 == sku_codec 的字母表常量,不是手写字面量)
- tests/test_blacklist_push.py::test_backfill_preview_is_byte_identical_when_no_opaque_keys(新;opaque=0 时预览文案不含 ⚠)

**验收**:python -m pytest -q tests/test_blacklist.py tests/test_blacklist_push.py;python cli.py blacklist_push -p backfill=1(预览,不加 apply,零写入;`总数/永久禁止` 两个数必须与改前一致)

#### 0b-15 · `services/feed_track.py` · 175-180(违禁回执反哺黑名单的注释)

**改动**:**不改代码逻辑,只改注释**:179-180 行「只收 kind=list(sku=asin 约定);跟卖 sku 是自编号提不出 ASIN,其行内终态由跟卖表 F/J 列承担」改为:
```python
        # 只收 kind=list(MP_ITEM)。黑名单键由 blacklist.record_asins 经登记簿
        # 按 (店,sku) 反查 —— 本函数只负责把 store + sku 原样递过去,
        # **不许在这里自己解 ASIN**(那就是第二份规则,conventions §六)。
        # 跟卖走 MP_ITEM_MATCH ⇒ kind=match ⇒ 天然不进这个桶,
        # 其行内终态由跟卖表 F/J 列承担。
```
181-188 行的 `prohibited` 列表推导保持原样(实测它已经在传 `store` 与 `sku`)。

**为什么**:sku_plan §3.3 把 `services/feed_track.py:179-190` 列为「违禁回执反哺黑名单写错键 → 传 source_key」。但正确的收口点是 `blacklist.record_asins`(0b-12)而不是这里:在 feed_track 再解一次 ASIN 会造出第二条实现路径。这里唯一要做的是把「sku=asin 约定」这句已经作废的注释改掉 —— 留着它,下一个读代码的人会以为这里可以直接拿 sku 当 ASIN 用。

**测试**:
- tests/test_feed_track.py::test_prohibited_receipt_flows_into_blacklist(既有 299 行,不变,必须仍绿:它 monkeypatch 了 record_asins,断言递过去的是 store+sku 原文)
- tests/test_sku_guard.py::test_feed_track_does_not_resolve_asin_itself(守门;services/feed_track.py 源码里不含 resolve_many / extract_asin / listing_sources)

**验收**:python -m pytest -q tests/test_feed_track.py

#### 0b-16 · `services/order_audit.py` · 29-38(import 区)、68-72(ASIN_RE 常量区,在其下方新增函数)

**改动**:① 38 行之后补 `from services import sku_asin`(实测本文件目前**没有**导入它)。
② 68-72 行 `ASIN_RE` 的注释末尾补一句:「⚠ 它判的是**解出来的 asin** 像不像 ASIN,不是判 sku 的形态 —— 切码后 sku 永远不像 ASIN。」
③ 72 行之后新增订单链取 ASIN 的**唯一**入口:
```python
def line_asin(line: dict) -> str:
    """输入:订单行(orders.order_lines 列名)→ 输出:该行的源头 ASIN(大写;取不出返空串)。

    订单链一律以 `orders.order_lines.asin` 为准 —— 登记簿那一跳发生在**写入侧**
    (order_lines.upsert_order_lines / order_asin_normalize),判定侧不查库、不认
    SKU 形态。`asin` 为 NULL 的存量行才回落形态提取,那是兜底不是主路。
    """
    return (str(line.get("asin") or "").strip().upper()
            or sku_asin.extract_asin(line.get("sku") or "") or "")
```

**为什么**:sku_plan §3.3 把 `services/order_audit.py:358-361` 标成「直接正则 `^B[0-9A-Z]{9}$`,不调 extract_asin,最容易漏」,后果是「新品每一单判『待人工』,订单审核链事实停摆」。而 `judge` 是纯函数(无连接),不能查登记簿 —— 正确解法是登记簿那一跳在写入侧已经落进 `order_lines.asin`,判定侧只读列(实测 workflows/order_audit.py:183 的 `_PICK_SQL` 与 :195 的 `_ONE_SQL` 都已经 SELECT 了 asin,行 dict 里有这个键)。表达式与 `workflows/order_audit.py:1238-1239` 现有的 `_phish_record` 写法逐字相同,所以这个函数同时收编了那一处(0b-21),四个调用点从此共用一份规则。

**测试**:
- tests/test_order_audit.py::test_line_asin_prefers_the_column_then_falls_back_to_shape(新)
- tests/test_sku_guard.py::test_order_chain_derives_the_asin_in_exactly_one_place(守门;`^B[0-9A-Z]{9}$` 在订单链里只准出现在 services/order_audit.py,workflows/order_audit.py 不许出现 extract_asin)

**验收**:python -m pytest -q tests/test_order_audit.py -k line_asin

#### 0b-17 · `services/order_audit.py` · 358-361(judge 的形态闸)

**改动**:```python
    asin = line_asin(line)
    if not ASIN_RE.fullmatch(asin):
        return AuditResult(MANUAL, f"SKU「{line.get('sku') or '空'}」解不出 ASIN"
                                   f"(登记簿无记录且形态不符),采集器不受理,"
                                   f"需人工核对", detail)
```
措辞变化(展示的东西从「大写化后的 sku」换成「原样 sku」并说清为什么)是有意的:切码后运营看到的是 12 位随机串,写「不是 ASIN 形态」等于没说,得告诉他「登记簿里没这条」。**行为上唯一的变化**是 asin 的来源(见 D-0b-2);`ASIN_RE` 这道闸本身、`MANUAL` 结论、`rescrape=False`(不进待采清单)三条全部不变。

⚠ 本条是**本批次唯一的判定口径变化**,按 estimated_pr_split 单独成一个提交,PR 描述里必须贴体检①②的输出。

**为什么**:同 0b-16。这道闸的存在理由(采集侧建任务时就丢弃非 ASIN,推了也白推,行会永远挂着等一个不会来的快照)在切码后一字不变,变的只是「怎么拿到那个 ASIN」。三位审查者对本条无异议,第一位提醒「它藏在一个叫零行为变化的批次里,最容易被评审一眼放过」——故升格为独立提交 + 硬性体检前置。

**测试**:
- tests/test_order_audit.py::test_judge_non_asin_sku_is_not_called_pending_scrape(既有 1334-1339 行;note 断言从 `"ASIN 形态" in res.note` 改成 `"解不出 ASIN" in res.note`,status/rescrape 两条不改)
- tests/test_order_audit.py::test_judge_uses_the_asin_column_when_sku_is_opaque(新;sku=12 位不透明码 + asin=B0XXXXXXXX ⇒ 正常进入判定链而不是待人工)
- tests/test_order_audit.py::test_judge_every_branch_returns_a_verdict(既有 1342 行,不变,必须仍绿——LINE 夹具无 asin 键,line_asin 回落 extract_asin("B0TEST0001") ⇒ 全部既有 judge 用例零改动)

**验收**:python -m pytest -q tests/test_order_audit.py;体检①②(见 acceptance_commands)必须先跑并贴给所有者

#### 0b-18 · `workflows/order_audit.py` · 423(_snapshots)

**改动**:`asins = sorted({(r["sku"] or "").strip().upper() for r in lines if r.get("sku")})` 改为 `asins = sorted({a for a in (rules.line_asin(r) for r in lines) if a})`。函数 docstring(409-422 行)不改。

**为什么**:快照按 `catalog.latest_snapshot.asin` 存,拿不透明码去查恒空 ⇒ 每一行都「无快照」⇒ 全判待人工 + 每轮为它们烧一次采集配额。必须与 judge 用同一个 `line_asin`,否则 `snaps` 的键与 judge 算出的 asin 对不上,会出现「快照取回来了但判定说没有」的静默错位。

**测试**:
- tests/test_order_audit.py::test_snapshots_keyed_by_line_asin(新;sku 与 asin 不同的行,快照按 asin 落位)
- tests/test_order_audit.py::test_snapshots_picks_newest_when_params_differ(既有 887 行)与 test_snapshot_query_gates_on_freshness(既有 880 行)必须仍绿

**验收**:python -m pytest -q tests/test_order_audit.py

#### 0b-19 · `workflows/order_audit.py` · 461-463(_scrape_fails)

**改动**:```python
    pairs = {(rules.line_asin(r), rules.norm_zip(r.get("postal_code")))
             for r in lines}
    pairs = {(a, z) for a, z in pairs if a and z}
```
(463 行原样保留,它已经在滤空。)

**为什么**:`ops.audit_scrape` 的键是 (asin, zip),拿不透明码查恒空 ⇒ `scrape_fail` 永远是 None ⇒ 「重采也没用」那类失败(variant_offset / parse_error)拿不到终局结论,行永远挂「待采集」等一个不会来的快照,每轮还烧一次配额。这正是文件里 458-459 行注释在防的那个形状。

**测试**:
- tests/test_order_audit.py::test_scrape_fails_keyed_by_line_asin(新)
- tests/test_order_audit.py::test_unretryable_scrape_failure_is_terminal_reject(既有 1499 行)与 test_every_unretryable_type_is_terminal(既有 1523 行)必须仍绿

**验收**:python -m pytest -q tests/test_order_audit.py

#### 0b-20 · `workflows/order_audit.py` · 479(_judge_all 主循环取 asin)

**改动**:479 行 `asin = (line.get("sku") or "").strip().upper()` 改 `asin = rules.line_asin(line)`;480-483 行(norm_zip / snaps.get / judge / fails.get)一字不改;488 行 `if res.rescrape and asin and zip5: want.append((asin, zip5))` 一字不改 —— 它推给采集器的从此是真 ASIN。

**为什么**:三处(_snapshots / _scrape_fails / _judge_all)必须同源,否则 want 清单里会混进不透明码,`_push_scrape` 推过去采集器直接丢弃,而摘要里的待采数还照报 —— 静默卡死的典型形状。

**测试**:
- tests/test_order_audit.py::test_want_list_carries_real_asins(新;sku 为不透明码的行,待采清单里是 asin 不是码)
- tests/test_order_audit.py::test_run_pushes_scrape_for_missing_snapshot(既有 680 行)、test_push_scrape_splits_same_asin_into_waves(既有 1155 行)必须仍绿

**验收**:python -m pytest -q tests/test_order_audit.py

#### 0b-21 · `workflows/order_audit.py` · 140-143(import)、1237-1240(_phish_record 的 asin_of)

**改动**:① 1237-1240 行改为 `asin_of = {c["order_line_id"]: rules.line_asin(c) for c in cands}`(与原表达式逐字等价,只是收编到唯一出处)。
② 142 行 import 列表里删掉 `sku_asin`(实测全文件只有 1239 行一处引用,被 ① 干掉;动手前再 `grep -n sku_asin workflows/order_audit.py` 确认)。
③ `_phish_cands`(1184-1190)与 `_phish_selfheal_cands`(1212-1216)产出的候选行里 `store`/`sku`/`asin` 三个键保持不变 —— `line_asin` 只用到 asin 与 sku 两个键。

**为什么**:sku_plan §3.3 的第 14 处。收编到 `rules.line_asin` 之后,「订单行怎么算出 ASIN」这件事在整条链上只有一份实现,守门测试(0b-16 的第二条)才钉得住;顺手把工作流对 `services/sku_asin` 的直接依赖去掉 —— 依赖只准自上而下,工作流该调的是 order_audit 这块业务积木,不是形态规则积木。

**测试**:
- tests/test_store_events_phishing.py 全部 _phish_record 相关用例必须仍绿(**原工作包把它们写在 tests/test_order_audit.py 里,是错的**:实测 `_phish_record` / `_phish_cands` 的测试在 tests/test_store_events_phishing.py:140-360)
- tests/test_store_events_phishing.py::test_asin_missing_records_origin_store_only(既有 166 行;输入是 `_line(asin=None, sku="裸订货号")` ⇒ line_asin 回落 extract_asin 返 "" ⇒ asin_missing 仍为 True,断言不改)
- tests/test_store_events_phishing.py::test_phish_record_uses_line_asin(新)

**验收**:python -m pytest -q tests/test_order_audit.py tests/test_store_events_phishing.py tests/test_store_events_tro.py

#### 0b-22 · `workflows/sources_backfill.py` · 16-19 与 21-26(模块 docstring)、34-38(import)、66-67(分桶)、71-80(摘要)、81-86(dry-run 返回)、87-94(写入)

**改动**:① 38 行之后补 `from services import sku_codec`(**不新增任何形态判据常量** —— 不透明码字母表/长度/`is_opaque` 的唯一之家是 0a 交付的 services/sku_codec,见 depends_on 与 risks 第 3 条)。
② 66-67 行两桶改三桶,**写路由一字不改**:
```python
        amz = [(s, k) for s, k in gap if _ASIN_RE.fullmatch(k or "")]
        rest = [(s, k) for s, k in gap if not _ASIN_RE.fullmatch(k or "")]
        # 旧格式存量(三段式/纯数字/跟卖号/人工号)vs 疑似新码漏登记 ——
        # 后者才是报警:不透明码只能由 sku_codec.mint 在同一事务里发+登记,
        # 在架却查不到登记行 = 「谁上架谁登记」被绕过了(或 mint 写库回滚过)
        orphan = [(s, k) for s, k in rest if sku_codec.is_opaque(k or "")]
        legacy = [(s, k) for s, k in rest if not sku_codec.is_opaque(k or "")]
```
③ 73-74 行摘要首行括号内文案改成 `格式像 ASIN(登记 amz){len(amz)},旧格式存量(登记 unknown,不自动维护){len(legacy)}`(既有断言 `"unknown,不自动维护)1"` 仍是子串,保持绿)。
④ **只在 `orphan` 非空时**追加一行,且**插在抬头行之后**(`lines.insert(1, ...)`,不是 `insert(0, ...)`):
```python
        if orphan:
            lines.insert(1, f"{mode}⚠ 疑似新码漏登记 {len(orphan)} 行 —— "
                            f"不透明码只能由 sku_codec.mint 发码即登记,"
                            f"在架却无登记行说明有人绕过了上架主链或 mint 事务回滚过;"
                            f"样本={[k for _s, k in orphan[:5]]}")
```
⑤ 79-80 行的 unknown 样本行改成从 `legacy` 取样(别把报警样本重复报两遍)。
⑥ 87-94 行 `listing_sources.register` 的入参**一字不改**(仍按 `_ASIN_RE` 二分 amz/unknown):orphan 也登记成 unknown 是对的 —— 它们的真实来源查不出来,`unknown` 的语义就是「不参与任何自动破坏动作,等人工归类」。
⑦ 模块 docstring 16-19 行「摘要长期应为 0 行 —— 非零本身就是报警」那段改成两句:旧格式存量非零 = 旧系统还在产出(已知噪声,不报警);新码桶非零 = 上架主链被绕过(真报警)。21-26 行「规则」那段第一条末尾的「sku=asin 约定」加一句「(仅对存量成立;新码由 mint 登记,不走本工作流)」。

**为什么**:sku_plan §3.3 的 `workflows/sources_backfill.py:46/66/90` 一条:切码后所有新 SKU 都会落进 `_ASIN_RE` 不匹配的那一桶,「非零即报警」这条语义当场作废 —— 而这条语义正是「谁上架谁登记」被绕过时唯一的告警线。分桶把它救回来。写路由不动是因为本批次要零行为变化,而且 unknown 对 orphan 码本来就是正确归类。

**采纳审查意见(第二、四位审查者 blocker「不透明码字母表被登记四份」)**:原稿 0b-03 要在 services/sku_asin 新建 `OPAQUE_ALPHABET`/`OPAQUE_LEN`/`is_opaque_form`,与 0a 的 registry 常量、0a-07 的 `sku_codec.is_opaque`、横切包的 `sku_codec._ALPHABET` 四份并存,两条守门断言互斥。本批次**撤回** 0b-03 的常量与函数,直接调 sku_codec —— services→services 合法,且字母表只有一份。
**采纳审查意见(第四位审查者 minor)**:原稿用 `lines.insert(0, ...)` 会把 ⚠ 行顶到 `🧪 [DRY-RUN]` 抬头行**前面**,而 cli 的链通知只取首行(`notify_fmt.first_line_of`)⇒ 一条 dry-run 的告警会以真跑的面目出现在飞书里。改成 `insert(1, ...)` 并把 `mode` 前缀拼进告警行。

**测试**:
- tests/test_sources_backfill.py::test_dry_run_reports_blind_spot_without_writing(既有 30-39 行;摘要文案「其余」→「旧格式存量」,`"unknown,不自动维护)1"` 与 `"MANUAL-001"` 两条断言不改)
- tests/test_sources_backfill.py::test_execute_routes_by_sku_format(既有 42-55 行,不变,必须仍绿 —— 写路由一字未改)
- tests/test_sources_backfill.py::test_opaque_code_without_registry_row_is_alarmed(新;gap 含一个 12 位不透明码 ⇒ 第 2 行以 ⚠ 开头且含样本)
- tests/test_sources_backfill.py::test_dry_run_alarm_keeps_the_dry_run_banner_first(新;dry-run 下首行仍以 🧪 开头,⚠ 行在第 2 行且自带 🧪 前缀)
- tests/test_sources_backfill.py::test_summary_is_byte_identical_when_no_opaque_codes(新;守门零行为变化 —— 只有旧格式时摘要不含 ⚠ 行)
- tests/test_sources_backfill.py::test_opaque_codes_are_still_registered_as_unknown(新;报警不等于不登记)

**验收**:python -m pytest -q tests/test_sources_backfill.py;python cli.py sources_backfill --dry-run   # 一行不写;摘要里不得出现 ⚠(生产库里今天不该有不透明码)

#### 0b-23 · `tests/test_sku_guard.py` · 白名单区(0a 建的那份;若 0a 未建则由本批次按同名新建)

**改动**:**只增删这一份文件的白名单条目与断言,不新建第二份守门文件。** 本批次要加的七条(实现手法统一「读源码文本 + 断言子串」,与 tests/test_feishu_guard.py:42-95 的白名单 dict + 「白名单不许烂掉」用例同款,不引入 AST 依赖):
① `test_registry_hop_lives_in_services_only` —— workflows/ 下任何 .py 不许出现 `catalog.listing_sources` 或 `resolve_many`(白名单:workflows/sources_backfill.py 的 `_SQL_GAP`,理由「它就是登记簿的补给线本身」)。
② `test_order_chain_derives_the_asin_in_exactly_one_place` —— `^B[0-9A-Z]{9}$` 在订单链里只准出现在 services/order_audit.py;workflows/order_audit.py 不许出现 `extract_asin`。
③ `test_both_cleaners_fill_sql_carry_store` —— 两条 `_FILL_SQL` 都必须含 `store` 与 `IS NOT DISTINCT FROM`。
④ `test_extract_asin_callsite_whitelist` —— 白名单**逐字钉死**;0b 从中**删掉** services/order_lines.py:169、services/blacklist.py:99/157、workflows/order_audit.py,**加入** services/order_audit.py(`line_asin` 的兜底腿)与 services/order_lines.py(`_fill_asins` 不用它、但 27 行 import 还在,按文件粒度登记),并把 services/product_events.py 的理由改成「**仅 store 为空的平台级事件分支**(product_ingest / audit_store.event_row / product_audit:617;audit_history_fold 直插 SQL 不经本函数)」。
⑤ `test_legacy_shapes_resolve_identically` —— 裸 ASIN / 三段式 / 纯数字倒查 / PHUMWMT 四种存量形态,经 `resolve_many`(登记簿为空时)与 `extract_asin` 输出逐字相同。**这是本批次「零行为变化」的机器证明,必须在第一个提交里就写。**
⑥ `test_feed_track_does_not_resolve_asin_itself` —— services/feed_track.py 源码不含 resolve_many / extract_asin / listing_sources。
⑦ `test_only_cleaner_workflows_call_resolve_pairs` —— 见 0b-03。
每条测试函数 docstring 首行写「钉的是哪条规矩 + 违反了会怎么静默出事」(「输入→输出」对守门测试不适用)。

**为什么**:**采纳审查意见(四位审查者一致的 blocker「同一批守门断言被拆进四个新建测试文件」)**:0a-26 的 tests/test_sku_identity_guard.py、0b 原稿的 tests/test_sku_asin_consumers.py、B2-21 的 tests/test_sku_codec_guard.py、横切 C0-GUARD-1 的 tests/test_sku_guard.py 四份并存,`extract_asin` 白名单出现 3 次、`abandoned_at IS NULL` 白名单 3-4 种口径,白名单本身就成了双轨 —— 守门测试自己犯了它要守的规矩(conventions §六),而且已经开始互相判红。本批次**撤回** new_modules 里的 tests/test_sku_asin_consumers.py,统一到 **tests/test_sku_guard.py**(名字与仓内既有 tests/test_feishu_guard.py 同族,四位审查者里两位明确推荐这个名字)。白名单是会随批次缩短的清单,只有维护在一处才不会烂。

**测试**:
- tests/test_sku_guard.py::test_the_whitelists_do_not_rot(新;白名单里登记的文件/函数/常量必须真实存在,照抄 tests/test_feishu_guard.py 的同名用例)
- 上列 ①~⑦ 七条本身即测试

**验收**:python -m pytest -q tests/test_sku_guard.py

#### 0b-24 · `registry/resources.py` · 294-311(ORDER_SALES)与 330-349(ORDER_RETURNS)

**改动**:① ORDER_SALES 的 `_fields(...)` 在 300 行 `sku="SKU"` 之后紧接着加 `asin="ASIN"`。② ORDER_RETURNS 的 `_fields(...)` 在 339 行 `sku="SKU"` 之后加 `asin="ASIN"`。③ 两处各加一行行内注释:`# asin=源头 ASIN,order_lines.asin 投影(登记簿反查后新旧码都有值);列由所有者在飞书建,建列前本条目不许进载荷(见 D-0b-4)`。**不许在任何业务代码里写字面量 "ASIN"**(CLAUDE.md:飞书字段名只准引用 registry 字段常量)。位置紧挨 sku 只是给读代码的人看的,飞书侧列序由所有者建列时决定,程序不管列序。

**为什么**:sku_plan §1「问 3」第三段所有者已拍板:订单表加 ASIN 列。切码后飞书销售/售后订单表只有 12 位随机串,运营再也认不出是哪个产品 —— 这是切换后运营侧最先撞上的墙。registry 登记是铁律 3。
**采纳审查意见(第一位审查者 major「registry 四处重复排 item」)**:横切包的 C0-REG-2 与本条做同一件事,处置为**只留本条**,横切 C0-REG-2 删除;本条与它的消费方(0b-25/0b-26)锁在同一个 PR(见 estimated_pr_split)。

**测试**:
- tests/test_order_center_push.py::test_sales_registry_has_asin_field(新;`resources.ORDER_SALES.fields.asin == "ASIN"`)
- tests/test_order_center_push.py::test_returns_registry_has_asin_field(新)

**验收**:python -m pytest -q tests/test_order_center_push.py

#### 0b-25 · `services/order_center.py` · 50-64(_SALES_SQL)、66-76(_RETURNS_SQL)

**改动**:① `_SALES_SQL` 的 51 行 `SELECT order_line_id, store, po_id, line_number, sku, product_name, qty,` 改 `SELECT order_line_id, store, po_id, line_number, sku, asin, product_name, qty,`。WHERE 子句(58-63)与那两条注释一字不改。
② `_RETURNS_SQL` 的 72 行 `r.refunded_qty, r.carrier, r.tracking_no, l.order_date` 改 `r.refunded_qty, r.carrier, r.tracking_no, l.order_date, l.asin`(`orders.return_lines` 没有 asin 列,SQL 已在 74 行 `LEFT JOIN orders.order_lines l USING (order_line_id)`,顺手取即可);在 66 行 `_RETURNS_SQL = """` 上方加一行注释:`# asin 从 order_lines 借(return_lines 没这一列;订单行滚出库或孤儿退货时为 NULL,飞书那格空着 —— 不猜)`。

**为什么**:sku_plan §3.5 飞书表一节明列的两条。售后表不加自己的 asin 列是有意的(D-0b-6):退货行的身份锚点是 order_line_id,再存一份 asin 就成了两份真值,订单侧改码后两边会飘。

**测试**:
- tests/test_order_center_push.py::test_sales_sql_selects_asin(新;SQL 文本断言)
- tests/test_order_center_push.py::test_returns_sql_borrows_asin_from_order_lines(新;断言 `l.asin` 在文本里且 JOIN 仍是 LEFT JOIN)

**验收**:python -m pytest -q tests/test_order_center_push.py

#### 0b-26 · `services/order_center.py` · 358-390(push_sales 的 desired 载荷,重点 370)、393-422(push_returns,重点 405)

**改动**:① `push_sales`:370 行 `f.line_number: r["line_number"], f.sku: r["sku"],` 之后加 `f.asin: r["asin"],`。② `push_returns`:405 行 `f.sku: r["sku"],` 之后加 `f.asin: r["asin"],`。**None 必须留在载荷里**(不要写 `if r["asin"]`):order_center 的既有约定是「省略 = 飞书保留旧值,送 null 才是清空」,tests/test_order_center_push.py:78-79 那条断言钉着它。
⚠ 本条与 0b-24/0b-25 一起,只在所有者**建完两张表的「ASIN」文本列之后**才合(D-0b-4)。

**为什么**:同 0b-25。载荷里 None 也要在,否则一个曾经有 ASIN、后来订单行滚出窗口的售后行会永远留着一个过时的 ASIN。
**采纳审查意见(第三位审查者 minor「_adapt_rows 每轮刷 WARNING」)**:实测 services/order_center.py:180-193,列不在表里时 `_adapt_rows` 每轮打一条 `logger.warning("表「%s」以下列不写入:%s")`。原稿 D-0b-4「代码先合、所有者后建列」会让这条 WARNING 常态化直到建列 —— 而它正是「列名建错(小写 asin / ASIN码)」唯一的发现渠道,常态化就等于脱敏。改成:整组飞书改动推到第二个 PR,建列后才合,窗口期为零。

**测试**:
- tests/test_order_center_push.py::test_push_sales_row_shape_and_no_delete(既有 65-81 行;`_SALES_ROW` 夹具(49-62 行)加 `"asin": "B0AAAAAAA1"`;新增断言 `d[F_SALES.asin] == "B0AAAAAAA1"`)
- tests/test_order_center_push.py::test_push_returns_key_is_rma_plus_line(既有 84-100 行;row 的 None 字典(86-91 行)里加 "asin")
- tests/test_order_center_push.py::test_asin_is_sent_even_when_null(新;显式钉「None 也进载荷」)
- tests/test_order_center_push.py::test_missing_asin_column_is_skipped_not_written(新;打桩 list_fields 不返回 ASIN,断言 adapted 载荷里没有该键、指纹与不含 asin 的载荷一致 —— 这是「建列前零重推」的机器证明)

**验收**:python -m pytest -q tests/test_order_center_push.py;所有者建完列后跑 `python cli.py order_center_push -p table=sales -p days=90`,摘要应是「更新 ≈N」(一次性全量重推,预告不是故障);第二次跑必须回到「跳过 N」;售后表同款两步

#### 0b-27 · `registry/resources.py` · 253-266(ONLINE_PRODUCTS_SHEET,columns 在 262-265)

**改动**:`columns` 元组末尾追加 `"source_key"`(共 17 项,A~Q)。在 255 行注释「列序 = catalog.walmart_items 的字段序,改列序必须两处同步」后面补两句:`# 2026-09-xx 起末尾多一个投影列 source_key(catalog.listing_sources.source_key,` / `# LEFT JOIN 取,未登记行空),它不是 walmart_items 的字段;`「追加在末尾是硬要求 —— 电子表格按 range 坐标写,插中间全体错位」。

**为什么**:sku_plan §1「问 4」与 §3.5:切码后在线产品总表只剩 12 位随机串,运营与排查都要能反向对到 ASIN。追加末尾而不是插在 sku 旁边,是 listing_sheet 那条已经踩过的坑。
**采纳审查意见(第二、四位审查者 major「列名两包冲突」)**:0b 原稿写中文 `"来源码"`、横切 C0-REG-3 写 `"source_key"`,两条断言互斥。实测 workflows/catalog_sync.py:274 `rows = [list(sheet.columns)] + data_rows` —— 这个元组**就是写进飞书的表头文字**,而现有 16 项全是英文标识符(store/sku/itemId/…)。故取 `source_key`(与既有表头同风格、也与 0b-28 的 SQL 列名一致);中文名「来源码」写进 docs/feishu_tables.md 给人读。横切 C0-REG-3 删除,本条与 0b-28 锁同一 PR。

**测试**:
- tests/test_catalog_sync.py::test_projection_columns_match_registry(既有 375-379 行,自动覆盖 —— SQL 列数必须跟着变成 17)
- tests/test_catalog_sync.py::test_online_sheet_last_column_is_source_key(新;`resources.ONLINE_PRODUCTS_SHEET.columns[-1] == "source_key"`,钉「只准追加末尾」)

**验收**:python -m pytest -q tests/test_catalog_sync.py

#### 0b-28 · `services/walmart_catalog.py` · 139-150(_PROJECTION_SQL 及其下方三行注释)

**改动**:```
_PROJECTION_SQL = """
SELECT w.store, w.sku, w.item_id, w.upc, w.gtin, w.product_name, w.shelf,
       w.product_type, w.variant_group_id, w.variant_group_info::text,
       w.price, w.currency, w.avail_qty, w.published_status, w.lifecycle_status,
       w.unpublished_reasons, ls.source_key
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku
WHERE w.missing_since IS NULL
ORDER BY w.store, w.sku
"""
```
**必须 LEFT JOIN**(未登记的在架行照样要进表,只是来源码空着);**不许加 `ls.abandoned_at IS NULL`**(那一列是批次 2 才有的,而且按 synthesis §6 的消费方契约,`abandoned_at IS NULL` 只准出现在 sku_codec.mint / list_new 去重闸 / alloc_push._SQL_ONLINE 三处 —— 投影是展示,已弃码的在架僵尸行更要看得见);**不加 `ls.source_type='amz'` 限定**(跟卖行的 GTIN 也是运营要看的来源信息,与 0a 那些「拿来当 ASIN 比对」的收口点性质不同 —— 这里只展示不比对)。147 行「列序与 registry … 一一对应,改必同步」的注释保留,后面补一句「最后一列 source_key 来自登记簿 LEFT JOIN(不限 source_type:amz=ASIN、match=匹配 GTIN),未登记行为空」。148-150 行三行注释不动。

**为什么**:同 0b-27。全表别名化(w./ls.)是加 JOIN 后的必要动作,否则 `store`/`sku` 歧义。`projection_rows`(153-170)的 `_cell` 对 None 已经返回空串,新列不需要额外处理。既有 `test_projection_columns_match_registry` 用 `_PROJECTION_SQL.split("FROM")[0]` 数逗号,SELECT 段里没有 FROM 字样,改后仍成立(已核)。

**测试**:
- tests/test_catalog_sync.py::test_projection_rows_cell_conversion(既有 309-319 行;夹具行(312-314)加第 17 个元素;新增断言 `rows[0][16]` 为 source_key 或空串;既有的 `rows[0][14]`/`rows[0][15]` 两条不受影响)
- tests/test_catalog_sync.py::test_projection_columns_match_registry(既有;改后必须复跑确认数逗号法仍成立)
- tests/test_catalog_sync.py::test_projection_left_joins_the_registry(新;断言 `LEFT JOIN catalog.listing_sources` 在文本里、`abandoned_at` **不**在文本里)
- tests/test_catalog_sync.py::test_projection_excludes_missing_rows(既有 370-372 行;断言字符串改成 `WHERE w.missing_since IS NULL`)

**验收**:python -m pytest -q tests/test_catalog_sync.py;所有者先在飞书把该工作表列数扩到 ≥17(见 risks),再 `python cli.py catalog_sync -p store=<一家店>` 看表末尾出现 source_key 列且 amz 行有值

#### 0b-29 · `docs/feishu_tables.md` · 61(在线产品总表行)、95-97(4 号「类型要求」)、105-109(销售订单列清单)、127-130(售后订单列清单)

**改动**:① 61 行在线产品总表那格末尾补:「2026-09-xx 加第 17 列 `source_key`(表头即此英文串,与既有 16 列同风格;人读名叫「来源码」= catalog.listing_sources.source_key,amz=ASIN、match=匹配 GTIN,LEFT JOIN 取、未登记行空)。⚠ 程序**不扩列**(sheet_overwrite 只 ensure_rows),所有者须先把该工作表列数扩到 ≥17 再放行 catalog_sync,否则撞 90204 并拖累 product_chain 整链。」
② 105 行销售订单的程序列清单里,「SKU、」之后插入「ASIN、」;并在 109 行段末补一句:「**ASIN 是 2026-09-xx 加的**:程序载荷与 registry 常量在所有者建完列**之后**才合(D-0b-4),所以不存在「表里没列、程序每轮刷 WARNING」的窗口期;合并后第一次 push 会把 90 天窗口全量重推一遍(行指纹含全部程序列,加列 ⇒ 指纹全变),这是预告不是故障,第二次跑就回到「跳过 N」。建议挑非高峰时段建列 —— push 挂在 order_sync 链尾,那一轮会明显变慢(但不会失败,push_after 永不抛错)。」
③ 127 行售后订单清单里「SKU、」之后插入「ASIN、」,并在 130 行段末注明「ASIN 从 order_lines 借(return_lines 无此列),订单行滚出窗口或孤儿退货行为空 —— 与该表 `下单时间` 同一口径」。
④ 95-97 行 4 号「类型要求」那条补一句:「新增的 ASIN 列(销售/售后两表)为**文本**类型。建列前用 `list_fields` 确认表里没有同名人工列 —— 有的话程序一登记就开始覆盖它。」

**为什么**:CLAUDE.md「动了表同步文档」;而且「一次性全量重推 90 天窗口」与「工作表要先扩列」这两件事必须白纸黑字写在契约文档里 —— 不写的话,下一个人看到某天推送量暴涨会当成故障查半天,而扩列那条不写就是 product_chain 整链失败。

**测试**:
- (无)

**验收**:人眼复核;grep -n "ASIN" docs/feishu_tables.md 三处都在;grep -n "source_key" docs/feishu_tables.md 命中 61 行

#### 0b-30 · `docs/db_schema.md` · 287-291(product_events.asin 列注释)、416(orders.order_lines 那一格里的 asin 段)

**改动**:① 287-291 行 asin 列注释改成:「产品源头侧标准码;`record_many` 补填 —— **带 store 的走 catalog.listing_sources 反查(切码后唯一通路)**,store 为空的**平台级事件**按 `services/sku_asin` 形态提取(四个来源:product_ingest / audit_store.event_row / product_audit 补采 / **audit_history_fold 直插 SQL,绕过 record_many、asin 列直填**);两条都提不出存 NULL,消费方 coalesce(asin, sku);存量补洗走 sku_normalize 工作流(其 _FILL_SQL 带 store 维度,并用 IS NOT DISTINCT FROM 兼容 store=NULL 的平台级行)。」
② 416 行那格里「由 `order_asin_normalize` 按 `services/sku_asin` 补填」改成「由 `order_lines.upsert_order_lines` 落库当场经登记簿反查补填(`_fill_asins`,每批一条 SELECT);**纯数字 item_id 形态**由 `order_asin_normalize` 扫尾(那一跳要按 (店,item_id) 查 walmart_items,写入路径上做不了,且带一级按 item_id 全局兜底的反查);**提不出留 NULL**,不许拿 sku 原文当 asin」。
③ 671-681 行 `catalog.asin_blacklist` 那节补一句:「键的推导:实时侧 `blacklist.record_asins`、回填/重建侧 `blacklist._LATEST_CTE`,**两条都经 listing_sources(source_type='amz')反查 source_key**,查不到回落 product_events.asin / 订货号原文并告警计数(2026-09-xx)。」

**为什么**:CLAUDE.md「动了表同步 docs/db_schema.md」。本批次不加列不改表,但改的正是这两列**怎么被填**、以及黑名单键**怎么被推导**的规则 —— 库表文档里写着的填法一旦过时,下一个人会照着过时的说法去改消费方。第 ③ 条是随 0b-14 一起补的(原工作包漏了整处)。

**测试**:
- (无)

**验收**:人眼复核

#### 0b-31 · `docs/sku_plan.md` · 120-132(§3.3 表格中本批次涉及的 9 行)、370-383(§7 批次 0)、471-473(§8「黑名单 or sku 兜底口径」)

**改动**:① §3.3 表格里本批次已闭合的 9 行(120 order_audit.py:358-361、121 workflows/order_audit.py 四处、122 blacklist.py:99、123 blacklist.py:157、124 order_lines.py:169、125 product_events.py:167、129 feed_track.py:179-190、131 sources_backfill.py、132 两条清洗工作流)在「改法」列末尾各加「【0b 已闭合】」。
② §3.3 表格**新增一行**(原表漏列,本批次补上):`| services/blacklist.py:205-215 _LATEST_CTE(回填/重建侧取键) | ASIN 黑名单被整表重灌成随机码键,拦不住任何东西 | 经登记簿 LEFT JOIN 取 coalesce(ls.source_key, e.asin, e.sku)【0b 已闭合,见 0b-14】|`,并把标题「14 处」改成「15 处」。
③ §7「批次 0」370-383 那段的「可以分两个 PR 合:0a…、0b…」改成记录 0b 的实际范围(两个 PR:0b 代码 + 0b-飞书列),并写进本工作包新增/撤回的三件事:新增 `services/sku_asin.resolve_pairs`(两条清洗工作流共用的批量入口,带 store,倒查两级)、`services/order_audit.line_asin`(订单链取 ASIN 的唯一出处);**撤回**「0b 自建不透明码形态判据」(字母表唯一之家是 services/sku_codec,守门测试唯一之家是 tests/test_sku_guard.py)。
④ §8 471-473 行「黑名单 `or sku` 兜底口径」那条后面补「【0b 默认:保持原文兜底,加日志计数 + 回填侧 opaque 计数;见 D-0b-1】」;465-467 行「飞书建列」那条补「【0b:代码分第二个 PR,建完列再合 —— 建列前程序载荷里没有这一列,零 WARNING 零重推】」。

**为什么**:sku_plan 是这次改造的总账;批次做完不回填,下一个批次的执行者会重做一遍已经做过的事(2026-08-14 那轮盘点的教训)。第 ② 条尤其重要:`_LATEST_CTE` 是四份工作包与 sku_plan 都漏掉的一处,不补进总账,下一轮盘点还会漏。

**测试**:
- (无)

**验收**:人眼复核;grep -n "0b 已闭合" docs/sku_plan.md 应命中 10 处

### 新模块

- `tests/test_sku_guard.py(**若 0a 已建则本批次只增删其白名单与断言,绝不新建第二份**)`
  - API:SKU 身份改造的**唯一**守门测试之家(只放跨文件的结构性断言;单文件行为断言仍回各自的 test_*.py)。本批次要在其中登记的七条见 0b-23。文件结构照抄仓内既有 tests/test_feishu_guard.py:33-95 —— 顶部 `ROOT = Path(__file__).resolve().parents[1]`,白名单是模块级 dict(键 = 仓内相对路径或 (路径, 函数名),值 = 理由字符串,**理由里必须写死「哪个批次收口后删这一条」**),外加一条 `test_the_whitelists_do_not_rot`(登记项失效即红)。实现手法统一「读源码文本 + 断言子串」,不引入 AST 依赖。
  - docstring 规则:模块 docstring 首行:「SKU 身份收口的守门测试(唯一一份):登记簿那一跳只准住在 services,形态规则只准有一份。」第二段必须写清三件事:①这些断言是**结构性**的(谁调谁、规则住哪儿),不是行为断言 —— 行为回归在各自的 test_*.py 里;②白名单为什么要逐字钉死(sku_plan §7:防止切换后又长出新洞 —— 静默失效的形态是「不报错、摘要正常、功能悄悄没了」,只有守门测试拦得住);③**为什么只有这一份文件**(2026-09-02 四份工作包各建一份守门文件、白名单口径互相判红,守门测试自己犯了 conventions §六;从此增删条目不新建文件)。每个测试函数 docstring 首行写「钉的是哪条规矩 + 违反了会怎么静默出事」。

### DDL

(无)

### 文档同步

- docs/feishu_tables.md(0b-29:在线产品总表第 17 列 source_key + **所有者须先扩列否则撞 90204 拖累 product_chain**;销售/售后订单列清单加 ASIN + 建列后一次性全量重推 90 天窗口的预告 + 建非高峰时段的建议;4 号类型要求补「ASIN 为文本」与 list_fields 查重名)
- docs/db_schema.md(0b-30:product_events.asin 与 orders.order_lines.asin 两处列注释的填法改成「登记簿优先、形态兜底」,平台级事件四个来源点名含 audit_history_fold;catalog.asin_blacklist 节补「键的两条推导腿都经登记簿」。本批次无 DDL,只改规则描述)
- docs/sku_plan.md(0b-31:§3.3 九行标【0b 已闭合】+ **新增 _LATEST_CTE 一行、14 处改 15 处**;§7 批次 0 记录实际范围、两个新增积木与两处撤回;§8「or sku 兜底口径」与「飞书建列」两条标默认)

### 守门测试

- tests/test_sku_guard.py::test_legacy_shapes_resolve_identically —— 裸 ASIN / 三段式 / 纯数字倒查 / PHUMWMT 四种存量形态,经 resolve_many(登记簿为空时)与 extract_asin 输出逐字相同。**本批次「零行为变化」的机器证明,必须在第一个提交里就写**:若 0a 把 resolve_many 实现成「已登记的非 amz 行返回 None、不回落」,今天登记成 unknown 的三段式在架行会在事件账本/黑名单/订单三处同时把 asin 变成 NULL,三条链一起失明且全部不报错。
- tests/test_sku_guard.py::test_registry_hop_lives_in_services_only —— workflows/ 下任何 .py 不许出现 `catalog.listing_sources` 或 `resolve_many`(白名单只有 sources_backfill._SQL_GAP)。违反的静默后果:清洗/审核工作流各写一份 JOIN,规则扩充时漏改没人调的那份不报错。
- tests/test_sku_guard.py::test_order_chain_derives_the_asin_in_exactly_one_place —— `^B[0-9A-Z]{9}$` 只准出现在 services/order_audit.py;workflows/order_audit.py 不许出现 extract_asin。违反的静默后果:judge 与 _snapshots 各算各的 asin,快照取回来了判定说没有。
- tests/test_sku_guard.py::test_both_cleaners_fill_sql_carry_store —— 两条 _FILL_SQL 都必须含 `store` 与 `IS NOT DISTINCT FROM`。违反的静默后果:A 店的 asin 写到 B 店的行上;或平台级事件(store=NULL)整批漏填。
- tests/test_sku_guard.py::test_extract_asin_callsite_whitelist —— 调用文件集合逐字钉死;0b 删掉 order_lines:169 / blacklist:99,157 / workflows/order_audit,加入 services/order_audit(line_asin 兜底腿),product_events 的理由改成「仅 store 为空的平台级分支」。新增调用点即红。
- tests/test_sku_guard.py::test_feed_track_does_not_resolve_asin_itself —— services/feed_track.py 源码不含 resolve_many / extract_asin / listing_sources。违反的静默后果:黑名单键出现第二条推导路径。
- tests/test_sku_guard.py::test_only_cleaner_workflows_call_resolve_pairs —— workflows/ 里出现 resolve_pairs 的只允许两条清洗工作流(三入口分工的执法点,见 0b-03)。
- tests/test_sku_asin.py::test_bucket_units_are_documented_and_stable —— 四个形态桶按 distinct sku 计:同一 sku 在 3 家店,四档数字与单店时相同。违反的静默后果:摘要里的形态分布悄悄乘了个店铺数,所有者据它决定要不要 apply。
- tests/test_sku_asin.py::test_numeric_itemid_hop_falls_back_to_the_global_lookup —— 订单行在 T2、walmart_items 只有 T1 那行时仍解得出。违反的静默后果:今天补得上的行改后补不上,order_lines.asin 空值率上升而没有任何报错。
- tests/test_blacklist.py::test_latest_cte_consumers_are_unchanged —— _BACKFILL_ASIN_SQL / _BACKFILL_COUNT_SQL / _CHANNEL_COUNT_SQL 去掉 CTE 前缀后的文本逐字与改前相同(改前文本硬编码进测试)。违反的静默后果:改 CTE 时顺手动了写路径,黑名单被重灌成另一副样子。
- tests/test_sources_backfill.py::test_summary_is_byte_identical_when_no_opaque_codes —— 无不透明码时摘要不含 ⚠ 行。
- tests/test_sources_backfill.py::test_dry_run_alarm_keeps_the_dry_run_banner_first —— dry-run 下首行仍以 🧪 开头。违反的静默后果:一条空跑告警以真跑的面目进飞书链通知。
- tests/test_order_center_push.py::test_asin_is_sent_even_when_null —— None 也进载荷(省略 = 飞书保留旧值)。
- tests/test_order_center_push.py::test_missing_asin_column_is_skipped_not_written —— 表里没这列时整列跳过且指纹不变(「零重推」的机器证明)。
- tests/test_blacklist.py::test_collect_brands_dedupe_key_is_still_the_sku —— ops.dedupe 的 key 仍是 sku 原文(D-0b-3 守门)。
- tests/test_blacklist.py::test_collect_brands_resolution_does_not_depend_on_which_store_survived_the_collapse —— items 两种顺序必须得到同一个 brand 键。
- tests/test_order_lines.py::test_upsert_asin_guard_still_coalesces —— COALESCE(EXCLUDED.asin, t.asin) 仍在。违反的静默后果:每轮同步把 order_asin_normalize 填好的值冲回 NULL。
- tests/test_order_lines.py::test_fill_asins_is_one_query_per_batch 与 tests/test_product_events.py::test_record_many_issues_one_lookup_per_call —— 防逐行往返(record_many 最坏调用方 cleanup_history_import 41.7 万行 / 每批 1 万)。
- tests/test_cleanup_history.py::test_record_many_carries_occurred_at(既有,不改)—— 它的夹具只实现了 executemany,没有 execute/fetchall。这条**天然**守着「resolve_many 对空 pairs 不开游标」这条契约:一旦 0a 改成无条件开游标,它立刻红。

### 验收命令

```bash
python -m pytest -q   # 全量必须全绿(本批次触及 13 个既有测试文件 + 1 个守门文件)
```
```bash
python -m pytest -q tests/test_sku_guard.py tests/test_sku_asin.py tests/test_sku_normalize.py tests/test_order_asin_normalize.py tests/test_order_lines.py tests/test_product_events.py tests/test_cleanup_history.py tests/test_blacklist.py tests/test_blacklist_push.py tests/test_feed_track.py tests/test_order_audit.py tests/test_store_events_phishing.py tests/test_order_center_push.py tests/test_catalog_sync.py tests/test_sources_backfill.py   # 本批次靶向集
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) AS 非等号amz行 FROM catalog.listing_sources WHERE source_type='amz' AND source_key IS NOT NULL AND source_key <> sku;"   # 体检⑤ **合并硬闸,必须为 0**。非 0 = refdata/schema.sql:232-233 那条右端无锚点的回填正则造出来的行(B0XXXXXXXX-2 这类被登记成 amz、source_key=left(sku,10)),它们今天 extract_asin 提不出、asin 恒 NULL,改后会被登记簿解出 —— 是修复但**是行为变化**。修数据(改判 unknown)与修正则归 0a,0b 不许在这个数非 0 时合并
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) AS 非规范source_key FROM catalog.listing_sources WHERE source_type='amz' AND source_key IS NOT NULL AND (source_key <> upper(btrim(source_key)) OR source_key !~ '^[A-Z0-9]{10}$' OR source_key !~ '[A-Z]');"   # 体检⑥ **合并硬闸,必须为 0**。运营在上架表 B 列填的小写/带空白 ASIN 会原样进 source_key(services/listing_sheet.read_rows 只 strip 不 upper,workflows/list_new.py:260-263 原样写),而 extract_asin 会 upper 后再校验形态 —— 两条腿口径不同就会给出不同的 asin
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) AS 窗口内总行, count(*) FILTER (WHERE asin IS NOT NULL AND sku <> asin) AS 判定会变, count(*) FILTER (WHERE asin IS NULL) AS 仍无asin FROM orders.order_lines WHERE order_date >= now() - interval '90 days' AND coalesce(sale_status,'') <> 'Cancelled';"   # 体检① D-0b-2 的波及面。「判定会变」= 从『非 ASIN 形态·待人工』转入正常判定的存量行数,**合并 0b-17 前必须贴给所有者**
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT sku, asin, count(*) FROM orders.order_lines WHERE order_date >= now() - interval '90 days' AND asin IS NOT NULL AND sku <> asin GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;"   # 体检② 上一条的样本,人眼确认确实是三段式/纯数字存量而不是脏数据
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) AS 在架未登记 FROM catalog.walmart_items w WHERE w.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls WHERE ls.store=w.store AND ls.sku=w.sku);"   # 体检③ 登记簿覆盖率基线 = 切码前 resolve 会回落到形态提取的行数,先记下来(也是 D-0b-1 的风险面)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT source_type, count(*), count(*) FILTER (WHERE source_key IS NULL) AS 无来源码 FROM catalog.listing_sources GROUP BY 1 ORDER BY 2 DESC;"   # 体检④ amz 行的 source_key 必须几乎全非空,否则 resolve 的主路是空的、全靠兜底撑着
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) AS 跨店同itemid FROM (SELECT item_id FROM catalog.walmart_items WHERE item_id IS NOT NULL GROUP BY item_id HAVING count(DISTINCT store) > 1) t;"   # 体检⑦ 决定 0b-01 两级倒查里「第二级全局兜底」到底救回多少行;非 0 就证明「按 store 收窄 = 少补」不是假设
```
```bash
python cli.py order_asin_normalize   # 预览:零写入。改前改后各跑一次,「可解析 N 个 sku」**不得下降**;形态四档数字必须逐字相同;新增的 registry_differs 必须是 0(非 0 即体检⑤那批行)
```
```bash
python cli.py sku_normalize   # 同上,事件账本侧;同样比对「可解析 N 个 sku」与四档形态数
```
```bash
python cli.py sources_backfill --dry-run   # 一行不写;摘要里不得出现 ⚠(生产库今天不该有不透明码,出现即说明 sku_codec.is_opaque 误判或真有人绕过主链);首行必须仍以 🧪 开头
```
```bash
python cli.py blacklist_push -p backfill=1   # 预览不加 apply,零写入;「时间线共 N 个 ASIN / 永久禁止 M 个」两个数必须与改前一致(0b-14 的零行为变化验收)
```
```bash
python cli.py order_audit -p line=<一条 sku=asin 的存量行 order_line_id> -p scrape=0 -p push=0 -p wait=0   # 单行复核 D-0b-2 第一步:结论必须一字不变
```
```bash
python cli.py order_audit -p line=<一条三段式 sku 的存量行 order_line_id> -p scrape=0 -p push=0 -p wait=0   # 单行复核 D-0b-2 第二步:看它从『待人工』转成什么;确认无异常再放开采集
```
```bash
python cli.py order_center_push -p table=sales -p days=90   # 第二个 PR(飞书列)合并后跑:摘要应是「更新 ≈N」(一次性全量重推,预告不是故障);第二次跑必须回到「跳过 N」
```
```bash
python cli.py order_center_push -p table=returns -p days=90   # 售后表同款两步
```
```bash
python cli.py catalog_sync -p store=<一家店>   # 所有者先把在线产品总表工作表列数扩到 ≥17,再跑;表末尾出现 source_key 列,amz 行有值、未登记行空
```

### 决策点

- **D-0b-1|黑名单 `or sku` 原文兜底口径(sku_plan §8 未拍板项)**
  - 默认:**保持现状**:`resolve_many(...) or sku` —— 登记簿与形态都解不出时仍用订货号原文入键,只额外加一条 logger.warning + 计数(conventions §六 真兜底三要件之「触发必须记日志计数」)。record_asins / collect_brands / _LATEST_CTE 三处口径一致;回填侧另加一个**只读**的 `opaque` 计数(0b-14②),不过滤、不丢行。
  - 备选:(a) 统一改成「登记簿查不到就不入选」:黑名单键永远是标准 ASIN,但会**丢行** —— 违禁品因为没登记而进不了黑名单,下一轮照样上架烧 UPC 烧配额;(b) 分场景:feed_track 来的(kind=list,一定登记过)不兜底、problem_scan 来的(可能是 match/unknown 行)兜底 —— 两套口径,违反单一路径。
  - 影响:保持现状 = 零行为变化,代价是切码后若真有「在架却没登记」的行(体检③ 那个数)命中违禁,会往 asin_blacklist 灌一个随机码 —— 该键永远拦不住任何东西,但也不误伤(它匹配不到任何真 ASIN)。改成不入选 = 拦截行为变化,必须所有者拍板。日志计数与 opaque 计数是两条路的共同前提:先让它可见,跑一个月看真实频次再定。
- **D-0b-2|order_audit.judge 改读 order_lines.asin 后,存量三段式/纯数字行的判定变化(**本批次唯一的判定口径变化**)**
  - 默认:**采纳 sku_plan §3.3 的改法**:judge 用 `line_asin(line)`(asin 列优先、形态提取兜底)。后果是存量三段式(JTZW-B08M4D1GMT-38)与纯数字 item_id 形态的订单行,从今天的「SKU 不是 ASIN 形态 → 待人工、不推采集」转入正常判定链(推采集、算限价、出通过/拒绝结论)。**合并前必须先跑体检①②并让所有者点头;0b-17 单独成一个提交(必要时单独成 PR)**。
  - 备选:(a) 严格零行为变化:`asin = line.get("asin") if line.get("asin") == line.get("sku") else line.get("sku")` —— 只对 sku=asin 的行用新路,其余维持原判。丑,且切码后 sku 永远 ≠ asin,这个条件会在批次 2 当天全线失效,等于把改造推迟到最危险的那一刻;(b) 分阶段:0b 只改 _snapshots/_scrape_fails 两处取数(它们查空 = 无快照 = 本来就判待人工,零后果),judge 那一处留到批次 2 —— 但那样 want 清单与 judge 结论会短暂不同源,是本工作包最想避免的静默错位。
  - 影响:默认路的收益:这批存量行今天是**永久卡死**的(judge 说「不是 ASIN 形态」⇒ 不标 rescrape ⇒ 永远没人再看一眼),改完它们第一次真正进入审核。风险:体检① 的「判定会变」若是个大数(>2000),会在合并当天产生一批新的采集请求(烧配额)+ 一批新结论涌进飞书。缓解:合并当天先 `-p scrape=0` 跑一轮只看结论分布,确认无异常再放开采集;若那个数很大,把 0b-17 摘成独立 PR 单独验收(0b-18/19/20/21 单独存在是安全的 —— 它们只把查空的键换成能查到的键,最坏情况是维持现状)。
- **D-0b-3|collect_brands 的 ops.dedupe 去重键(scope=cleanup:brand_asin)**
  - 默认:**保持 sku 原文**(144 行 cands 键与 176 行 _MARK_SQL 实参都不动),加注释说明后果,加守门测试钉住;同时把「折叠后的 store 不得当查询键」写进注释并按全部出现过的店定序反查(0b-13②)。
  - 备选:改成解析后的 asin:同一 ASIN 在多店只收一次品牌。
  - 影响:保持 = 零行为变化。切码后 sku 全局唯一 ⇒ 同一 ASIN 在两家店会各触发一次品牌收集;渠道表/闸门表都是 DO NOTHING 幂等,后果只是多一次 catalog.products 查询和 stats 里 brand_known +1,不产生错误数据。改成 asin 会改变**存量**三段式行的去重粒度(今天按整串三段式去重,改后按中段 ASIN 去重),那是行为变化,不该混进零行为变化批次。
- **D-0b-4|飞书三处加列的合并时机(**已按审查意见反转**)**
  - 默认:**所有者先建列,代码后合**:0b-24/25/26/27/28/29 六条拆成**第二个 PR**「0b-飞书列接线」,只有在所有者(a) 在 ORDER_SALES 建文本字段「ASIN」、(b) 在 ORDER_RETURNS 建文本字段「ASIN」、(c) 把在线产品总表工作表列数扩到 ≥17 之后才合。合并后第一次 push 会把 90 天窗口全量重推一遍(销售 + 售后各一次),这一次写入量大但 order_center 的写通道自带批次预算与节流(conventions §八),不需要特殊处理;预告写进 docs/feishu_tables.md。
  - 备选:(a) 原稿的「代码先合、所有者后建列」:`_adapt_rows`(services/order_center.py:180-193)对表中不存在的列**每轮打一条 logger.warning**,窗口期长度不可控 ⇒ 告警常态化 ⇒ 脱敏,而这条 WARNING 正是「列名建错成小写 asin / ASIN码」唯一的发现渠道;更致命的是 ONLINE_PRODUCTS_SHEET 那一对:registry 加到 17 列而投影 SQL 还是 16 列(或反过来)会让既有 test_projection_columns_match_registry 当场红,而 catalog_sync 往没扩列的工作表写 17 列会撞 90204 并拖累 product_chain 整链。(b) 用 reconcile=1 主动触发重推:没必要,指纹变了自然会推。
  - 影响:默认路把窗口期压成零:没有 WARNING、没有 registry/SQL 列数不一致的中间态、没有 90204。代价是第二个 PR 要等所有者动手,所以它不阻塞第一个 PR(六处收口 + 守门 + 两条清洗工作流全在第一个 PR 里)。建列前所有者必须用 `list_fields` 确认表里没有同名人工列 —— 有的话程序一登记就开始覆盖它(sku_plan §8 已列此风险)。建列请挑非高峰时段:push 挂在 order_sync / returns_sync 链尾,重推那一轮会明显变慢(但不会失败,push_after 永不抛错)。
- **D-0b-5|在线产品总表新增列的列名(**已按审查意见改判**)**
  - 默认:追加**一列**,元组元素取 **`source_key`**(值 = listing_sources.source_key),位置 = 末尾第 17 列;人读中文名「来源码」只写进 docs/feishu_tables.md。
  - 备选:(a) 原稿的中文 `"来源码"`(所有者原话);(b) 加两列「来源」「来源码」(source_type + source_key):match 行的来源码是 GTIN,不加来源类型的话运营看到一串数字不知道是什么。
  - 影响:**改判理由(第二、四位审查者 major)**:实测 workflows/catalog_sync.py:274 `rows = [list(sheet.columns)] + data_rows` —— registry 的 columns 元组**就是写进飞书的表头文字行**,而现有 16 项全是英文标识符(store/sku/itemId/…)。中文与英文并排会让表头看起来像两批人建的;更实际的是横切包已经按 `source_key` 排了 item,两个名字两条断言互斥,必须二选一。取 `source_key` 同时与 0b-28 的 SQL 列名一致。一列而不是两列:切到批次 2 之后 SKU 首字母自带来源信息,「来源」列会变冗余;在此之前 match 行的来源码看起来像一串 GTIN 数字,运营需要口头交代一次。
- **D-0b-6|售后表 ASIN 的来源:借 order_lines 还是给 return_lines 加列**
  - 默认:**借**:`_RETURNS_SQL`(services/order_center.py:66-76)已在 74 行 LEFT JOIN order_lines,顺手 `SELECT l.asin`。orders.return_lines **不加** asin 列(本批次零 DDL)。
  - 备选:给 return_lines 加 asin 列并在 flatten_return_lines 里填:退货行自带 ASIN,不依赖订单行是否还在窗口内。
  - 影响:借的代价:订单行滚出库/孤儿退货行(退货来了但订单没拉到)时飞书那格空着。这与仓内既有口径一致(同一条 SQL 里的 `l.order_date` 也是借来的、也会空)。加列的代价是两份真值 —— 批次 3 存量改码后订单侧 asin 由 replaced_by 链归并,退货侧那一份不会跟着动,会飘。
- **D-0b-7|product_events 平台级事件(store 为空)的 asin 来源(**事实描述已按审查意见订正**)**
  - 默认:**保持 extract_asin**,不查登记簿。平台级来源共四处,全部满足「sku 本来就是 asin,extract_asin 恒等返回」:services/product_ingest.py:266(sku=rec["asin"])、services/audit_store.py:196-215 event_row(sku=outcome.asin)、workflows/product_audit.py:617(sku=asin)、workflows/audit_history_fold.py:74-87(**直插 SQL,绕过 record_many**,`SELECT asin, asin, NULL, …`)。
  - 备选:给这些事件补上 store 再走登记簿 —— 但 product_ingest 那一刻**根本没有店铺**(sku_plan §5.1「问 1」的核心论据),补不出来。
  - 影响:零风险。**订正(第一、三位审查者)**:原稿把三处写成「product_ingest / product_audit / cleanup_history_import」。实测 services/cleanup_history.py:64/75 把 store 原样带进 record_many,所以 41.7 万行历史导入走的是**登记簿腿**(每批 1 万行多一条批量 SELECT,可接受,但性能评估必须按这个算);而真正的第四个平台级写入点是 audit_history_fold 的直插 SQL —— 它同时绕过 record_many 的事件码校验,守门白名单必须显式登记它并写明理由(直接 INSERT、asin 列直填、无 SKU 语义)。这三行注释与文档不订正,下一个人按错误事实推理会得出错误结论。
- **D-0b-8|不透明码形态判据放哪(**原 0b-03 已撤回**)**
  - 默认:**本批次不新增任何形态判据常量或函数**;sources_backfill 直接调 0a 交付的 `services/sku_codec.is_opaque`(services→services,依赖方向合法)。不透明码字母表 `_ALPHABET`、长度、「至少含一个字母」三条口径的唯一之家是 services/sku_codec。
  - 备选:(a) 原稿:在 services/sku_asin 新建 `OPAQUE_ALPHABET`/`OPAQUE_LEN`/`is_opaque_form`;(b) registry/resources.py 的 `SKU_ALPHABET`(0a-01 提案)。
  - 影响:**撤回理由(第二、四位审查者 blocker)**:四份工作包分别把字母表放进 registry(0a-01)、services/sku_asin(0b-03)、services/sku_codec(0a-07 + 横切 D4)三个地方,并配了两条互斥的守门断言(0a-26④ 断言 schema.sql 字符类 == registry 常量;横切断言 == sku_codec 常量),不可能同时绿;形态判据本身也分叉(0a-02 的索引带 `AND sku ~ '[A-Z]'`、0b-03 与横切 C0-DDL-3 没有),同一个 12 位串在三处得出不同判定。字母表一旦有两份,剔掉 0/O/1/I/L/U 这条纪律漏改一处就静默失效 —— 这正是原 0b-03 自己写在 docstring 里要防的事。0b 撤回自己那份是代价最小的解法(0b 在 0a 之后,sku_codec 已存在),并把这条依赖写进 depends_on。

### 依赖

- **批次 0a 必须先合**。0b 依赖 0a 交付的 `services/sku_asin.resolve(conn, store, sku)` 与 `resolve_many(conn, pairs) -> dict[tuple[str|None, str], str]`,契约必须是:①登记簿 `catalog.listing_sources` 按 (store, sku) 查,`source_type='amz'` 且 source_key 非空 ⇒ 返回 source_key,**但 source_key 必须先 `.strip().upper()` 再过 `is_standard_asin` 校验,不合格就当没有、回落 extract_asin**(第三位审查者 blocker:services/listing_sheet.read_rows 只 strip 不 upper、workflows/list_new.py:260-263 把 `r["asin"]` 原样写进 source_key,运营在上架表 B 列填一个小写 ASIN 就足以让两条腿给出不同的键);②**其余一切情况(match/self/1688/unknown 行、未登记行)一律回落 `extract_asin(sku)`** —— 这是本批次零行为变化的地基:今天 sources_backfill 把三段式 sku 登记成 source_type='unknown'、source_key=NULL,若 resolve 对已登记的非 amz 行直接返回 None,那些行的 asin 会从「B0GXX75JN5」变成 NULL,是**回归**;③解析不出的键**不出现在返回 dict 里**(与既有 resolve_skus「提不出的不进映射」同款约定),调用方用 `.get()` 决定兜底;④一次调用**一条** SQL(批量),不许逐条往返;⑤**pairs 为空时直接 `return {}`,不开游标** —— tests/test_cleanup_history.py::test_record_many_carries_occurred_at 与 tests/test_sku_asin.py::test_record_many_autofills_asin_column 的夹具都只实现了 executemany,开游标就当场红。
- **0a 必须同批修掉 refdata/schema.sql:232-233 的存量回填正则**:`sku ~ '^B0[A-Z0-9]{8}'` 右端无锚点 + `left(sku, 10)`,会把 `B0XXXXXXXX-2` 这类重上后缀 SKU 登记成 amz 且 source_key ≠ sku。正则右锚成 `'^B0[A-Z0-9]{8}$'`、`left(sku,10)` 改 `sku`(与 workflows/sources_backfill.py:46 的 `^B[0-9A-Z]{9}$` 同口径),并把**已经插进去的**那批行 UPDATE 成 unknown。**0b 的合并硬闸是体检⑤⑥必须为 0**;不修数据只修正则不够(INSERT 带 ON CONFLICT DO NOTHING,不会回头改旧行)。
- **0a 必须交付 `services/sku_codec.is_opaque` 与其字母表常量**(D-0b-8):0b-22 的分桶直接调它,本批次不再定义第二份。0a 若把字母表放在 registry 或别处,0b-22 跟着改 import 一行即可,但**只准有一份**。
- **守门测试文件唯一**:0a 建 `tests/test_sku_guard.py`(白名单 dict + `test_the_whitelists_do_not_rot`,形态照抄 tests/test_feishu_guard.py),0b 只增删其中的条目与断言,**不许新建第二份**(0b 原稿的 tests/test_sku_asin_consumers.py 已撤回)。若 0a 落地时用了别的文件名,0b 改用那个名字,同样不新建。
- **0b 与 0a 都改 services/sku_asin.py**(0a 加 resolve/resolve_many,0b 改 resolve_skus→resolve_pairs、加两级倒查、改 samples、补模块 docstring 分工段),必然冲突 —— 串行合,不要并行开分支。
- **飞书侧三件事需所有者动手,且是第二个 PR 的硬前置**(第一个 PR 不受阻):① ORDER_SALES 表建文本字段「ASIN」;② ORDER_RETURNS 表建文本字段「ASIN」;③ 在线产品总表工作表列数扩到 ≥17。建列前用 `list_fields` 确认无同名人工列。
- 本批次**不依赖**所有者未拍板的 A(product_clear RETIRE 是否豁免)/ B(0101119 是否码与 UPC 同换)/ C(alloc_push 是否对齐去重闸)三项 —— 它们全部落在 list_new / alloc_push / problem_scan / upc_pool 一侧,与 0b 的七个收口点零交集。0b 可以在三项都未拍板时合并。

### 风险

- **D-0b-2 是本批次唯一的真实判定口径变化**,而它藏在一个叫「零行为变化」的批次里 —— 最容易被评审一眼放过。合并前必须跑体检①②,把「判定会变」的行数与样本贴给所有者。若那个数 >2000,把 0b-17 摘成独立 PR 单独验收(0b-18/19/20/21 单独存在是安全的)。
- **体检⑤⑥是合并硬闸,不是「看一眼再决定」**(采纳第三位审查者两条 blocker)。实测 refdata/schema.sql:232-233 的回填正则 `^B0[A-Z0-9]{8}` **右端没有锚点**且写 `left(sku, 10)`,`B0XXXXXXXX-2` 这类重上后缀 SKU 会被登记成 amz、source_key='B0XXXXXXXX'。这批行今天在 order_lines.asin / product_events.asin / 黑名单键三处都是「提不出 ⇒ NULL 或原文」,改后会被登记簿解出真 ASIN —— 方向是修复,但**是行为变化**,而且与 0a 的 15 处 SQL 收口叠加后会把这批行第一次送进自动删除面(services/maintenance_intents 的 `_SQL_VARIANT_OFFSET` / `_SQL_LONG_OOS` 产出的是删除意图)。同理体检⑥:上架表 B 列一个小写 ASIN 就能让 source_key 与 extract_asin 两条腿分叉。两个数都必须为 0 才允许合并,修数据与修正则归 0a。
- **不透明码字母表与守门文件的跨包冲突已就地解决,但需要 0a 配合**:0b 撤回了自建的 `OPAQUE_ALPHABET`/`is_opaque_form`(原 0b-03)与自建守门文件 `tests/test_sku_asin_consumers.py`(原 new_modules),改成依赖 0a 的 `sku_codec.is_opaque` 与 `tests/test_sku_guard.py`。若 0a 落地时没交付这两样,0b-22 与 0b-23 会当场卡住 —— 这是本批次唯一的「上游没交付就做不了」的点,开工前先确认。
- **resolve_many 的回落契约是地基,写错方向就是静默回归**:若 0a 实现成「已登记的非 amz 行返回 None,不回落」,今天登记成 unknown 的三段式在架行会在事件账本/黑名单/订单三处同时把 asin 变成 NULL —— 三条链一起失明,而且全部不报错。tests/test_sku_guard.py::test_legacy_shapes_resolve_identically 是唯一的机器防线,必须在 0b 的第一个提交里就写。
- **倒查那一跳的两级设计是「保住既有覆盖面」而不是「顺手优化」**:原稿把它收窄到店维度并对 store 为 NULL 的对整条跳过,实测那是严格的少补(`_ITEMID_SQL` 今天不带 store、映射键是裸 sku、`_FILL_SQL` 只按 sku 匹配 ⇒ 任意一家店有该 item_id 就补全所有店,product_events 的 store=NULL 行也补得上)。执行时**不许把第二级删掉当简化** —— 它是这条零行为变化论证的字面凭据。跑预览时对比改前改后的 `numeric_resolved`,数字必须相同;`numeric_cross_store` 就是靠第二级救回来的那部分,非 0 说明跨店同 item_id 真实存在(体检⑦)。
- **性能**:record_many 与 upsert_order_lines 每次调用多一条 SELECT。record_many 的最坏调用方是 cleanup_history_import(41.7 万行、每 1 万行一批,**且带 store ⇒ 走登记簿腿**,原稿按「平台级不查库」算的估计是错的)⇒ 多 42 条批量 SELECT,每条 unnest 1 万个 (店,sku) 对去 JOIN listing_sources,可接受但必须实测一次;upsert_order_lines 是每店每轮一条,可接受。**必须**是批量一条,不能退化成逐行 —— test_fill_asins_is_one_query_per_batch 与 test_record_many_issues_one_lookup_per_call 两条测试就是为此。
- **测试夹具的连锁修改**(不先处理会看到一片假红,容易误判成代码写错):① tests/test_product_events.py 的 `_Conn.cursor()` 返回 self,新增的 SELECT 会挤进 `conn.sqls[0]`,`test_record_many_serializes_detail` 的三条断言要改成 `conn.sqls[-1]`;② tests/test_order_lines.py 的 `_FakeCursor`(271-285)没有 `fetchall`,`_fill_asins` 的 SELECT 会当场 AttributeError,且 `conn.cur.calls[0]` 从 executemany 变成 SELECT,三条既有 upsert 断言要跟着改;③ tests/test_order_asin_normalize.py 的 `_Cur.execute` 按 `"SELECT DISTINCT sku FROM orders.order_lines"` 分支,SQL 改了它就静默走 else 返回空行(**假绿**,不是假红,更危险);④ 同文件 `test_missing_column_says_run_db_init_not_a_traceback` 的 `_Boom` 也按 `"SELECT DISTINCT sku"` 判断,同样会假绿;⑤ tests/test_sku_normalize.py 的 `_Cur` 按 `"DISTINCT sku"` 分支、`conn.filled` 解包两元组;⑥ tests/test_catalog_sync.py:312-314 的投影夹具是 16 元素元组。
- **tests/test_sku_normalize.py 已经存在**(原工作包写「新建该测试文件」并给了三个不存在的测试名)。实际里面是 test_preview_profiles_without_writing / test_apply_fills_only_resolved / test_no_pending_rows 三条,断言文案是 `"待洗 4 个"`、`"可解析 3 个"`、`"3/4 个 sku 解析成功"` —— 0b-05 的摘要改法是**围着这三条断言设计的**(sku 级数字放在这三个位置,组合数另起),照做就不用改它们的断言,只改夹具。同理 `_phish_record` 的测试在 tests/test_store_events_phishing.py 而不是 tests/test_order_audit.py(原工作包写错了归属)。
- **sources_backfill 的 ⚠ 行会进链通知**:它常驻 product_chain(紧跟 catalog_sync),而 conventions §四 的链通知只发首行 —— 所以告警行必须 `insert(1, ...)` 而不是 `insert(0, ...)`(否则 dry-run 的 🧪 抬头被顶掉,空跑告警以真跑面目进飞书),并且要在告警行里自带 `mode` 前缀。同时这意味着 `sku_codec.is_opaque` 一旦误判某种存量形态,会天天刷屏并很快被无视:test_opaque_form_rejects_every_legacy_shape(在 0a 的 sku_codec 测试里)必须穷举仓内已知的全部存量形态(裸 ASIN / 三段式 / 纯数字 item_id / PHUMWMT+日期+序号 / 运营手填的 MANUAL-xxx)。
- **回滚方案**(原工作包与四份包里只有横切包写了,而横切包按审查意见要被拆解 —— 故在此写死):0b 第一个 PR 全部是代码改动,`git revert` 即可,无 DDL、无新列、不动任何数据;唯一要注意的是 revert 之后 `order_lines.asin` 会退回「extract 时点填」,已经由 `_fill_asins` 填进去的值**不会被冲掉**(`_ASIN_GUARD` 的 COALESCE 守着),所以 revert 是干净的。0b 第二个 PR(飞书列)revert 后飞书表里那两列/一列**留空不删**(程序不删列也不删行),下一轮 push 因为指纹变化会再全量重推一次 —— 这是 revert 的已知代价,提前告知所有者。
- **本批次范围之外、但审查中被点名的四条,已路由给对应批次,0b 不做**:① `workflows/product_clear.py:20-21` 的 RETIRE 注释更正(归批次 2 决策 A 或横切);② `services/alloc_survey._SQL_SALES` 按 (store, sku) 挂销量、改码后迁过码的品销量恒 0(归批次 3;对照组 services/product_pool.py 已按 order_lines.asin 聚合,写法有先例);③ `workflows/problem_scan._SQL_INFLIGHT` 的在途防重在改码后失效(归批次 3 的 sku_migrate 前置闸);④ `services/variant_group.group_id` 仍从 parent ASIN 派生、经 mp_conform 写进 Visible.variantGroupId 发给沃尔玛,货源仍可从变体组 ID 倒推(与本次改造的核心目标直接冲突,四份包里只活在 risks 文本里没人认领 —— 建议立一条独立决策项并写进 sku_plan §8)。这四条与 0b 的七个收口点零交集,但**必须有人接**,否则交付后没人会回来处理。
- **行号会漂**:本工作包的全部 file:line 是 2026-09-02 逐个打开文件核对过的(services/sku_asin.py:58-110、workflows/sku_normalize.py:33-77、workflows/order_asin_normalize.py:52-128、services/order_lines.py:166-169/374-375/416/451-458、services/product_events.py:86/145-173、services/blacklist.py:51/87-105/135-179/205-215/243-249、services/feed_track.py:175-194、services/order_audit.py:68-72/358-361、workflows/order_audit.py:142/423/461-463/479/1237-1240、workflows/sources_backfill.py:34-98、registry/resources.py:253-266/294-311/330-349、services/order_center.py:50-76/358-390/393-422、services/walmart_catalog.py:139-150、docs 三份)。动手前仍请用锚点字符串 `grep -n` 再定位一次,别照抄行号做 sed。

### PR 切分

**两个 PR,顺序不可换。**

**PR-0b-1「收口 + 守门」(不依赖任何所有者动作,可立即开工)**,三个提交:
· 提交 1「积木与清洗侧」:0b-01~0b-08(resolve_pairs 两级倒查 / samples 去重 / 模块 docstring 三入口分工 / 两条清洗工作流带 store / 就地守门更新)+ tests/test_sku_guard.py::test_legacy_shapes_resolve_identically(**零行为变化的机器证明必须在这一步就绿**)。跑完 `pytest -q` 必须已全绿。
· 提交 2「消费方收口」:0b-09~0b-16、0b-18~0b-23(order_lines / product_events / blacklist 三处含新补的 _LATEST_CTE / feed_track 注释 / line_asin / order_audit 三处取数 + _phish_record / sources_backfill 分桶 / tests/test_sku_guard.py 全套白名单)。
· 提交 3「D-0b-2」:**只有 0b-17 一条**(judge 改读 asin 列)。**体检①②的输出必须贴进 PR 描述**;若所有者有顾虑,把这个提交摘出来当 PR-0b-1b 单独走 —— 提交 1、2 单独存在是安全的。

**PR-0b-2「飞书列接线」(硬前置:所有者已建完两张表的「ASIN」文本列 + 在线产品总表工作表扩到 ≥17 列)**,一个提交:0b-24~0b-29(registry 三处 + order_center 两条 SQL 与两处载荷 + walmart_catalog LEFT JOIN + docs/feishu_tables.md)。0b-30/0b-31 两份文档随 PR-0b-1 的提交 3 一起走。
**这个拆法是按审查意见反转的**(原稿是「代码先合、所有者后建列」):registry 加到 17 列而投影 SQL 还是 16 列会让既有 test_projection_columns_match_registry 当场红;往没扩列的工作表写 17 列会撞 90204 并拖累 product_chain 整链;订单两表在建列前会每轮刷一条 `_adapt_rows` 的 WARNING,把「列名建错」唯一的发现渠道刷成噪声。

**工时与日历**(原工作包与四份包全都没给,审查点名):PR-0b-1 约 3 人日代码 + 1 人日夹具连锁修改(risks 第 7 条列了六处),关键路径不是写代码而是**上游**——0a 必须先交付 resolve_many 的五条契约、sku_codec.is_opaque、tests/test_sku_guard.py,并修掉 schema.sql 的回填正则与那批数据(体检⑤⑥归零)。PR-0b-2 约 0.5 人日代码,日历上取决于所有者建三处列(可与 PR-0b-1 并行准备)。

**改动规模**:PR-0b-1 触及 11 个源文件 + 11 个测试文件;PR-0b-2 触及 3 个源文件 + 2 个测试文件 + 1 份文档。净增约 240 行(其中测试约 150 行)。
