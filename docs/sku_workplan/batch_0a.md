## 批次 0a|身份积木 + 维护/审核/分配侧 SQL 收口(零行为变化)【第 2 版,已按四位审查者意见修订;行号 2026-09-02 逐条重开文件核对】

> ## ⚠ 所有者定稿覆盖(2026-09-02,优先级高于下文任何 item)
> 1. **上架表表头已由所有者重排(2026-09-02 第二次),SKU 在 C 列,不是 V 也不是 R**。
>    新 21 列(A~U)按顺序:店铺 / ASIN / **SKU** / walmart上架标题 / walmart_product_type /
>    审核结果 / **类别** / **具体内容** / 审核日期 / amz价格 / 库存 / walmart价格 / 是否上架 /
>    上架feedid / 上架日期 / 未上架理由 / 上架结果 / 报错 / feed查询日期 / **登记日期** / **查询编码**。
>    对应元组:store, asin, **sku**, list_title, product_type, audit_result, **audit_category**,
>    **audit_reason**, audit_date, amz_price, stock, walmart_price, listed, feed_id, list_date,
>    not_listed_reason, list_result, list_fail_reason, feed_check_date, **registered_date**,
>    **query_code**(旧尾部 real_title / real_pt / real_upc / upc_match 四列已被所有者删除)。
>    与旧布局的差异:① SKU 插在 C ⇒ 旧 C~E 右移一列(标题 D、PT E、审核结果 F);② 旧 F「理由」
>    拆成 G「类别」+ H「具体内容」⇒ 旧 G 之后全部右移两列(审核日期 I、amz价格 J、库存 K、
>    walmart价格 L、是否上架 M、feedid N、上架日期 O、未上架理由 P、上架结果 Q、报错 R、
>    feed查询日期 S);③ T「登记日期」、U「查询编码」是所有者新列,**程序不读不写**(语义待所有者
>    说明),只在元组里占位。`_COLS` 仍为 21。
>    **所有硬编码 range 必须按新布局重排**(listing_sheet 里 `C{r}`→`D{r}`、`H{r}:N{r}`→`J{r}:P{r}`、
>    `C{r}:G{r}`→`D{r}:I{r}`、`F{r}`→`H{r}`、`N{rn}`→`P{rn}`、`K{r}:Q{r}`→`M{r}:S{r}`、`O:Q`→`Q:S`、
>    `K:N`→`M:P`、`A{r}:B{r}` 不变),并**改成由 columns 元组算列字母**(`col_letter("sku")`)而不是
>    再写死字母,这是本次重排最大的教训。审核链的「类别」写 `products.audit_reason`(37 政策类目),
>    「具体内容」写 `audit_reason.human_reason` 的人话(pass 行留空);`write_audit_notes` 的一句
>    人话写「具体内容」。工作包 item 文本里的 `V{r}` / 「V 列」/ `R{r}` 一律按此表换算。
>    ⚠ 生产上旧代码仍按旧布局写:表头改完到本分支上线之间,product_audit / list_new / feed_poll /
>    sku_locked_heal 任何一次真跑都会写错列(标题会盖掉 SKU 列)。
> 2. **飞书列名统一叫「来源码」**:销售订单表、售后订单表新增字段是「来源码」(不是
>    「ASIN」),值仍取 `order_lines.asin`;在线产品总表「来源码」在 **Q 列**(第 17 列),
>    元组元素名 `source_key`。四列所有者均已建好,飞书列接线 PR 不再等建列。
> 3. **来源字母定稿**:`SKU_SOURCE_LETTERS = {"amz": "A", "match": "B", "1688": "C", "self": "H"}`,
>    批次 0a 落 registry 时直接填值,不再留空。
>

**目标**:建立 SKU 身份三块积木(services/sku_codec.py 为编码规则唯一之家 / services/sku_asin.pick_asin+resolve+resolve_many / registry 只登记 SKU_SOURCE_LETTERS)、登记簿弃码三列 + 三条**本批一次建到位、后续批次不许重建**的索引、upc_pool 两个烧号状态值;并把维护链、审核候选、冲突视图、风险追溯、采集推送、实证 PT、分配三件、上架去重/重试/变体组这 15 处「拿 SKU 当 ASIN 用」的读侧口径统一收口到唯一身份表达式:SQL 侧 coalesce(ls.source_key, w.sku)(ls = source_type='amz' 的 LEFT JOIN),Python 侧 sku_asin.pick_asin(source_key, sku)。同批修掉 schema.sql 存量回填正则缺右锚这一处**会在 db_init 当场制造 source_key≠sku 行**的双轨(审查者三 F1),并建**全套 SKU 改造唯一的一份守门文件** tests/test_sku_guard.py。mint/abandon 本批只建不接线(接线在批次 2),abandoned_at / replaced_by 全库为 NULL,故新加谓词天然恒真。

**零行为变化**:是

六条论证(第 1、5、6 条为本次修订新增/改写)。

(1) SQL 硬等号 6 处:今天右边是裸 w.sku,只有「SKU 本身就是 ASIN」的行命中。改成 coalesce(ls.source_key, w.sku) 后——未登记行 ls 全 NULL 回落 w.sku(相同);source_type≠'amz' 的行被 ON 条件挡在 LEFT JOIN 外,同样回落 w.sku(相同);amz 行取 source_key,而存量 amz 行的 source_key 只有两个来源:refdata/schema.sql:230-236 的 db_init 回填与 workflows/list_new.py:260-263 / workflows/match_listing.py:243-246 的显式登记(list_new 传的就是 r["asin"],今天 sku=asin),对长度 10 的裸 ASIN 两者都给出 source_key=sku。

(2) 唯一的理论缺口不是理论的,已定位成必然产物并同批修掉(审查者三 F1,已实测复核):schema.sql:231-232 的回填条件是 sku ~ '^B0[A-Z0-9]{8}' —— **右端没有锚点**,且写 left(sku, 10);而生产在跑的 workflows/sources_backfill.py:46 用的是 ^B[0-9A-Z]{9}$(有右锚、整串入 source_key)。两份口径不同 = 既有双轨。后果的方向是**把行从破坏动作看不见变成看得见**:B0 开头且长度 >10 的存量 SKU(重上后缀 B0XXXXXXXX-2 这类)今天在 _SQL_VARIANT_OFFSET / _SQL_LONG_OOS / product_audit mode=online 三条**删除意图**产出面外,回填一旦把它们登记成 amz + source_key=左 10 位,收口后就进来了。处置两条,缺一不可:① 0a-03 把回填正则右锚成 '^B0[A-Z0-9]{8}$' 并把 left(sku,10) 写成 sku,与 sources_backfill 逐字同口径 —— 这一条**保住**当前行为(不修才是改行为:0a 验收本身就要跑 db_init);② 体检①(source_type='amz' AND source_key IS NOT NULL AND source_key <> sku 的行数)**升级成合并硬闸**,必须为 0 才允许合并,非 0 先修数据再合,不是「看一眼再决定」。

(3) Python 侧 pick_asin(source_key, sku) 的两条腿必须同口径(审查者三 F2,机制已更正):extract_asin 第一件事是 .strip().upper() 再要求 _PLAIN 全匹配(services/sku_asin.py:32-38);若 source_key 那条腿只取原文,则运营在上架表 B 列填的小写 ASIN(services/listing_sheet.py:67-69 只 strip 不 upper,workflows/list_new.py:263 把它原样写进 source_key)会被原样返回。⚠ 审查者三给的机制不成立(它以为 extract_asin('b0abcdefgh') 返 None —— 实际返 'B0ABCDEFGH',因为先 upper),但结论成立:今天 alloc_push 的 online 集合里是大写 'B0ABCDEFGH'(与 claims 的键同形),裸取 source_key 之后会变成小写垃圾键 ⇒ 该品被判「不在架」⇒ 已在架的品被重新派工写进上架表(alloc_push DANGEROUS=True)。故 pick_asin 的 source_key 腿定义为「归一(strip+upper)后再过 is_standard_asin,不过就落 extract_asin(sku)」,对存量与今天逐字同值;并加体检①b 量化非规范 source_key 的行数。SQL 侧不需要同样的校验:那六处 coalesce 的结果只用于与 products.asin / snapshots.asin 做等值 JOIN,脏值今天不匹配、改后也不匹配,结论相同。

(4) 新加的 ls.abandoned_at IS NULL(list_new 去重闸、alloc_push._SQL_ONLINE):本批无任何代码写 abandoned_at(abandon() 零接线,守门测试反向钉死),全库该列恒 NULL;又因为都是 LEFT JOIN,未登记行的 ls.abandoned_at 也是 NULL,谓词对每一行恒真,结果集逐行相同。它在批次 2 才真正开始筛行,提前落地是为了让批次 2 只改一处写侧。

(5) 新列/新索引/新常量/新模块:三列 ADD COLUMN IF NOT EXISTS 全部可空无默认;两条新唯一索引都带 WHERE sku ~ 不透明码形态 的局部条件,存量一行都落不进去(存量形态要么长度≠12、要么含 0/1/O/I/L/U 或 '-'),既不会因存量跨店重复 sku / 同 GTIN 多跟卖码而建索引失败(见风险 R2),也不改变任何查询语义;活码唯一索引**本批就带上 replaced_by IS NULL**(该列全库 NULL,谓词恒真),这样批次 3 一条索引都不必 DROP/CREATE(解审查者一/二/四的同一条 blocker)。sku_codec 全模块无调用者(inert);upc_pool 只新增两个常量与两个中文标签,不改任何写入点。

(6) 0a-25 的 _SQL_ATTEMPTS 代际过滤改读 e.detail->>'source_key'(不再读 product_events.asin),因此**不依赖 0b-11 的落地时序**(审查者一 minor,取其选项②)。本批全库无 sku_abandoned 事件 ⇒ LATERAL 恒返 NULL ⇒ 谓词恒真 ⇒ 退化成今天的跨码累计。归并风险另由体检⑥ 硬闸兜住。

以上六条之外,本批不动任何判定分支、不动 feed 载荷、不写库。

### 改动清单

#### 0a-01 · `registry/resources.py` · 103(AMZ_CUSTOM_FLAG_KEY)之后、105 的「沃尔玛 feed 规范」横幅之前插入新段

**改动**:**只新增一个常量**:SKU_SOURCE_LETTERS: dict[str, str] = {}(source_type → 首位来源字母;四个取值由所有者定,见 depends_on,未定前留空)。段头注释三句:① 这里登记的是「所有者要定的取值」,属外部配置(铁律 3);② **编码规则本身(字母表 / 长度 / 随机段长 / 重抽次数 / 占位码 / is_opaque 判据)不在 registry,唯一之家是 services/sku_codec.py**,registry 不许再抄一份;③ 字母必须来自 sku_codec._ALPHABET、互不相同、不助记(sku_plan §2)。

**为什么**:解审查者二/四的 blocker:原稿把 SKU_ALPHABET/SKU_LEN 放 registry,而横切包 D4 与 0b-03 各自又放了一份,配出两条互斥的守门断言(schema 字符类 == registry 常量 vs == sku_codec 常量),不可能同时绿。裁决理由:CLAUDE.md 铁律 3 管的是「路径/token/表 ID/服务器地址」这类外部资源,12 位码的字母表是内部编码规则,不是外部资源;而 SKU_SOURCE_LETTERS 是所有者要拍的取值,确属配置。空 dict 让 mint 在字母定下来之前 fail loud(缺省不猜),is_opaque/source_of 不依赖它,故 0a 可先落地。

**测试**:
- tests/test_sku_codec.py::test_source_letters_are_distinct_and_inside_the_alphabet
- tests/test_sku_guard.py::test_the_opaque_alphabet_is_born_only_in_sku_codec

**验收**:python -m pytest -q tests/test_sku_codec.py tests/test_sku_guard.py

#### 0a-02 · `refdata/schema.sql` · 213-221(建表)与 226-227(现有 listing_sources_key_idx)之后,228 的存量回填之前

**改动**:三条 ALTER 加列 abandoned_at timestamptz / abandoned_reason text / replaced_by text;三条索引(DDL 全文见 ddl 段,**名字与条件本批定死,批次 2/3 与横切包一律引用不许重建**):listing_sources_opaque_sku_uidx(全局 sku 唯一,局部条件 = 不透明码形态 AND sku ~ '[A-Z]')、listing_sources_live_uidx((store,source_type,source_key) 唯一,局部条件 = abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL AND 不透明码形态)、listing_sources_live_key_idx(非唯一,(store,source_type,source_key) WHERE abandoned_at IS NULL AND replaced_by IS NULL,给 mint 的复用查询用)。表头注释补一段:列名用 abandoned 不用 retired,「码弃用 ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用」三个同名异义;行永不 DELETE;三列只准由 services/sku_codec 写。

**为什么**:synthesis 定稿的弃码三列 + 并发双 mint 靠部分唯一索引拦。两条唯一索引必须限「不透明码形态」:存量 sku=asin 跨店重复是既成事实,存量 match 行同 GTIN 多个 PHUMWMT 码也可能重复,无条件唯一对存量一律建不起来,而 db_init 是把整份 schema.sql 一次 conn.execute(workflows/db_init.py:41-43),一条索引建失败整份回滚 ⇒ 生产建库直接停摆。⚠ **本条同时解审查者一/二/四共同点名的 blocker**:活码索引在三个包里有三个名字(live_key_uk / live_uidx / active_uidx),批次 3 的 DROP INDEX IF EXISTS 打不中任何一个 ⇒ 静默 no-op,而它接着裸建的无条件唯一索引会把 db_init 整份回滚。处置:本批把最终条件(**含 replaced_by IS NULL**)一次建到位,批次 3 不再 DROP/CREATE 任何索引,只做「核验 indexdef 已含 replaced_by IS NULL」;全局唯一索引统一取名 listing_sources_opaque_sku_uidx 且**必须两段条件都在**(横切 C0-DDL-3 漏了 AND sku ~ '[A-Z]',会让不含 0/1 的 12 位纯数字沃尔玛 item id 落进「新码」索引,与 sku_codec.is_opaque 的判据不一致)。

