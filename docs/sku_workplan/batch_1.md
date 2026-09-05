## 批次 1|上架表 V 列「SKU」+ 回执/自愈链读 V 列 + UPC 池口径(修订版 2026-09-02,已吸收四位审查者意见并逐条回仓核对行号)

> ## ⚠ 所有者定稿覆盖(2026-09-02,优先级高于下文任何 item)
> 1. **上架表表头已由所有者重排(2026-09-02 第二次),SKU 在 C 列,不是 V 也不是 R**。
>    新 21 列(A~U)按顺序:店铺 / ASIN / **SKU** / walmart上架标题 / walmart_product_type /
>    审核结果 / **类别** / **具体内容** / 审核日期 / amz价格 / 库存 / walmart价格 / 是否上架 /
>    上架feedid / 上架日期 / 未上架理由 / 上架结果 / 报错 / feed查询日期 / **登记日期** / **查询编码**。
>    对应元组:store, asin, **sku**, list_title, product_type, audit_result, **audit_category**,
>    **audit_reason**, audit_date, amz_price, stock, walmart_price, listed, feed_id, list_date,
>    not_listed_reason, list_result, list_fail_reason, feed_check_date, **register_date**,
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

**目标**:给上架表加末列 V「SKU」,并把「这一行的 SKU 是什么」收口成唯一函数 services/listing_sheet.row_sku(V 列;空则回落 B 列 ASIN),让回执反哺器(sync_from_ledger)、Unknown 自愈器(heal_unknown)、SKU_LOCKED 自愈链(sku_locked_heal 的五个键)、UPC 池 mark_used 从「上架 sku=asin 约定」切到「读行上的 SKU」;同时把 UPC 撞库反查从 upc_pool.sku(批次 2 起将存真码)改成领号键 (store, asin),并顺带修掉它的跨店误烧号与 missing 计数可为负两个既有缺陷。本批只加能力不写 V:list_new 仍传八值 ⇒ V 全表为空 ⇒ 全链回落 B 列 ⇒ 除一处有意的修复外行为不变。批次 2 的 mint 只需改 row_sku 一行 + 给 write_submit_cols 传第 9 值即可通电。

**零行为变化**:否

除 _mark_upc_conflicts 一处有意的修复外,零行为变化。论证逐条如下(每条都在当前仓库状态 2026-09-02 上重新核过行号与代码文本)。

【零变化的部分】
1. V 列全表为空:本批不写 V(workflows/list_new.py:221 与 :1769 两个 write_submit_cols 调用点仍传八值,B1-15 明确只改 docstring;不做存量回填,决策 D2 默认「不灌 ASIN 进 V」)。`row_sku(r) = (r.get("sku") or r.get("asin") or "").strip()`,V 空时逐字节等于原来的 `r["asin"]` —— read_rows 取值时已经 `str(c).strip()`(services/listing_sheet.py:67),再 strip 是幂等。
2. read_rows 只是把读取宽度从 A~U 扩到 A~V。两种飞书返回形态都安全:返回 22 格时第 22 格恒为空串;返回 21 格(飞书裁掉不存在的列)时 `cells + [""] * width`(:67-68)补位后 `d["sku"] == ""`。行过滤条件 `d["asin"] or d["store"]`(:70)不变 ⇒ 行集合不变。**但读取范围本身从 A2:U 变成 A2:V 是真的发生了** —— 这不是行为变化只在「飞书接受该范围」的前提下成立,故 acceptance #1 是阻塞式前置(见 risks 第 1 条与决策 D6)。
3. write_submit_cols 只在传了第 9 值且非空时多追加一个 `V{r}:V{r}` 段;八值调用产生的 ranges 与改造前一字不差。api/feishu.py:842-875 的 `_coalesce` 只合并**列区间完全相同且行号紧邻**的相邻两项,而本函数逐行交替产出 C / H:N(/V)三种段,原本就一个也粘不起来,加 V 不改变这一点。
4. clear_for_relist 本来就只写 `K{r}:Q{r}`(:302),V 在该区间之外,零代码改动(只补 docstring + 反向测试钉)。
5. sku_locked_heal 的五个 (店, SKU) 键 —— :62-64 todo 过滤、:79 RETIRE 载荷、:92 冷却表落库、:102 事件、:233 locked_by_pair,外加 :197 的 burn_pairs —— 必须**同时**换成同一个来源,否则键两侧不同源会导致同一 SKU 重复提交 RETIRE 或冷却期满找不到行。本批一起换,V 空时六处仍全是 ASIN ⇒ 与 listing.retire_cooldown 里的存量 pending 行仍对得上,与 upc_pool.asin 也仍对得上。
6. heal_unknown 把一个键拆成两个(台账/目录按 row_sku,UPC 池按 ASIN),V 空时两者同值 ⇒ 分支走向、写出的 ranges、mark_used/release 的参数全不变。
7. list_new:259 的 mark_used 第二元由 `r["asin"]` 改 `listing_sheet.row_sku(r)`,V 空时同值;写进 catalog.upc_pool.sku 的内容一字不变。:260-268 的 listing_sources.register 与 product_events.record_many 本批**不动**。

【唯一有意的差异 —— _mark_upc_conflicts 改按 (store, asin) 反查】
旧写法 `SELECT upc, sku FROM catalog.upc_pool WHERE sku = ANY(%s) AND status <> 'conflict'`(:351-353)用的是不带店维度的 `upc_pool.sku` 列,所以 A 店某 ASIN 撞库时,B 店同一 ASIN 名下的 UPC 会被一起标 conflict 永久弃用(本仓 2026-08-28 起明确「跨店不互拦」,同 ASIN 多店在架是常态,这个误伤能真实发生)。新写法按 (store, asin) 反查后只烧当事店的号。

两处次生差异,都是把既有缺陷一并修掉,而不是新引入的:
(a) **匹配状态面**。旧写法的 `status <> 'conflict'` 字面上匹配 ''/claimed/used/bad_prefix 四种状态,但 `upc_pool.sku` 列**只由 mark_used 写**(services/upc_pool.py:210-219,同时把 status 置 'used'),`release` 又会把 store/asin 清空(:228-238),所以旧写法的实际命中集恒等于「该 ASIN 名下 status='used' 的行」。新写法显式写成 `AND p.status IN ('claimed','used')`,与 `upc_pool.burn_for_retire`(:188-206)逐字同口径 —— 相对旧实际命中集多纳入同 (店,ASIN) 处于 'claimed' 的行。由于 `upc_pool.claim` 是「先复用后新领」(:146-160),一个 (店,ASIN) 至多一个活号,实际集合仍是同一行;而 sync_from_ledger 处理的本来就是「已提交过 feed」的行,那个号必然已 'used'。acceptance #9 的体检上线前核一次,有历史多活号的先人工并号。**不沿用 `status <> 'conflict'`**:它与另一个烧号出口口径不同,是 conventions §六「每个能力只有一条实现路径」的违反,借这次改键一并统一。
(b) **missing 计数**。旧写法 `missing = len(set(asins)) - n`(:358),n 是命中的**行数**;一个 ASIN 名下若有两个活号,missing 会变成负数,而 `if missing:` 对负数为真,会打出「UPC 撞库 -1 个在池中找不到」这种胡话告警。新写法改数「一个号都没命中的 pair 数」,不会为负。

【本批**不**处理、且必须留给批次 2 的一件事(审查者点名,此处显式驳回并说明理由)】
`sync_from_ledger` 与 `_mark_upc_conflicts` 都没有 execute/dry_run 形参,而 workflows/feed_poll.py:38 `DANGEROUS = False` 导致 cli.py:307 强制 `params["execute"] = True` —— 也就是说 `python cli.py feed_poll --dry-run` 今天就已经在真写飞书 O/P/Q 并真标 conflict。**这是既有行为,本批不改**:加 execute 门禁会让 dry-run 从「写」变成「不写」,那本身就是行为变化,直接违背本批的零行为变化承诺。它真正变致命是在批次 2 接 abandon(不可逆弃码 + 永久烧 UPC)那一刻,所以修复归批次 2(连同重新评估 feed_poll 的 DANGEROUS 标记),本批只把它写进 risks 备查。

### 改动清单

#### B1-01 · `registry/resources.py` · 503-513(LISTING_SHEET 上方注释块)、514-524(Spreadsheet 定义;columns 元组 518-523,`"upc_match"),` 在 523)

**改动**:① 503 行「21 列 A~U」改「22 列 A~V」;② 508 行(`# R=真实walmart标题 … U=UPC是否一致`)之后、509 行之前新增一行 `# V=SKU(沃尔玛侧 SKU,2026-09 SKU 改造加;存量行为空 ⇒ 全链回落 B 列 ASIN)`;③ 512 行「表头再动一次,只改这里」之后补一句 `# ⚠ 只准在**末尾**追加:写入侧全是字母硬编码的连续段(A:B / C / C:G / F / H:J / H:N / K:Q / N / O:Q / V),插在中间全体静默错位。`;④ 523 行 `             "upc_match"),` 改为 `             "upc_match", "sku"),`。不动 A/B 与中间任何一列,不动 514-517 与 524。

**为什么**:列序的唯一出处就是这条元组(铁律 3:表 ID/字段名只从 registry 取);read_rows 按它 zip,加在末尾才不会让既有的字母 range 错位。V 是 SKU 改造全链(回执找行/自愈/退役/批次 3 改码)的读侧锚点。【审查处理】审查者甲称元组定义在 514-524、`"upc_match"),` 在 524 —— 复核后:`Spreadsheet(` 调用确在 514-524,但 columns 元组体是 518-523,`"upc_match"),` 确在 523(524 是收尾的 `)`)。原工作包行号正确,此处仅把两个口径都写明以免执行者误判。【审查处理】审查者甲/丁指出横切包 C0-REG-1 与本条重复登记 LISTING_SHEET 的 sku 列 —— 采纳:本条是该处 registry 改动的**唯一出处**,横切 C0-REG-1 应删除;本 PR 必须与消费方改动(B1-10/11/12/13/14)同批,否则 V 是一列没人读的死列。

**测试**:
- tests/test_audit_sheet_loop.py::test_columns_contract(见 B1-20:改成 22 并新增 `assert cols[21] == "sku"`、`assert cols[:21] == (…原 21 个字段名…)` 钉住前 21 列一个没动)
- tests/test_listing_sku_col.py::test_v_is_the_last_column_named_sku

**验收**:python -m pytest -q tests/test_audit_sheet_loop.py::test_columns_contract && python -c "from registry import resources as r; c=r.LISTING_SHEET.columns; print(len(c), c[-1], c[:21])"  # 期望 22 与 sku,且前 21 项与改造前逐字相同

#### B1-02 · `services/listing_sheet.py` · 1-30(模块 docstring;列契约首行 3,列清单 4-7,列权责 13-25)

**改动**:① 第 3 行「列契约(21 列 A~U,所有者建表 2026-08-07):」→「列契约(22 列 A~V;A~U 所有者建表 2026-08-07,V 于 2026-09 SKU 改造追加):」;② 第 7 行之后补一行 `  V=SKU(沃尔玛侧 SKU)`;③ 第 19 行「list_new 写 C/H/I/J(数据回显)与 K/L/M/N(提交结果);回执反哺器只写 O/P/Q;」补三句:`list_new 提交时另写 **V**(V 必须与 K/L/M 同一次写出)`、`回执反哺器与自愈器**只读 V、不写 V**`、`clear_for_relist 清 K~M/O~Q 但**不清 V**(码不随清列作废)`;④ docstring 末尾(第 29 行之后)新增一段:`⚠ 三个「SKU/ASIN」别混:V 列 = 沃尔玛侧那条记录的 SKU;B 列 = 亚马逊来源码 ASIN;catalog.upc_pool.asin = 领号复用键,永远存 ASIN,不随 SKU 改造变。取行上的 SKU 只准调 row_sku()。`

**为什么**:本文件是上架表读写的唯一积木,列权责跨界写就是 bug(文件头注自述);V 的写权责与「不清 V」如果不写在头注里,下一个人清列时顺手把 V 一起清掉,码就与沃尔玛侧那条记录脱钩。

**测试**:
- (文档改动,无独立测试;由 B1-20 中的 test_clear_for_relist_never_clears_v 反向钉住「不清 V」这条口径)

**验收**:python -m pytest -q tests/test_listing_sku_col.py

#### B1-03 · `services/listing_sheet.py` · 41

**改动**:`_COLS = 21          # A~U` 改为 `_COLS = len(resources.LISTING_SHEET.columns)   # 22 = A~V(列数唯一出处在 registry,别再写死数字)`。

**为什么**:写死 22 就制造了列数的第二个出处,registry 再加列时这里漏改 ⇒ read_rows 少读一列、末列恒空、不报错(与 A/B 对调那次同款静默错位形态)。派生即无从漂。【审查处理】审查者丙指出本条一落地 read_rows 立即读到 V,是本批唯一无法自检的风险点 —— 采纳:见 risks 第 1 条与决策 D6,acceptance #1 升级为阻塞式合并前置。

**测试**:
- tests/test_listing_sku_col.py::test_col_width_comes_from_registry(断言 `listing_sheet._COLS == len(resources.LISTING_SHEET.columns) == 22`)

**验收**:python -m pytest -q tests/test_listing_sku_col.py::test_col_width_comes_from_registry

#### B1-04 · `services/listing_sheet.py` · 47-73(read_rows);docstring 48-56(「21 列全量一把读」在 **52**),取值在 57-72,末列字母在 63

**改动**:① 63-64 行 `pairs = feishu.sheet_values_rows(sheet, "A", chr(ord("A") + width - 1), 2, total)` 的末列字母改用 `feishu._col_letter(width)`(与 services/maint_sheet.py:62、:74 同法);② docstring 第 52 行「21 列全量一把读在表长大后必炸(2026-08-19 生产实证…)」改「22 列全量一把读在表长大后必炸(2026-08-19 生产实证当时是 21 列,audit_sheet 当场炸在这里)」—— 事故是 21 列时发生的,别把史实改掉;③ docstring 末尾新增一句,把 acceptance #1 的实证结果写死:`⚠ 飞书对「范围列数超出工作表实际列宽」的行为已于 <上线日期> 实证为 <报错 90204 / 裁掉多余列>(见 PR 描述贴的 A1:V1 探针输出)。列没建就上线的失败形态因此是 <整链 FeishuError 早退 / 末列恒空>。` 由执行者按实测填。其余(width 计算 62、cells 补位 67-68、行过滤 70)一个字不动。