**测试**:
- tests/test_sku_guard.py::test_schema_opaque_predicate_matches_the_codec_alphabet
- tests/test_sku_guard.py::test_the_live_unique_index_is_named_once_and_carries_replaced_by
- tests/test_sku_guard.py::test_pg_two_stores_may_share_a_legacy_sku_but_never_a_new_code
- tests/test_sku_guard.py::test_pg_two_live_rows_may_share_a_legacy_key_but_never_a_minted_one

**验收**:python cli.py db_init && python cli.py db_init  # 连跑两次证幂等;然后 psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources' ORDER BY 1"(必须恰好四条:key_idx + opaque_sku_uidx + live_uidx + live_key_idx)

#### 0a-03 · `refdata/schema.sql` · 228-236(存量一次性回填的 INSERT ... SELECT;两处 CASE 在 231-232)

**改动**:把两处判型正则右锚并去掉截断:231 行 CASE WHEN sku ~ '^B0[A-Z0-9]{8}' THEN 'amz' ELSE 'unknown' END → CASE WHEN sku ~ '^B0[A-Z0-9]{8}$' THEN 'amz' ELSE 'unknown' END;232 行 CASE WHEN sku ~ '^B0[A-Z0-9]{8}' THEN left(sku, 10) END → CASE WHEN sku ~ '^B0[A-Z0-9]{8}$' THEN sku END(右锚之后长度必为 10,left(sku,10) 与 sku 逐字等价,写 sku 是为了把「截断」这个陷阱从代码里删掉)。229-230 的注释补两句:① 本处判型与 workflows/sources_backfill.py:46 的 _ASIN_RE 是**同一条口径**,改一处必须同步另一处(conventions §六:一个能力一条实现路径);② 右锚是硬要求 —— 缺右锚会把 B0XXXXXXXX-2 这类重上后缀 SKU 判成 amz 并把 source_key 截成前 10 位,身份键与 SKU 从此不等,而这批行会因此第一次进入维护链的删除意图产出面。

**为什么**:**本条是审查者三 F1 的处置,也是零行为变化论证第 (2) 条的一半**。不改不是「保持现状」:0a 的验收本身就要跑 db_init,而回填是 INSERT ... ON CONFLICT DO NOTHING —— 已登记行不动,但**在架未登记行**(体检③ 数它们)会在那一刻被按旧口径登记成 amz + source_key=左 10 位,当场把 source_key≠sku 的行造出来,把身份收口的零变化论证从「理论缺口」变成「合并当天的既成事实」。修了之后 db_init 与生产在跑的 sources_backfill 同口径,这批行仍登记为 unknown ⇒ 维护链的 INNER JOIN 照旧排除它们 ⇒ 当前行为逐行不变。

**测试**:
- tests/test_sku_guard.py::test_backfill_regex_agrees_with_sources_backfill(读 schema.sql 与 workflows/sources_backfill.py,断言两处判型都右锚、且 amz 分支写入的是整串)
- tests/test_sources_backfill.py::test_backfill_classifies_by_the_same_shape_rule(既有分桶用例,断言不变)

**验收**:python -m pytest -q tests/test_sku_guard.py tests/test_sources_backfill.py;体检① 必须为 0(见 acceptance_commands)

#### 0a-04 · `refdata/schema.sql` · 237-255(catalog.upc_pool 表头注 237-243、建表 244-254、status 行内注释 246)

**改动**:状态机注释补两个新值:burned_delete(DELETE 经观测核验后弃码同时烧号)/ burned_lock(SKU_LOCKED 自愈退役后烧号);并写明它们与 conflict 的分工——conflict 只表示「全站已存在该 UPC(撞库)」,烧号不再复用这个语义。246 行 status 的行内注释同步为 ''/claimed/used/conflict/bad_prefix/burned_delete/burned_lock。本批不改任何写入这些值的代码。

**为什么**:synthesis 规则 2:烧号用独立状态值,不复用语义为撞库的 conflict,否则 UPC 池表投影与 pool_stats 里永远分不清「这号是撞库废的」还是「我们主动烧的」。

**测试**:
- tests/test_upc_pricing.py::test_burn_statuses_are_registered_in_schema_and_labels

**验收**:python -m pytest -q tests/test_upc_pricing.py

#### 0a-05 · `refdata/schema.sql` · 521-549(视图 catalog.audit_listing_conflicts;硬等号在 527)

**改动**:live_rejected CTE 内 527 行的 JOIN 改为两步:先 LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz',再 JOIN catalog.products p ON p.asin = coalesce(ls.source_key, w.sku) AND p.marketplace = 'US'。LATERAL 段(541-549)一个字不动——548 行 coalesce(ev.asin, ev.sku) = lr.asin 必须与 product_events_identity_idx(schema.sql:188)逐字一致。

**为什么**:sku_plan §3.2:视图失效 ⇒ problem_scan 的审核来源建议归零。**保留 AND ls.source_type = 'amz'**,与 0a-12..0a-26 其余十四处身份表达式同形:横切包 C0-DDL-5 给了一份不带 source_type 的同名视图 DDL(视图是 DROP+CREATE,后合的静默覆盖先合的),审查者一/四都判 0a-04 为准 —— 本条采纳,C0-DDL-5 应从横切包删除,它的 docs/db_schema.md 同步说明并入 0a-06。不带 source_type 的版本在存量上结论碰巧相同,但语义更弱(match 行会拿 GTIN 当身份键去撞 products.asin),等价性论证不成立。

**测试**:
- tests/test_problem_scan.py::test_conflicts_view_join_matches_its_index(既有 :292,断言 LATERAL 的 coalesce(ev.asin, ev.sku) 表达式不受影响)
- tests/test_problem_scan.py::test_conflicts_view_reads_identity_through_the_registry(新增:断言视图文本含 ls.source_type = 'amz' 与 coalesce(ls.source_key, w.sku),且不再含 p.asin = w.sku)

**验收**:python -m pytest -q tests/test_problem_scan.py && python - <<'PY'
from registry import db
with db.pg_conn() as c, c.cursor() as cur:
    cur.execute("EXPLAIN SELECT * FROM catalog.audit_listing_conflicts LIMIT 1")
    print("\n".join(r[0] for r in cur.fetchall()))
PY

#### 0a-06 · `docs/db_schema.md` · 161-177(listing_sources 段:建表块 166-172、索引注 173-176)、179-191(upc_pool 段:表头注 180-183、status 行 186)

**改动**:listing_sources 建表块补 abandoned_at/abandoned_reason/replaced_by 三列;索引注下补三条新索引的名字与局部条件全文,并抄两句「abandoned_at/abandoned_reason/replaced_by 只由 services/sku_codec 写(abandon;批次 3 的 mint_replacement)」「码弃用 ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用」;186 行 status 注释补 burned_delete/burned_lock 与其与 conflict 的分工;180-183 表头注同步。另在 listing_sources 段补一句:存量回填的判型正则与 workflows/sources_backfill.py:46 同口径(0a-03)。

**为什么**:CLAUDE.md:动了表同步 docs/db_schema.md(该文件是表结构事实来源,schema.sql 是其同步产物,见 workflows/db_init.py 头注)。横切包 C0-DOC-1 与批次 3 R2 也各排了一份 db_schema 同步 —— 本条声明 listing_sources / upc_pool 两段由 0a 唯一交付,后续批次只追加自己新增的对象。

**测试**:
- (无)

**验收**:git diff --stat docs/db_schema.md refdata/schema.sql(两份必须同一次提交)&& python -m pytest -q tests/test_readme.py

#### 0a-07 · `services/sku_asin.py` · 文件尾(现有 samples 在 103-110,文件共 110 行)追加新段;模块 docstring(1-16)顶部补一段口径说明

**改动**:新增三个函数、一条新 SQL 常量。① pick_asin(source_key, sku) -> str | None:docstring 首行「输入:登记簿 amz 行的 source_key(可空)+ sku → 输出:ASIN(登记簿优先但要过形态闸,提不出返 None)」;实现 = k = str(source_key or '').strip().upper();若 k 且 is_standard_asin(k) 则返回 k,否则返回 extract_asin(sku)。**两条腿必须同口径**(归一 + 形态校验),docstring 写死「登记簿只是优先级,不是免检通道」。② _REG_SQL = SELECT store, sku, source_key FROM catalog.listing_sources JOIN unnest(%s::text[], %s::text[]) AS t(s,k) ON store=t.s AND sku=t.k WHERE source_type='amz' AND source_key IS NOT NULL(**不按 abandoned_at 过滤**:旧码带着订单回来必须还查得到,消费方契约)。③ resolve_many(conn, pairs) -> dict[tuple[str,str], str]:一条 SQL 取键,逐对过 pick_asin,提不出的**不进映射**(与 resolve_skus 同纪律,调用方留 NULL)。④ resolve(conn, store, sku) 单条壳。模块 docstring 补三条分工:resolve/resolve_many 是 **services 内部的有界批量反查**(几十几百对);**全表级取数一律在 SQL 里 LEFT JOIN 登记簿再调 pick_asin,不要拿十万对去 unnest**(0a-20..0a-23 就是这么做的);批次 0b 若要给清洗工作流做 resolve_pairs(含纯数字倒查那一跳),必须**建在 resolve_many 之上**,不许在旁边另起一条批量入口。

**为什么**:sku_plan §6:登记簿那一跳必须放在 services/sku_asin 内,否则守门测试 tests/test_order_asin_normalize.py:114 test_rules_are_not_reimplemented_here 拦;pick_asin 把「优先级规则 + 形态闸」收成唯一出处,让五个调用点不必各写一遍 `key or extract_asin(sku)`。⚠ **形态闸是本次修订新增**(审查者三 F2):services/listing_sheet.py:67-69 读表只 strip 不 upper,workflows/list_new.py:263 把 r["asin"] 原样写进 source_key,裸取会让一个小写 ASIN 变成 alloc_push online 集合里的垃圾键,而真 ASIN 仍然缺席 ⇒ **已在架的品被重新派工写回上架表**。审查者给的机制(说今天 extract_asin 会返 None)不成立 —— extract_asin 先 upper 再匹配,今天返的是大写 ASIN;但结论成立,故采纳。三个入口的分工写进 docstring 是采纳审查者二关于「resolve / resolve_many / resolve_pairs 三个入口没人定分工」的 minor。

**测试**:
- tests/test_sku_asin.py::test_resolve_agrees_with_extract_asin_on_every_legacy_shape
- tests/test_sku_asin.py::test_registry_key_wins_over_the_pattern
- tests/test_sku_asin.py::test_a_lowercase_registry_key_is_normalized_like_the_pattern_does(新增:source_key='b0abcdefgh' 必须给出 'B0ABCDEFGH')
- tests/test_sku_asin.py::test_a_malformed_registry_key_falls_back_to_the_pattern(新增:source_key='B0XXXXXXXX-2' / '  ' / GTIN 都不许被当身份键)
- tests/test_sku_asin.py::test_unregistered_and_non_amz_rows_fall_back_to_the_pattern
- tests/test_sku_asin.py::test_resolve_still_answers_for_an_abandoned_row
- tests/test_sku_asin.py::test_resolve_many_is_one_query_for_many_pairs
- tests/test_sku_asin.py::test_an_opaque_code_is_invisible_to_extract_asin_but_resolvable

**验收**:python -m pytest -q tests/test_sku_asin.py tests/test_order_asin_normalize.py tests/test_sku_normalize.py

#### 0a-08 · `services/sku_codec.py` · 新文件

**改动**:见 new_modules 段的完整 API 与 docstring 规则。**本模块是 12 位不透明码编码规则的唯一之家**(_ALPHABET / _RANDOM_LEN / _LEN / _MAX_DRAWS / DRYRUN_PLACEHOLDER / is_opaque),registry 只放 SKU_SOURCE_LETTERS。四个公开函数 is_opaque / source_of / mint / abandon,一个原因词表 ABANDON_REASONS,一张 reason→烧号状态映射。**mint 无 dry_run 形参**;**撞码分两个明确分支各记各的日志计数**。本批不接任何调用点。

**为什么**:sku_plan §6 + synthesis:抽码与登记必须同一函数同一事务(不存在「抽了没登记」),弃码只有一个实现(conventions §六单路径)。三处采纳审查者意见:① 编码常量之家定在这里(审查者二/四 blocker,理由见 0a-01);② **删掉 dry_run 形参**(审查者二:写库函数不该有「这次不写」的模式,而且 0a 与批次 2 会并出两条 dry-run 路径)—— dry-run 由调用方决定不调 mint、直接用 DRYRUN_PLACEHOLDER,而 workflows/list_new.py:1583 的 `if not execute: return` 在 :1650 调 _prep_rows 之前,空跑本来就走不到 mint;③ **撞码不做 catch-all 重抽**(审查者二/四:无 target 的 ON CONFLICT DO NOTHING 会把「并发双 mint 撞活码键」和「随机撞码」吞成一种,重抽 5 次必然次次失败,最后抛出的诊断词指向随机源,排障方向全错)—— 改成:INSERT 拿不到行时先重跑一次复用查询,查到即返回对方刚写的码(logger.warning + _concurrent_mint 计数,这与 mint 的复用语义一致),查不到才当随机撞码重抽(logger.info + _sku_redraws 计数),两个分支条件明确、各记日志,满足 conventions §六真兜底三要件。

**测试**:
- tests/test_sku_codec.py::test_is_opaque_rejects_every_legacy_shape
- tests/test_sku_codec.py::test_is_opaque_needs_a_letter_so_numeric_item_ids_never_pass
- tests/test_sku_codec.py::test_alphabet_excludes_the_confusable_glyphs
- tests/test_sku_codec.py::test_placeholder_can_never_pass_as_an_opaque_code
- tests/test_sku_codec.py::test_source_of_returns_none_until_the_owner_picks_letters
- tests/test_sku_codec.py::test_mint_reuses_the_live_row_instead_of_drawing
- tests/test_sku_codec.py::test_mint_draws_and_registers_in_the_same_call
- tests/test_sku_codec.py::test_mint_returns_the_other_process_code_on_a_live_key_conflict
- tests/test_sku_codec.py::test_mint_redraws_on_a_random_collision_then_raises_loudly
- tests/test_sku_codec.py::test_mint_has_no_dry_run_switch
- tests/test_sku_codec.py::test_mint_refuses_an_unmapped_source_type
- tests/test_sku_codec.py::test_abandon_is_idempotent
- tests/test_sku_codec.py::test_abandon_burns_only_for_amz_rows_and_only_for_burning_reasons
- tests/test_sku_codec.py::test_abandon_never_burns_on_sku_update
- tests/test_sku_codec.py::test_abandon_records_the_ledger_event_with_source_key_in_detail
- tests/test_sku_codec.py::test_abandon_refuses_an_unregistered_reason

**验收**:python -m pytest -q tests/test_sku_codec.py

#### 0a-09 · `services/upc_pool.py` · 26-27(STATUS_CN)、8-16(docstring 状态机)、188-207(burn_for_retire)

**改动**:新增常量 BURN_DELETE = "burned_delete"、BURN_LOCK = "burned_lock";STATUS_CN 补 {"burned_delete": "删除烧号", "burned_lock": "锁死烧号"};docstring 状态机补这两个值与「conflict 只表示撞库」的分工。burn_for_retire(188-207)**保持原样**(仍写 conflict),只在 190-193 的 docstring 后补一行注释指向批次 2:接 sku_codec.abandon 时改为 burn(conn, pairs, status) 并由 abandon 传状态。

**为什么**:0a 的零行为变化承诺:改写入状态值会改变 UPC 池表投影里的字样,属可见变化,推迟到批次 2 与 abandon 接线同批做(同一处改一次,不留双轨,见决策 D)。claim 的复用查询是白名单 status IN ('claimed','used')(services/upc_pool.py:147-157),新值天然被排除,无需改。⚠ 横切包 C0-REG-4b 也排了同一处改动 —— 采纳审查者一,STATUS_CN 归本条唯一交付,C0-REG-4 只保留 UPC_SHEET E 列口径那半条。

**测试**:
- tests/test_upc_pricing.py::test_burn_statuses_are_registered_in_schema_and_labels
- tests/test_upc_pricing.py::test_claim_reuse_ignores_burned_rows
- tests/test_upc_pricing.py::test_burn_for_retire_marks_conflict(既有 :100,断言不变——本批不改写入值)

**验收**:python -m pytest -q tests/test_upc_pricing.py

#### 0a-10 · `services/product_events.py` · 94-120(事件码常量区 95-109、EVENTS 集合 113-120)

**改动**:新增常量 SKU_ABANDONED = "sku_abandoned"、SKU_REPLACED = "sku_replaced"(接在 109 行 AUDIT_REJECTED 之后),并加入 118 行那一组的 EVENTS 集合。不改 record_many 的任何逻辑(:167 的 extract_asin 归 0b)。同时在常量区注释里写死两个码的 detail 结构约定:sku_abandoned 记 {old_sku, reason, source_type, source_key, burned_upcs};sku_replaced 记 {old_sku, new_sku, reason, source_type, source_key}。

**为什么**:record_many(:156-159)对未登记事件码 fail loud,sku_codec.abandon 要写这两个码,不先登记就会在批次 2 抛 ValueError —— 而 0a 自己的 tests/test_sku_codec.py::test_abandon_records_the_ledger_event 也会当场红。⚠ 横切包 C0-TEST-1 把这两个码排进批次 2,与本条冲突;采纳审查者一:统一到 0a 登记(提前登记零副作用,EVENTS 只是入参校验白名单),C0-TEST-1 改成「核验已登记」。detail 结构约定写进注释是因为 0a-25 的代际过滤要读 detail->>'source_key'(见 0a-25 的 why)。

**测试**:
- tests/test_product_events_registry.py::test_constants_match_ledger_strings(既有 :49,补两条断言)
- tests/test_product_events_registry.py::test_record_many_accepts_every_registered_code(既有 :35,自动覆盖)
- tests/test_product_events_registry.py::test_no_stray_event_literals_in_emitters(既有 :60,保持绿)

**验收**:python -m pytest -q tests/test_product_events_registry.py tests/test_product_events.py

#### 0a-11 · `services/listing_sources.py` · 1-15(模块 docstring)、29-42(register)

**改动**:docstring 末尾补消费方契约三句:① 本模块的 register 只负责首次登记(backfill 与跟卖 B 列人工号),**自动抽码一律走 services/sku_codec.mint**;② abandoned_at/abandoned_reason/replaced_by 三列**只准由 services/sku_codec 写**(0a 的 abandon;批次 3 的 mint_replacement),本模块与任何工作流都不得 UPDATE 它们;③ 本表的 INSERT 只有两个合法出口(本模块 register 与 sku_codec.mint 家族),新增第三个即违规(守门测试扫 INSERT INTO catalog.listing_sources)。register 函数体不动。

**为什么**:两个模块都往同一张表写,不写死分工就会长出第二条抽码路径(conventions §六)。两处采纳审查者二的 minor:① 原稿 docstring 写「三列只准由 abandon 写」,批次 3 的 mint_replacement 也写 replaced_by,那句话到批次 3 就成了假话 —— 改写成「只准由 services/sku_codec 写」;② 原稿守门只扫 UPDATE,INSERT 完全不设防,将来谁在工作流里补一条 INSERT 就长出第三条登记路径且不报错 —— 守门同时扫 INSERT。

**测试**:
- tests/test_sku_guard.py::test_only_sku_codec_writes_the_abandon_columns
- tests/test_sku_guard.py::test_the_registry_table_has_exactly_two_insert_sites

**验收**:python -m pytest -q tests/test_sku_guard.py

#### 0a-12 · `services/maintenance_intents.py` · 190-192(_SQL_AMZ_JOIN 里 ls 的 INNER JOIN 在 190-191,products JOIN 在 192)

**改动**:192 行 JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = w.sku 改为 ... AND p.asin = coalesce(ls.source_key, w.sku)。ls 已在 190-191 以 INNER JOIN + source_type='amz' 就位,顺序在前可直接引用。用 coalesce 而不是裸 ls.source_key:register 允许 source_key 缺省(services/listing_sources.py:41 的 r.get("source_key")),NULL 键的 amz 行今天靠 p.asin = w.sku 命中,裸取会把它们静默丢掉。

**为什么**:sku_plan §3.2 第一条:三个 amz provider 共用这条取数,失效 = 维护链对新码永久失明(不改价、不清零),而且不报错。

**测试**:
- tests/test_maintenance.py::test_amz_join_reads_the_identity_key_not_the_raw_sku(新增:断言 SQL 文本含 coalesce(ls.source_key, w.sku) 且不含 p.asin = w.sku)
- tests/test_maintenance.py::test_amz_join_honors_routing_and_stockzero(既有 :274,断言不变)

**验收**:python -m pytest -q tests/test_maintenance.py && python cli.py maintenance_scan -p preview=1

#### 0a-13 · `services/maintenance_intents.py` · 201-203(_SQL_AMZ_JOIN 内 latest_snapshot LATERAL 的关联条件,硬等号在 202)

**改动**:202 行 WHERE l.marketplace = 'US' AND l.asin = w.sku 改为 ... AND l.asin = coalesce(ls.source_key, w.sku)。ls 在 FROM 顺序中位于 193 行的 LATERAL 之前,可引用。

**为什么**:同 0a-12:最新快照对不上 ⇒ 价格/库存/标题三个 provider 全部拿不到 amz 侧现值。

**测试**:
- tests/test_maintenance.py::test_amz_join_reads_the_identity_key_not_the_raw_sku(同上,一并断言 l.asin = coalesce(...))

**验收**:python -m pytest -q tests/test_maintenance.py

#### 0a-14 · `services/maintenance_intents.py` · 231-236(_SQL_VARIANT_OFFSET 的 SELECT 231、FROM vo 232、硬等号 233、ls JOIN 234-235、latest_status 236)

**改动**:把驱动表从 vo 换成 walmart_items,让 ls 先就位再按身份键接 vo:FROM catalog.walmart_items w / JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' / JOIN vo ON vo.asin = coalesce(ls.source_key, w.sku) / LEFT JOIN latest_status s ON s.store = w.store。SELECT 列表(231)、WHERE 段(237-245)、ORDER BY(246)一字不动。

**为什么**:sku_plan §3.2 第三条:永久偏移的品删不掉。存量等价性:ls 本来就是 INNER JOIN(未登记行今天已被 234-235 排除),coalesce 对存量 amz 行 = w.sku,与今天的 w.sku = vo.asin 同集合。⚠ 换驱动表会让计划从索引嵌套循环变成哈希连接,故验收里有一条 EXPLAIN(R3)。

**测试**:
- tests/test_maintenance.py::test_variant_offset_intents_gates_and_store_cap(既有 :282,断言 ls.source_type = 'amz' 仍在)
- tests/test_maintenance.py::test_variant_offset_joins_scrape_failures_through_the_registry(新增:断言含 vo.asin = coalesce(ls.source_key, w.sku),不含 w.sku = vo.asin)

**验收**:python -m pytest -q tests/test_maintenance.py && python - <<'PY'
from registry import db
from services import maintenance_intents as m
with db.pg_conn() as c, c.cursor() as cur:
    cur.execute("EXPLAIN " + m._SQL_VARIANT_OFFSET, {"min_batches": 2})
    print("\n".join(r[0] for r in cur.fetchall()))
PY

#### 0a-15 · `services/maintenance_intents.py` · 279-290(live CTE,SELECT 在 280、ls INNER JOIN 在 282-283)、322-323(win CTE 的 FROM…JOIN 与 GROUP BY)

**改动**:280 行 SELECT w.store, w.sku, coalesce(req.want,'') AS want 改为 SELECT w.store, w.sku, coalesce(ls.source_key, w.sku) AS asin, coalesce(req.want,'') AS want(ls 已在 282-283 INNER JOIN 且限 amz);322 行 FROM live JOIN obs o ON o.asin = live.sku 改为 ON o.asin = live.asin;323 行 GROUP BY live.store, live.sku, live.want 补 live.asin。最终 SELECT(325)与 WHERE(327-331)不动——输出的仍是真 SKU,意图行照旧按 (store, sku) 走。

**为什么**:sku_plan §3.2 第四条:连续缺货删除对新码失明。catalog.snapshots 是按 ASIN 存的,live 必须带出 ASIN 才能接上。

**测试**:
- tests/test_maintenance.py::test_long_oos_live_cte_carries_the_identity_key(新增:断言含 coalesce(ls.source_key, w.sku) AS asin 与 o.asin = live.asin)
- tests/test_maintenance.py::test_long_oos_delete_sql_guards(既有 :330,三道判据断言不变)
- tests/test_maintenance.py::test_long_oos_window_is_evaluated_per_channel(既有 :351,断言不变)

**验收**:python -m pytest -q tests/test_maintenance.py && python cli.py maintenance_scan -p preview=1(与改前逐行 diff,见 acceptance_commands「意图集合对拍」)

#### 0a-16 · `workflows/product_audit.py` · 409-411(_pick_where 的 mode=online 分支返回值,硬等号在 411)

**改动**:改成两条腿的 OR,两条都走得上索引:"p.audit_status = 'approved' AND (EXISTS (SELECT 1 FROM catalog.walmart_items w WHERE w.sku = p.asin AND w.missing_since IS NULL) OR EXISTS (SELECT 1 FROM catalog.listing_sources ls JOIN catalog.walmart_items w ON w.store = ls.store AND w.sku = ls.sku WHERE ls.source_type = 'amz' AND ls.source_key = p.asin AND w.missing_since IS NULL))"。**不要写成 coalesce 形式**:这里是对 products 每行的相关子查询,coalesce 表达式用不上 walmart_items_sku_idx(schema.sql:152),几十万行候选会退化成逐行全表扫(与 2026-08-14 视图挂死同一类事故,schema.sql:1121 踩坑注)。第二条腿走 listing_sources_key_idx(schema.sql:226-227)。404-408 的口径注释保留并补一句「第二条腿只覆盖新码,存量下是第一条腿的子集」。

**为什么**:sku_plan §3.2 第五条:在架 pass 复审候选恒空。存量等价性:第一条腿逐字保留今天的语义,第二条腿在存量下是空集(存量 amz 行 source_key = sku,已被第一条腿覆盖),切换后才开始起作用。⚠ 本条是守门测试 test_sku_and_asin_hard_equality_is_extinct 唯一的永久白名单项(第一条腿故意保留 w.sku = p.asin),原稿说「白名单为空」是错的,已改。

**测试**:
- tests/test_audit_rules_wiring.py::test_pick_where_online_scopes_to_listed_rows(既有 :150,补断言:含 catalog.listing_sources 与 ls.source_key = p.asin,仍不含 published_status)
- tests/test_audit_rules_wiring.py::test_online_candidates_also_match_through_the_registry(新增)
- tests/test_audit_rules_wiring.py::test_online_mode_is_pinned_to_l0(既有 :166,断言不变)

**验收**:python -m pytest -q tests/test_audit_rules_wiring.py && python cli.py product_audit -p mode=online --dry-run

#### 0a-17 · `services/risk_trace.py` · 125-134(_ITEMS_SQL)、8-12(模块 docstring 的四证据源列表,①号在 8)