**为什么**:chr(ord("A")+n) 是手算列字母的第二份实现,超过 Z 就错;仓里已有 feishu._col_letter(api/feishu.py:578-584)作为唯一算法(maint_sheet 已在用),单一实现路径。宽度本身由 _COLS 派生,加 V 后自动变 22 ⇒ 读 A2:V{total}。【审查处理】审查者丙指出「fail loud 不是静默」目前只是假设(api/feishu.py:704-715 只对 90221 兜底,其余 FeishuError 直接 raise —— 这一半已核实;飞书对越界列区间返回什么无法离线验证)—— 采纳:实证前置化(acceptance #1 阻塞),并把结论写进本函数头注,免得下一个人再猜。注:本批不改 blacklist_sheet.py:124 / catmap_import.py:69 / blacklist_push.py:80 里同款的 chr(ord(...)),那三处与上架表无关,属独立清理项。

**测试**:
- tests/test_listing_sku_col.py::test_read_rows_reads_through_column_v(打桩 feishu.sheet_values_rows 捕获实参,断言 (first_col, last_col) == ("A", "V"))
- tests/test_listing_sku_col.py::test_read_rows_pads_a_21_cell_legacy_row(返回 21 格的存量行,断言结果 dict 里 `sku == ""` 且其余 21 个字段与改造前逐字相同 —— 钉住「飞书裁列」这一分支)
- tests/test_listing_sku_col.py::test_read_rows_narrow_read_has_no_sku_key(read_rows(upto="audit_result") 的行 dict 里没有 "sku" 键 —— 钉住 row_sku 必须用 .get;现实调用方是 audit_targets:206-212)

**验收**:python -m pytest -q tests/test_listing_sku_col.py -k read_rows

#### B1-05 · `services/listing_sheet.py` · 新增函数,插在 read_rows 结束(73)与 append_assignments(76)之间

**改动**:新增 `def row_sku(r: dict) -> str:`,函数体 `return (r.get("sku") or r.get("asin") or "").strip()`。docstring 首行「输入:上架表一行 → 输出:该行的沃尔玛 SKU(V 列;为空回落 B 列 ASIN)。」正文钉四件事:(a) **全仓「这一行的 SKU 是什么」的唯一出处**,任何工作流不得自己写 `r["sku"] or r["asin"]`;(b) 存量行 V 空 ⇒ 回落 B 列 = 原来的「上架 sku=asin 约定」,所以本函数在存量数据上与 `r["asin"]` 逐字节等价;(c) 必须用 .get —— read_rows(upto=…) 的窄读行没有 sku 键;(d) 批次 2 起本函数第一优先级插入 `r.get("_sku")`(预备期 mint 挂在行上、V 列尚未写出的那一轮),**改造点只有这一行**。

**为什么**:conventions §六「每个能力只有一条实现路径」:回执找行、Unknown 自愈、退役载荷、冷却键、mark_used 五处都要回答同一个问题,散着写五份 `or` 表达式,批次 2 通电时漏改任何一份都是静默失效(找不到行 ⇒ O/P/Q 永不回填;退役发错码 ⇒ 退不到)。收成一个函数后批次 2 只改一行。

**测试**:
- tests/test_listing_sku_col.py::test_row_sku_falls_back_to_asin_for_legacy_rows(四例:{asin:B0X} → B0X;{sku:N7Q…, asin:B0X} → N7Q…;{sku:"   ", asin:B0X} → B0X;{} → "")
- tests/test_listing_sku_col.py::test_nobody_recomputes_the_row_sku(源码文本守门:读 services/listing_sheet.py、workflows/list_new.py、workflows/sku_locked_heal.py 三个文件,断言其中不再出现 `["sku"] or` / `.get("sku") or` 这类第二份回落表达式(row_sku 自身所在的那一行除外),也不再出现旧形态 `skus = [r["asin"]`、`"sku": r["asin"]`、`cache[fid].get(r["asin"])`)

**验收**:python -m pytest -q tests/test_listing_sku_col.py -k row_sku

#### B1-06 · `services/listing_sheet.py` · 新增两个函数,插在 write_submit_cols 结束(151)与 write_data_cols(154)之间

**改动**:① 私有段构造器 `def _sku_range(rownum: int, sku: str) -> tuple[str, list[list]]:`,体 `return (f"V{rownum}:V{rownum}", [[sku or ""]])`,docstring 首行「输入:行号 + SKU → 输出:V 列单格写入段。**V 列写入范围的唯一构造点**(字母 V 在本文件只准出现在这里)。」② 公开单列写函数 `def write_sku_col(updates: list[tuple[int, str]], execute: bool = True) -> int:`,体:空列表返 0;`execute=False` 时按本文件成例逐行 `logger.info("[DRY-RUN] 将回写 第%d行 V=%s", rownum, sku)`(前 20 行,超出打一行省略)并返 0;真跑 `return feishu.sheet_write_ranges(resources.LISTING_SHEET, [_sku_range(r, s) for r, s in updates])`。docstring 首行「输入:[(行号, SKU)] → 输出:写入行数。**只写 V 列**。」正文写清三件事:用途 = 批次 3 改码回写 V / 存量补码 / 人工修行;**提交当轮的 V 不走这里**,由 write_submit_cols 与 K/L/M 同一次写出(理由见 B1-07);**本函数在批次 1 与批次 2 都没有调用方,这是有意的**(conventions §五 第三类「从未跑过、在批次待办里」= 活),批次 3 的 sku_migrate confirmed 分支是它的第一个调用方,盘点时不得判死。

**为什么**:sku_plan §3.5 点名要「新增只写 V{r} 的函数」。写函数与 write_submit_cols 共用 _sku_range 这一个段构造点,才不算双轨(§六):字母 V 与单格形状只有一处定义,registry 万一再加列时只有一处要改。dry-run 分支是本文件全部写函数的既有纪律(缺省即真跑,空跑不写库/不写表)。

**测试**:
- tests/test_listing_sku_col.py::test_write_sku_col_touches_only_v(断言写出的 ranges 全是 `V{r}:V{r}`,一格一值)
- tests/test_listing_sku_col.py::test_write_sku_col_dry_run_writes_nothing(execute=False 时 feishu.sheet_write_ranges 一次都不被调用,返回 0)
- tests/test_listing_sku_col.py::test_v_letter_is_written_in_exactly_one_place(源码守门:services/listing_sheet.py 里 `f"V{` 形态的字面量只允许出现在 _sku_range 那一行)

**验收**:python -m pytest -q tests/test_listing_sku_col.py -k write_sku_col

#### B1-07 · `services/listing_sheet.py` · 131-151(write_submit_cols;docstring 132-136,dry-run 分支 139-144,`title, rest = vals[0], vals[1:]` 在 **147**,两个 append 在 148-149,写入 150,return 151)

**改动**:签名不变。① docstring 首行改「输入:[(行号, [C,H,I,J,K,L,M,N] 八值;可选第 9 值 = V 列 SKU)] → 输出:写入行数。」正文补三句:(a)「V 必须与 K/L/M **同一次调用**写出 —— 两次写之间崩掉会留下 K=Yes 而 V 空的行,回执反哺器按 V 找不回它、回落 B 列又对不上台账里的真 SKU,O/P/Q 永不回填」;(b)「批次 1 只加能力:list_new 仍传八值 ⇒ 不写 V ⇒ 零行为变化;批次 2 预备期 mint 出码后开始传第 9 值」;(c)「第 9 值**永远是可选**:八值调用必须一直合法(tests/test_listing_sku_col.py::test_write_submit_cols_eight_values_is_byte_identical 钉住),批次 2 不得改成必填」。② 147 行 `title, rest = vals[0], vals[1:]` 改为 `title, rest = vals[0], vals[1:8]`(**必改**,理由见 why)。③ 149 行之后补 `if len(vals) > 8 and vals[8]: ranges.append(_sku_range(r, vals[8]))`。④ dry-run 分支 141 行的日志文案加 V:`"[DRY-RUN] 将回写 第%d行 C+H:N(+V)=%s"`。

**为什么**:V 是 K/L/M 的同伴列(同一次提交的产物),分两次写会产生「已提交但无码」的中间态,而回执反哺器正是靠 V 找行 —— 这是本次改造里唯一会把行永久卡死的裂缝。可选第 9 值让批次 1 与批次 2 共用同一个函数,不新开第二条写 V 的路径。【订正原工作包一处事实错误】原稿称不切片时「8 格塞进 H:N 段会被 feishu._check_shape 当场拒」—— 复核 api/feishu.py:754-777,`_check_shape` **只**校验列数 > 95 与单元格 > 40000 字符,**不校验值矩阵形状与 range 是否匹配**;sheet_write_ranges(:1022-1034)也没有这道检查。真实失败形态是:8 格照发给飞书,由飞书在 `_sheet_put` 处以 90202(validate RangeVal fail)整批拒 —— 后果一样致命(整批写入失败),但机制必须写对,否则执行者会去 _check_shape 里找不存在的拦截逻辑。【审查处理】审查者乙指出批次 2 的 B2-10 拟把第 9 值改成必填,会打红本批的护栏测试 —— 采纳:本条 docstring (c) 与 guard_tests 把「永远可选」写成契约,批次 2 若要改必须显式立 item 说明为什么护栏可以撤。

**测试**:
- tests/test_listing_sku_col.py::test_write_submit_cols_eight_values_is_byte_identical(传八值,断言 ranges 恰好是 [("C{r}:C{r}", 1 格), ("H{r}:N{r}", 7 格)],没有任何 V 段)
- tests/test_listing_sku_col.py::test_write_submit_cols_writes_v_when_ninth_value_given(传九值,断言多一段 `V{r}:V{r}` 且 H:N 段仍是 7 格)
- tests/test_listing_sku_col.py::test_write_submit_cols_skips_empty_ninth_value(第 9 值为空串 ⇒ 不写 V,不产生空段)
- tests/test_list_new.py::test_multi_slice_results_line_up_with_their_own_rows(既有,回归:八值路径不变)

**验收**:python -m pytest -q tests/test_listing_sku_col.py -k write_submit_cols && python -m pytest -q tests/test_list_new.py

#### B1-08 · `services/listing_sheet.py` · 287-304(clear_for_relist);docstring 288-293,写入 range 在 302

**改动**:**代码不动**(302 行仍是 `K{r}:Q{r}` 配 7 格,V 天然在区间外)。docstring 288 行首行保持「清列行数(K~M 与 O~Q 清空,N 写自愈标记)」,正文(293 行之前)补一句:「**V 列不清**:码的寿命由登记簿 catalog.listing_sources 的 abandoned_at 说了算(sku_plan §5.3 四个弃码点),不由清列决定;SKU_LOCKED 自愈链清列后 list_new 重上时是否复用旧码,由批次 2 的 mint/abandon 判 —— 这里留着旧码只为可读与回执追查。清 V = 让这行与沃尔玛侧那条 (店, SKU) 记录彻底脱钩。」

**为什么**:conventions §五「判不准就判活」的同款风险:下一个人看到「恢复成新行」很容易顺手把 V 一起清掉,而清 V 会让回执/退役都找不回这一行,且与批次 2 的四个弃码点抢判据(弃码只有 sku_codec.abandon 一个出口,清列不是弃码点)。用 docstring + 反向测试钉死。

**测试**:
- tests/test_listing_sku_col.py::test_clear_for_relist_never_clears_v(断言写出的每个 range 都是 `K{r}:Q{r}`,没有一个以 V 开头;并断言值矩阵仍是 7 格)

**验收**:python -m pytest -q tests/test_listing_sku_col.py::test_clear_for_relist_never_clears_v && python cli.py sku_locked_heal --dry-run

#### B1-09 · `services/listing_sheet.py` · 336-361(_mark_upc_conflicts;docstring 337-345,SQL 351-353,循环 355-357,missing 358-360),调用点在 557

**改动**:签名 `def _mark_upc_conflicts(asins: list[str]) -> int:` 改为 `def _mark_upc_conflicts(pairs: list[tuple[str, str]]) -> int:`(pair = (店铺, ASIN))。① docstring 首行改「输入:撞库的 [(店铺, ASIN)] → 输出:标记数。」,保留 339-345 现有的两段口径注释(UPC 永久弃用 / 所有者 2026-08-09 澄清「撞库只说明这个号被占,照常领新号重试」)一字不动,另补一段:「按 **(店铺, ASIN)** 反查 —— 领号键就是 (store, asin)(`upc_pool.claim`),`asin` 列永远存 ASIN;`sku` 列从批次 2 起存真 SKU,再按它反查会一个都找不到而且不报错。带上 store 还修掉了旧写法的跨店误伤:sku 列不带店维度,A 店撞库会把 B 店同 ASIN 的号一起烧掉(2026-08-28 起同 ASIN 多店在架是常态)。状态条件与 `upc_pool.burn_for_retire` 同口径,烧号出口只有一套判据。」② 函数体:`uniq = sorted(set(pairs))`;SQL 改成 psycopg3 的 unnest 配对写法(**不许用 `(a,b) IN %s`**,2026-08-09 踩过):`SELECT p.upc, p.store, p.asin FROM catalog.upc_pool p JOIN unnest(%s::text[], %s::text[]) AS t(s, a) ON p.store = t.s AND p.asin = t.a WHERE p.status IN ('claimed', 'used')`,实参 `([s for s, _ in uniq], [a for _, a in uniq])`;循环体仍 `upc_pool.mark_conflict(conn, upc, asin)`(第三参保持传 ASIN —— mark_conflict 写的是 upc_pool.asin 列,services/upc_pool.py:242-246);③ missing 改成数「零命中的 pair」:`hit = {(s, a) for _u, s, a in found}` / `missing = len([p for p in uniq if p not in hit])`,告警文案保持不变。调用点 557 行 `n_conflict = _mark_upc_conflicts(conflicts)` 不变(conflicts 的元素形状由 B1-11 改成二元组)。

**为什么**:批次 2 起 mark_used 写真 SKU 进 upc_pool.sku,按 sku 反查会静默归零(撞库的号永不标 conflict ⇒ 反复领到坏号,sku_plan §3.4)。改按领号键 (store, asin) 是唯一不受 SKU 改造影响的锚点。跨店误烧号与 missing 可为负是顺带修掉的既有缺陷(见 zero_behavior_change_argument)。【审查处理】审查者丙提出把 `status <> 'conflict'` 收紧成 `IN ('claimed','used')`、并修 missing 负数 —— 两条**全部采纳**(见 zero_behavior_change_argument 次生差异 a/b)。【审查处理】审查者乙建议本批一步到位把入参改成 (store, 行上的 SKU)、函数内经登记簿翻回 ASIN,以省掉批次 2 的第二次改签名 —— **驳回**,两条理由:(1) 那要求 batch 1 依赖批次 0a 交付的登记簿反查积木(services/sku_asin.resolve_*),会打掉本批「不依赖 0a 任何符号、可独立开发与合并」这条性质;(2) 批次 2 在此处到底加不加 abandon 取决于**未拍板**的决策 B,若选「不换」则 SKU 根本不需要传进来 —— 现在按猜测定形状,churn 概率更高而不是更低。守门测试 test_upc_pool_is_still_keyed_by_asin 钉住的是「池反查这一跳永远按 (store, ASIN)」,这条口径无论决策 B 怎么拍都成立。

**测试**:
- tests/test_listing_sku_col.py::test_upc_conflicts_looked_up_by_store_and_asin(假游标断言 SQL 里含 `p.store = t.s` 与 `p.asin = t.a`、不含 `sku = ANY`、不含 `IN %s`;两个实参是等长 text 数组)
- tests/test_listing_sku_col.py::test_upc_conflict_does_not_burn_the_other_store_same_asin(池中 (T1,B0X) 与 (T2,B0X) 各一号,只传 (T1,B0X),断言 mark_conflict 只对 T1 的那个 upc 调用一次)
- tests/test_listing_sku_col.py::test_upc_conflict_only_burns_claimed_or_used(断言 SQL 含 `p.status IN ('claimed', 'used')`,与 services/upc_pool.py:202 逐字同口径)
- tests/test_listing_sku_col.py::test_upc_conflict_missing_counter_never_goes_negative(同 (T1,B0X) 两个 used 行,断言 missing == 0 且不打 warning)
- tests/test_list_new.py::test_upc_conflict_marked_orthogonally(既有,:153-173;把 :172 的 `assert marked == [asin]` 改成 `assert marked == [("T1", asin)]` —— 该测试用 `lambda a: …` 打桩,位置参数,签名改名不影响)

**验收**:python -m pytest -q tests/test_listing_sku_col.py -k upc_conflict && python -m pytest -q tests/test_list_new.py::test_upc_conflict_marked_orthogonally

#### B1-10 · `services/listing_sheet.py` · 394-513(heal_unknown);具体 416-417、428-431、438、454-455、463-464、474-475、481-482、489-490、495-500

**改动**:键一分为二。① 417 行 `skus = [r["asin"] for r in unknown]` 改 `skus = [row_sku(r) for r in unknown]   # 台账/目录按真 SKU 找`,并在其后新增一行 `asins = [r["asin"] for r in unknown]   # UPC 池按领号键 (店, ASIN) 找`。② 428-430 行 UPC 池查询的实参由 `(list(set(skus)),)` 改 `(list(set(asins)),)`(SQL 文本 428-429 不动,本来就是 `WHERE status = 'claimed' AND asin = ANY(%s)`)。③ 438 行 `key, rn = (r["store"], r["asin"]), r["rownum"]` 改为三元:`key, akey, rn = (r["store"], row_sku(r)), (r["store"], r["asin"]), r["rownum"]`;`key` 继续喂 receipts(439)与 online(485)。④ 454、463、474、481、489 行的 `if key in claimed` 全改 `if akey in claimed`;455、464、475 行的 `claimed[key]` 改 `claimed[akey]`。⑤ 482、490 行 `upc_used.append((claimed[key], r["asin"]))` 改 `upc_used.append((claimed[akey], row_sku(r)))`(mark_used 的第二元是要写进 upc_pool.sku 的 SKU,批次 2 起就是真码)。⑥ docstring(394-406)在 405 行之后补一句:「台账与目录按 **V 列 SKU**(row_sku,空则回落 B 列)对账;UPC 池按 **(店, B 列 ASIN)** 找号 —— 两套键别混,upc_pool.asin 永远是 ASIN。」401-405 行的三条收尾路径描述不动。

**为什么**:`_SQL_HEAL_RECEIPT`(376-384)打 ops.feed_items.sku、`_SQL_HEAL_ONLINE`(385-391)打 catalog.walmart_items.sku,这两张表存的是沃尔玛侧真 SKU;而 upc_pool 的领号键是 (store, asin)(services/upc_pool.py:146-160)。批次 2 一通电两者就分叉,不拆键的话:要么自愈永远查不到台账(行永久卡 Unknown、UPC 永久占用,sku_plan §3.4),要么按真码去 UPC 池找号找不到 —— 该回收的不回收(号被永久占着)、该标已用的不标(下轮 claim 复用出一个其实已发出去的号 ⇒ 同 UPC 双上架,upc_pool 头注写的生死规则)。

**测试**:
- tests/test_list_new.py::test_heal_unknown_three_paths(既有,:538-599;V 空回归:断言逐个 range 与 `used == [("0011", a2)] and released == ["0022"]` 一字不改仍通过)
- tests/test_listing_sku_col.py::test_heal_unknown_uses_v_for_ledger_and_asin_for_upc_pool(V 填真码的行:假游标断言喂给 _SQL_HEAL_RECEIPT/_SQL_HEAL_ONLINE 的第二个数组是真码、喂给 upc_pool 查询的数组是 ASIN;断言 mark_used 收到的 pair 第二元是真码)
- tests/test_listing_sku_col.py::test_upc_pool_is_still_keyed_by_asin(反向守门:源码扫 services/listing_sheet.py,断言 upc_pool 相关查询与 _mark_upc_conflicts 的反查一律出现 `asin`,不出现 row_sku 参与的池键)

**验收**:python -m pytest -q tests/test_list_new.py::test_heal_unknown_three_paths tests/test_listing_sku_col.py -k "heal_unknown or upc_pool"

#### B1-11 · `services/listing_sheet.py` · 516-565(sync_from_ledger);具体 535、536-556(循环)、542、547、553-555、557

**改动**:① 535 行 `conflicts: list[tuple[str, str]] = []       # (asin, 全部码) → 正交标 UPC 池` 的注释改成 `# [(店铺, ASIN)] → 正交标 UPC 池(领号键)`(类型标注本来就是二元组,原注释文字是错的,一并改对)。② 循环体 537 行之后新增 `sku = row_sku(r)`;542 行 `st = cache[fid].get(r["asin"])      # 上架 sku=asin 约定` 改 `st = cache[fid].get(sku)   # V 列真 SKU;V 空回落 B 列 ASIN(存量行)`。③ 547 行 `desc = descs.get(fid, {}).get(r["asin"])` 改 `.get(sku)`。④ 553-554 行 `codes.get(fid, {}).get(r["asin"], set())` 改 `.get(sku, set())`。⑤ 555 行 `conflicts.append(r["asin"])` 改 `conflicts.append((r["store"], r["asin"]))`(撞库标号走领号键,与 B1-09 对齐)。556 行 O{r}:Q{r} 的写入与 558-565 的摘要文案一字不动。

**为什么**:feed_track.item_results / item_errors / item_codes 三个 dict 的键都是台账里的 SKU(ops.feed_items.sku = 沃尔玛侧真 SKU)。批次 2 起用 ASIN 去 get 必返 None ⇒ 每行都 `continue` ⇒ O/P/Q 永不回填、上架结果永远停在「处理中」,而且摘要显示正常(sku_plan §3.4 列的第一危险形态)。

**测试**:
- tests/test_list_new.py::test_listing_reflector_writes_opq(既有,:50;V 空回归)
- tests/test_list_new.py::test_error_desc_joined_into_p_column(既有,:124;V 空回归)
- tests/test_listing_sku_col.py::test_receipt_lookup_uses_v_when_present(两行:一行 V=真码、一行 V 空;台账 dict 只按各自的键给终态,断言两行都被回填 O/P/Q —— 钉住「新码按 V 找、存量按 B 列找」同时成立)

**验收**:python -m pytest -q tests/test_list_new.py -k "reflector or error_desc" && python -m pytest -q tests/test_listing_sku_col.py -k receipt_lookup

#### B1-12 · `workflows/sku_locked_heal.py` · 62-64、79、92、102、125;文件头 doc 9-27

**改动**:全部改用 `listing_sheet.row_sku(r)`(模块 34 行已 `from services import … listing_sheet …`,无需新增导入)。① 62-64 行 todo 过滤:在推导式之前定义局部 `def _key(r): return (r["store"], listing_sheet.row_sku(r))`,过滤条件写成 `if _key(r) not in open_pairs and _key(r) not in failed_pairs`(两处必须同源,不许各写一遍表达式)。② 79 行 `skus = [r["asin"] for r in srows]        # 上架 sku=asin 约定` 改 `skus = [listing_sheet.row_sku(r) for r in srows]   # V 列真 SKU;V 空回落 B 列(存量行)` —— 这条就是提交给沃尔玛的 RETIRE_ITEM 载荷。③ 92 行冷却表落库 `(store_name, r["asin"], res["feed_id"])` 改 `(store_name, listing_sheet.row_sku(r), res["feed_id"])`。④ 102 行事件 `{"sku": r["asin"], …}` 改 `{"sku": listing_sheet.row_sku(r), …}`。⑤ 125 行 dry-run 打印的 `skus = [r["asin"] for r in srows]` 同 ②。⑥ 文件头 doc 的「防重与安全」段(19-27)补一句:「行的 SKU 一律经 `listing_sheet.row_sku`(V 列,空则回落 B 列 ASIN)—— 退役发的必须是沃尔玛侧那条记录的 SKU,发 ASIN 会退不到或退错;冷却表键、事件 sku、todo 过滤键三者与它同源,任一处不同源 = 冷却防重失效或行永久卡死。」

**为什么**:sku_plan §3.4 点名的最危险一条:「退役发的是 ASIN,退不到/退错」。而且 62-64 / 92 的键必须与 233 行(B1-13)同源:两侧不同源 ⇒ 冷却防重失效 ⇒ 同一 SKU 每轮重复提交 RETIRE_ITEM(RETIRE_ITEM 官方无配额值、本仓按 DELETE 同档保守,conventions §七,重复提交直接烧配额)。listing.retire_cooldown 的部分唯一索引是 (store, sku)(:47-51),存量行里存的是 ASIN,V 空时 row_sku 仍返 ASIN ⇒ 与存量冷却行对得上。

**测试**:
- tests/test_sku_locked_heal.py::test_retire_submits_and_starts_cooldown(既有,:65-86;`_row` 加 sku 键后回归:仍断言 `submitted["skus"] == ["B0LOCK0002", "B0LOCK0003"]` 与 INSERT 第二参同值)
- tests/test_sku_locked_heal.py::test_retire_uses_the_v_column_sku_when_present(新增:`_row(2, sku="N7QM2X9RT4W3")`,断言 submit_feed 收到的 skus == ["N7QM2X9RT4W3"]、冷却表 INSERT 的第二个参数也是它、product_events 记的 sku 也是它)
- tests/test_sku_locked_heal.py::test_legacy_rows_still_retire_by_asin(新增:V 空的行,断言三处仍是 ASIN)
- tests/test_sku_locked_heal.py::test_dry_run_reports_without_touching_anything(既有,:52-63;回归:dry-run 打印用 row_sku 且不写冷却表)
- tests/test_sku_locked_heal.py::test_multi_slice_results_line_up_with_their_own_rows(既有,:89-117;回归:各片挂各片 feed_id 的断言不变)

**验收**:python -m pytest -q tests/test_sku_locked_heal.py && python cli.py sku_locked_heal --dry-run

#### B1-13 · `workflows/sku_locked_heal.py` · 233

**改动**:`locked_by_pair = {(r["store"], r["asin"]): r for r in locked}` 改 `locked_by_pair = {(r["store"], listing_sheet.row_sku(r)): r for r in locked}`。

**为什么**:`_relist` 用 `locked_by_pair.get((store_name, sku))`(:190)把冷却表里的 (store, sku) 映射回表行;冷却表的 sku 由 B1-12 的 92 行写入。两边不同源 ⇒ 冷却期满、回执成功却找不到行 ⇒ 走「行已被人工改过,只关冷却不清列」分支(:200-203) ⇒ 行永久卡在 O=SKU_LOCKED、UPC 永久占用,而且日志只有一条 logger.info,没人会看见。另:243-244 行的 failed_pairs 也用 `in locked_by_pair` 过滤,同一个键空间,改本行即同步生效。

**测试**:
- tests/test_sku_locked_heal.py::test_ripe_success_clears_row_failed_marks(既有,:134-;回归)
- tests/test_sku_locked_heal.py::test_ripe_success_matches_row_by_v_column(新增:冷却表 state 里的 sku 是真码、行 V 也是真码,断言 `cleared == [该行行号]`;再造一例冷却表存真码而行 V 空 ⇒ 走「只关冷却不清列」,钉住这条降级路径是有意的)
- tests/test_sku_locked_heal.py::test_open_cooldown_not_resubmitted_and_failed_needs_human(既有,:120-131;回归:failed_pairs 过滤仍成立)

**验收**:python -m pytest -q tests/test_sku_locked_heal.py

#### B1-13B · `workflows/sku_locked_heal.py` · 186-207(_relist 的 success 分支;burn_pairs.append 在 **197**,row 取自 190),烧号调用在 208-212

**改动**:197 行 `burn_pairs.append((store_name, sku))` 改为 `burn_pairs.append((store_name, row["asin"] if row is not None else sku))`,行尾补注释 `# 烧号按领号键 (store, ASIN):upc_pool.asin 永远是 ASIN(burn_for_retire → services/upc_pool.py:188-206);sku 变量是冷却表里的 SKU,批次 2 起就不再等于 ASIN`。196 行上方那段「旧号永久弃用(2026-08-19…)」的注释一字不动,只在其后加上面这句。

**为什么**:**本条为本次修订新增(审查者丙 missing #5 与我自己复核共同发现,原工作包完全没有)。** `upc_pool.burn_for_retire(conn, pairs)`(services/upc_pool.py:188-206)的 SQL 是 `WHERE store = t.s AND asin = t.a`,匹配的是 upc_pool 的 **asin 列**;而 197 行喂给它的 `sku` 来自 `_SQL_OPEN` 读回的 listing.retire_cooldown.sku —— B1-12 已把那一列的写入源改成 row_sku。批次 1 里两者恒等(V 空),但批次 2 一通电就分叉:烧号 SQL 一行都匹配不上 ⇒ **退役后旧 UPC 不烧** ⇒ 下一轮 `claim` 的「先复用后新领」(:146-160)把那个已绑死的旧号复用回来 ⇒ 「清列重上领新号」成为空话、SKU_LOCKED 死循环重演,而且全程零报错(rowcount=0 时 burn_for_retire 连日志都不打)。改法在批次 1 里逐字节等价:row 非 None 时 `row["asin"]` 与 `sku` 同值(键本来就是用 row_sku 建的、row_sku 此刻返 ASIN);row 为 None(行已被人工改过)时回落原值,与今天完全一致。在本批一并修掉是最省的:文件已经打开、测试已经在改,而批次 2 那时症状是「静默不烧号」,最难发现。

**测试**:
- tests/test_sku_locked_heal.py::test_burn_key_is_the_row_asin_not_the_cooldown_sku(新增:冷却表 sku 是真码、行 V 是真码而 B 列是 ASIN,断言 burn_for_retire 收到的 pair 第二元是 **ASIN**)
- tests/test_sku_locked_heal.py::test_ripe_success_clears_row_failed_marks(既有,回归:V 空时 burn_pairs 仍是 ASIN,烧号条数不变)

**验收**:python -m pytest -q tests/test_sku_locked_heal.py -k "burn or ripe"

#### B1-14 · `workflows/list_new.py` · 259(mark_used);**不动** 260-264(listing_sources.register)与 265-269(product_events.record_many)

**改动**:259 行 `upc_pool.mark_used(conn, [(u, r["asin"]) for r, u in batch])` 改 `upc_pool.mark_used(conn, [(u, listing_sheet.row_sku(r)) for r, u in batch])`,行尾补注释 `# 写进 upc_pool.sku 的是行上的 SKU(V 列;批次 1 仍回落 B 列 ASIN,批次 2 起是真码)`。listing_sheet 已在 :99 导入。

**为什么**:sku_plan §3.4 与 synthesis required_changes #8 都点名 mark_used 要改传真 SKU。upc_pool.sku 是纯投影列(飞书 UPC 池 E 列的数据源,services/upc_pool.py:106-126),先切没有副作用。**register / record_many 本批显式不动**:批次 2 要把它们改成读预备期 mint 挂在行上的 `r["_sku"]`(登记簿是码的权威出生地,不能反过来从表投影里取),现在改成读 V 会制造第二个真值来源,批次 2 还要再改一次。

**测试**:
- tests/test_list_new.py::test_multi_slice_results_line_up_with_their_own_rows(既有,:1049;回归:`seen["released"]` 与写出的 K 列不变)
- tests/test_listing_sku_col.py::test_mark_used_carries_the_row_sku(直接调 ln._apply_submit_result,打桩 upc_pool.mark_used 捕获实参:V 空的行传 ASIN,V 有值的行传真码;同时断言 listing_sources.register 的 sku/source_key 本批仍是 ASIN —— 反向钉住「本批不动写侧」)

**验收**:python -m pytest -q tests/test_list_new.py tests/test_listing_sku_col.py -k "mark_used or slice"

#### B1-15 · `workflows/list_new.py` · 13(文件头 doc)、251-256(_apply_submit_result docstring);**代码零改动**,两个 write_submit_cols 调用点 221 与 1769 一字不动

**改动**:**本批不改代码,只改两处文字。** ① 13 行「驱动表 = 上架表(registry.LISTING_SHEET,21 列):」改「(registry.LISTING_SHEET,22 列 A~V;V=SKU 于 2026-09 追加,批次 1 只加列不写):」。② _apply_submit_result 的 docstring(251-256)在 255 行之后补一句:「updates 的值列表本批仍是**八值**(C,H,I,J,K,L,M,N)—— V 列由批次 2 预备期 mint 出码后作为第 9 值追加,**两条落地路径(:219 首轮直接提交 / :1767 延后结算)必须一起加**,漏一条就会有一批行 K=Yes 而 V 空、回执反哺器再也找不回它们。」

**为什么**:「两轮共用一条落地路径」是该函数存在的全部理由(现 docstring 253-255 自述),批次 2 的接线点必须在这里留下书面记号,否则延后结算那条路极易被漏(与它当年漏 mark_used / listing_sources.register 是同一形态)。本批显式不改代码,是为了守住零行为变化。13 行的「21 列」是本次加列后**唯一残留的错误列数陈述**(其余 21 列字样全在 tests/test_feishu*.py 与 tests/test_audit_sheet_loop.py:54 里,那些是 2026-08-19 事故的史实描述,**不许改**,见 B1-20)。

**测试**:
- tests/test_list_new.py::test_deferred_rows_write_no_terminal_state_until_the_settle_round(既有,:793;回归:两轮写出的值列表仍是 8 格)

**验收**:python -m pytest -q tests/test_list_new.py && grep -n '22 列 A~V' workflows/list_new.py

#### B1-16 · `registry/resources.py` · 934-936(UPC_SHEET 上方注释;936 行是「…D=店铺 E=SKU F=上架日期。」)、937-942(UPC_SHEET 定义,columns 在 941)

**改动**:**决策点 D3,默认取 (a)**。(a) 默认 —— 不加列,只把口径写死在注释里:936 行之后新增 `# ⚠ E 列语义 = catalog.upc_pool.sku 的投影(services/upc_pool.project_to_sheet:106-126 写 C{r}:F{r})。批次 1 之前它实际存的是 ASIN(上架 sku=asin 约定);批次 2 起 mark_used 写**真 SKU**(经 services/listing_sheet.row_sku),E 列届时显示 12 位不透明码。ASIN 不丢:catalog.upc_pool.asin(领号复用键 (store, asin))永远存 ASIN,要在表上看就按 (b) 加列。`,columns 元组(941)不动。(b) 备选 —— 所有者先在飞书 UPC 池表建 G 列「ASIN」,然后:941 行 columns 追加 `"asin"`;services/upc_pool.py:256-264 的 `lookup` 的 SELECT 加 `asin`、返回四元组改五元组;:106-126 的 `project_to_sheet` 的 vals(122-123)追加 `asin or ""`、写入 range(125)由 `C{r}:F{r}` 改 `C{r}:G{r}`;并同步 docs/feishu_tables.md 的 UPC 池行。

**为什么**:sku_plan §3.5 与 §8 都把「UPC 池表 E 列口径」列为待决项,本条是它的**唯一归属**。选 (a) 的理由:批次 1 承诺零行为变化,加列会立刻改动 project_to_sheet 的写入宽度(即行为变化),而 E 列在批次 2 之前显示的内容一个字都不会变;真要给运营看 ASIN,库里已有权威列,随时可加。【订正原工作包行号】原稿写「UPC_SHEET 在 935-943」「lookup 在 services/upc_pool.py:56-63」—— 复核后 UPC_SHEET 注释 934-936、定义 937-942、columns 941;`lookup` 在 **256-264**(56-63 是 sync_rows 的 SQL 段)。【审查处理】审查者甲指出 0a-08 与横切 C0-REG-4 也在动 upc_pool 的状态/口径登记 —— 采纳:本条只管 **UPC_SHEET 的 E 列口径注释**,不碰 services/upc_pool.py:26-27 的 STATUS_CN(那归 0a-08 一处);横切 C0-REG-4 中与 UPC_SHEET E 列相关的部分应删除,决策合并为本包 D3。

**测试**:
- (a) 无新增测试;(b) tests/test_listing_sku_col.py::test_upc_pool_sheet_projects_asin_column(断言 project_to_sheet 写的是 `C{r}:G{r}` 且第 5 格是 ASIN)

**验收**:(a) python -m pytest -q tests/test_list_new.py && grep -n 'E 列语义 = catalog.upc_pool.sku' registry/resources.py;(b) 先跑 python -c "from api import feishu; from registry import resources; print(feishu.sheet_values_small(resources.UPC_SHEET, 'A1:G1'))" 确认 G 列表头是「ASIN」,再 python -m pytest -q

#### B1-17 · `tests/test_list_new.py` · 27-32(_sheet_row;`[""] * 21` 在 **28**)

**改动**:28 行 `d = dict(zip(resources.LISTING_SHEET.columns, [""] * 21))` 改 `d = dict(zip(resources.LISTING_SHEET.columns, [""] * len(resources.LISTING_SHEET.columns)))`,并在函数上方加一行注释「宽度从 registry 取,加列即自动跟上 —— zip 在最短处截断,写死数字会让新列的键根本不出现在夹具行里」。**必改**。

**为什么**:这个夹具是三个测试文件的共同上架表行工厂(tests/test_audit_sheet_loop.py:32 与 tests/test_product_ingest.py:10 都 `from tests.test_list_new import _sheet_row`);zip 在最短处截断,不改的话 22 列时 "sku" 键根本不会出现在夹具行里,凡是走 row_sku 的路径都会退化成 .get 兜底,**测不出问题而且全绿**。这是本批最容易假绿的一处,所以单列一条,不许混进别的改动里被忽略。

**测试**:
- tests/test_list_new.py(整文件回归)
- tests/test_audit_sheet_loop.py(整文件回归)
- tests/test_product_ingest.py(整文件回归)

**验收**:python -m pytest -q tests/test_list_new.py tests/test_audit_sheet_loop.py tests/test_product_ingest.py

#### B1-18 · `tests/test_sku_locked_heal.py` · 37-40(_row)

**改动**:`_row` 签名加 `sku=""` 参数,返回的 dict 里加 `"sku": sku`;上方加注释「默认 V 空 = 存量行形态;传 sku= 即模拟批次 2 之后的真码行」。新增 B1-12/B1-13/B1-13B 里列的五个测试:test_retire_uses_the_v_column_sku_when_present / test_legacy_rows_still_retire_by_asin / test_ripe_success_matches_row_by_v_column / test_burn_key_is_the_row_asin_not_the_cooldown_sku。

**为什么**:自愈链的五个 (店, SKU) 键 + 一个烧号键改了源,必须同时有「V 有值」与「V 空」两种形态的测试,否则批次 2 通电时才发现键不同源 —— 而那时的症状是重复 RETIRE、行永久卡死、旧号不烧,都不是报错。

**测试**:
- tests/test_sku_locked_heal.py::test_retire_uses_the_v_column_sku_when_present
- tests/test_sku_locked_heal.py::test_legacy_rows_still_retire_by_asin
- tests/test_sku_locked_heal.py::test_ripe_success_matches_row_by_v_column
- tests/test_sku_locked_heal.py::test_burn_key_is_the_row_asin_not_the_cooldown_sku

**验收**:python -m pytest -q tests/test_sku_locked_heal.py

#### B1-19 · `tests/test_listing_sku_col.py` · 新建文件

**改动**:新建本批的钉子文件,模块 docstring 写明「批次 1|上架表 V 列与 row_sku 的回归与守门:V 空必须与改造前逐字相同,V 有值必须走真 SKU」。收录 B1-01/03/04/05/06/07/08/09/10/11/14/16 各条 tests 字段里点名的测试,外加两条:① 整体等价性回归 `test_legacy_sheet_is_byte_identical_end_to_end` —— 构造 5 行全 V 空的上架表(含 Unknown 行、在途行、SKU_LOCKED 行),分别跑 heal_unknown 与 sync_from_ledger,把写出的 (range, 值矩阵) 列表与硬编码的期望值逐字比对(期望值直接抄自改造前的 tests/test_list_new.py::test_heal_unknown_three_paths:590-599 与 ::test_listing_reflector_writes_opq 的断言);② `test_match_sheet_receipt_lookup_reads_its_own_b_column` —— 源码断言 services/match_sheet.py:109 与 :112 仍按 `r["sku"]` 取值(跟卖表 B 列本来就是 SKU 列,registry MATCH_SHEET.columns[1] == "sku"),钉住「MATCH_SHEET 本批不动」这条结论有代码依据,而不只是文档里一句话。

**为什么**:本批的全部价值就是「加了能力但什么都没变」,必须有一个文件把「没变」这件事钉住,否则批次 2 出问题时无法二分是哪一批引入的。守门测试(三条源码文本扫描)防止后来者又长出第二份 row_sku 或第二处硬编码 V。【审查处理】审查者甲/乙/丁一致指出全套材料里守门测试被拆进四个文件、白名单互相打架 —— 采纳其精神,见决策 D8:本文件里的三条守门都是**只扫 3 个固定文件、不含随批次演进的白名单**的局部守门;若合并时批次 0a 已交付 tests/test_sku_guard.py,这三条搬进去,**不许两处都有**。【审查处理】审查者丁 missing #8 指出 match_sheet 回执找行改码后仍成立这条结论没有测试钉住 —— 采纳,加测试 ②。

**测试**:
- tests/test_listing_sku_col.py(整文件)

**验收**:python -m pytest -q tests/test_listing_sku_col.py

#### B1-20 · `tests/test_audit_sheet_loop.py` · 398-410(test_columns_contract;docstring 399-405,`assert len(cols) == 21` 在 **410**)

**改动**:410 行 `assert len(cols) == 21                               # A~U` 改 `assert len(cols) == 22                               # A~V`,并在其后加两行:`assert cols[21] == "sku"                             # V=SKU(2026-09 SKU 改造)` 与 `assert cols[:21] == (…把现有 21 个字段名逐字抄进来…)   # V 只准加末尾,前 21 列一个不许动`。docstring(399-405)末尾补一句:「V 于 2026-09 追加在**末尾** —— 写入侧 A:B / C / C:G / F / H:J / H:N / K:Q / N / O:Q / V 全是字母硬编码,新列只准加末尾。」**同文件 :54 的「21 列全量读撞 10MB」不许改** —— 那是 2026-08-19 事故的史实,当时确实是 21 列;同理 tests/test_feishu.py:405、tests/test_feishu_channels.py:373、tests/test_feishu_guard.py:10 三处也不许跟着 sed。

**为什么**:这条是既有的列序守门测试,加列必须同步,否则整套回填静默写到隔壁列去(它自己的 docstring 说的)。特意点名四处「21 列」史实描述不许改,是因为本批唯一的机械动作就是全仓搜 21 列,盲 sed 会把事故记录改成假的。【订正原工作包行号】原稿写「404-410 / docstring 398-403」—— 复核后测试体是 398-410,docstring 是 399-405,断言在 410。

**测试**:
- tests/test_audit_sheet_loop.py::test_columns_contract

**验收**:python -m pytest -q tests/test_audit_sheet_loop.py::test_columns_contract && grep -n '21 列全量读撞' tests/test_audit_sheet_loop.py  # 期望仍在,史实未被误改

#### B1-21 · `docs/feishu_tables.md` · 63(「上架表(新)」一行)

**改动**:该行里「21 列 A~U(较旧 26 列砍 状态跟踪/最近跟踪日期,产品事件账本承接;U=核验 UPC 一致性)」改为「22 列 A~V(较旧 26 列砍 状态跟踪/最近跟踪日期,产品事件账本承接;U=核验 UPC 一致性;**V=SKU**,2026-09 SKU 改造追加,批次 1 只加列不写值 ⇒ 存量行为空 ⇒ 全链回落 B 列 ASIN,取值只准经 `services/listing_sheet.row_sku`)」。同行末尾的「⚠ 列序唯一出处 = registry.resources.LISTING_SHEET.columns」保留。**不改** :64 跟卖表那行(MATCH_SHEET 本次不动)。

**为什么**:CLAUDE.md「改了 workflow 同步对应文档」;飞书表清单是运营与下一个人查列序的第一入口。

**测试**:
- (文档,无测试;由 acceptance 的 grep 核)

**验收**:grep -n '22 列 A~V' docs/feishu_tables.md

#### B1-22 · `docs/listing_plan.md` · 336、345;**不改** 199

**改动**:① 336 行「上架表**新建**于在线产品表格,21 列 A~U(砍掉 状态跟踪/最近跟踪日期,」补成「…21 列 A~U(2026-09 加 V=SKU,现 22 列 A~V)(砍掉…」。② 345 行「- [x] registry:LISTING_SHEET(21 列)/MATCH_SHEET(11 列)」的 21 改 22,并在行末注明「MATCH_SHEET 本次不动:B 列本来就是 SKU 列(跟卖号 PHUMWMT…,运营手填优先,registry MATCH_SHEET.columns[1]=="sku"),回执找行已按 `r["sku"]`(services/match_sheet.py:109、:112)」。③ **199 行「26→21 列」不改** —— 那是 2026-08 的迁移史实。

**为什么**:listing 链的定稿文档,列数写错会让下一个人按 21 去算字母。顺带把「MATCH_SHEET 不动」的理由留在纸面上,免得后来者以为漏了(它在 B1-19 里另有源码测试钉住)。

**测试**:
- (文档,无测试)

**验收**:grep -n '22 列\|A~V' docs/listing_plan.md && grep -n '26→21 列' docs/listing_plan.md  # 史实行仍在

#### B1-23 · `docs/sku_plan.md` · 157(§3.5 上架表那一行)、370-383(§7 批次 0 段;`UPC 撞库标记改按 (store, asin)` 在 372-373)、385-389(§7 批次 1 段)、465-468(§8 待办两条)

**改动**:① 157 行的「要做的」由计划语气改成完成记录:「**已加 V 列「SKU」**(批次 1,2026-09):`columns` 末尾追加、`_COLS` 改从 registry 派生、新增 `row_sku`/`_sku_range`/`write_sku_col`、`write_submit_cols` 收可选第 9 值;**本批不写 V**,写入由批次 2 接」。② **372-373 行把「UPC 撞库标记改按 (store, asin)」从批次 0 的清单里移到批次 1** —— 实际的批次 0a 工作包 26 条 item 里没有任何一条碰 services/listing_sheet,继续挂在批次 0 名下会让执行者以为别人会做而跳过它。③ 385-389 的批次 1 段改写:补「实际范围还包含 `_mark_upc_conflicts` 改按 (store, asin)(顺带修跨店误烧号与 missing 计数可为负)、`heal_unknown` 的键一分为二(台账/目录按 SKU、UPC 池按 ASIN)、`sku_locked_heal` 的烧号键改按行的 ASIN(:197)」,并把「改读 `r["sku"] or r["asin"]`」改成「改读 `listing_sheet.row_sku(r)`(唯一出处,不许各处自己写 or 表达式)」;同段删掉「销售/售后订单表「ASIN」列;在线产品总表「来源码」列」两句(那三列归批次 0b,不在本批)。④ 465-467「飞书建列」一项里勾掉「上架表 V「SKU」」(所有者已建列后);468「UPC 池表 E 列「SKU」口径」一项按 B1-16 的决议(默认 (a):E 列改存真 SKU,ASIN 留在库里 catalog.upc_pool.asin,表上暂不加列)标注并勾掉。

**为什么**:sku_plan 是这次改造的总账,批次做完不回写就会让批次 2 的执行者按过时的清单干活(而且 sku_plan 里的行号已经在漂,§8 明说过)。②是本次修订新加的:审查者丁核实批次 0a 工作包不含此项,原工作包 depends_on 里那句「若 0a 已经改完,本包 B1-09 降级为核验」是错的,会造成两边都不做。

**测试**:
- (文档,无测试)

**验收**:grep -n 'V 列「SKU」' docs/sku_plan.md && sed -n '370,390p' docs/sku_plan.md  # 人眼核:撞库标记已从批次 0 段移到批次 1 段

### DDL

(无)

### 文档同步

- docs/feishu_tables.md:63(上架表 21 列 A~U → 22 列 A~V,V=SKU;跟卖表那行 :64 不动)
- docs/listing_plan.md:336、345(列数 21 → 22;顺带注明 MATCH_SHEET 本次不动的理由与代码依据 services/match_sheet.py:109/:112);:199「26→21 列」是史实,不动
- docs/sku_plan.md:157(§3.5 上架表行改成完成记录)、:372-373(把「UPC 撞库标记改按 (store, asin)」从批次 0 段移到批次 1 段 —— 0a 工作包实际不含此项)、:385-389(批次 1 段补 _mark_upc_conflicts / heal_unknown 拆键 / sku_locked_heal 烧号键,删掉归 0b 的三列)、:465-468(§8 勾掉「上架表 V 列」、按 D3 标注 UPC 池 E 列口径)
- registry/resources.py:503-513(LISTING_SHEET 上方注释块,随 B1-01 一起改,不是独立文档)、:934-936(UPC_SHEET E 列口径注释,随 B1-16 一起改)
- services/listing_sheet.py:1-30(模块 docstring 列契约与列权责,随 B1-02 一起改)、:48-56(read_rows 头注补飞书越界列区间的实证结论,随 B1-04 一起改)
- workflows/list_new.py:13(文件头 doc「21 列」→「22 列 A~V」,随 B1-15 一起改)
- workflows/sku_locked_heal.py:19-27(文件头「防重与安全」段补 row_sku 同源纪律,随 B1-12 一起改)
- docs/db_schema.md —— **本批不动**:零 DDL,catalog.upc_pool / listing.retire_cooldown / catalog.listing_sources 的结构一个字节没改(listing_sources 的 abandoned_at/replaced_by 等列属批次 0a)

### 守门测试

- tests/test_listing_sku_col.py::test_nobody_recomputes_the_row_sku —— 源码文本守门:services/listing_sheet.py / workflows/list_new.py / workflows/sku_locked_heal.py 三个文件里不得出现第二份「V 空回落 B 列」表达式(`["sku"] or`、`.get("sku") or`,row_sku 自身那一行除外),也不得残留旧形态 `skus = [r["asin"]`、`"sku": r["asin"]`、`cache[fid].get(r["asin"])`。新增即红(conventions §六 单一实现路径)。
- tests/test_listing_sku_col.py::test_v_letter_is_written_in_exactly_one_place —— services/listing_sheet.py 里 `f"V{` 形态的字面量只允许出现在 _sku_range 一行;防止将来又冒出第二处硬编码 V(A/B 对调那次的同款错位风险)。
- tests/test_listing_sku_col.py::test_col_width_comes_from_registry —— `_COLS == len(resources.LISTING_SHEET.columns) == 22`;防止列数长出第二个出处。
- tests/test_audit_sheet_loop.py::test_columns_contract —— 既有守门,升级到 22 列并断言 `cols[21] == "sku"` 与 `cols[:21]` 一个没动(V 只准加末尾)。
- tests/test_listing_sku_col.py::test_clear_for_relist_never_clears_v —— 反向钉死:清列只写 `K{r}:Q{r}`,永远不碰 V(弃码只有 sku_codec.abandon 一个出口,清列不是弃码点)。
- tests/test_listing_sku_col.py::test_upc_pool_is_still_keyed_by_asin —— 反向钉死:upc_pool 的 claim/heal 查询、_mark_upc_conflicts 反查、sku_locked_heal 的 burn_pairs 一律用 (store, ASIN),不许用 row_sku(领号复用键不随 SKU 改造变)。
- tests/test_listing_sku_col.py::test_upc_conflict_only_burns_claimed_or_used —— `_mark_upc_conflicts` 的状态条件必须与 services/upc_pool.burn_for_retire(:198-203)逐字同口径,两个烧号出口不许各有一套判据。
- tests/test_listing_sku_col.py::test_legacy_sheet_is_byte_identical_end_to_end —— 全表 V 空时 heal_unknown 与 sync_from_ledger 写出的 (range, 值矩阵) 与改造前逐字相同(本批「零行为变化」的总闸)。
- tests/test_listing_sku_col.py::test_write_submit_cols_eight_values_is_byte_identical —— 八值调用不产生 V 段、H:N 恒 7 格。**这条同时是给批次 2 的护栏**:第 9 值永远可选,批次 2 若要改成必填必须显式立 item 撤销本条并说明理由,不许默默删断言。
- tests/test_listing_sku_col.py::test_match_sheet_receipt_lookup_reads_its_own_b_column —— 反向钉死:跟卖表回执找行按 `r["sku"]`(MATCH_SHEET B 列本来就是 SKU 列),本次改造不动它,后来者别顺手统一成 row_sku(那是上架表专用的积木)。
- ⚠ 归属纪律:以上守门都只扫固定的 3~4 个文件、不含随批次演进的白名单,故留在 tests/test_listing_sku_col.py。若合并时批次 0a 已交付 tests/test_sku_guard.py,全部搬进去,**两处不许都有**(决策 D8;审查者甲/乙/丁一致指出的四份守门文件互相打架问题,在本批的处理方式)。

### 验收命令

```bash
【① 阻塞式合并前置,必须先跑并把输出贴进 PR 描述】python -c "from api import feishu; from registry import resources; print(feishu.sheet_values_small(resources.LISTING_SHEET, 'A1:V1'))"   # 确认飞书上架表已建 V 列且第 22 格表头是「SKU」。返回不足 22 格 = 列没建(禁止合并);抛 FeishuError = 飞书对越界列区间报错(同样禁止合并,并把错误码记进 B1-04 要求的 read_rows 头注)。走标准小范围读薄壳,不绕通道。
```
```bash
python -m pytest -q   # 全量必须全绿(CLAUDE.md 常用命令)
```
```bash
python -m pytest -q tests/test_listing_sku_col.py tests/test_list_new.py tests/test_sku_locked_heal.py tests/test_audit_sheet_loop.py tests/test_product_ingest.py   # 本批直接波及的五个测试文件(原稿把 tests/test_alloc_push.py 也列进来是错的:本批不碰 alloc_push,已删)
```
```bash
python -c "from registry import resources as r; c=r.LISTING_SHEET.columns; print(len(c), c[-1])"   # 期望 `22 sku`
```
```bash
python cli.py list_new --dry-run   # 摘要各闸门桶计数必须与改造前一致;不得出现 FeishuError(证明 A2:V 读得通)
```
```bash
python cli.py list_new --dry-run -p store=<单店>   # 单店缩窄复核一次
```
```bash
python cli.py sku_locked_heal --dry-run   # 「将退役 N 个」列出的仍是 ASIN(存量 V 空);不写冷却表
```
```bash
python cli.py feed_poll --dry-run   # ⚠ 注意:feed_poll 的 DANGEROUS=False,cli.py:307 会强制 execute=True,这条命令**会真写飞书 O/P/Q 并真标 UPC conflict**(既有行为,本批不改,见 risks)。跑它是为了验证 sync_from_ledger/heal_unknown 在 22 列读宽下不炸;不接受这个副作用就跳过本条,改跑上面两条 dry-run。
```
```bash
psql "$(python -c 'from registry import db; print(db.pg_dsn())')" -c "SELECT count(*) FROM catalog.upc_pool WHERE status='used' AND sku IS DISTINCT FROM asin;"   # 期望 0:批次 1 结束时 upc_pool.sku 仍恒等于 asin,非 0 说明有人提前写了真码
```
```bash
psql "$(python -c 'from registry import db; print(db.pg_dsn())')" -c "SELECT store, asin, count(*) FROM catalog.upc_pool WHERE status IN ('claimed','used') GROUP BY 1,2 HAVING count(*)>1;"   # 期望 0 行:一个 (店,ASIN) 至多一个活号。非 0 则 _mark_upc_conflicts 改键后一次会烧多个号(claim 的 DISTINCT ON 明确容忍多活号,2026-08-19 加复用逻辑之前的存量可能有),需先人工并号再合并
```
```bash
psql "$(python -c 'from registry import db; print(db.pg_dsn())')" -c "SELECT store, sku FROM listing.retire_cooldown WHERE status IN ('pending','failed');"   # 人眼核:在途/失败冷却行的 sku 全是 ASIN 形态(与 V 空的行对得上);出现 12 位不透明码说明批次 2 已提前跑过,本批不得再合
```
```bash
grep -rn 'chr(ord("A")' services/listing_sheet.py   # 期望零命中(已换 feishu._col_letter);blacklist_sheet.py / catmap_import.py / blacklist_push.py 三处不在本批范围,仍有命中属正常
```

### 决策点

- **D1|决策 B(所有者未拍板)—— UPC 撞库 0101119 时码与 UPC 是否一起换**
  - 默认:默认「换」(码与 UPC 同寿命)。对**批次 1 的代码没有影响**:本批只把 _mark_upc_conflicts 的反查键从 upc_pool.sku 改成 (store, asin)、并把状态条件与 burn_for_retire 对齐,烧号动作与语义一字不动(仍是 mark_conflict → 'conflict')。
  - 备选:(a) 换(默认,sku_plan §5.3 弃码点 ③):批次 2 在烧号处同时调 sku_codec.abandon(store, sku, 'upc_conflict'),并按 sku_plan 建议把状态值从 'conflict' 分化出 burned_lock/burned_delete;(b) 不换(维持 2026-08-09 口径「撞库只说明号被占,照常领新号重试同一 SKU」):批次 2 此处不接 abandon。两种都不改本批任何一行代码。
  - 影响:选 (a) 时批次 2 要在 B1-09 改完的那个函数里加弃码调用 —— 那时才需要把行上的 SKU 也传进来(见 D7 的取舍记录),本批的 (store, asin) 反查正好是 abandon 内部烧号需要的键,不用返工;选 (b) 时本批改动同样成立且**不需要**任何签名变化。两条路都不影响批次 1 的零行为变化承诺。
- **D2|存量行是否一次性把 B 列 ASIN 回填进 V 列**
  - 默认:默认**不回填**,V 保持空。
  - 备选:(a) 不回填(默认):「V 有值」⇔「这行发过真码」,批次 2 的 mint 与批次 3 的改码可以直接用「V 是否为空」区分存量与新码;回落逻辑由 row_sku 一处承担。(b) 回填 ASIN 让表面统一:需要写一个一次性 workflow(或用 write_sku_col 批量写),代价是「V 有值」从此不再等价于「发过真码」,批次 3 迁移时要另找判据(只能回登记簿查),而且回填本身是对生产表的一次全量写(几千行,飞书写通道要跑好几批)。
  - 影响:选 (b) 会让本批不再是零行为变化(表面数据变了),并削弱批次 2/3 最省事的判据。默认 (a) 的唯一代价是运营在表上暂时看不到 SKU 列有值 —— 而在批次 2 之前本来也确实没有真 SKU。
- **D3|UPC 池表(UPC_SHEET)E 列「SKU」口径(sku_plan §8 待决项,本包是唯一归属)**
  - 默认:默认 (a):不加列,只把口径写进 registry 注释 —— E 列 = catalog.upc_pool.sku 的投影,批次 2 起显示真 SKU;ASIN 留在 catalog.upc_pool.asin(领号复用键),要看再加列。见 B1-16。
  - 备选:(a) 默认:registry/resources.py:934-936 只改注释,零代码;(b) 加 G 列「ASIN」:所有者先在飞书建列 → UPC_SHEET.columns(:941)追加 "asin" → services/upc_pool.py:256-264 lookup 的 SELECT 与返回元组加 asin → :106-126 project_to_sheet 的 vals 追加 asin、写入 range 由 `C{r}:F{r}` 改 `C{r}:G{r}` → 同步 docs/feishu_tables.md。
  - 影响:(b) 立刻改变 project_to_sheet 的写入宽度 = 批次 1 不再零行为变化,且 lookup 返回形状变了要核它的全部调用者;(a) 的代价是批次 2 之后运营在 UPC 池表上只看得到 12 位不透明码、看不到它给了哪个 ASIN(要查库或等 (b))。建议按 (a) 走,把 (b) 挂到批次 2 与「在线产品总表加来源码列」一起做,让所有者只建一次列。**横切包 C0-REG-4 中重复登记本项的部分应删除**(审查者甲指出的四处 registry 重复登记之一)。
- **D4|读 A2:V 之前是否在代码里加一次表头/列数核验**
  - 默认:默认**不加代码检查**,但把实证从「上线纪律」升级为「阻塞式合并前置」:acceptance #1 的 `sheet_values_small(LISTING_SHEET, 'A1:V1')` 必须先跑通、输出贴进 PR,并把飞书对越界列区间的真实行为写进 read_rows 头注(B1-04 ③)。
  - 备选:(a) 不加代码(默认):零新增代码;失败形态由实证确定(api/feishu.py:704-715 对非 90221 的 FeishuError 直接 raise,若飞书报错则是响亮的异常;若飞书裁列则末列恒空、read_rows 的 `+ [""] * width` 补位天然接住,两种都不会写错列)。(b) 在 read_rows 里加一次性表头核验(仿 services/maint_sheet 的 `_HEADER_NAMES`/`_header` 做法,断言 A1:V1 第 22 格是「SKU」):多一次飞书读(list_new/feed_poll 一天跑很多次,每次 read_rows 都多一个请求),且核验逻辑本身是本文件的新能力。
  - 影响:(b) 能把「列建了但表头写错」也拦住,代价是每轮多一次飞书调用与一段新代码。(a) 拦不住「表头名写错」—— 但表头名对程序无意义(读取全程按 registry 字段名与位置,不按表头文字),所以那是纯人眼可读性问题。【审查处理】审查者丙把「列没建就上线」评为 blocker,理由是 depends_on 里的硬前置只是人的纪律、代码里没有东西阻止 PR 先合。采纳其结论但不采纳其手段:不加代码闸,改为把探针做成 PR 的阻塞检查项(acceptance #1)+ 决策 D6 的合并时序 + risks 第 1 条。
- **D5|决策 A(product_clear 停用是否给 problem_scan 加豁免)与决策 C(alloc_push 派工口径是否对齐去重闸)**
  - 默认:决策 A 默认「RETIRE 不弃码,豁免另议」;决策 C 默认「对齐」。**两者对批次 1 都是零影响**,本工作包不含任何相关改动。
  - 备选:A:(1) 加 lifecycle=RETIRED + 本仓 retire_submitted 豁免(推荐前半:不弃码);(2) 改采「RETIRE 回执成功即弃码」简化版。C:(1) alloc_push._SQL_ONLINE 对齐去重闸口径;(2) 不对齐(分配链派、list_new 每轮拦并写 N 理由)。四种组合下批次 1 的代码完全相同。
  - 影响:两条都落在批次 0a(alloc_push._SQL_ONLINE)与批次 2(弃码点)里。列在此处只为说明:批次 1 的执行者**不需要**等这两条拍板就能开工与合并。另注:审查者甲指出 synthesis required_changes #1 里的 `workflows/product_clear.py:20-21` 注释更正在决策 A 取**默认值**时同样必须做,而全套包里无人认领 —— 该条不属本批范围(本批不碰 product_clear),已在 risks 里留言转交批次 2 或横切包。
- **D6|B1-01(registry 加列)与其余各条的合并时序**
  - 默认:默认**整批一个 PR、但不许在 acceptance #1 通过之前合并**。合并顺序上把 B1-01 作为该 PR 的最后一个提交,便于评审一眼看清「加列」与「读列」的界线。
  - 备选:(a) 默认:一个 PR。理由 —— 只加列不改读侧,V 就是一列没人读的死列;只改读侧不加列,`r.get("sku")` 恒 None,B1-19 里「V 有值」那半边测试全部测不到真东西。(b) 审查者丙的建议:先合 B1-05/06/07(row_sku / _sku_range / write_submit_cols 第 9 值),它们不依赖列宽;等所有者建完列再单独合 B1-01+B1-03+B1-04(读宽变化)。这样「列没建」的风险窗口里生产上跑的仍是 21 列读宽。
  - 影响:(b) 更保守,代价是两个 PR、两次全量回归,而且第一个 PR 里 row_sku 的「V 有值」分支没有任何生产数据能触发(纯测试覆盖)。若所有者建列的时间点不确定(比如要等几天),选 (b);若建列可以在合并当天完成,选 (a)。**无论选哪个,acceptance #1 都是合并的阻塞前置。**
- **D7|_mark_upc_conflicts 的入参形状(2 元组 (store, ASIN) vs 含行上 SKU 的 3 元组)**
  - 默认:默认 **2 元组 `(store, ASIN)`**,与 B1-09 一致。
  - 备选:(a) 默认:2 元组,正好是 upc_pool 领号键,函数内不需要任何登记簿反查,批次 1 可完全独立于批次 0a 开发与合并。(b) 审查者乙建议的一步到位:入参改 `(store, 行上的 SKU)`,函数内经登记簿把 SKU 翻回 ASIN 再查池 —— 好处是批次 2 只剩「加一次 abandon 调用」。(c) 3 元组 `(store, ASIN, 行上的 SKU)`:批次 2 零签名变化。
  - 影响:(b) 被驳回:它要求批次 1 依赖批次 0a 交付的 services/sku_asin 反查积木,打掉本批「不依赖 0a 任何符号」这条性质,而这条性质是本批能与 0a/0b 并行开发的全部依据。(c) 被驳回:批次 2 在此处到底加不加 abandon 取决于**未拍板的决策 B**,若选「不换」,第三元将永远无人使用 —— 按猜测预留参数,churn 概率更高而不是更低。留给批次 2 的接线说明写在 B1-09 的 docstring 里:需要 SKU 时在调用点 555 处一并收集,或给本函数加**可选**第三元,但池反查这一跳的键永远是 (store, ASIN)(由 guard test 钉住)。
- **D8|本批三条源码守门测试的落脚文件**
  - 默认:默认放 **tests/test_listing_sku_col.py**;若合并时批次 0a 已交付 tests/test_sku_guard.py,则在同一个 PR 里搬进去,**两处不许都有**。
  - 备选:(a) 默认:本批三条守门(row_sku 唯一出处 / V 字母唯一出处 / 池键永远按 ASIN)都只扫 3~4 个固定文件、不含随批次缩短的白名单,与「跨批次白名单」性质不同,放本批自己的钉子文件不会产生维护分叉。(b) 一律进 tests/test_sku_guard.py:与 0a/0b/2/3 的守门统一门牌。
  - 影响:审查者甲/乙/丁一致指出全套材料里守门被拆进四个文件、白名单互相打架(extract_asin 调用点白名单出现三次、`abandoned_at IS NULL` 白名单三种数目)。那个问题的根源是**含白名单的**守门被复制;本批三条不含白名单,所以 (a) 不制造该问题。写成默认 (a) + 条件搬迁,是为了让本批既能独立合并,又不在 0a 落地后留下第二个门牌。

### 依赖

- 【硬前置|所有者动作,阻塞合并】飞书上架表末尾新建 V 列,表头写「SKU」。建之前先用 `list_fields` / 读 A1:U1 确认表里没有同名人工列(有的话程序一登记就开始覆盖它,sku_plan §8 已列出这条纪律)。建完由执行者跑 acceptance #1 的探针并把输出贴进 PR —— **探针没跑通不许合并**(决策 D4/D6)。
- 【技术上无依赖|批次 0a / 0b】本批**不 import 批次 0 的任何符号**(不用 sku_codec、不用 sku_asin.resolve_*、不碰 listing_sources 的新列、不写任何 DDL),可以在 main 上独立开分支、独立合并。顺序上排在 0a 之后只是评审方便,不是技术依赖。**订正原工作包**:原稿写「批次 0 的清单里也列了 _mark_upc_conflicts 改按 (store, asin),若 0a 已改完本包 B1-09 降级为核验」—— 复核批次 0a 工作包的 26 条 item,**没有任何一条碰 services/listing_sheet**;那句话来自 docs/sku_plan.md §7 批次 0 段(:372-373)的旧分配,而该分配从未落进 0a 的工作包。B1-09 是本批独有、不可跳过的改动;sku_plan 的分配由 B1-23 ② 同步改正。跳过它的后果:批次 2 起 0101119 撞库永不标 conflict、反复领到坏号,重演 SKU_LOCKED 死循环。
- 【被依赖】批次 2 的接线点全部落在本批交付的接口上:row_sku 加 `r.get("_sku")` 优先级(一行)、write_submit_cols 的第 9 值(_apply_submit_result 的两条落地路径 :219 与 :1767 都要传)、write_sku_col(批次 3 改码回写 V 与 confirmed 后的补写)。本批不合,批次 2 无处落地。
- 【不受影响,别顺手动】MATCH_SHEET(跟卖表)本次不动:B 列本来就是 SKU 列(PHUMWMT+日期+序号,运营手填优先,registry MATCH_SHEET.columns[1] == "sku"),services/match_sheet.py:109/:112 的回执找行已经按 `r["sku"]`(B1-19 加源码测试钉住);RETIRE_SHEET(停用删除表)B 列是运营手填 SKU,手动通道全格式通吃,不动;MAINT_SHEET 不动;UPC_SHEET 按 D3 默认只改注释。
- 【交给别人,本批不认领(审查者甲 missing 转交)】synthesis required_changes #1 里的 `workflows/product_clear.py:20-21` 注释更正(「停用/下架 → RETIRE_ITEM(可恢复)」要加注「现行 problem_scan 策略下可恢复窗口 ≈ 下一轮扫描之前」)在决策 A 取默认值时同样必须做,但全套包里无人认领。本批不碰 product_clear,建议由批次 2 或横切包立一条 item(file=workflows/product_clear.py, lines=20-21, tests=无, acceptance=grep)。

### 风险

- **列没建就上线(本批最高风险,已升级处置)**:B1-03 让 `_COLS` 从 registry 派生,于是 B1-01 落地那一刻 `read_rows()` 立即读 A2:V —— 而它是 workflows/list_new、workflows/feed_poll(经 listing_sheet.sync_from_ledger / heal_unknown)、workflows/sku_locked_heal:229、workflows/product_audit -p from_sheet=1(经 audit_targets)四条链的公共入口,前两条挂在 product_chain 上。飞书对「范围列数超出实际列宽」到底报错还是裁列,**离线无法验证**(api/feishu.py:704-715 只对 90221 兜底、其余 FeishuError 直接 raise 这一半已核实;单元测试里 feishu 全被打桩,这个假设永远测不出来)。缓解:acceptance #1 升级为阻塞式合并前置,输出必须贴进 PR;实证结论写进 read_rows 头注(B1-04 ③);另加 test_read_rows_pads_a_21_cell_legacy_row 覆盖「裁列」分支。若所有者建列时间不定,按决策 D6 备选 (b) 把 B1-01/03/04 拆成第二个 PR。【审查者丙 F4 采纳】
- **tests/test_list_new.py:28 的 `[""] * 21` 漏改**:zip 在最短处截断,夹具行里根本不会出现 "sku" 键,所有「V 有值」的测试都测不到真东西,而且全绿。这是本批最容易假绿的一处 —— B1-17 单列一条就是为了不让它混进别的改动里被忽略;而且该夹具被 tests/test_audit_sheet_loop.py:32 与 tests/test_product_ingest.py:10 共用,漏改会同时污染三个文件的回归结论。
- **write_submit_cols 的 `vals[1:]` 漏改成 `vals[1:8]`**:批次 1 传八值时不会暴露(切片结果相同),批次 2 一传九值就把 8 格塞进 `H{r}:N{r}` 段。**订正原工作包对失败机制的描述**:`api/feishu._check_shape`(:754-777)只校验列数 > 95 与单元格 > 40000 字符,**不校验值矩阵与 range 的形状是否匹配**,`sheet_write_ranges`(:1022-1034)也没有这道检查 —— 8 格会照发给飞书,由飞书在 `_sheet_put` 处以 90202(validate RangeVal fail)**整批拒**。后果一样致命:那时正在真跑上架、UPC 已 mark_used、事件已落库、表上 K 列还是空,下一轮读表会重发一遍(workflows/list_new.py:1701-1703 头注自述的正是这个裂缝)。B1-07 已把它标成必改。
- **sku_locked_heal 的键不同源(本批把它从四处扩到六处)**::62-64(todo 过滤)、:79(RETIRE 载荷)、:92(冷却表落库)、:102(事件)、:233(locked_by_pair)、:197(burn_pairs)。任意一处漏改就会出现「冷却防重失效 ⇒ 每轮重复提交 RETIRE_ITEM」(官方无配额值、本仓按 DELETE 同档保守,conventions §七)、「冷却期满找不到行 ⇒ 只关冷却不清列 ⇒ 行永久卡 SKU_LOCKED、UPC 永久占用」(只有一条 logger.info,没人会看见)、或「退役后旧号不烧 ⇒ claim 把绑死的旧号复用回来 ⇒ SKU_LOCKED 死循环重演」(rowcount=0 时 burn_for_retire 连日志都不打)。B1-12/B1-13/B1-13B 必须同一个 PR 一起改,由四个新测试(V 有值 / V 空 / 烧号键)双向钉住。**其中 B1-13B(:197 burn_pairs 改按行的 ASIN)是本次修订新增的,原工作包完全没有这一处 —— 审查者丙 missing #5 与复核 services/upc_pool.burn_for_retire:198-203 的 `WHERE store = t.s AND asin = t.a` 共同发现。**
- **heal_unknown 拆键拆错方向**:把 UPC 池查询也改成按 row_sku,批次 2 一通电就再也找不到 claimed 的号 —— 该回收的不回收(号被永久占着)、该标已用的不标(下轮 claim 复用出一个其实已发出去的号 ⇒ 同 UPC 双上架,services/upc_pool.py:13-15 头注写的生死规则)。守门测试 test_upc_pool_is_still_keyed_by_asin 就是拦这个。
- **_mark_upc_conflicts 改键后一次烧多号**:若历史上某个 (店,ASIN) 名下留了多个 claimed/used 的号(`claim` 的 `SELECT DISTINCT ON (store, asin) … ORDER BY … claimed_at NULLS LAST` 明确容忍这种数据,2026-08-19 加复用逻辑之前的存量可能有),新写法会把它们一次全标 conflict。缓解:acceptance #10 的 SQL 体检上线前核一次,有多号的先人工并号;另外 missing 计数已改成「零命中的 pair 数」,不会再出现旧写法那种 `-1 个在池中找不到` 的胡话告警。
- **`feed_poll --dry-run` 今天就在真写(本批发现、显式不修)**:services/listing_sheet.sync_from_ledger(:516)与 _mark_upc_conflicts(:336)都没有 execute/dry_run 形参;workflows/feed_poll.py:38 `DANGEROUS = False` ⇒ cli.py:307 强制 `params["execute"] = True`,`dry_run` 只是透传给 run() 而这两个函数根本不读。所以空跑 feed_poll 会真回填 O/P/Q、真标 UPC conflict。**本批不改**:加门禁会让 dry-run 从「写」变「不写」,那本身是行为变化,直接违背零行为变化承诺。它真正变致命是在批次 2 于此处接 abandon(不可逆弃码 + 永久烧 UPC)那一刻 —— **转交批次 2**:给 sync_from_ledger / _mark_upc_conflicts 加 execute 形参、feed_poll 按 cli 传下来的值透传、并重新评估 feed_poll 的 DANGEROUS 标记。【审查者乙 blocker #2,采纳其结论、按批次归属驳回其落点】
- **「V 有值 = 发过真码」这条判据的脆弱性**:任何人(包括运营)在表上手填 V,都会让 row_sku 返回一个沃尔玛侧不存在的 SKU ⇒ 回执找不到行、退役退不到。本批不做防护(表是展示面,登记簿是权威),但批次 2 起 V 与 catalog.listing_sources 不一致时应有体检 —— 留给批次 2 的工作包。
- **批次 2 的存量冷却行与真码不匹配**:listing.retire_cooldown 里的存量 pending/failed 行存的是 ASIN;批次 2 之后同一行的 V 变成真码 ⇒ locked_by_pair 对不上 ⇒ 走「只关冷却不清列」(:200-203)。本批不受影响(V 空),但批次 2 上线前必须确认 retire_cooldown 无在途行(acceptance #11),这条已提前写进验收里备查。
- **回滚方案(全套材料里唯一一份在横切包的散文里,本批自带一份)**:① 代码 `git revert` 即可,本批零 DDL、零不可逆写;② registry 的 sku 列**可以**随 revert 一起去掉(纯 Python 元组,不是 DDL);③ 飞书上的 V 列**不删**(conventions §五:删列不可回滚,而且删了下次重做还要所有者再建一遍);V 列此刻全空,留着零副作用;④ revert 后 read_rows 回到 A2:U,已建的 V 列不再被读,行为回到改造前;⑤ **不需要**任何数据修复:本批没写过任何一格 V、没改过任何一行库数据(upc_pool.sku 仍恒等于 asin,由 acceptance #9 保证)。唯一要留意的是:若 revert 时 `_mark_upc_conflicts` 已在生产上按 (store, asin) 跑过几轮,那几轮少烧的跨店号回不来 —— 但那正是本批要修的缺陷,回滚回去只会重新开始误烧。
- **行号漂移**:本工作包里每个行号都在 2026-09-02 的当前仓库状态上逐个打开文件核对过,与 docs/sku_plan.md §3.4 表格里的行号有出入(例如 sku_plan 写 listing_sheet.py:376-391/428-431,实际 heal_unknown 在 394-513、UPC 池查询在 428-431、键在 438)。本次修订另订正了原工作包的六处漂移:read_rows docstring「21 列」在 **52** 不是 55;write_submit_cols 的 `title, rest = vals[0], vals[1:]` 在 **147** 不是 148,两个 append 在 **148-149** 不是 150-151;_mark_upc_conflicts 结束在 **361** 不是 360;UPC_SHEET 在 **937-942**(注释 934-936)不是 935-943,其 `lookup` 在 **256-264** 不是 56-63;test_columns_contract 是 **398-410**(断言在 410)不是 404-410;_apply_submit_result 结束在 **285** 不是 283。执行时以本包为准,并在动手前再 grep 一次锚点字符串确认没被别的 PR 挪动。
- **别盲 sed「21 列」**:全仓还有四处「21 列」是 2026-08-19 那次 10MB 事故的史实描述 —— tests/test_audit_sheet_loop.py:54、tests/test_feishu.py:405、tests/test_feishu_channels.py:373、tests/test_feishu_guard.py:10,以及 docs/backlog.md:273 与 docs/listing_plan.md:199 的「26→21 列」迁移史。全部**不许改**。本批要改的只有六处:registry/resources.py:503、services/listing_sheet.py:3 与 :52、workflows/list_new.py:13、tests/test_audit_sheet_loop.py:410、docs 三处(B1-21/22/23)。
- **守门测试归属**:审查者甲/乙/丁一致指出全套材料把守门拆进四个文件、白名单互相打架。本批的三条守门不含随批次演进的白名单(只扫 3~4 个固定文件),所以不制造那个问题;但仍按决策 D8 留了条件搬迁条款 —— 0a 若已交付 tests/test_sku_guard.py,搬进去,两处不许都有。

### PR 切分

建议 **1 个 PR**(约 +290 / −60 行:4 个源文件 registry/resources.py、services/listing_sheet.py、workflows/sku_locked_heal.py、workflows/list_new.py;3 个测试文件改 + 1 个新建;3 个文档)。理由:B1-01(加列)与 B1-10/11/12/13/13B/14(读 V)必须同批 —— 只加列不改读侧,V 就是一列没人读的死列;只改读侧不加列,`r.get(\"sku\")` 恒 None,B1-19 里「V 有值」那半边测试全部测不到真东西。

**合并顺序上把 B1-01 放在该 PR 的最后一个提交**(决策 D6 默认 (a)),便于评审一眼看清「加列」与「读列」的界线;无论如何 acceptance #1 的 A1:V1 探针是阻塞前置。

若必须拆(决策 D6 备选 (b),适用于所有者建列时间不定的情况),按下面两刀,**两个 PR 之间可以上线**(1a 单独上线只改了纯新增的、没有调用方的函数与三处 docstring,读宽仍是 21 列,零风险):
- **PR 1a|只加能力,不改读宽、不改消费方**:B1-02(docstring)、B1-05(row_sku)、B1-06(_sku_range + write_sku_col)、B1-07(write_submit_cols 收第 9 值 + `vals[1:8]`)、B1-08(clear_for_relist docstring)、B1-15(list_new docstring,但「22 列」那句先不改)、B1-19 中与这些相关的测试。约 +150 行。
- **PR 1b|加列 + 切读宽 + 消费方切到 row_sku + UPC 撞库改键**:B1-01、B1-03、B1-04、B1-09、B1-10、B1-11、B1-12、B1-13、B1-13B、B1-14、B1-15(补「22 列」)、B1-16、B1-17、B1-18、B1-20、B1-19 剩余测试、B1-21/22/23(文档)。约 +140 / −60 行。这一刀里含全部有意的行为差异(跨店误烧号修复 + 状态口径对齐 + missing 计数修复),单独 review 更容易看清。

**工时与关键路径**(全套材料里五个批次包一律没给,审查者丁 missing #3 采纳):
- 写码 + 测试 ≈ **2 人日**(源码改动很小,大头是 tests/test_listing_sku_col.py 的十六个用例与 test_legacy_sheet_is_byte_identical_end_to_end 的期望值逐字抄写)。
- 文档同步 ≈ **0.3 人日**。
- **关键路径不在写码,而在两件人的动作**:① 所有者在飞书上架表建 V 列并确认没有同名人工列(不可并行、无法代劳);② 执行者跑 acceptance #1 探针并把飞书对越界列区间的真实行为写进 read_rows 头注。这两件之前,PR 可以开、可以评审、可以跑单测,但**不许合并**。
- 与 0a/0b 的关系:三者技术上互不依赖,**可以三条并行开发**;若人手只有一个,建议 0a → 1 → 0b(批次 1 最短、且它交付的接口是批次 2 的落地点,早合早解锁)。
- 生产观测期:合并后至少跑满 **一整轮 product_chain + 一轮 feed_poll**(即一个自然日),核对 list_new 摘要的各闸门桶计数与 sync_from_ledger 的「回填 N 行」与前一日同量级,再开批次 2。