**改动**:_ITEMS_SQL 改成先 UNION 两条腿再聚合:WITH hit AS (SELECT store, sku, missing_since, lifecycle_status, created_at, last_seen_at FROM catalog.walmart_items WHERE sku = ANY(%(asins)s::text[]) UNION SELECT w.store, w.sku, w.missing_since, w.lifecycle_status, w.created_at, w.last_seen_at FROM catalog.listing_sources ls JOIN catalog.walmart_items w ON w.store = ls.store AND w.sku = ls.sku WHERE ls.source_type = 'amz' AND ls.source_key = ANY(%(asins)s::text[])) SELECT store, bool_or(missing_since IS NULL AND coalesce(upper(lifecycle_status),'ACTIVE') = 'ACTIVE'), array_agg(DISTINCT sku), min(created_at), max(last_seen_at) FROM hit GROUP BY store。两条腿各自走索引(walmart_items_sku_idx / listing_sources_key_idx),UNION 去重防同一行两边都命中。123-124 的索引注释补一句「两条腿各命中一个索引;写成 OR 会两个都用不上」。docstring 第 8 行 ①号证据源那一行补「身份按登记簿 amz 键与裸 sku 取并集」。

**为什么**:sku_plan §3.6:risk_trace 是 TRO/钓鱼波及展开的四证据源之一,①号源按裸 sku 匹配,切换后同一 ASIN 的新码行整条追不出来——而这条链的口号(docstring:5-6)就是「宁可多追一家,不能漏一家」。存量下第二条腿是第一条腿的子集,并集不变。②号源 _SOURCES_SQL(138-143)已按 source_type='amz' 反查 source_key,不需改。

**测试**:
- tests/test_risk_trace.py::test_items_leg_has_all_three_still_listed_conditions(既有,断言仍成立)
- tests/test_risk_trace.py::test_every_sql_param_is_cast_in_risk_trace(既有,新 SQL 两处 ::text[] 均带 cast)
- tests/test_risk_trace.py::test_items_leg_also_matches_through_the_registry(新增)
- tests/test_risk_trace.py::test_pg_traces_all_four_evidence_sources(既有 pg 用例,补一行「新码行也被 ①号源追到」的种子)

**验收**:python -m pytest -q tests/test_risk_trace.py

#### 0a-18 · `workflows/product_refresh.py` · 63-75(_SQL_TARGETS,SELECT DISTINCT w.sku 在 68)、78-90(_targets,过滤在 89)、57-58(_ASIN_RE)

**改动**:_SQL_TARGETS 的 68-70 改为 SELECT DISTINCT coalesce(ls.source_key, w.sku) AS asin FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' LEFT JOIN latest_status s ON s.store = w.store;71-73 三个条件不变;74 行 ORDER BY w.sku 改为 ORDER BY 1。_targets 的 89 行 _ASIN_RE 过滤与 90 行 dropped 计数保留不动,57-58 的注释补一句「这条正则是**采集侧的合法 ASIN 形态闸**(与 idents.ASIN_RE、sources_backfill._ASIN_RE 同口径),不是 SKU→ASIN 规则,两者不是同一能力,守门测试也因此不扫它」。

**为什么**:sku_plan §3.3:切换后 SKU 不再是 ASIN 形态,推采集目标会静默归零 ⇒ 维护链新鲜度的源头断掉。存量下 coalesce 恒等 w.sku,推的集合逐个相同。注释那一句是为了让守门测试的口径解释得通(_ASIN_RE 不进 extract_asin 白名单,因为它是另一个能力)。

**测试**:
- tests/test_product_ingest.py::test_refresh_targets_sql_gates(既有 :593;把 :599 的 assert "SELECT DISTINCT w.sku" 改成 assert "coalesce(ls.source_key, w.sku)" 与 assert "SELECT DISTINCT";596-598 三道闸断言与 600 的 TIMEOUT_HOURS/DANGEROUS 断言不变)
- tests/test_product_ingest.py::test_refresh_targets_come_from_the_registry_key(新增)
- tests/test_product_ingest.py::test_refresh_filters_non_asin_skus(既有 :603,不变)

**验收**:python -m pytest -q tests/test_product_ingest.py && python cli.py product_refresh --dry-run(推送 ASIN 数与改前一致)

#### 0a-19 · `services/audit_rules.py` · 170-181(load_context 里的实证 PT 段:注释 171-174、import 175、SQL 176-177、循环 179-181)

**改动**:175 行 from services.sku_asin import extract_asin 改为 from services.sku_asin import pick_asin;176-177 的 SQL 改为 cur.execute("SELECT ls.source_key, w.sku, w.product_type FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.product_type IS NOT NULL AND w.product_type <> ''");179-181 循环改为 for key, sku, pt in cur.fetchall(): asin = pick_asin(key, sku) or sku; by_asin.setdefault(asin, set()).add(pt)。171-174 的注释保留并补一句「登记簿优先、模式提取兜存量,规则与优先级的唯一出处仍是 services/sku_asin」。182-187 的「先数 DISTINCT 再过闸」注释与逻辑一字不动。

**为什么**:sku_plan §3.3:实证 PT 对新码失明。这里必须走 Python 侧的 pick_asin 而不是纯 SQL coalesce——今天三段式 SKU 是靠 extract_asin 取中段 ASIN 的(2026-08-11 评审 I-1 修的正是这个,注释 171-174 写着),纯 SQL 换成 coalesce 会把这批行的键换成三段式原文,那是真正的行为变化。

**测试**:
- tests/test_audit_rules_wiring.py::test_confirmed_pt_keys_prefer_the_registry_then_the_pattern(新增:假 conn 喂三行——裸 ASIN 行、三段式行、带 source_key 的不透明码行——断言三个键分别是 sku / 中段 ASIN / source_key)
- tests/test_audit_rules_wiring.py::test_confirmed_pt_still_drops_cross_store_disagreements(新增:同 ASIN 两店两 PT 仍不采信)

**验收**:python -m pytest -q tests/test_audit_rules_wiring.py

#### 0a-20 · `services/alloc_survey.py` · 184-190(_SQL_ONLINE,注释 174-183)、279-291(enrich,拆包 289、取 asin 291)、796-797(load_rows 收集 asins)

**改动**:_SQL_ONLINE 改为 SELECT w.store, w.sku, w.product_type, w.published_status, ls.source_key FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.missing_since IS NULL AND coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE' ORDER BY w.store, w.sku。**174-183 的「必须排 RETIRED」注释与 lifecycle 条件原样保留**(见 why)。enrich 的 289 行拆包改为 store, sku, pt, published, src_key = (list(it) + [None] * 5)[:5];291 行改为 asin = sku_asin.pick_asin(src_key, sku)。796-797 改为 asins = sorted({a for a in (sku_asin.pick_asin(it[4] if len(it) > 4 else None, it[1]) for it in items) if a})。**补位写法必须保留**:tests/test_alloc_audit.py:19-26 的 ITEMS 是 4 元组直接喂 enrich,补位成 None 后回落 extract_asin = 今天的行为。

**为什么**:sku_plan §3.3:全落 no_asin ⇒ 冲突判定、品牌占用、类目建议整片失明。全表级取数放在 SQL 里 LEFT JOIN 拿键,避免对十万级 (store,sku) 做一次巨大的 unnest(0a-07 docstring 写死的分工)。⚠ **本条不动 lifecycle 条件**,是对 synthesis required_changes #6 后半句(「alloc_survey._SQL_ONLINE 也对齐去重闸口径、不再排 RETIRED」)的**显式驳回**(采纳审查者一的 finding:原稿在 0a-19 与 B2-24 给了同一文件两条相反指令)。理由三条:① 这条 SQL 管的是占用与冲突口径,不是派工口径,alloc_survey.py:176-183 的 2026-08-15 结论仍成立(退市行不算活货位,不然占用与冲突组凭空多出一批);② 仓内有两条守门钉着它 —— tests/test_alloc_audit.py:846-847 与 tests/test_store_perf.py:203;③ 去掉它是真行为变化,不属零变化批次。要对齐须另立 item 并给差额清单。

**测试**:
- tests/test_alloc_audit.py::test_online_query_is_ordered_and_carries_published(既有 :282;:284 的 ORDER BY 断言改成 "ORDER BY w.store, w.sku" —— JOIN 后 store 列名有歧义必须限定)
- tests/test_alloc_audit.py::test_online_sql_excludes_retired_rows(既有 :839,:846-847 两条断言必须**保持原样绿**)
- tests/test_alloc_audit.py::test_enrich_prefers_the_registry_key_over_the_pattern(新增:5 元组带 source_key 的行)
- tests/test_alloc_audit.py::test_enrich_maps_asin_brand_and_category(既有 :91,4 元组用例必须仍绿——这就是补位写法的锁)
- tests/test_alloc_audit.py::test_enrich_counts_unresolvable_sku(既有 :102,不变)
- tests/test_store_perf.py(既有 :203 断言 lifecycle 仍在 sv._SQL_ONLINE,保持绿)
- tests/test_claim_audit.py(既有 :29 monkeypatch 打在 ca.sv.sku_asin.extract_asin 上;pick_asin 在 sku_asin 模块内按全局名调 extract_asin,打桩仍生效,不需改)

**验收**:python -m pytest -q tests/test_alloc_audit.py tests/test_claim_audit.py tests/test_store_perf.py

#### 0a-21 · `workflows/alloc_push.py` · 46-53(_SQL_ONLINE,注释 46-48)、67-73(run 里取 online 集合,fetchall 在 71-72、滤空在 73)

**改动**:**本批只做身份收口一步**:_SQL_ONLINE 改为 SELECT w.store, w.sku, ls.source_key FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.missing_since IS NULL AND coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE' AND ls.abandoned_at IS NULL;72 行改为 online = {(s, sku_asin.pick_asin(k, sku)) for s, sku, k in cur.fetchall()};73 行滤空不变。46-48 的注释保留(lifecycle 那一条的理由不动)并补一句「abandoned_at IS NULL 在批次 2 之前恒真(全库该列为 NULL 且这是 LEFT JOIN),提前落地是为了让写侧切换只改一处」。⚠ **口径对齐(去掉 lifecycle 那一行)不在本批**,它是真行为变化,见 decisions C —— 本 item 的 change 到此为止,执行者不要顺手做掉。

**为什么**:sku_plan §3.3 + synthesis 规则 4:身份收口不做,已在架的品会被重新派工、重复上架(本工作流 DANGEROUS=True,直接写飞书上架表)。⚠ 采纳审查者三:原稿把「第二步:去掉 lifecycle 过滤」写在同一个 item 的 change 字段里,评审很容易连着做掉,而那一步**在存量数据上就立刻生效**(catalog_sync 显式扫一轮 RETIRED,那批行 missing_since 为 NULL,今天算「不在架 ⇒ 该派工」,去掉之后变成「算在架 ⇒ 不派工」)。已把它物理移出 change 字段,只留在 decisions C 里。

**测试**:
- tests/test_alloc_push.py::test_products_already_online_are_not_pushed(既有 :64,夹具行补第三列 source_key)
- tests/test_alloc_push.py::test_online_set_reads_the_registry_key(新增)
- tests/test_alloc_push.py::test_online_set_still_excludes_retired(新增:反向钉死 lifecycle 条件仍在,批次 2 若对齐口径再翻转这条断言)
- tests/test_alloc_push.py::test_abandoned_predicate_is_inert_while_no_code_is_abandoned(新增:abandoned_at 为 NULL 的行仍被算作在架)
- tests/test_alloc_push.py::test_dry_run_writes_nothing(既有 :177,不变)
- tests/test_sku_guard.py::test_abandoned_at_predicate_only_where_the_whitelist_says

**验收**:python -m pytest -q tests/test_alloc_push.py && python cli.py alloc_push --dry-run(待派工行数与改前一致)

#### 0a-22 · `workflows/alloc_plan.py` · 105-108(_SQL_ONLINE_SKU)、111-127(_listed_asins,fetchall 在 123-124、推导式在 125-127)

**改动**:_SQL_ONLINE_SKU 改为 SELECT w.store, w.sku, ls.source_key FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.missing_since IS NULL AND w.published_status = 'PUBLISHED'(published_status 这一条是本工作流自己的口径,不动);125-127 的推导式改为 {a for store, sku, k in rows if store in registered and not sv.is_excluded(store) and (a := sku_asin.pick_asin(k, sku))}。112-120 的 docstring 不动。

**为什么**:sku_plan §3.3:「已在架」集合恒空 ⇒ 方案表把已上架的品当候选重排一遍。

**测试**:
- tests/test_alloc_plan.py::test_listed_asins_read_the_registry_key(新增)
- tests/test_alloc_plan.py::test_pending_delist_is_deduped_across_the_two_gates(既有 :117,夹具补第三列)

**验收**:python -m pytest -q tests/test_alloc_plan.py && python cli.py alloc_plan --dry-run

#### 0a-23 · `workflows/alloc_products.py` · 60-63(_SQL_ONLINE_SKU,注释 55-59)、97-103(run 里建 online 字典,fetchall 在 100)

**改动**:同 0a-22 的 SQL 改法(本工作流同样是 published_status = 'PUBLISHED' 口径);100-103 改为 for store, sku, k in cur.fetchall(): a = sku_asin.pick_asin(k, sku); if a: online.setdefault(a, set()).add(store)。55-59 的「占用店 vs 在线店是两个问题」注释不动。

**为什么**:同 0a-22:在线店一列恒空 ⇒ 「占用在 A、货在 B」这类信息全抹掉。

**测试**:
- tests/test_alloc_products.py::test_online_stores_read_the_registry_key(新增)
- tests/test_alloc_products.py::test_csv_carries_price_sales_revenue_and_both_owner_columns(既有 :163,夹具补第三列)

**验收**:python -m pytest -q tests/test_alloc_products.py && python cli.py alloc_products --dry-run

#### 0a-24 · `workflows/list_new.py` · 302-306(_SQL_LISTED_ASINS,注释 302-303)、350(_GateState.listed_pairs 字段注释)、366-371(_load_gate_state 里的装载,注释 367-370)

**改动**:_SQL_LISTED_ASINS 改为 SELECT DISTINCT w.store, coalesce(ls.source_key, w.sku) FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.missing_since IS NULL AND ls.abandoned_at IS NULL。**必须 LEFT JOIN**(未登记的在架行也要拦,否则 db_init 回填之间新出现的行静默漏闸);**不加 lifecycle 条件**——RETIRED 行只要码未弃就拦,退市档案不由 list_new 复活(2026-08-28 定稿,plan.md:166 的 7,342 行批量复活事故)。371 行装载语句不变(仍是 {(store, sku) for store, sku in ...},第二列现在是身份键),350 行字段注释与 367-370 的注释改成「在架 (店铺, **身份键**) 对」;1227 行的闸判 (store_name, r["asin"]) 不动。302-303 注释补两句:① 第二列是身份键不是 SKU 串;② **本闸只按 amz 身份键去重**(LEFT JOIN 带 source_type='amz'),match 行的码寿命由 match_listing 自己的通道管 —— 这是与 synthesis rule 4 字面写法(不带 source_type)的一处**有意偏差**,后果是「已弃码的 match 僵尸行仍会挡新码」,对只处理 amz 行的 list_new 无实害。

**为什么**:sku_plan §3.4 第一条 + synthesis 规则 4:去重闸失效是切换后最贵的一条(同店重复上架,烧 UPC 烧 MP_ITEM 配额,而且不报错)。存量下 coalesce 恒等 w.sku、abandoned_at 恒 NULL,拦截集合逐个相同。⚠ 采纳审查者一的 minor:原稿加了 source_type='amz' 却没说明它与 synthesis 契约的偏差,契约与实现的差没有任何东西钉住 —— 现在既写进注释,也加了一条正向测试把这个有意偏差钉死。

**测试**:
- tests/test_list_new.py::test_dedup_gate_reads_the_registry_key(新增)
- tests/test_list_new.py::test_dedup_gate_has_no_lifecycle_condition(新增:SQL 文本不含 lifecycle,防有人照抄 alloc_push 的排 RETIRED 写法)
- tests/test_list_new.py::test_dedup_gate_still_blocks_unregistered_rows(新增:LEFT JOIN 而非 JOIN)
- tests/test_list_new.py::test_dedup_gate_ignores_non_amz_registry_rows(新增:钉住上面那处有意偏差)

**验收**:python -m pytest -q tests/test_list_new.py && python cli.py list_new --dry-run;体检② 新旧行数必须相等

#### 0a-25 · `workflows/list_new.py` · 659(MAX_LIST_ATTEMPTS)、661-669(_SQL_ATTEMPTS)、672-701(_retry_rows:cur.execute 在 690-691、阈值比较在 695)

**改动**:_SQL_ATTEMPTS 改为按 (店, 身份键) 计数并加代际过滤:SELECT t.store, t.asin, count(*) FROM ops.feed_items f LEFT JOIN catalog.listing_sources ls ON ls.store = f.store AND ls.sku = f.sku AND ls.source_type = 'amz' JOIN unnest(%s::text[], %s::text[]) AS t(store, asin) ON f.store = t.store AND coalesce(ls.source_key, f.sku) = t.asin LEFT JOIN LATERAL (SELECT max(occurred_at) AS since FROM catalog.product_events e WHERE e.store = t.store AND e.event = %(abandoned)s AND e.detail->>'source_key' = t.asin) g ON true WHERE f.feed_type = 'MP_ITEM' AND (g.since IS NULL OR f.submitted_at > g.since) GROUP BY t.store, t.asin。事件名用 product_events.SKU_ABANDONED 常量传参(不写字面量)。690-691 的传参不变(本来传的就是 store 与 r["asin"]),695 的比较键不变,659 的 MAX_LIST_ATTEMPTS=3 不变(注释里的「同 (店铺,SKU)」改成「同 (店铺,身份键)」)。注释补三句:无弃码事件 ⇒ 跨码累计(退化成今天的口径);有弃码事件 ⇒ 只数最近一次弃码之后的提交;代际上限(同 (store,source_type,source_key) 弃码行数 ≥ 阈值即拦)属批次 2。

**为什么**:sku_plan §3.4 第二条:每次新码 count 恒 0 ⇒ FAILED 无限重试。⚠ 采纳审查者一的 minor 并取其**选项②**:原稿用 coalesce(e.asin, e.sku) = t.asin 去认弃码事件,而 catalog.product_events.asin 要到 0b-11 才经登记簿反查(今天 services/product_events.py:167 仍是 extract_asin(r["sku"])),在「0a 已合、0b 未合」的窗口里 abandon 写出的事件 asin 恒为 NULL、sku 是不透明码 ⇒ 代际过滤永不命中。改读 abandon 自己写进 detail 的 source_key(0a-10 已把 detail 结构约定登记进常量区注释),**本条因此不依赖 0b-11 的落地时序**,0a 可独立先合。⚠ 归并风险仍在:同一 (店, ASIN) 在 feed_items 里若有多个不同 sku,按身份键计数会合并而可能提前触顶 —— 体检⑥ 必须返回 0 才叫零行为变化。

**测试**:
- tests/test_list_new.py::test_attempts_are_counted_per_store_and_identity_key(新增)
- tests/test_list_new.py::test_attempts_fall_back_to_cross_code_counting_without_an_abandon_event(新增)
- tests/test_list_new.py::test_attempts_only_count_after_the_last_abandon_event(新增)
- tests/test_list_new.py::test_attempts_generation_filter_reads_the_event_detail_not_the_asin_column(新增:钉住不依赖 0b-11)
- tests/test_list_new.py::test_retry_cap_is_still_three(既有/新增,断言 MAX_LIST_ATTEMPTS == 3)

**验收**:python -m pytest -q tests/test_list_new.py;体检⑥ 必须返回 0

#### 0a-26 · `workflows/list_new.py` · 704-710(_FAMILY_LISTED_SQL)、728-731(_variant_plan 里的调用与参数)

**改动**:_FAMILY_LISTED_SQL 改为 SELECT w.sku, w.variant_group_id, coalesce(w.variant_group_info->>'isPrimary','') AS is_primary FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz' WHERE w.store = %(store)s AND w.missing_since IS NULL AND coalesce(ls.source_key, w.sku) = ANY(%(asins)s::text[]);参数名 skus 改成 asins(传进来的本来就是同族 ASIN,名字一直是错的),729-731 同步改键名。732-734 的循环与 735-737 的 except 兜底、731 的 a != r["asin"] 都不动。SQL 上方补一行注释:**本条有意不加 abandoned_at 谓词** —— 变体同族查的是「这家店此刻还挂着哪些同族成员」这个在架事实,与码是否已弃用无关;abandoned_at 只在 mint 复用、去重闸、alloc_push 三处出现(消费方契约)。

**为什么**:sku_plan §3.4 第三条:变体组查同族已在架退化 ⇒ 新成员拿不到已有 variantGroupId。⚠ 那行注释是采纳审查者一的 minor:required_changes #5 说 _FAMILY_LISTED_SQL「同口径经登记簿反查」,原稿没加 abandoned_at 也没写为什么不加,留下一处没人解释的差异。

**测试**:
- tests/test_variant_group.py::test_list_new_lookup_is_store_scoped_and_failure_tolerant(既有 :406,:415 起的断言里 store = %(store)s 与 missing_since IS NULL 仍在)
- tests/test_variant_group.py::test_family_lookup_matches_through_the_registry(新增:断言含 coalesce(ls.source_key, w.sku) = ANY(%(asins)s::text[]),且不含 abandoned_at)

**验收**:python -m pytest -q tests/test_variant_group.py tests/test_variant_conn.py

#### 0a-27 · `workflows/product_clear.py` · 20-21(模块 docstring 的动作映射段)

**改动**:20 行「停用/下架 → RETIRE_ITEM(可恢复)」后补一句注(不改任何代码):「⚠ 『可恢复』在本系统里只是一个窗口:problem_scan 的扫描面是 published_status 非 PUBLISHED 且 missing_since IS NULL、**无 lifecycle 豁免**(workflows/problem_scan.py:77-83),在途只挡 48h(:102-111),而退市档案的观测形态正是 UNPUBLISHED + end date has passed ⇒ 停用的品一到两轮就会被自动链建议 DELETE。要让停用真正可恢复,须给 problem_scan 加豁免(决策 A,尚未拍板)。」

**为什么**:补审查者一点名的 missing M1(required_changes #1 里唯一没有 item 承载的一条)。它**在决策 A 取默认值时同样必须改** —— 默认值(RETIRE 不弃码、不加豁免)恰恰就是「可恢复只是个窗口」那种情况,原稿却只把它写在 B2 decisions A 的 alternative 分支与横切 risks 里(risks 不是 item,没有 file/lines/验收)。放进 0a 是因为决策 A 的默认值正由 0a 的 ABANDON_REASONS 词表编码,记录该默认值后果的注释应与它同批。纯注释,零代码风险。

**测试**:
- (无)

**验收**:grep -n 'problem_scan' workflows/product_clear.py && python -m pytest -q tests/test_product_clear.py

#### 0a-28 · `tests/test_sku_guard.py` · 新文件

**改动**:**整套 SKU 改造唯一的一份守门文件**,形态照抄 tests/test_feishu_guard.py(白名单 dict 在文件顶部、每条写理由与预期收口批次、末尾一条 test_the_whitelists_do_not_rot)。七条断言:① test_sku_and_asin_hard_equality_is_extinct —— 正则扫 services/**.py、workflows/**.py、refdata/schema.sql,匹配 x.asin = y.sku / x.sku = y.asin 形态;**白名单恰好一项**:workflows/product_audit.py(mode=online 候选的第一条腿,理由见 0a-16,永久豁免不是待办)。② test_extract_asin_callers_are_whitelisted —— AST 引用集 + 文本 grep 双做(conventions §五:按名字 grep 双向出错);0a 之后允许的文件:services/sku_asin.py(自身)、services/order_lines.py(→0b)、services/product_events.py(→0b)、services/blacklist.py(→0b)、workflows/order_audit.py(→0b)、workflows/order_asin_normalize.py(仅 docstring 提及,→0b)、workflows/order_history_import.py(永久:只导旧数据)、workflows/pt_backfill.py(永久:旧库);0a 收口的五个文件(services/audit_rules.py、services/alloc_survey.py、workflows/alloc_push.py、workflows/alloc_plan.py、workflows/alloc_products.py)出现即红。**不扫 is_standard_asin**(workflows/brand_scrape.py:91、workflows/product_refresh.py:58 是「合法 ASIN 形态闸」,与 SKU→ASIN 是两个能力)。③ test_abandoned_at_predicate_only_where_the_whitelist_says —— 全仓 *.py 里出现 'abandoned_at' 的文件必须在白名单里;0a 登记三项:services/sku_codec.py、workflows/list_new.py、workflows/alloc_push.py;**refdata/schema.sql 显式排除在 .py 扫描面之外**并在白名单注释里写明「DDL 的部分索引条件不是消费方过滤,不计入」;批次 3 只往白名单加 workflows/sku_migrate.py 一行。④ test_only_sku_codec_writes_the_abandon_columns —— 全仓扫 UPDATE catalog.listing_sources,只允许 services/sku_codec.py。⑤ test_the_registry_table_has_exactly_two_insert_sites —— 全仓扫 INSERT INTO catalog.listing_sources,只允许 services/listing_sources.py 与 services/sku_codec.py。⑥ test_schema_opaque_predicate_matches_the_codec_alphabet / test_the_opaque_alphabet_is_born_only_in_sku_codec / test_the_live_unique_index_is_named_once_and_carries_replaced_by / test_backfill_regex_agrees_with_sources_backfill —— 读 refdata/schema.sql,断言两条唯一索引的字符类字面量与 services.sku_codec._ALPHABET 逐字相同、都带 AND sku ~ '[A-Z]';活码唯一索引名在全文只出现一次且条件含 replaced_by IS NULL;字母表字面量在 registry/**.py 里一次都不出现;回填正则右锚且与 sources_backfill._ASIN_RE 同口径。⑦(pg,照抄 tests/test_risk_trace.py:216-252 的 _DSN/_pg_up/needs_pg/pg 夹具)test_pg_two_stores_may_share_a_legacy_sku_but_never_a_new_code / test_pg_two_live_rows_may_share_a_legacy_key_but_never_a_minted_one —— 各插两行验证约束方向。

**为什么**:sku_plan §7:波及面一次做完并加守门防止切换后又长出新洞。⚠ **本条把四位审查者一致点名的 blocker 处置掉**:原稿的 tests/test_sku_identity_guard.py 与 0b 的 test_sku_asin_consumers.py、批次 2 的 test_sku_codec_guard.py、横切的 test_sku_guard.py 是四份文件,extract_asin 白名单重复 3 处、abandoned_at 白名单重复 4 处且数目三种口径(三处/四处含 schema/四处含 sku_migrate)、UPDATE 白名单重复 3 处、字母表一致性断言两条互斥 —— 守门测试自己犯了它要守的 conventions §六。裁决:文件名取 tests/test_sku_guard.py(与仓内既有 tests/test_feishu_guard.py 同族),0a 建齐全部白名单,0b/1/2/3 与横切的对应 item 一律降级为「只改这一份的白名单条目」。另两处纠正:白名单里 product_audit.py 是永久项(不是空表);C0-GUARD-2 把 services/audit_rules.py 标成「→0b 收口」而 0a-19 在 0a 就收口了,按那份白名单执行会让这条守门在 0a 合并当天假绿。

**测试**:
- tests/test_sku_guard.py(本文件自身,含 test_the_whitelists_do_not_rot)

**验收**:python -m pytest -q tests/test_sku_guard.py

### 新模块

- `services/sku_codec.py`
  - API:**编码规则常量(全仓唯一之家;registry 只放 SKU_SOURCE_LETTERS)**:_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"(30 符号,剔 0/O、1/I/L、U);_RANDOM_LEN = 11;_LEN = 12;_MAX_DRAWS = 5;DRYRUN_PLACEHOLDER = "DRYRUN000000"(12 位但含 0 ⇒ is_opaque 恒 False,与 list_new._UPC_PLACEHOLDER 同纪律)。

**弃码原因词表**:ABANDON_DELETE_VERIFIED='delete_verified' / ABANDON_SKU_LOCKED='sku_locked' / ABANDON_UPC_CONFLICT='upc_conflict' / ABANDON_SKU_UPDATE='sku_update';ABANDON_REASONS = frozenset(以上四个);_BURN_STATUS = {delete_verified: upc_pool.BURN_DELETE, sku_locked: upc_pool.BURN_LOCK}(upc_conflict 不烧——号已由 listing_sheet.mark_conflict 标 conflict;sku_update 不烧——码换了 item 还在)。

is_opaque(sku) -> bool:归一(strip+upper)后 len == _LEN 且每个字符都在 _ALPHABET 里 且 至少含一个 A-Z 字母。**最后那一条不能省**:12 位纯数字的沃尔玛 item id 若恰好不含 0/1 会被误判成新码(schema.sql 的两条部分唯一索引条件与它逐字对齐,守门测试钉住)。

source_of(sku) -> str | None:is_opaque 才查 registry.resources.SKU_SOURCE_LETTERS 的反查表,取首字母对应的 source_type;字母表未定或首字母未登记返 None(不猜)。

mint(conn, store, source_type, source_key, *, workflow) -> str:**没有 dry_run 形参**(写库函数不设「这次不写」模式,conventions §六;空跑由调用方不调它 + 用 DRYRUN_PLACEHOLDER 表达,而 workflows/list_new.py:1583 的 `if not execute: return` 本来就在 :1650 调 _prep_rows 之前)。① 复用查询:SELECT sku FROM catalog.listing_sources WHERE store=%s AND source_type=%s AND source_key=%s AND abandoned_at IS NULL AND replaced_by IS NULL ORDER BY created_at LIMIT 1 —— 命中即复用(**不按形态过滤**:存量 sku=asin 的活行照样复用,存量迁码是批次 3 的事;条件与 listing_sources_live_uidx / live_key_idx 的局部条件逐字对齐)。② 未命中:letter = SKU_SOURCE_LETTERS[source_type](KeyError → ValueError 点名 source_type 与 registry 常量名),code = letter + 11 位 secrets.choice(_ALPHABET),INSERT … ON CONFLICT DO NOTHING RETURNING sku。**拿不到行时分两个明确分支,各记各的日志计数**(conventions §六真兜底三要件,不做 catch-all):(a) 先重跑一次 ① —— 查到 ⇒ 说明另一个进程刚给同一个品发了码,返回对方那个码,logger.warning + 模块级 _concurrent_mint 计数(这与 mint 的复用语义一致,不是失败);(b) 仍查不到 ⇒ 撞的是 (store,sku) 主键或 listing_sources_opaque_sku_uidx 全局唯一 = 随机撞码,logger.info + _sku_redraws 计数后重抽,至多 _MAX_DRAWS 次,仍撞抛 RuntimeError(「随机源坏了,不是运气」)。抽码与登记在同一函数同一事务,不存在抽了没登记。

abandon(conn, store, sku, reason, *, replaced_by=None) -> bool:reason 不在 ABANDON_REASONS 抛 ValueError;同一事务内 UPDATE catalog.listing_sources SET abandoned_at=now(), abandoned_reason=%s, replaced_by=coalesce(%s, replaced_by) WHERE store=%s AND sku=%s AND abandoned_at IS NULL RETURNING source_type, source_key;返 0 行 = 已弃或不存在 ⇒ 幂等 no-op 返 False;返 1 行且 source_type='amz' 且 reason 在 _BURN_STATUS 里 ⇒ 调 upc_pool 按 (store, source_key) 烧号并写对应状态(match 行 source_key 是 GTIN,只标不烧);最后 product_events.record_many 记一条 SKU_REPLACED(replaced_by 非空时)或 SKU_ABANDONED,**detail 必须带 source_key**(0a-25 的代际过滤读它,不读 product_events.asin 列),其余字段按 0a-10 登记的结构:{old_sku, new_sku?, reason, source_type, source_key, burned_upcs}。返 True。
  - docstring 规则:模块 docstring 必须钉死五件事:①「码弃用 ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用」三个同名异义,列名故意用 abandoned;② 四个弃码点(delete_verified 观测 / SKU_LOCKED RETIRE 成功+冷却 / 0101119 撞库 / SkuUpdate 观测确认)与「其余一切下架都不弃码」的反向清单(product_clear / problem_product_cleanup / maintenance / catalog_sync.mark_missing / feed_track 不得调 abandon);③ 消费方契约:resolve / 维护链 JOIN / 事件归并 / 订单反查一律**不按 abandoned_at 过滤**,该谓词只准出现在 mint、list_new 去重闸、alloc_push._SQL_ONLINE 三处(批次 3 起增 sku_migrate 候选选取为第四处;refdata/schema.sql 的部分索引条件是 DDL 不计入);④ **本模块是 12 位不透明码编码规则的唯一之家** —— 字母表 / 长度 / 随机段长 / 重抽上限 / 占位码 / is_opaque 判据都在这里出生,registry 只登记 SKU_SOURCE_LETTERS,schema.sql 的索引条件与本模块常量由守门测试对齐,任何人在别处再抄一份即违规;⑤ 文件头注明:本模块在批次 0a **只建不接线**,接线在批次 2(不是死代码,是批次待办 —— conventions §五「从未跑过、在批次待办里 = 活」)。每个函数 docstring 首行写「输入→输出」。
- `tests/test_sku_codec.py`
  - API:sku_codec 的全部单测;用假 conn(照抄 tests/test_sku_asin.py:75-96 的 _Cur/_Conn 写法)覆盖:字母表剔混淆字符 / 占位码永不被判成新码 / 来源字母互不相同且都在字母表内 / is_opaque 拒绝全部存量形态且要求至少一个字母 / source_of 在字母未定时返 None / mint 复用活行 / mint 抽码与登记同一次调用 / **mint 撞活码键时返回对方进程的码(不是重抽到抛错)** / mint 随机撞码时重抽并最终 fail loud / **mint 没有 dry_run 形参**(inspect.signature 断言) / mint 对未映射 source_type 抛 ValueError 并点名常量名 / abandon 幂等 / abandon 按 source_type 与 reason 分流烧号 / abandon 对 sku_update 不烧 / abandon 事件 detail 带 source_key / abandon 拒绝未登记 reason。
  - docstring 规则:每个用例 docstring 一句话说明「它防的是哪种静默失效」。撞码那两条要写清为什么必须分两个分支:合成一条会让并发双 mint 被诊断成随机源故障,排障方向全错。
- `tests/test_sku_guard.py`
  - API:见 item 0a-28 的七条断言。文件顶部是白名单 dict(每条:键=仓内相对路径,值=(预期收口批次, 理由)),末尾一条 test_the_whitelists_do_not_rot 核验白名单里登记的文件/函数/常量确实还存在。
  - docstring 规则:文件头写明三件事:① 本文件是**整套 SKU 改造唯一的一份守门文件**,0b/1/2/3 与横切包只准增删这里的白名单条目,不许再建第二份(四份并存是 conventions §六要禁的形态,且白名单已被实测出互相打架);② 白名单里每一条都要写清理由与预期收口批次,永久豁免(product_audit 的第一条腿、两个旧库导入工作流)要显式标 permanent;③ 与 tests/test_feishu_guard.py 的分工:同款纪律、不同域,改守门先改白名单、别删断言。

### DDL

```sql
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS abandoned_at    timestamptz;
```
```sql
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS abandoned_reason text;
```
```sql
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS replaced_by     text;
```
```sql
-- 全局 sku 唯一:只对不透明新码生效。存量 sku=asin 跨店重复是既成事实,无条件唯一建不起来
-- (db_init 一次 conn.execute 整份 schema.sql,一条失败全份回滚 —— workflows/db_init.py:41-43)。
-- 字符类必须与 services.sku_codec._ALPHABET 逐字一致(守门测试钉住);末尾 sku ~ '[A-Z]' 与
-- sku_codec.is_opaque 的「至少一个字母」同口径,防 12 位纯数字沃尔玛 item id 混进来。
-- ⚠ 名字与条件由批次 0a 定死,批次 2/3 与横切包一律引用、不许重建。
CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_opaque_sku_uidx
    ON catalog.listing_sources (sku)
    WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$' AND sku ~ '[A-Z]';
```
```sql
-- 活码键唯一:拦并发双 mint。同样只对新码生效——存量 match 行同一 GTIN 可能挂过多个
-- PHUMWMT 码,存量 amz 行也可能因回填 left(sku,10) 撞键;mint 只 INSERT 不透明码,限定
-- 形态后对 mint 的保护是完整的。**replaced_by IS NULL 本批就带上**(该列全库 NULL,谓词
-- 恒真,零行为变化),这样批次 3 一条索引都不必 DROP/CREATE,只做 indexdef 核验。
CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_live_uidx
    ON catalog.listing_sources (store, source_type, source_key)
    WHERE abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL
      AND sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$' AND sku ~ '[A-Z]';
```
```sql
-- mint 的复用查询用(要能看见存量活行,故不限形态);局部条件与 mint 的 WHERE 逐字对齐。
CREATE INDEX IF NOT EXISTS listing_sources_live_key_idx
    ON catalog.listing_sources (store, source_type, source_key)
    WHERE abandoned_at IS NULL AND replaced_by IS NULL;
```
```sql
-- 0a-03:存量一次性回填的判型正则右锚 + 去掉截断(替换 refdata/schema.sql:231-232 两行)。
-- 与生产在跑的 workflows/sources_backfill.py:46 `^B[0-9A-Z]{9}$` 同口径(conventions §六
-- 一个能力一条实现路径)。缺右锚会把 B0XXXXXXXX-2 这类重上后缀 SKU 判成 amz 且把
-- source_key 截成前 10 位,身份键与 SKU 从此不等,那批行会第一次进入删除意图产出面。
INSERT INTO catalog.listing_sources (store, sku, source_type, source_key, workflow)
SELECT store, sku,
       CASE WHEN sku ~ '^B0[A-Z0-9]{8}$' THEN 'amz' ELSE 'unknown' END,
       CASE WHEN sku ~ '^B0[A-Z0-9]{8}$' THEN sku END,
       'backfill'
FROM catalog.walmart_items
ON CONFLICT (store, sku) DO NOTHING;
```

### 文档同步

- docs/db_schema.md:161-177(listing_sources:三新列 + 三条索引全文 + 「三列只由 services/sku_codec 写」+ 回填正则与 sources_backfill 同口径)与 :179-191(upc_pool 状态机补 burned_delete/burned_lock 及其与 conflict 的分工)—— 由 0a-06 唯一交付,横切 C0-DOC-1 与批次 3 R2 的同名条目改为「核验在位」
- docs/conventions.md 新增 §九「SKU 身份口径」(本批建立,后续批次只补条目):① **身份表达式的两条可复制字面量** —— SQL 侧 `coalesce(ls.source_key, w.sku)` 且 ls 是 `ON ls.store=…AND ls.sku=…AND ls.source_type='amz'` 的 LEFT/INNER JOIN,Python 侧 `sku_asin.pick_asin(source_key, sku)`;② **abandoned_at IS NULL 的权威白名单** —— 消费方 .py 三处(sku_codec.mint / list_new 去重闸 / alloc_push._SQL_ONLINE),批次 3 起加 sku_migrate 候选选取为第四处,refdata/schema.sql 的部分索引条件是 DDL 不计入;③ **不透明码编码规则的唯一之家是 services/sku_codec.py**,registry 只登记 SKU_SOURCE_LETTERS;④ 守门只有一份 tests/test_sku_guard.py
- docs/sku_plan.md §6(把「全局 sku 唯一只能对新码生效,约束形态由批次 0a 工作包定」替换成本包定稿的三条索引 DDL 与名字)、§7 批次 0a 勾选与实际拆分说明(0a 只做读侧收口与积木;alloc_push 口径对齐第二步移到批次 2;alloc_survey._SQL_ONLINE 的 lifecycle 条件**不动**,并写明这是对 synthesis required_changes #6 后半句的显式驳回及其三条理由)、§3.2 表内 schema.sql:527 那一行补「视图 DDL 由批次 0a 唯一交付」
- docs/plan.md 决策日志:记一条「2026-09-02 批次 0a 定稿:身份表达式唯一出处;三条索引名与条件一次建到位(含 replaced_by IS NULL),后续批次不许重建;编码规则之家定在 services/sku_codec;守门只有 tests/test_sku_guard.py 一份;db_init 回填正则右锚修双轨;mint/abandon 建而不接线」
- services/risk_trace.py 模块 docstring 第 8 行(①号证据源,代码内文档,随 0a-17 一起改)与 services/sku_asin.py 模块 docstring(三个反查入口的分工,随 0a-07 一起改)

### 守门测试

- tests/test_sku_guard.py::test_sku_and_asin_hard_equality_is_extinct —— 全仓 *.py + schema.sql 里 `x.asin = y.sku` / `x.sku = y.asin` 形态;白名单**恰好一项**:workflows/product_audit.py(mode=online 第一条腿,永久豁免,理由=索引)
- tests/test_sku_guard.py::test_extract_asin_callers_are_whitelisted —— AST 引用集 + 文本 grep 双做;0a 收口的五个文件出现即红;不扫 is_standard_asin(另一个能力)
- tests/test_sku_guard.py::test_abandoned_at_predicate_only_where_the_whitelist_says —— 消费方 .py 三处,schema.sql 排除在扫描面外,批次 3 只加一行 sku_migrate
- tests/test_sku_guard.py::test_only_sku_codec_writes_the_abandon_columns —— 全仓只有 sku_codec 出现 UPDATE catalog.listing_sources
- tests/test_sku_guard.py::test_the_registry_table_has_exactly_two_insert_sites —— INSERT INTO catalog.listing_sources 只允许 listing_sources.register 与 sku_codec
- tests/test_sku_guard.py::test_schema_opaque_predicate_matches_the_codec_alphabet —— schema.sql 两条唯一索引的字符类 == services.sku_codec._ALPHABET,且都带 AND sku ~ '[A-Z]'
- tests/test_sku_guard.py::test_the_opaque_alphabet_is_born_only_in_sku_codec —— registry/**.py 里不出现字母表字面量(防审查者二/四点名的三处并存)
- tests/test_sku_guard.py::test_the_live_unique_index_is_named_once_and_carries_replaced_by —— schema.sql 全文中活码唯一索引名只出现一次且条件含 replaced_by IS NULL(防批次 3 的 DROP INDEX 打空 + 无守护裸建)
- tests/test_sku_guard.py::test_backfill_regex_agrees_with_sources_backfill —— db_init 回填判型与 workflows/sources_backfill.py:46 同口径且右锚
- tests/test_sku_guard.py::test_the_whitelists_do_not_rot —— 白名单登记项失效即点名(照抄 tests/test_feishu_guard.py:348)
- tests/test_sku_guard.py::test_pg_two_stores_may_share_a_legacy_sku_but_never_a_new_code
- tests/test_sku_guard.py::test_pg_two_live_rows_may_share_a_legacy_key_but_never_a_minted_one
- tests/test_list_new.py::test_dedup_gate_has_no_lifecycle_condition —— 防有人照抄 alloc_push 的排 RETIRED 写法(plan.md:166 的 7,342 行退市档案批量复活事故)
- tests/test_alloc_push.py::test_online_set_still_excludes_retired —— 反向钉死 0a 不做决策 C 第二步
- tests/test_alloc_audit.py::test_online_sql_excludes_retired_rows(既有 :839)与 tests/test_store_perf.py:203 —— 反向钉死 alloc_survey 的 lifecycle 条件不被顺手统一
- tests/test_sku_asin.py::test_resolve_agrees_with_extract_asin_on_every_legacy_shape 与 ::test_a_lowercase_registry_key_is_normalized_like_the_pattern_does —— 两条腿同口径(sku_plan §7 指定的测试钉 + 审查者三 F2)
- tests/test_order_asin_normalize.py::test_rules_are_not_reimplemented_here —— 既有守门(:114),必须保持绿(登记簿那一跳留在 services/sku_asin)
- tests/test_sku_codec.py::test_mint_has_no_dry_run_switch —— 钉死单一 dry-run 路径(防批次 2 再长出第二条)

### 验收命令

```bash
python -m pytest -q  # 全量必须全绿(改前先在基线提交上跑一遍存数)
```
```bash
python -m pytest -q tests/test_sku_codec.py tests/test_sku_asin.py tests/test_sku_guard.py
```
```bash
python cli.py db_init && python cli.py db_init  # 连跑两次证幂等;三条 ALTER + 三条索引 + 改过的回填必须一次通过(整份 schema.sql 一次 execute,一条失败全份回滚)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources' ORDER BY 1"  # 恰好四条;live_uidx 的条件必须含 replaced_by IS NULL
```
```bash
★体检①(**合并硬闸**,必须返回 0,不为 0 一律不合并、先修数据)psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) FROM catalog.listing_sources WHERE source_type='amz' AND source_key IS NOT NULL AND source_key <> sku"  # 非 0 = 存在 B0 开头且长度>10 的存量 SKU 被旧回填截成了前 10 位,这批行改造后会第一次进入删除意图产出面(见零变化论证 2)。修法:把这批行 UPDATE 成 source_type='unknown',或按人工判定改正 source_key,再重跑本条
```
```bash
★体检①b(**合并硬闸**,必须返回 0)psql … -c "SELECT count(*) FROM catalog.listing_sources WHERE source_type='amz' AND source_key IS NOT NULL AND source_key !~ '^(?=.*[A-Z])[A-Z0-9]{10}$'"  # 非规范大写 ASIN 的登记键(小写/带空白/GTIN 混入);pick_asin 的形态闸会让它们回落模式提取,与今天同值,但存在即说明上架表 B 列有脏值,先清洗
```
```bash
体检②(去重闸口径新旧行数必须相等)psql … -c "SELECT (SELECT count(*) FROM (SELECT DISTINCT store, sku FROM catalog.walmart_items WHERE missing_since IS NULL) a) AS old_n, (SELECT count(*) FROM (SELECT DISTINCT w.store, coalesce(ls.source_key, w.sku) FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store=w.store AND ls.sku=w.sku AND ls.source_type='amz' WHERE w.missing_since IS NULL AND ls.abandoned_at IS NULL) b) AS new_n"
```
```bash
体检③(未登记在架行数;fallback 覆盖面 + 0a-03 的受益面,只记录不卡关)psql … -c "SELECT count(*) FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store=w.store AND ls.sku=w.sku WHERE ls.sku IS NULL AND w.missing_since IS NULL"
```
```bash
体检④(活码键重复,给批次 2 的 mint 预警)psql … -c "SELECT source_type, count(*) FROM (SELECT store, source_type, source_key FROM catalog.listing_sources WHERE source_key IS NOT NULL AND abandoned_at IS NULL GROUP BY 1,2,3 HAVING count(*)>1) t GROUP BY 1"
```
```bash
体检⑤(存量 sku 跨店重复条数;证明全局唯一只能限不透明码)psql … -c "SELECT count(*) FROM (SELECT sku FROM catalog.listing_sources GROUP BY sku HAVING count(*)>1) t"
```
```bash
★体检⑥(_SQL_ATTEMPTS 归并风险,必须返回 0)psql … -c "SELECT count(*) FROM (SELECT f.store, coalesce(ls.source_key, f.sku) AS asin FROM ops.feed_items f LEFT JOIN catalog.listing_sources ls ON ls.store=f.store AND ls.sku=f.sku AND ls.source_type='amz' WHERE f.feed_type='MP_ITEM' GROUP BY 1,2 HAVING count(DISTINCT f.sku)>1) t"
```
```bash
意图集合对拍(sku_plan §7 指定的唯一实测):在基线提交上跑 `python cli.py maintenance_scan -p preview=1 > /tmp/before.txt`,合并后同机同时段跑 `python cli.py maintenance_scan -p preview=1 > /tmp/after.txt`,`diff /tmp/before.txt /tmp/after.txt` 只许差时间戳类字样;**按 kind 分组的条数必须逐项相等,且 delete 类的差额单列出来给所有者看**(不只看总数——0a 唯一可能扩大的就是删除面)
```
```bash
python cli.py product_refresh --dry-run  # 推送 ASIN 数与改前一致
```
```bash
python cli.py alloc_push --dry-run;python cli.py alloc_plan --dry-run;python cli.py alloc_products --dry-run  # 三份「已在架」计数与改前一致
```
```bash
python cli.py list_new --dry-run  # 去重闸拦截条数(counts['dedup'])与改前一致
```
```bash
python cli.py product_audit -p mode=online --dry-run  # 候选数与改前一致
```
```bash
EXPLAIN 三处(防计划退化,R3;**用可执行形态,不要把带 %(name)s 占位符的 SQL 粘进 psql**):python - <<'PY'
from registry import db
from services import maintenance_intents as m
SQLS = {"variant_offset": (m._SQL_VARIANT_OFFSET, {"min_batches": 2}),
        "conflicts_view": ("SELECT * FROM catalog.audit_listing_conflicts", None)}
with db.pg_conn() as c, c.cursor() as cur:
    for name, (sql, args) in SQLS.items():
        cur.execute("EXPLAIN " + sql, args)
        plan = "\n".join(r[0] for r in cur.fetchall())
        assert "Seq Scan on walmart_items" not in plan and "Seq Scan on products" not in plan, (name, plan)
        print(name, "OK")
PY  # product_audit mode=online 的候选 WHERE 同法(from workflows import product_audit; product_audit._pick_where('online', {}))
```

### 决策点

- **A|product_clear 停用(RETIRE)是否给 problem_scan 加豁免**
  - 默认:默认假设:RETIRE **不弃码**(abandon 的原因词表里没有 product_clear_retire 这一项),豁免与否另议。0a 按此实现:ABANDON_REASONS = {delete_verified, sku_locked, upc_conflict, sku_update} 四项,守门测试反向钉死 product_clear / problem_product_cleanup / maintenance / catalog_sync.mark_missing / feed_track 不得调 abandon;并由 0a-27 把「可恢复只是个窗口」的事实注进 workflows/product_clear.py:20-21(该注释在默认值下同样必须改,原稿漏了,见审查者一 M1)。
  - 备选:若所有者选简化版「RETIRE 回执成功即弃码」:ABANDON_REASONS 增加 product_clear_retire,_BURN_STATUS 给它配 BURN_LOCK(每次停用烧一个 UPC),调用点在批次 2 的 product_clear 回执处接;同时 workflows/product_clear.py:20-21「停用(可恢复)」的措辞改为「不可恢复」。0a 的改动量差别只有词表里的一个成员 + 一条测试 + 0a-27 那句注释的措辞。
  - 影响:只影响 sku_codec 的原因词表、守门测试的反向清单与 0a-27 的注释措辞;0a 的 15 处 SQL 收口与三条索引不受任何影响,两种选择都能跑。
- **B|UPC 撞库 0101119 时码与 UPC 是否一起换**
  - 默认:默认假设:一起换。0a 在 ABANDON_REASONS 里保留 upc_conflict,并在 _BURN_STATUS 里**不给它配烧号状态**(该号已由 listing_sheet._mark_upc_conflicts 标成 conflict,再烧一次是重复动作)。调用点(services/listing_sheet._mark_upc_conflicts)属批次 1/2,0a 不接。
  - 备选:若所有者选「撞库只换号不换码」(维持 2026-08-09 口径):从 ABANDON_REASONS 里删掉 upc_conflict,守门测试的反向清单里补上 listing_sheet 不得调 abandon。
  - 影响:同 A:只动词表一个成员与一条守门断言,0a 其余部分不变。
- **C|alloc_push 派工口径是否对齐去重闸(含对 synthesis required_changes #6 后半句的处置)**
  - 默认:默认假设:**alloc_push 对齐但拆两步;alloc_survey 不对齐**。0a 只做第一步(alloc_push 身份收口 + 落地恒真的 ls.abandoned_at IS NULL),零行为变化;第二步(去掉 workflows/alloc_push.py:52 的 lifecycle ACTIVE 过滤,让「RETIRED 且未弃码」的 ASIN 不再派工)是真行为变化 —— **在存量数据上就立刻生效**(catalog_sync 显式扫一轮 RETIRED,那批行 missing_since 为 NULL),随批次 2 的弃码点接线一起上,上之前必须跑 alloc_push --dry-run 并把差额名单(lifecycle=RETIRED AND missing_since IS NULL 的 (店, ASIN) 清单)贴给所有者。required_changes #6 里「services/alloc_survey._SQL_ONLINE(:184-190)也对齐、不再排 RETIRED」那半条**显式驳回**,理由三条:① 它管的是占用与冲突口径,不是派工口径,alloc_survey.py:176-183 的 2026-08-15 结论(退市行不算活货位,否则占用与冲突组凭空多出一批)仍成立;② 仓内有两条守门钉着它(tests/test_alloc_audit.py:846-847、tests/test_store_perf.py:203);③ 去掉它是真行为变化,不属零变化批次。
  - 备选:若所有者要连 alloc_survey 一起对齐:批次 2 另立一条独立 item 改 services/alloc_survey.py:184-190,同时必须改 services/risk_trace.py:17-19 那段把 alloc_survey._SQL_ONLINE 当「同款判法」引证的注释(不改它,下一个人会顺手把 still_listed 也统一),并给行为差额清单。若所有者选 alloc_push 也「不对齐」:0a 第一步照做(本来就零变化),批次 2 不动 lifecycle;代价是分配链派一次、list_new 每轮拦一次并写 N 理由,配额不烧但运营困惑。若所有者选「立刻对齐」:本批次不再是零行为变化,不建议。
  - 影响:影响 workflows/alloc_push.py:52 一行、services/risk_trace.py:17-19 的注释,以及 tests/test_alloc_push.py::test_online_set_still_excludes_retired 这条反向守门(批次 2 对齐时翻转它);其余 items 全不受影响。
- **D|upc_pool.burn_for_retire 何时改写状态值**
  - 默认:默认假设:0a 只登记 burned_delete / burned_lock 两个值与中文标签,**不改任何写入点**(burn_for_retire:188-207 仍写 conflict),真正改写状态值与函数签名(burn(conn, pairs, status))推迟到批次 2 接 abandon 时一次做完。
  - 备选:若要在 0a 就改成 burn(conn, pairs, status) 并让 workflows/sku_locked_heal.py 传 BURN_LOCK:功能上安全(claim 的复用查询用 claimed/used 白名单,全仓无任何代码按 status='conflict' 分支,只有 UPC 池表投影的中文字样从「冲突」变成「锁死烧号」),但 0a 就不再是严格零行为变化。
  - 影响:只影响 services/upc_pool.py:188-207 与 workflows/sku_locked_heal.py 的一行。
- **E|不透明码编码规则(字母表/长度/is_opaque)归哪个模块 —— 本包已裁决,记录在案**
  - 默认:**唯一之家 = services/sku_codec.py**;registry/resources.py 只登记 SKU_SOURCE_LETTERS。理由:CLAUDE.md 铁律 3 管的是路径/token/表 ID/服务器地址这类外部资源,12 位码的字母表是内部编码规则;而来源字母是所有者要拍的取值,确属配置。schema.sql 的两条索引条件与 sku_codec._ALPHABET 由 tests/test_sku_guard.py::test_schema_opaque_predicate_matches_the_codec_alphabet 对齐,registry 里出现字母表字面量即红。
  - 备选:若所有者/评审坚持放 registry:0a-01 改回登记 SKU_ALPHABET/SKU_LEN/SKU_RANDOM_LEN,sku_codec 从 registry 取,守门断言改成比对 registry 常量;**但 0b-03 提议的 services/sku_asin.OPAQUE_ALPHABET 与横切 D4 的 sku_codec._ALPHABET 必须同时删掉** —— 三处并存是本次审查四位里两位点名的 blocker(两条守门断言不可能同时绿)。无论选哪边,全仓只准有一份。
  - 影响:只影响 registry/resources.py 一段、services/sku_codec.py 顶部一段、tests/test_sku_guard.py 两条断言;15 处 SQL 收口与三条索引不受影响。
- **F|守门测试文件名 —— 本包已裁决,记录在案**
  - 默认:**tests/test_sku_guard.py,全套 SKU 改造唯一一份**(与仓内既有 tests/test_feishu_guard.py 同族同形态:白名单 dict + test_the_whitelists_do_not_rot)。0a 建齐五张白名单,0b/1/2/3 与横切包的对应 item 全部降级为「只改这一份的白名单条目」。
  - 备选:无可接受的替代:原稿四个包各建一份(test_sku_identity_guard / test_sku_asin_consumers / test_sku_codec_guard / test_sku_guard),extract_asin 白名单重复 3 处、abandoned_at 白名单重复 4 处且数目三种口径、字母表一致性断言两条互斥 —— 四位审查者里三位独立点名。
  - 影响:0a-28 一个 item;其余批次各省一个 new_module。

### 依赖

- 无代码依赖:0a 是 SKU 改造的第一个批次,可直接在 main 上开分支。**也不再依赖 0b-11**(0a-25 的代际过滤改读 product_events.detail->>'source_key',不读 asin 列,采纳审查者一的 minor 选项②)。
- 所有者拍板项(**不阻塞 0a**,因为 mint 零接线):SKU_SOURCE_LETTERS 的四个字母取值(sku_plan §8)。0a 落地时该常量为空 dict,mint 对未映射 source_type 抛 ValueError 并点名 registry 常量名。
- 决策 A / B 只影响 sku_codec 的 ABANDON_REASONS 词表与 0a-27 的注释措辞,两种选择 0a 都能落地;决策 C 的第二步不在 0a 内;决策 E / F 已在本包裁决,但**需要横切包与批次 2/3 同步接受**(否则守门测试与索引名会互相判红)。
- **跨包前置(必须在写第一行代码前拍平,四位审查者共同点名)**:① 活码/全局唯一索引的名字与条件由本包定死(listing_sources_live_uidx 含 replaced_by IS NULL / listing_sources_opaque_sku_uidx 含两段条件),横切 C0-DDL-2/3 改为「由 0a-02 交付」,批次 3 S1 降级为「核验 indexdef」,不许 DROP/CREATE;② audit_listing_conflicts 视图 DDL 由 0a-05 唯一交付(带 source_type='amz'),横切 C0-DDL-5 删除;③ 事件码 SKU_ABANDONED/SKU_REPLACED 由 0a-10 在本批登记,横切 C0-TEST-1 改为核验;④ upc_pool.STATUS_CN 由 0a-09 唯一交付,横切 C0-REG-4 只保留 UPC_SHEET E 列口径那半条。
- 验收需要能连生产 PG(六条体检 SQL + 三条 EXPLAIN + tests/test_sku_guard.py 的两条 pg 用例;pg 用例照抄 tests/test_risk_trace.py:216-252 的 needs_pg 写法,连不上自动 skip)。**体检① 与 ①b 是合并硬闸**,不是「看一眼再决定」。
- 下游:批次 0b(订单/事件/黑名单/order_audit)依赖本批的 sku_asin.pick_asin/resolve_many 与 tests/test_sku_guard.py 的白名单;批次 1 依赖登记簿三列;批次 2 依赖 sku_codec 全部四个函数与两条唯一索引;批次 3 依赖 replaced_by 列与已含 replaced_by IS NULL 的活码索引(因此不需要重建索引)。

### 风险

- R1|**存量 amz 行 source_key ≠ sku(已升级为硬闸并同批修根因)**:db_init 回填的判型正则缺右锚(schema.sql:231-232,已实测),对「B0 开头且长度>10」的 SKU 会判成 amz 并把 source_key 截成前 10 位;这批行今天在三条删除意图产出面之外,收口后会进来。处置两条:0a-03 修正则(与 sources_backfill.py:46 同口径),体检① 升级成**合并硬闸**必须返回 0。⚠ 采纳审查者三 F1 —— 原稿把它写成「理论缺口 / 不为 0 就逐行给所有者看后再决定」,严重低估了后果(这不是身份键不同,是自动删除面扩大),且没意识到 0a 验收本身要跑 db_init 会当场把这批行造出来。
- R2|**pick_asin 两条腿口径不一(已处置)**:extract_asin 先 .strip().upper() 再过 _PLAIN(services/sku_asin.py:32-38),而 source_key 是从上架表 B 列原样落库的(services/listing_sheet.py:67-69 只 strip 不 upper;workflows/list_new.py:263)。裸取会让小写 ASIN 变成 alloc_push online 集合里的垃圾键 ⇒ 已在架的品被重新派工(DANGEROUS=True 直接写飞书上架表)。处置:0a-07 的 source_key 腿归一 + 过 is_standard_asin;体检①b 量化。⚠ 采纳审查者三 F2 的结论,但**驳回其机制**:它以为今天 extract_asin('b0abcdefgh') 返 None,实际返 'B0ABCDEFGH'(先 upper),真实机制是「今天大写、改后小写」的键形变化,已在论证里更正。
- R3|两条唯一索引在存量数据上建不起来:存量 sku=asin 跨店重复是必然,存量 match 行同一 GTIN 挂过多个 PHUMWMT 码也很可能。已用「局部条件限不透明码形态」先天规避;体检④⑤ 量化存量重复面,给批次 2/3 预警。若哪天有人把局部条件去掉,db_init 会整份回滚(schema.sql 一次 execute),生产建库直接失败 —— 这正是批次 3 原稿裸建无条件唯一索引会造成的后果,已由本批把最终条件一次建到位堵住。
- R4|查询计划退化:把等值条件换成 coalesce 表达式会让索引失效。product_audit 的相关子查询已改成两条腿 OR(而不是 coalesce)专为避开这一条;risk_trace 改成 UNION 两条腿同理;maintenance_intents._SQL_VARIANT_OFFSET 换了驱动表,计划从索引嵌套循环变成哈希连接。缓解:验收里三条 EXPLAIN 必须无 Seq Scan on walmart_items / products,且**已改成可执行形态**(原稿要求把带 %(name)s 占位符的 SQL 粘进 psql,粘进去就是语法错 —— 采纳审查者四)。
- R5|_SQL_ATTEMPTS 归并触顶:同一 (店, ASIN) 在 ops.feed_items 里若有多个不同 sku,按身份键计数会把它们合并,原本还能重试的行可能直接判「达上限」。缓解:体检⑥ 必须返回 0。代际过滤本身已不依赖 0b-11(改读 detail->>'source_key'),0a 可独立先合。
- R6|测试文本断言破裂面比想象大:tests/test_alloc_audit.py:284(ORDER BY store, sku)、tests/test_product_ingest.py:599(SELECT DISTINCT w.sku)、tests/test_risk_trace.py 的 _ITEMS_SQL 断言、tests/test_variant_group.py:415、tests/test_problem_scan.py:292 都钉的是 SQL 文本。这些不是「测试碍事」而是设计如此(SQL 文本就是契约),每一条都要人眼确认改的是断言不是语义。**反向钉住不许动的三条**:tests/test_alloc_audit.py:846-847、tests/test_store_perf.py:203(alloc_survey 的 lifecycle)、tests/test_claim_audit.py:29(monkeypatch 打在 sku_asin.extract_asin 上,pick_asin 在同模块内按全局名调它,打桩仍生效,不需改)。
- R7|sku_codec 是本批唯一的无调用者模块。conventions §五明确「从未跑过、在批次待办里」= 活;必须在文件头写清它属于批次 2 的接线目标,否则下一次死代码盘点会有人把它判死。
- R8|**跨包一致性是本批最大的非技术风险**(四位审查者共同结论):原稿与横切包/批次 2/3 在四层重复且命名不一(索引名三种、守门文件四份、abandoned_at 白名单三种数目、DDL/registry/docs 所有权重复)。本版已把 DDL / registry 身份段 / 守门 / 视图 / 事件码 / STATUS_CN 六项声明为 0a 唯一交付并写进 depends_on;**若横切包仍作为独立交付物落地,0a 合并当天就会有测试红或索引被静默覆盖**。建议横切包拆解归位,只保留它独有的四样(api_blueprint §retire 语义更正、conventions §九、orders.v_order_line_dupes 体检视图、catalog_health F 段)。
- R9|**回滚方案**(原稿缺,采纳审查者二/四):0a 全部改动可 git revert;三条新列**不 DROP**(conventions §五:DROP COLUMN 不可回滚,未连库核对一律不执行),三条新索引留着无害(存量一行都落不进);revert 后身份表达式退回裸 sku,而 abandoned_at/replaced_by 全库为 NULL,读侧结果与 revert 前逐行相同 —— 0a 是可无损回滚的批次,这也是把它排在最前面的理由之一。
- R10|**本批不处理、已交接的四条**(审查者一/二的 missing,均不属 0a 域):① services/blacklist.py:205-243 的 rebuild_asin_blacklist 按 coalesce(asin, sku) 推黑名单键,切码后会把不透明码灌进 catalog.asin_blacklist(与 §3.3 点名的三大危险失效之一同形)⇒ **交 0b**,并请 R4 把这一处补进 sku_plan §3.3 表;② services/alloc_survey._SQL_SALES(:236-245)与其三个消费点按 (store, sku) 挂销量,批次 3 改码后迁过码的品销量恒 0 ⇒ **交批次 3**(仓内已有正确先例:services/product_pool.py 按 order_lines.asin 聚合);③ workflows/problem_scan._SQL_INFLIGHT(:102-111)按 (store, sku) 做在途防重,改码后新码行无历史 ⇒ **交批次 3**,建议并进 sku_migrate 的 _preflight;④ services/variant_group.group_id 由 parent ASIN 派生并经 mp_conform 写进 Visible.variantGroupId 发给沃尔玛 —— 换成不透明 SKU 之后货源仍可从组 ID 倒推,与本次改造的核心目标直接冲突,目前只活在批次 2 的 risks 文本里 ⇒ **请横切包立一条编号决策并由 R4 写进 sku_plan §8 待决清单**;⑤ workflows/audit_history_fold.py 直接 INSERT catalog.product_events 绕过 record_many,是第 4 个平台级事件写入点 ⇒ **交 0b**(0b-11 的注释与 0b-28 的文档只列了三个)。
- R11|与旧系统并跑的红线不受本批影响(0a 不发任何 feed、不写任何库),但批次 2 之前必须确认旧仓 auto_listing / retire_and_relist / product_clear / daily_cleanup 的 cron 已停(synthesis open_questions #15 仍未闭合);审查者二/四建议把它做成批次 2 的可执行前置闸(crontab -l 截图 + -p legacy_stopped=<日期> 缺省即 ⛔ 早退),本包认同并转交批次 2。
- R12|行号漂移:本版所有 file:line 已于 2026-09-02 逐条重开文件核对(修正原稿四处:_retry_rows 是 672-701 不是 684-696、upc_pool 表在 237-255 不是 243-256、audit_rules 的 import 在 175、sku_asin 文件共 110 行)。执行时仍建议先用锚点字符串 grep -n 定位再动手,不要照抄行号跑 sed/patch。

### PR 切分

建议**两个 PR**(原稿建议单 PR,本版采纳其自备的备选切法并定为首选,理由是第一个 PR 自身零消费方、可独立全绿,评审面从 28 个文件降到 12 个):

- **PR-0a-1「积木 + schema + 守门」**(items 0a-01 ~ 0a-11、0a-27、0a-28;约 12 个文件:registry 1 + refdata 1 + services 5 + workflows 1 + docs 2 + tests 新 2):全部无消费方,合并后行为一个字节不变。必须在本 PR 里跑完:db_init 连跑两次、pg_indexes 核验、体检①/①b(硬闸)/③④⑤、tests/test_sku_codec.py + tests/test_sku_guard.py 全绿。
- **PR-0a-2「15 处读侧收口」**(items 0a-12 ~ 0a-26;约 16 个文件:services 4 + workflows 6 + tests 改 9 + docs 1):合并前**重跑体检①/①b/⑥**,并做「意图集合对拍」与五条 --dry-run 计数对拍。

**不要按工作流域切成三个 PR**:15 处收口共用同一个身份表达式,拆开会出现「一半按登记簿、一半按裸 sku」的中间态,而这个中间态恰恰是最难发现的一类(不报错、摘要正常)。

**回滚**:见 risks R9(可无损 revert;新列不 DROP,新索引留着无害)。

**工时与日历(原稿缺,采纳审查者四)**——把「码的工时」与「人的等待」分开:
- 代码 ≈ **3~4 人日**(PR-0a-1 约 1.5 人日,PR-0a-2 约 2 人日,含测试)。
- 外部依赖:**零**。0a 不需要所有者建飞书列、不需要单品实测、不需要来源字母定值(mint 零接线),**可立刻开工并与批次 2 的所有者实测并行**。
- 唯一的日历风险是体检①/①b:若非 0,先要人工判定那批行怎么修(1 人日量级,取决于行数),再合并。
- 关键路径提示:整条 SKU 改造的关键路径不是写代码,而是所有者的四个来源字母定值、决策 A/B/C 拍板、七项单品实测与四列飞书建列;0a 是唯一一个完全不卡这些的批次,建议第一个启动。
