## 批次 2|写侧切换(唯一有行为变化的批次):list_new / match_listing 预备期 mint 不透明码 + 四个弃码点接 sku_codec.abandon + 退役冷却与代际上限两道新闸 + 试点上限闸 + 守门反向钉死

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

**目标**:把「SKU = ASIN」这条隐含约定从写侧彻底摘掉:上架提交的 sku 从此是 registry 定值来源字母 + 11 位不透明随机码,身份唯一出处 catalog.listing_sources,码在 list_new 预备期 _prep_rows 抽、提交前已 commit、复用到显式弃码为止;弃码只在四个点发生且只有 sku_codec.abandon 一个实现;list_new 增加「退役冷却中」「换码次数达上限,待人工」两道闸把「弃码→新码→再弃码」的烧号烧配额循环堵死,并新增 limit 参数让「一店一品 → 10 个 → 全店」的试点节奏成为代码闸而不是口头纪律。批次 0/1 已把读侧全部收口(登记簿反查 + 上架表 V 列),本批是那些收口第一次真正被用到的一批 —— 验收重点不是「新码发出去了」,而是「新码发出去之后,维护链 / 订单链 / 回执自愈链一个都没瞎」。本次修订相对原稿的五处实质变化:(1) 弃码点 3 所在的 listing_sheet.sync_from_ledger 走的是 feed_poll 反哺链,而 feed_poll 现在完全不认 --dry-run(DANGEROUS=False ⇒ cli.py:307 恒 execute=True,run() 也不读 params["dry_run"]),把不可逆的 abandon 放进去等于 `cli.py feed_poll --dry-run` 会真弃码真烧号 —— 新增 B2-29 把 execute 贯通五个反哺器;(2) 新增 B2-30 给 list_new 加 limit,原稿把试点节奏写在验收命令的括号里而 `-p limit=` 是个不存在的功能;(3) 守门测试合并进全项目唯一一份 tests/test_sku_guard.py(0a 建),本批只增删白名单,不再新建第四个守门文件;(4) match_listing 的 mint 事务不再包住逐行的沃尔玛 _precheck 调用;(5) alloc_survey._SQL_ONLINE 显式不改并写明理由,把原稿 change 正文与 decisions C 自相矛盾的地方拍平。

**零行为变化**:否

本批故意有行为变化,是整个 SKU 改造里唯一一批。变的东西共七处:(1) MP_ITEM / MP_ITEM_MATCH 载荷里的 sku 从 ASIN(或 PHUMWMT 续号)变成不透明码 —— 这是改造目的本身;(2) 提交成功后 upc_pool.sku、product_events.sku、上架表 V 列写的是新码而不是 ASIN(upc_pool.asin 列与 ASIN 归并不受影响,由登记簿反查承担);(3) 登记簿写入时点从「提交成功后 register」提前到「预备期 mint」,于是没提交成功的行也会留下活码行 —— 这是刻意的:码稳定才有 payload_key 在途防重、_SQL_ATTEMPTS 限次、upc_pool.claim 原号复用三条护栏,每次重上抽新码等于每轮白烧一个 UPC(sku_plan.md:261-265 第二条硬约束);(4) delete_verified 观测落地、SKU_LOCKED 退役成功、UPC 撞库 0101119(决策 B)三处开始写 abandoned_at 并烧号,此前只有 sku_locked_heal 一处裸 burn_for_retire(workflows/sku_locked_heal.py:208-212);(5) list_new 多两道闸,命中写 N 理由不写终态,与既有闸门同语义;(6) list_new 多一个 limit 参数(缺省 None = 与今天完全一致,只有显式传值才截断);(7) feed_poll 从此认 --dry-run:空跑时五个反哺器一行飞书、一行 PG 都不写。第 (7) 项是修复既有安全红线破口而不只是给新功能让路 —— 今天 `cli.py feed_poll --dry-run` 就已经会真的调 upc_pool.mark_conflict 永久烧号,本批只是在往这条路上加不可逆的 abandon 之前先把闸装上。不变的也要说死:存量 sku=asin 的行一个字节不动(登记簿里它们的 source_key 就是回填的 asin);历史订单行的 sku 永不改写;alloc_survey._SQL_ONLINE 排 RETIRED 的口径不动(它答的是「占用/冲突里这家店有没有活货位」,不是「该不该派工」,见 B2-24);services/risk_trace.still_listed 的 lifecycle 条件不动;product_clear 停用(RETIRE)、库存归零、缺席 missing_since、被沃尔玛 unpublish、提交被拒/FAILED/Unknown/PROHIBITED 五类「下架」一律不弃码(守门测试反向钉死)—— 沃尔玛侧那条记录还在、还绑着我们的 UPC,抽新码等于同店两条同内容记录加白烧一个号。

### 改动清单

#### B2-01 · `registry/resources.py` · 160(锚点::159 `WALMART_ERR_CONTENT = frozenset({"EXT_DATA_ERROR_07705958490105"})` 与 :161 `# ── 报错归类(第一步:引擎与对照报告用;换轨接线在第二步)` 之间新起一节;行号已复核)

**改动**:新增一节「SKU 编码:来源字母登记」,只放所有者定值的数据、不放算法。常量 SKU_SOURCE_LETTERS: dict[str, str],键为裸字符串 amz / match / 1688 / self(行内注释写明「取值须与 services/listing_sources.SOURCE_* 逐字一致;registry 是最底层,铁律 1 不许 import services,一致性由 tests/test_sku_guard.py 保证」),值为四个互不相同的大写字母,先填占位值(建议 amz=N / match=Q / 1688=T / self=X),每个值行内注释「占位,待所有者定值;定值后已 mint 的码不追溯」。同节再加 env 覆盖函数 sku_source_letters() -> dict[str, str],docstring 首行「输入:无(读环境变量)→ 输出:{来源类型: 大写字母}」:读环境变量 SKU_SOURCE_LETTERS(形如 amz=N,match=Q,1688=T,self=X),未配置返回上面的常量,解析出的字母不在字母表内或有重复一律抛 LookupError(不许回落默认值)。**字母表本身与 11 位长度不进 registry**:横切包决策 D4 已裁定 _ALPHABET / _LEN / is_opaque 唯一出生地是 services/sku_codec.py,registry 只留 SKU_SOURCE_LETTERS —— 本条据此不再新增 SKU_ALPHABET,校验字母是否合法时由 sku_guard 的断言去比对 sku_codec 的字母表,不在 registry 里放第二份。

**为什么**:铁律 3:一切外部定值只准从 registry 取;sku_plan §2 明写「来源字母由所有者定,映射只在 registry」。占位而不是留空的理由:留空会让整条上架链在字母未定时硬停,而占位值本身是任意的、只要互不相同就不影响正确性;真正致命的是「已 mint 的码回不去」,所以把这条风险写进注释并放进试点前置门,而不是靠代码兜。env 覆盖是为了所有者定值当天不必改代码就能上线,同时解析失败抛错而非回落 —— 回落会让「我改了字母」变成静默无效。**审查采纳**:四位审查者中有三位指出字母表被登记了三到四份(registry.SKU_ALPHABET / sku_asin.OPAQUE_ALPHABET / sku_codec._ALPHABET),两条守门断言互斥、不可能同时绿;本条按横切 D4 收敛到 sku_codec 一处,原稿里「字母表归 sku_codec」这句从注释升级为可执行约束(registry 里不出现字母表字面量,守门扫)。

**测试**:
- tests/test_sku_guard.py::test_source_letters_are_distinct_and_in_the_codec_alphabet(四个字母互不相同、长度为 1、都在 services.sku_codec._ALPHABET 内 —— 断言直接读 sku_codec 常量,不读 registry 的第二份)
- tests/test_sku_guard.py::test_source_letters_cover_exactly_the_registered_source_types(键集合 == {listing_sources.SOURCE_AMZ, SOURCE_MATCH, SOURCE_1688, SOURCE_SELF};SOURCE_UNKNOWN 不在其中 —— unknown 是回填桶,永不 mint)
- tests/test_sku_guard.py::test_env_override_refuses_a_bad_letter_instead_of_falling_back(monkeypatch 设 SKU_SOURCE_LETTERS=amz=O 触发 LookupError;O 不在字母表内)
- tests/test_sku_guard.py::test_registry_holds_no_second_alphabet(registry/resources.py 文本里不出现 23456789ABCDEFGHJKMNPQRSTVWXYZ 这个字符类)

**验收**:python -m pytest tests/test_sku_guard.py -q 全绿;python -c "from registry import resources; print(resources.sku_source_letters())" 打印四对映射

#### B2-02 · `services/sku_codec.py` · 批次 0a 建的模块,本批在其常量区与函数区追加(若批次 0a 尚未落地,本条与批次 0a 合并交付)

**改动**:追加四组东西:(a) 常量 RETIRE_COOLDOWN_HOURS = 24(行内注释:官方无明文,按旧仓实证 legacy_survey.md:1649 保留;唯一出处,sku_locked_heal 与 list_new 闸门都读它)与 MAX_SKU_GENERATIONS = 3(同 (store, source_type, source_key) 已弃码行数达到它即拦并点名人工);(b) 弃码原因常量 REASON_DELETE_VERIFIED = "delete_verified" / REASON_SKU_LOCKED = "sku_locked" / REASON_UPC_CONFLICT = "upc_conflict" / REASON_SKU_UPDATE = "sku_update",并在模块 docstring 用一张表钉死四个原因各对应哪个调用点、烧不烧号(前三个烧,sku_update 不烧);(c) placeholder(source_type: str) -> str:返回 letter + "DRYRUN00000"(共 12 位),docstring 首行「输入:来源类型 → 输出:dry-run 专用占位码(永不入库、永不与真码相撞)」,注释写明 0 与 U 被字母表明确剔除,所以占位码在形态上就不可能是任何一个真 mint 出来的码;(d) **mint 的最终签名在本批钉死**:mint(conn, store, source_type, source_key, *, workflow) -> str,**没有 dry_run 关键字参数**,取字母走 registry.sku_source_letters(),source_type 查不到字母直接抛 LookupError。若批次 0a 已按 `mint(..., workflow, *, dry_run=False)` 交付,本批必须删掉那个 dry_run 分支并把调用点补齐 workflow —— 两条 dry-run 路径并存就是双轨。

**为什么**:「常量单一出处」是 conventions §六的硬要求:24h 冷却此前长在 sku_locked_heal 的 params 默认值里(workflows/sku_locked_heal.py:227 `int(params.get("cooldown_hours", 24))`),泛化成 list_new 闸门后必然出现第二份,两份一漂就是「冷却期到底几小时」谁也说不清。placeholder 单独成函数而不是给 mint 加 dry_run 开关:写库函数不该有「这次不写」的模式(写操作永不自动兜底的同源纪律),调用点显式二选一,读代码的人一眼看得出这条路会不会落库。占位码用 0 和 U 这两个被剔除的符号,是让「占位码混进生产」在形态上自证 —— 不必靠人记得。**审查采纳**:两位审查者指出 mint 签名在 0a / 2 / 3 三个包里互斥(0a 有 dry_run kwarg 且 workflow 是位置参,本包的调用只传四个位置参,批次 3 又一版),按字面实现必 TypeError;本条把签名定死并明确「若 0a 已交付另一版,本批负责改齐」,不留「谁改谁」的空白。占位码来源同理收敛到 sku_codec.placeholder 一处,registry 不留 SKU_DRYRUN_PLACEHOLDER。

**测试**:
- tests/test_sku_guard.py::test_placeholder_code_can_never_collide_with_a_minted_one(占位码长度 12、首位是来源字母、其余含至少一个不在字母表内的符号)
- tests/test_sku_guard.py::test_cooldown_and_generation_constants_have_one_home(全仓 grep:24 小时冷却与代际阈值的字面量只允许出现在 services/sku_codec.py;workflows/ 下只许出现常量名)
- tests/test_sku_guard.py::test_mint_refuses_an_unregistered_source_type(source_type="unknown" 抛 LookupError)
- tests/test_sku_guard.py::test_mint_has_no_dry_run_switch(inspect.signature(sku_codec.mint) 的参数名里不含 dry_run —— 双轨禁止的机械钉子)

**验收**:python -m pytest tests/test_sku_guard.py -q;grep -rn "cooldown_hours\", 24\|= 24  *#" workflows/ 无命中;python -c "import inspect;from services import sku_codec;print(inspect.signature(sku_codec.mint))"

#### B2-03 · `workflows/list_new.py` · 573-656(`def _prep_rows(` 在 :573);mint 块插在 :594 `llm_stats: dict = {}` 之后、:596 `def _one(conn, r: dict) -> tuple:` 之前(原稿写的 :591/:594 已漂 2-3 行,本次逐行复核过)

**改动**:在 _prep_rows 顶部、ThreadPoolExecutor 之前,加一段单事务顺序 mint:with db.pg_conn() as conn: 对 sorted(ready, key=lambda x: x["rownum"]) 逐行 r["_sku"] = sku_codec.mint(conn, r["store"], listing_sources.SOURCE_AMZ, r["asin"], workflow="list_new")。这段跑完随 with 退出 commit,之后才开 128 并发做 LLM 与 conform(worker 用的是 :620 `db.pg_conn(autocommit=True)` 的独立连接,与本段不共享)。同段末尾加一行日志计数:本轮 mint 返回值的去重后个数若小于行数,logger.info 报「本轮 N 行复用了同一个码(上架表同 (店,ASIN) 贴重了)」。函数 docstring(:575-589)补一段:预备期抽码挂 r["_sku"],_one_store 只回填 UPC 不抽码;抽码在 _prep_rows 是硬约束(见 why),不许挪进 _one_store、不许挪进 ThreadPoolExecutor 内。**不要给 r["_sku"] 任何 `or r["asin"]` 形式的兜底。**

**为什么**:三条硬理由。(1) 串行补试 store_retry.serial_second_pass 重跑的是 _one_store(定义在 :1681,补试调用点 :1820-1822),抽码若在里面,第二次会抽出新码 ⇒ 载荷不再一字不差 ⇒ api/feeds.payload_key 算出的指纹变了 ⇒ 在途防重不命中 ⇒ 首轮已发出去的那片被真的再发一次 = 双上架,而且全程不报错(tests/test_list_new.py:1099 的既有用例正是钉这条)。(2) 单事务顺序 mint 而不是在 128 个 worker 里各自 mint:worker 用的是 autocommit 连接(:618-620),128 路并发抢同一个 (store, source_type, source_key) 的部分唯一索引会制造大量唯一冲突重试;顺序做一遍是几百次单行 INSERT,相对 LLM 那段墙钟可以忽略。(3) 放在 LLM 之前满足「防重状态先落库再调接口」:码在任何一次外部调用之前就已经 commit,进程半路死掉重跑拿到的是同一个码。禁止 `or r["asin"]` 兜底:那正是「静默把 ASIN 当 SKU 发出去」的制造机,缺 _sku 就该 KeyError 炸在测试里。dry-run 安全性由位置保证:_prep_rows 的唯一调用点在 :1650,而 :1583 `if not execute:` 早在它之前就 return 了 —— 空跑根本走不到这段(这条已在本次复核中逐行确认)。**审查采纳**:一位审查者指出同一轮里同 (店,ASIN) 两行会拿到同一个码且「两个随机码相同不像两个 ASIN 相同那样扎眼」,故加那行去重计数日志。

**测试**:
- tests/test_list_new.py::test_mint_happens_in_prep_not_in_one_store(桩 sku_codec.mint 记调用次数;让 T2 首轮抛 socksio ProtocolError 走串行补试,断言 mint 调用次数 == 行数,即补试没有二次抽码)
- tests/test_list_new.py::test_second_pass_resubmits_a_byte_identical_payload(:1099 既有用例,追加断言 sent[0][0]["Orderable"]["sku"] == sent[1][0]["Orderable"]["sku"] 且该值不等于行的 asin)
- tests/test_list_new.py::test_code_is_committed_before_any_feed_call(桩里记录 mint 与 feeds.submit_feed 的调用先后,断言全部 mint 早于第一次 submit_feed)
- tests/test_list_new.py::test_duplicate_rows_reusing_one_code_are_counted_out_loud(同 (店,ASIN) 两行,断言摘要/日志里出现复用计数)

**验收**:python -m pytest tests/test_list_new.py -q;python cli.py list_new --dry-run -p store=<试点店> 摘要不出现 mint 相关写库日志

#### B2-04 · `workflows/list_new.py` · 602-605(`orderable = mp_mapper.build_orderable(` 在 :602,第一参 `r["asin"]` 在 :603)与 :608(`sku=r["asin"], variant=r.get("_vplan"))`)

**改动**::603 的第一参 r["asin"] 改为 r["_sku"];:608 的 sku=r["asin"] 改为 sku=r["_sku"]。:610-611 的 logger.info 与 :613 的 _dump_llm_debug 仍用 r["asin"](那是给人看的定位键,不是载荷)。

**为什么**:这两处是原点:mp_mapper.build_orderable(定义在 services/mp_mapper.py:649,`"sku": str(sku)` 在 :685)把第一参写进 Orderable.sku,那就是发给沃尔玛的 SKU;conform 的 sku= 会在 services/mp_conform.ensure_variant_bag(:612,`visible["variantGroupId"] = str(sku)[:300]` 在 :669)被当作单品占位 variantGroupId 写进 Visible。两处必须同改 —— 只改一处会出现「Orderable.sku 是新码、variantGroupId 还是 ASIN」的半身像,而 variantGroupId 是发给沃尔玛的,等于把 ASIN 从后门递出去。日志键保持 ASIN 是有意的:排障时人查的是 ASIN,不是随机码。⚠ 注意这只修好了**单品**口径;变体品的 variantGroupId 由 services/variant_group.group_id 从 parent ASIN 派生,本批不改,见决策 D 与 risks 第 1 条。

**测试**:
- tests/test_list_new.py::test_payload_sku_is_the_minted_code_not_the_asin(断言 assemble_mp_item 收到的 Orderable["sku"] == 登记簿里 mint 出来的那个码,且 != r["asin"])
- tests/test_list_new.py::test_single_item_variant_group_id_follows_the_code(spec 把变体三件套列必填时,Visible 的 variantGroupId 是新码而不是 ASIN)

**验收**:python -m pytest tests/test_list_new.py -k payload_sku -q

#### B2-05 · `workflows/list_new.py` · 1035-1066(`def _spec_precheck(` 在 :1035);改动点 :1052-1054(build_orderable,第一参 r["asin"] 在 :1053)与 :1057(sku=r["asin"])

**改动**:在 :1041 `lines = [...]` 之后、:1044 的循环之前取一次 ph = sku_codec.placeholder(listing_sources.SOURCE_AMZ);:1053 的第一参与 :1057 的 sku= 一起改成 ph。:1050 / :1060-1064 的逐行回显仍打 r["asin"];:1041 的抬头行文案改成 `  spec 预检(不领 UPC/不提交;sku 用占位码 {ph},真跑时由登记簿给真码):`。

**为什么**:_spec_precheck 只在 dry-run 分支被调到(:1583 `if not execute:` 之后、:1593),而 dry-run 绝不许写库 —— 用 placeholder 而不是 mint 就是这条纪律的落地。同时这是试点第 1 步的观测面:所有者要在 dry-run 里看见 sku 已经不是 ASIN 了,而且看得出它是占位的、不是真发的那个串。抬头行说破「占位码不是真码」,是因为占位码看起来也像一个码,不说破会让人以为真跑会发这个串。

**测试**:
- tests/test_list_new.py::test_spec_precheck_payload_uses_the_placeholder_code(check_spec=1 的 dry-run:conform 收到的 sku 是 placeholder 返回值,且 sku_codec.mint 零调用)
- tests/test_list_new.py::test_dry_run_uses_a_placeholder_code_and_writes_nothing(整条 dry-run:mint / listing_sources.register / upc_pool.claim 三个桩全部零调用)

**验收**:python cli.py list_new --dry-run -p check_spec=1 -p limit=1 -p store=<试点店>,输出里 spec 预检段可见占位码;pytest tests/test_list_new.py -k placeholder -q

#### B2-06 · `workflows/list_new.py` · 259(`upc_pool.mark_used(conn, [(u, r["asin"]) for r, u in batch])`,在 _apply_submit_result 的 submitted 分支内)

**改动**:改为 upc_pool.mark_used(conn, [(u, r["_sku"]) for r, u in batch])。

**为什么**:upc_pool.mark_used(services/upc_pool.py:210-219)写的是 upc_pool.sku 列 —— 那一列的名字就是 SKU(refdata/schema.sql:249 注释「已用时的沃尔玛 SKU」),此前存的是 ASIN。切换后必须存真 SKU:UPC 池表 E 列、pool_stats、以及运营从 UPC 反查「这个号发给了哪个 SKU」全靠它。upc_pool.asin 列(claim 时写入)继续存 ASIN 不动 —— 复用键 (store, asin) 是 claim 的契约(services/upc_pool.py:147-156 的 `DISTINCT ON (store, asin)` 复用查询),动它会让原号复用失效、每次重试白烧号。

**测试**:
- tests/test_list_new.py::test_mark_used_and_events_carry_the_code_not_the_asin(断言 mark_used 收到的 pair 第二元是新码;同用例覆盖 B2-08 的事件)
- tests/test_upc_pricing.py::test_claim_reuses_prior_upc_for_same_store_asin(既有用例,确认复用键仍是 (store, asin) 未被本批误改)

**验收**:pytest tests/test_list_new.py tests/test_upc_pricing.py -q;试点后 SQL:select upc, asin, sku from catalog.upc_pool where store='<试点店>' and used_at>=current_date —— sku 列是 12 位码、asin 列是 ASIN

#### B2-07 · `workflows/list_new.py` · 260-264(`listing_sources.register(conn, [...])` 五行,在 _apply_submit_result 的 submitted 分支内)

**改动**:删掉这五行。函数 docstring(:251-256)里「延后结算那条迟早漏掉 mark_used 或 listing_sources.register」改写为:登记已在预备期 mint 时完成(单一实现),这里只剩 mark_used 与事件。list_new 顶部对 listing_sources 的 import 保留(SOURCE_AMZ 仍要用)。

**为什么**:mint 内部已经在同一函数同一事务里 INSERT 了登记簿行(sku_codec 契约)。留着 register 就是同一能力两条实现路径(conventions §六「双轨禁止」),而且两条路的语义已经不同:mint 是「抽码即登记」,register 是「首次登记优先 ON CONFLICT (store, sku) DO NOTHING」(services/listing_sources.py:39)—— 后者对 mint 出来的行永远是空操作,留着只会让下一个读代码的人以为登记发生在提交之后,进而把 mint 挪到提交后去。

**测试**:
- tests/test_list_new.py::test_registration_happens_at_mint_not_after_submit(桩 listing_sources.register 记调用,断言真跑全程零调用;同时断言 mint 桩被调用了行数次)
- tests/test_sku_guard.py::test_listing_sources_register_callers_are_backfill_and_manual_only(全仓 AST:register 的调用点只允许出现在 workflows/sources_backfill.py、workflows/match_listing.py 的人工号分支、services/sku_codec.py;白名单带理由)

**验收**:pytest tests/test_list_new.py -k registration -q;grep -n "listing_sources.register" workflows/list_new.py 无命中

#### B2-08 · `workflows/list_new.py` · 265-269(`product_events.record_many(conn, [...])`,detail 现为 {"feed_id": ..., "price": ...} 在 :268)

**改动**::266 的 "sku": r["asin"] 改为 "sku": r["_sku"];:268 的 detail 追加第三键 "asin": r["asin"]。

**为什么**:product_events.record_many(services/product_events.py:167 `extract_asin(r["sku"])`)会自动填 asin 列 —— 不透明码提不出 ASIN,那一列会变 NULL,而消费方视图按 coalesce(asin, sku) 归并(catalog.product_risk、services/blacklist.py:209 的 _LATEST_CTE 等),NULL 会让同一产品跨店/跨代际不归并。批次 0b 的收口方案是让 record_many 经登记簿反查;在 list_new 这个写入点上,ASIN 本来就在手边,detail 里显式带一份是最省的补充证据,也让 _SQL_ATTEMPTS 的代际过滤不必依赖 asin 列(见 B2-23)。sku 字段本身必须是真发出去的码 —— 病历记的是「我们发了什么」,不是「我们心里想的是哪个产品」。

**测试**:
- tests/test_list_new.py::test_mark_used_and_events_carry_the_code_not_the_asin(事件 sku 是新码、detail["asin"] 是 ASIN)
- tests/test_product_events.py::test_record_many_leaves_asin_null_for_an_opaque_sku(不透明码进来时 asin 列为 NULL —— 这是批次 0b 反查方案要接的口,本批只钉住现象,批次 0b 落地后本用例翻转为「经登记簿补出 ASIN」)

**验收**:pytest tests/test_list_new.py -k events -q;试点后 SQL:select sku, asin, detail->>'asin' from catalog.product_events where event='list_submitted' and store='<试点店>' order by occurred_at desc limit 5

#### B2-09 · `workflows/list_new.py` · 270-285(_apply_submit_result 三个分支的 updates.append:submitted :270-275、failed :276-280、unknown :281-285)

**改动**:三个分支的回写值列表统一从 8 值扩到 9 值,第 9 值一律是 r["_sku"]:submitted 分支 [title, amz价, qty, walmart价, "Yes", feed_id, today, "", r["_sku"]];failed 分支 ["", "", "", "", "No", "", "", "提交被拒", r["_sku"]];unknown 分支 ["", "", "", "", "Unknown", "", today, "提交结局不确定,待对账", r["_sku"]]。

**为什么**:sku_plan 问 4 定稿:V 列在提交时与 K/L/M 同一次写回。三个分支都写而不只写 submitted,理由是 V 列的语义是「这一行当前持有的码」而不是「已成功上架的码」:failed/unknown 的行下一轮会用同一个码重来,运营要能从表上看到它(退役表 B 列手填、人工排障都要这个串);而且三分支同写才只有一条回写路径,不会出现「哪些结局写 V、哪些不写」这种要记的规则。

**测试**:
- tests/test_list_new.py::test_submit_writes_the_code_into_column_v(三种结局各一行,断言 write_submit_cols 收到的 vals 长度为 9 且末位是该行的码)

**验收**:pytest tests/test_list_new.py -k column_v -q

#### B2-10 · `services/listing_sheet.py` · 131-151(`def write_submit_cols(updates, execute=True) -> int:` 在 :131;docstring :132-136;dry-run 分支 :139-144;函数体 :145-151,其中 `title, rest = vals[0], vals[1:]` 在 :147、两个 ranges.append 在 :148-149)

**改动**:**核验优先、必要时补齐,但形态必须是「第 9 值可选」**:批次 1 的 B1-07 应已把函数体改成 title, rest = vals[0], vals[1:8] 并在 `if len(vals) > 8 and vals[8]:` 时追加第三个 ranges.append((f"V{r}:V{r}", [[vals[8]]]))。本批只做三件事:① 核验该写法在位且批次 1 的守门用例 test_write_submit_cols_eight_values_is_byte_identical 仍绿;② docstring 首行改为「输入:[(行号, [C, H..N 七值] 八值,或再带 V 一值共九值)] → 输出:写入行数」,并说明列不连续拆两到三个 range;③ dry-run 分支 :141 的日志文案改成打印 C+H:N(+V)。**若批次 1 未落地,本批补上,但一律用可选写法,禁止改成 `title, rest, sku = vals[0], vals[1:8], vals[8]` 这种必填形态。**

**为什么**:「提交时强制回写/覆盖 V」要落在提交那一次写里,不能另开一次飞书调用:飞书写通道有同表串行锁与批间节流(conventions §八),同一行拆两次调用等于把这一行的写入延迟翻倍,且中间挂掉会留下「K=Yes 但 V 空」的半截状态。**审查采纳(原稿此处会打红批次 1 的守门)**:原稿要求改成必填的三元解包,而批次 1 的 B1-07 明确把「八值调用逐字不变」立成了 guard test,必填形态一落地它当场 IndexError 变红,而批次 2 的 items 里没有任何一条说要删它 —— 那种红最容易被人顺手注释掉,于是批次 1 花整批建立的护栏当场作废。改成可选形态后两批共存:批次 1 的八值调用照旧,批次 2 的九值调用多写一段 V,一条路径两种入参长度,没有第二个函数。

**测试**:
- tests/test_list_new.py::test_submit_writes_the_code_into_column_v(断言写出的 ranges 里含 V{行号} 且值是码)
- tests/test_listing_sku_col.py::test_write_submit_cols_eight_values_is_byte_identical(批次 1 既有守门,本批必须仍绿 —— 八值调用一个字节不变)

**验收**:pytest tests/test_list_new.py tests/test_listing_sku_col.py -q;试点后人眼看上架表 V 列有 12 位码、K/L/M 同行有值

#### B2-11 · `workflows/list_new.py` · 常量区插在 :318(`_SQL_VERDICT` 的结束 `"""`)与 :321(`def load_verdicts(`)之间;_GateState 在 :345-355(字段 inactive…owned_brand);_load_gate_state 在 :358-389(最后一次 cur.execute 之后是 :374-387 的四个非 SQL 加载,return 在 :388-389)

**改动**:常量区新增两条 SQL。_SQL_RETIRE_COOLDOWN:select ls.store 不用,改写为 `select e.store, coalesce(ls.source_key, e.sku) as asin, max(e.occurred_at) from catalog.product_events e left join catalog.listing_sources ls on ls.store = e.store and ls.sku = e.sku **and ls.source_type = 'amz'** where e.event = %s and e.store is not null and e.occurred_at >= now() - make_interval(hours => %s) group by 1, 2`,第一个参数传 product_events.RETIRE_FEED_SUCCESS(见 B2-32,不写字面量)。_SQL_ABANDONED_GEN:`select store, source_key, count(*) from catalog.listing_sources where abandoned_at is not null and source_type = %s and source_key is not null group by 1, 2 having count(*) >= %s`。_GateState 末尾追加两个字段 cooling: dict(键 (店, ASIN) → 最近一次 retire_feed_success 时刻)与 over_gen: set(键 (店, ASIN),已弃码代数达上限);**字段必须追加在 :355 之后**,不许插中间。_load_gate_state 在 :387 之后补两次 cur.execute 并把结果并进 :388-389 的 return(位置参构造,新字段在末尾)。docstring :359「闸门链要的八份库侧快照」改成十份。

**为什么**:两道新闸的数据面要与既有八份快照同一次读完(_load_gate_state 是闸门链唯一的库侧取数点),否则逐行查库会在几百行的轮次里打出几百条 SQL。冷却按 (店, ASIN) 而不是 (店, SKU):退役发生在旧码上、重上用的可能是新码,按 SKU 键会在换码后立刻失效。用 left join 而不是 inner join:未登记的存量行也要能算出 ASIN(coalesce 回落 e.sku,存量 sku=asin 时结果相同)。代际计数按 (store, source_key) 而不是 (store, sku):一个产品换过几次码,正是要数的东西。字段追加在末尾是因为 _GateState 是按位置构造的 NamedTuple(:388-389),中间插字段会让后面全部错位,而错位不报错(集合与字典长得都一样,:346-347 原注释已写过这条教训)。**审查采纳两条**:① 原稿的 LEFT JOIN 漏了 `ls.source_type = 'amz'`,而跟卖行的 source_key 是 GTIN,冷却键会按 GTIN 建、与闸判用的 (store_name, r["asin"]) 永远对不上 ⇒ 跟卖品的退役冷却恒不生效且不报错;与 0a 全部收口处的身份表达式逐字对齐后修好。② 原稿把事件码 'retire_feed_success' 写成 SQL 里的字面量,违反 services/product_events.py:94 自述的「事件码常量唯一出处」纪律 —— 改为引用 B2-32 新增的具名常量。

**测试**:
- tests/test_list_new.py::_wire_execute_env(:946-948 的 ln._GateState(...) 八个位置参数补成十个,新增 {} 与 set();另有九处直接构造 _GateState 的用例同改:tests/test_list_new.py:86、239、271、315、350、382、410、443、491、618、645、864、1321)
- tests/test_list_new.py::test_gate_state_fields_are_appended_not_inserted(断言 _GateState._fields 的前八个名字与顺序未变)
- tests/test_list_new.py::test_cooldown_sql_scopes_the_registry_join_to_amz(SQL 文本断言含 ls.source_type = 'amz')

**验收**:pytest tests/test_list_new.py -q(全部既有用例必须先绿:_GateState 变长会打断所有真跑用例,这是本条的第一验收信号)

#### B2-12 · `workflows/list_new.py` · 插在 :1232(去重闸的 `continue`)之后、:1233(`holder = None if unplanned else st.owned_asin.get(r["asin"])`)之前;计数字典 n 在 :1481-1484;blocked 标签元组在 :1553-1559

**改动**:插入两道闸,顺序固定为:先代际上限、后退役冷却。代际上限:`if (store_name, r["asin"]) in st.over_gen:` → counts["gen_cap"] += 1;reasons.append((r["rownum"], "换码次数达上限,待人工"));continue。退役冷却:`if st.cooling.get((store_name, r["asin"])):` → counts["cooldown"] += 1;reasons.append((r["rownum"], "退役冷却中"));continue。两条 continue 之上各加三行注释说明判据与出处常量(sku_codec.MAX_SKU_GENERATIONS / sku_codec.RETIRE_COOLDOWN_HOURS)。:1481-1484 的 n 初始化字典补 "gen_cap": 0 与 "cooldown": 0;:1553-1559 的 blocked 标签元组在 ("dedup", "本店已在架") 之后补 ("gen_cap", "换码达上限") 与 ("cooldown", "退役冷却中")。

**为什么**:位置:必须在去重闸(:1227-1232)之后 —— 已在架的行根本不该走到这两道闸(它压根不是「再上架」);必须在占用闸(:1233)/黑名单闸(:1240)之前 —— 那两道是「这个产品该不该由这家店上」,而这两道是「这个 (店, 产品) 现在能不能上」,后者是更硬的时序事实,先判它可以少读两张台账。两道之间的顺序:代际上限在前,因为它是需要人介入的终局判断,冷却只是等一等;一行同时命中两者时,N 列该显示要人做的那条。写 N 不写终态与既有闸门同语义(:1204-1206 的注释已定这条:审核翻案/冷却期满下一轮自动续上)。摘要标签走既有 blocked 机制,恰好 0 时自动不打印(:1552「抑制的判据是恰好 0,不是看着不重要:1 也要报」)。理由文案取 synthesis 定稿原文,不许自创措辞 —— N 列文案是运营的判据面。

**测试**:
- tests/test_list_new.py::test_generation_cap_stops_the_code_churn_loop(over_gen 命中的行不进 candidates、N 列写「换码次数达上限,待人工」、摘要闸门行出现「换码达上限 1」)
- tests/test_list_new.py::test_retire_cooldown_gate_holds_the_row_and_names_it(cooling 命中的行写「退役冷却中」;把 occurred_at 拉到 25 小时前后再跑,该行放行)
- tests/test_list_new.py::test_new_gates_sit_between_dedup_and_claims(一行同时满足在架、代际超限、冷却三者,断言 N 列写的是「本店已在架」——顺序即语义)

**验收**:pytest tests/test_list_new.py -k "gate or cooldown or cap" -q;python cli.py list_new --dry-run 摘要闸门行在有命中时出现两个新标签

#### B2-13 · `workflows/sku_locked_heal.py` · _relist 在 :158-221;burn_pairs 声明 :170;append :197;烧号块 :208-212;import :34-35;cooldown_hours 默认值 :227;文件头三步链 :12-15

**改动**::170 的 burn_pairs 更名 abandon_pairs,注释改为「RETIRE 成功 + 冷却期满即弃码(码与 UPC 同寿命,唯一实现 sku_codec.abandon)」;:197 追加的仍是 (store_name, sku);:208-212 的 upc_pool.burn_for_retire 调用整块替换为 `with db.pg_conn() as conn: n_ab = sum(sku_codec.abandon(conn, s, k, sku_codec.REASON_SKU_LOCKED) for s, k in abandon_pairs)`,摘要行改为 f"  弃码 {n_ab} 个(登记簿标 abandoned_at,旧 UPC 烧号 burned_lock,重上必新码新号)"。:34-35 的 import 去掉 upc_pool、加上 sku_codec(全文再无 upc_pool 其它引用,已 grep 确认)。:227 的 `int(params.get("cooldown_hours", 24))` 改为 `int(params.get("cooldown_hours", sku_codec.RETIRE_COOLDOWN_HOURS))`。文件头 :12-15 的三步链描述补一句:第 ③ 步除清列外还弃码,下一轮 list_new 领到的是新码 + 新 UPC。

**为什么**:弃码点 2(四点中唯一绑回执的一个:锁死的 SKU 可能从未进过 walmart_items,没有观测可等)。必须换成 abandon 而不是继续裸 burn 的关键理由:burn_for_retire 的 SQL(services/upc_pool.py:198-203)是按 `store = t.s AND asin = t.a` 匹配的,而这里传进去的第二元是 retire_cooldown.sku —— 切换后它是不透明码,匹配恒空,烧号会静默失效,于是下一轮 claim(:147-156 按 (store, asin) 复用)把旧号原样复用回来给新码,必撞 0101119,而且看起来像运气差。abandon 内部经登记簿把码翻回 ASIN 再烧,这一跳是本批必须的。冷却常量改读 sku_codec 是为了与 list_new 新闸同一个数(两处各写 24 就是两份真相)。dry-run 安全:_relist 的 `if not execute:` 早退在 :164-168,本改动全在其后。

**测试**:
- tests/test_sku_locked_heal.py::test_ripe_success_abandons_the_code_and_burns_the_upc(改写 :134 既有 test_ripe_success_clears_row_failed_marks:回执 success + 冷却期满 ⇒ abandon 被调一次、reason=sku_locked、行被清列)
- tests/test_sku_locked_heal.py::test_failed_receipt_never_abandons(回执 failed ⇒ 冷却标 failed、abandon 零调用、不清列)
- tests/test_sku_locked_heal.py::test_retire_payload_carries_the_code_not_the_asin(批次 1 交付的 r["sku"] or r["asin"] 读法在本批必须真的生效:submit_feed 收到的 RETIRE_ITEM entries 是 V 列的码;V 空的存量行回落 ASIN)
- tests/test_sku_locked_heal.py::test_cooldown_hours_comes_from_the_codec_constant(不传 cooldown_hours 时用的是 sku_codec.RETIRE_COOLDOWN_HOURS)

**验收**:pytest tests/test_sku_locked_heal.py -q;python cli.py sku_locked_heal --dry-run(打印将退役/将核验条数,不动台账);grep -n upc_pool workflows/sku_locked_heal.py 无命中

#### B2-14 · `services/product_events.py` · 238-267(`def verify_deletions(conn, grace_hours: int = 48) -> tuple[int, int]:` 在 :238;docstring :239-246;返回 :267)

**改动**:返回值从 tuple[int, int] 改为 tuple[int, int, list[tuple[str, str]]],第三元是本次判定为 gone 的 (store, sku) 列表(与写进 events 的 DELETE_VERIFIED 行一一对应)。docstring 首行改为「输入:连接 + 宽限小时数 → 输出:(核验生效数, 未生效数, 生效的 (店, SKU) 列表)」,并加一句:第三元交给调用方(workflows/catalog_sync)去弃码 —— 本模块不 import sku_codec,避免账本模块与码模块互相依赖。

**为什么**:弃码点 1 需要「哪些 (店, SKU) 刚被观测核验删掉」这份名单。不把 abandon 直接写在本函数里的硬理由:sku_codec 要写 product_events(sku_abandoned 事件),product_events 再 import sku_codec 就是循环 import;而且账本模块的职责是记录事实,弃码是决策,决策归 workflow 层组合(铁律 1 的方向:workflows → services)。改签名而不是让 catalog_sync 另写一条同样的 SQL 去捞名单 —— 那是第二份判据,迟早与这份漂开。

**测试**:
- tests/test_product_events.py::test_verify_deletions_returns_the_verified_pairs(:81 既有 test_verify_deletions_verdicts 扩写::87 的 `gone, still = pe.verify_deletions(conn)` 改成三元解包,第三元 == [("T1", "S_GONE")],still 与 wait 都不在里面)

**验收**:pytest tests/test_product_events.py -q

#### B2-15 · `workflows/catalog_sync.py` · 227-234(`if results:` 在 :227;`verified, not_eff = product_events.verify_deletions(conn)` 在 :230;摘要行 :231-234)

**改动**::230 改为 `verified, not_eff, gone_pairs = product_events.verify_deletions(conn)`;紧接其后在同一个 `with db.pg_conn() as conn:` 块内加 `n_ab = sum(sku_codec.abandon(conn, s, k, sku_codec.REASON_DELETE_VERIFIED) for s, k in gone_pairs)`。:232-234 的摘要行追加 f",弃码 {n_ab}"(n_ab 为 0 时不打印,与既有 not_eff 同款条件写法)。顶部 import 增加 sku_codec。同段加三行注释:弃码只在 delete_verified 落地,不在 delete_feed_success 回执 —— 回执成功但后台没删是所有者实证过的故障模式(delete_not_effective,services/product_events.py:243-244),按回执弃码会让下一轮新码新 UPC 去上一个还活着的 item = 同店重复 listing,沃尔玛不会替你拦。

**为什么**:弃码点 1。放在 verify_deletions 之后同一事务里:弃码与 delete_verified 事件必须同生共死,分两个事务会出现「事件记了、码没弃」的半截状态,而 verify_deletions 的 open_ok CTE 靠 delete_verified 事件封口(services/product_events.py:220-226),下一轮不会再产出这一对,于是那个码永远弃不掉了。摘要要报数:弃码是不可逆的单向推进(登记簿没有撤销弃码的路径),每天弃了几个必须在通知里可见。

**测试**:
- tests/test_catalog_sync.py::test_delete_verified_abandons_the_code(桩 verify_deletions 返回 (1, 0, [("T1", "N7QM2X9RT4W3")]),断言 sku_codec.abandon 被调一次、reason=delete_verified、摘要含「弃码 1」)
- tests/test_catalog_sync.py::test_delete_not_effective_never_abandons(返回 (0, 1, []),abandon 零调用)
- tests/test_catalog_sync.py 既有四处 verify_deletions 桩(:484、:702、:726、:810)的 `lambda conn: (0, 0)` 全部改为 `lambda conn: (0, 0, [])`

**验收**:pytest tests/test_catalog_sync.py -q;python cli.py catalog_sync -p store=<试点店> 摘要「删除核验」行格式正确

#### B2-16 · `services/listing_sheet.py` · _mark_upc_conflicts 在 :336-361(SQL :351-353,循环 :355-357,missing 计数 :358-360);sync_from_ledger 的 conflicts 声明 :535、append :555、调用 :557

**改动**:决策 B 默认(码与 UPC 一起换):① 签名改为 `_mark_upc_conflicts(pairs: list[tuple[str, str]], execute: bool = True) -> int`,pairs = (店, 该行当前 SKU);② :555 改为 `conflicts.append((r["store"], row_sku(r)))` —— **必须调批次 1 交付的 row_sku(r),不许在本文件里手写 `r["sku"] or r["asin"]`**;③ 函数内先经登记簿把 SKU 翻成 ASIN,再按 (store, asin) 查池(批次 1 的 B1-09 已把池反查改成这个键,本批只跟着传对入参);④ 烧号与弃码合成**一次** sku_codec.abandon(conn, store, sku, sku_codec.REASON_UPC_CONFLICT) 调用 —— abandon 内部会烧该 (店, ASIN) 名下的号,本函数不再自己调 upc_pool.mark_conflict,只保留「池里找不到对应 UPC」的告警统计;⑤ execute=False 时只 logger.info 打印将弃码的 pair,一行不写(由 B2-29 从 feed_poll 传下来);⑥ :557 的调用改为 `_mark_upc_conflicts(conflicts, execute)`。docstring 改写:0101119 = 该 UPC 已被占用;所有者定稿 2026-09-02(决策 B):码与 UPC 一起换,拆掉「撞库 → 同 SKU 换 UPC → 0101211 → 自愈链」的死循环;保留 :342-344 那条 2026-08-09 澄清(撞库只说明号被占,与我们是否已上架无关,不得据此推断该走跟卖)。若所有者选决策 B 的替代分支(不换码),则本条只做 ①②③⑤⑥ 与类型修正,不加 abandon 调用,并在 docstring 写明为什么不换。

**为什么**:弃码点 3。:535 的类型标注(`list[tuple[str, str]]`)与 :555 的实际值(裸字符串 `r["asin"]`)早就对不上,本批顺手收口。换码的理由在 synthesis:唯一的反向证据是 services/upc_pool.py:132-135 记的 2026-08-19 实证「O=FAILED 换新号重发同一 SKU 必败」,而那次 FAILED 是不是 0101119 没记录;换码最坏只是多耗一个免费的码(码空间 30^11),不换则有重演 SKU_LOCKED 死循环的风险 —— 代价不对称,所以默认换。**审查采纳两条**:① 原稿写 `conflicts.append((r["store"], r["sku"] or r["asin"]))`,这是把批次 1 刚收口成 row_sku() 的回落表达式在同一个文件里手写第二遍,会直接打红批次 1 的守门 test_nobody_recomputes_the_row_sku —— 改调 row_sku(r),守门一个字不改。② 原稿没有 execute 形参,而本函数的宿主链 feed_poll 今天完全不认 --dry-run(见 B2-29),把不可逆的 abandon 放进去等于 `cli.py feed_poll --dry-run` 会真弃码真烧号 —— 这是审查里定级最高的一条,必须在同一批修掉。

**测试**:
- tests/test_list_new.py::test_upc_conflict_marked_orthogonally(:153 既有用例:桩 `lambda a: ...` 改成收 (store, sku) 对 + execute 关键字)
- tests/test_list_new.py::test_upc_conflict_swaps_both_the_code_and_the_number(0101119 回执 ⇒ abandon 被调、reason=upc_conflict;下一轮 mint 给出新码、claim 给出新号)
- tests/test_listing_sku_col.py::test_nobody_recomputes_the_row_sku(批次 1 既有守门,本批必须仍绿)
- tests/test_sku_guard.py::test_upc_conflict_branch_matches_the_owner_decision(决策 B 若改为不换,本用例反向断言 abandon 零调用 —— 两种选择各有一条钉子,改判据必须同时改这条测试)

**验收**:pytest tests/test_list_new.py -k upc_conflict -q;pytest tests/test_listing_sku_col.py -q

#### B2-29 · `workflows/feed_poll.py(主)+ services/listing_sheet.py / clear_sheet.py / maint_sheet.py / match_sheet.py(各加一个关键字参数)` · feed_poll.py:38 `DANGEROUS = False`;:133-161 run();:164-181 _one_chain;:184-199 _run_reflectors;五个反哺器入口:services/listing_sheet.py:516 sync_from_ledger、:394 heal_unknown、services/clear_sheet.py:75、services/maint_sheet.py:284、services/match_sheet.py:86

**改动**:把 execute 贯通反哺链,形态照抄仓内既有写法(workflows/sources_backfill.py:61、workflows/store_watch.py:144):① feed_poll.run() 在 :138 之前加 `execute = bool(params.get("execute")) and not params.get("dry_run")`,并把 :160 改成 `lines.extend(_run_reflectors(execute))`;② _run_reflectors(execute: bool) 透传给 _one_chain(chain, execute);③ _one_chain 的 :174 `line = sync()` 改为 `line = sync(execute=execute)`;④ 五个反哺器一律加 `execute: bool = True` 关键字参数,并在各自的终态写入处早退/跳过:listing_sheet.sync_from_ledger(:557 传给 _mark_upc_conflicts、:561 的 sheet_write_ranges)、listing_sheet.heal_unknown(:495-500 的 mark_used/release 与 :504 的 sheet_write_ranges)、clear_sheet(:110 writeback)、maint_sheet、match_sheet(各自的单次回写);execute=False 时打印「[DRY-RUN] 将回写 N 行 / 将弃码 N 个」并返回摘要,不发任何写请求。⑤ DANGEROUS 保持 False(feed_poll 不调沃尔玛写接口,cli 因此仍恒传 execute=True,--dry-run 走的是 params["dry_run"] 那条透传 —— 与 sources_backfill 等一批扫描类工作流同一形态),但在 :38 上方加三行注释说清「本工作流的 --dry-run 靠自己认 params['dry_run'],不靠 DANGEROUS;反哺器里有不可逆的 PG 写(弃码 + 烧号),漏认这一句 --dry-run 就完全失效」。⑥ feed_poll 文件头 :15-17「只读沃尔玛 + 记账,非危险」补一句:反哺器会写 PG(UPC 池状态、登记簿弃码),空跑必须用 --dry-run。

**为什么**:**审查采纳的最高优先级修复**(原稿完全没有这一条)。核实链路:workflows/feed_poll.py:38 `DANGEROUS = False` ⇒ cli.py:307 `params["execute"] = (not dry_run) if dangerous else True` 恒为 True;cli.py:311 另行透传 params["dry_run"],但 feed_poll.run(:133-161)根本不读它;_one_chain(:174)调 `sync()` 无参;services/listing_sheet.sync_from_ledger(:516)也没有 execute 形参。于是 `python cli.py feed_poll --dry-run` 今天就已经会真的调 upc_pool.mark_conflict 永久烧号,而本批还要往这条路上加**不可逆的 abandon**(弃码没有撤销路径)。CLAUDE.md 安全红线「缺省即真跑;空跑用 --dry-run;AI 改完代码先 dry-run、人眼确认后才跑真的」在这条链上是失效的,而 feed_poll 挂在 product_chain 里每轮自动跑。五个反哺器一起加关键字参数而不是只改上架那两个:_one_chain 用统一调用形态最省(否则要靠 inspect 判断谁认 execute,那是新的隐式约定);另外三个只写飞书,加一句 guard 是一行的事,顺带把它们的空跑也变成真的空跑。

**测试**:
- tests/test_list_new.py::test_feed_poll_dry_run_writes_nothing_anywhere(params={"execute": True, "dry_run": True} 跑 feed_poll.run:sku_codec.abandon / upc_pool.mark_conflict / upc_pool.mark_used / upc_pool.release / feishu.sheet_write_ranges 五个桩全部零调用,摘要里出现 [DRY-RUN] 字样)
- tests/test_list_new.py::test_feed_poll_real_run_still_writes(同样入参但不带 dry_run,断言写调用照常发生 —— 反向钉住「别把闸装成常闭」)
- tests/test_feishu_channels.py 或 tests/test_list_new.py::test_every_reflector_takes_an_execute_flag(inspect:_REFLECTOR_CHAINS 里登记的五个可调用对象签名都含 execute 关键字 —— 新增反哺器忘了加会被点名)

**验收**:python cli.py feed_poll --dry-run 输出全部带 [DRY-RUN]、PG 与飞书零写(试点前先跑一次,人眼确认);pytest tests/test_list_new.py -k feed_poll -q

#### B2-30 · `workflows/list_new.py` · run() 的 params 读取区 :1407-1414(`execute` 在 :1407、`gap_wait` :1411、`prep_workers` :1412、`jitter_ms` :1414);截断点在 :1518 `rg = _gate_by_row(...)` 之后、:1545 `_plan_variants(ready, n_var)` 之前(ready 组装完之处)

**改动**:run() 的参数区新增 `limit = int(params.get("limit", 0)) or None`(形态照抄 workflows/alloc_push.py:59 的既有写法);在 ready 组装完之后、_plan_variants 之前截断:`if limit and len(ready) > limit:` → 记 n_held = len(ready) - limit;ready = ready[:limit];lines.append(f"  ⚠ 人工上限 -p limit={limit}:本轮只做前 {limit} 行,其余 {n_held} 行留到下一轮(试点闸,不写 N 理由不写终态)")。截断**必须在全部闸门与数据过滤之后**,与 :1195-1196「配额不在这里切:先过全部闸门与数据过滤,幸存者再按店切片」同一条纪律。run() 的 docstring :1406 改为「输入:params(execute/store/check_spec/limit)→ 输出:闸门链与提交摘要」;文件头用法区补一行 `python cli.py list_new -p limit=1 -p store=X   # 试点:本轮只上 1 个品`。

**为什么**:**审查采纳(原稿把止损闸写在了验收命令的括号里)**。原稿试点第 2 步写的是「上架表只留一行待上,真跑 1 个品(list_new 无 limit 参数,手工只留一行或先加 -p limit=)」—— 本次逐行复核 run()(:1405-1420)确认 params 全集只有 execute/gap_wait/workers/submit_jitter_ms/store/check_spec,**`-p limit=` 是一个 items 里根本不存在的功能**。而 list_new 缺省即真跑、一跑就是全店 ready 行全部 mint + 提交 MP_ITEM,码已发、UPC 已 used,不可逆;唯一的替代方案是让人去生产上架表删行,那本身就是高危动作。同一份改造里批次 3 已经用 _stage_cap 把「1→10→整店」写成了代码闸,理由写的正是「纪律没有默认值替你挡」—— 批次 2 是唯一有行为变化的批次,更该有。缺省 None = 与今天逐字一致,不构成行为变化;截断放在闸门之后是因为被淘汰行不该占名额(否则 -p limit=1 可能一行都上不了,人会以为功能坏了)。

**测试**:
- tests/test_list_new.py::test_limit_truncates_after_the_gates_not_before(两行候选、一行被去重闸拦:-p limit=1 时提交的是幸存的那一行,而不是「名额被拦掉的那行占了」)
- tests/test_list_new.py::test_limit_absent_means_no_truncation(不传 limit 时行为与今天逐字一致,提交行数不变)
- tests/test_list_new.py::test_limit_says_how_many_it_left_behind(摘要里出现留到下一轮的条数)

**验收**:python cli.py list_new --dry-run -p store=<试点店> -p limit=1 摘要出现「共 1 行将进入 领UPC→LLM→提交」;pytest tests/test_list_new.py -k limit -q

#### B2-17 · `services/sku_codec.py` · abandon 的 docstring 与 reason 分派表(批次 0a 交付的函数体内)

**改动**:REASON_SKU_UPDATE 在本批只留接口不接调用点:abandon 收到该 reason 时走「只标 abandoned_at + replaced_by,不烧 UPC」分支(批次 0a 已实现),本批只补 docstring 一句「唯一调用方是批次 3 的 workflows/sku_migrate.py;本批全仓零调用,tests/test_sku_guard.py 钉住」,并在守门白名单里为它留一行「批次 3 启用前必须为空」的登记。

**为什么**:改码(SkuUpdate)是批次 3,但 reason 常量与不烧号分支现在就必须存在且被测试覆盖 —— 否则批次 3 会另开一条弃码实现(双轨)。同时守门要能区分「接口留着」与「有人偷偷用了」:白名单里显式写零调用,批次 3 改这一行时会被 code review 看见。

**测试**:
- tests/test_sku_guard.py::test_sku_update_reason_has_no_caller_yet(全仓 AST:REASON_SKU_UPDATE 除定义处外零引用)
- tests/test_sku_guard.py::test_sku_update_never_burns_a_upc(直接调 abandon(reason=sku_update),断言 upc_pool 烧号函数零调用)

**验收**:pytest tests/test_sku_guard.py -q

#### B2-18 · `workflows/match_listing.py` · SKU 序号与两道闸的加载 :133-139(date_str :135、`serial = match_feed.next_serial_start(conn, date_str)` :137);主循环 :149-181(_precheck 调用 :155、`r["gtin"] = spec["product_id"] or ""` :168、自动号 :169-171、build_match_item :172-176)

**改动**:**分两趟,mint 事务不许包住 _precheck 的沃尔玛调用**:① 删掉 :135 的 date_str 计算与 :137 的 serial 行(:136 的 `with db.pg_conn() as conn:` 与 :138-139 的 gate/banned 加载保留);② 主循环 :149-181 保持现状不开事务,只做 _precheck / _gate_reason / `r["gtin"] = ...`,把过闸的行攒进一个 ok 列表(:169-171 的自动号那三行整块删掉,build_match_item 那一段也从循环里挪到第二趟);③ 循环结束后开一个**短事务** `with db.pg_conn() as conn:`,对 ok 逐行:若 r["sku"] 为空 → `r["sku"] = sku_codec.mint(conn, r["store"], listing_sources.SOURCE_MATCH, r["gtin"] or r["upc"], workflow="match_listing")`;若 B 列已有人工号 → 保持不动并在同一处调 listing_sources.register(conn, [{store, sku, source_type: SOURCE_MATCH, source_key: r["gtin"] or r["upc"], workflow: "match_listing"}]) 把人工号登记进簿;然后在同一趟里调 build_match_item 组载荷、组装 by_store(build_match_item 的 TypeError/ValueError 分支照原样保留)。该 with 退出即 commit,仍在 :221 的 feeds.submit_feed 之前;④ dry-run(execute 为假)时整个第三趟用 sku_codec.placeholder(SOURCE_MATCH) 代替 mint、且不调 register、不开事务。

**为什么**:跟卖侧的 make_sku 是日期 + 4 位序号(services/match_feed.py:56-58),把上架日期写在 SKU 里(与货源隐匿目标直接冲突),而且每轮重发会取到新序号 ⇒ 载荷漂 ⇒ payload_key 防重失效。换成 mint 后,失败行下一轮拿到的是同一个码(活行复用),这正是跟卖侧现在缺的那条护栏。人工号必须在同一处 register 而不是等提交成功后:登记是「这个串归谁」的事实,与提交成不成功无关;提交成功后才登记会让被拒的人工号成为维护链眼里的孤儿(只有 sources_backfill 才捞得回来)。**审查采纳**:原稿要求「把 :149 的 for r in todo 循环整体包进 with db.pg_conn()」,但本次复核确认循环体 :155 是 `pre = _precheck(store, r["upc"], spec_cache)` —— 那是逐行的沃尔玛 SPEC 接口调用(每店固定出口代理、有速率桶与退避)。几百行就是几百次网络往返吊在一个 PG 事务上,每次 mint 在 catalog.listing_sources 上留的行锁要到循环结束才释放,与 list_new 的 mint 并发时互相等锁,而且是 PG 上典型的长事务坏味道。分两趟后事务只覆盖纯数据库操作,「先落库再调接口」仍然成立(commit 早于 submit_feed)。

**测试**:
- tests/test_match_listing.py::test_auto_sku_comes_from_mint_and_is_registered_before_submit(B 列空的行:sku 是 mint 返回的码;断言 mint 调用早于 submit_feed)
- tests/test_match_listing.py::test_mint_transaction_does_not_span_precheck_calls(桩记录 pg_conn 进出与 items_api.search_walmart_spec 的调用序,断言全部 precheck 都不在 mint 那个 with 块内)
- tests/test_match_listing.py::test_manual_sku_takes_priority(:191 既有用例扩写:B 列有人工号时 mint 零调用,且 register 收到该人工号 + source_key=GTIN)
- tests/test_match_listing.py::test_failed_match_row_reuses_the_same_code_next_round(同一 (店, GTIN) 连跑两轮,mint 桩返回活行 ⇒ 两轮载荷的 sku 相同)
- tests/test_match_listing.py::test_dry_run_prechecks_but_submits_nothing(:125 既有用例追加:dry-run 时 mint 与 register 均零调用,sku 是占位码)

**验收**:pytest tests/test_match_listing.py -q;python cli.py match_listing --dry-run 首条 Item 载荷里的 sku 是占位码

#### B2-19 · `workflows/match_listing.py` · 241-248(注释 :241-242「来源登记簿(所有者定稿):跟卖品 sku≠asin…」+ listing_sources.register 调用 :243-248,在 _one_store 的 submitted 分支内)

**改动**:删掉这八行(注释与调用一并),登记已在 B2-18 的第三趟里完成。:232-240 的 product_events.record_many 保留不动(它记的是提交事实,本来就该在提交成功后)。删后 :232 的 `with db.pg_conn() as conn:` 块里只剩事件写入,保持原结构不动。

**为什么**:与 B2-07 同一条纪律:登记只有一个时点、一条实现路径。此处还有一个具体的坏处:提交成功才登记意味着「跟卖被拒的行没有出身」,而 maintenance 的三个 amz provider 靠 source_type 路由 —— 没出身的行会落进 unknown,unknown 不参与任何自动破坏动作(services/listing_sources.py:8-11 的路由铁律),看起来安全,实际是这批货永久退出自动化。

**测试**:
- tests/test_match_listing.py::test_listing_sources_register_first_wins(:161 既有用例是纯 services 层单测,不动;另在 test_execute_routes_and_terminal_states(:133,其 :156-158 断言 calls["sources"])把断言改为覆盖新时点:register 在提交前被调、提交后零调用)

**验收**:pytest tests/test_match_listing.py -q;grep -n "listing_sources.register" workflows/match_listing.py 只剩 B2-18 那一处

#### B2-20 · `services/match_feed.py` · 18(`SKU_PREFIX = "PHUMWMT"`)、39-53(`def next_serial_start(`)、56-58(`def make_sku(`);另模块 docstring :7-10 描述 SKU 规则

**改动**:删除这三处,并把模块 docstring :7-10 那段「SKU 规则…沿用其编号格式 PHUMWMT + 提交日期 + 当日 4 位序号」改写为:SKU 由 services/sku_codec.mint 抽不透明码(B 列人工优先不变),本模块只负责 Item 构造,不再生成 SKU。删前的活性证据(conventions §五要求的两做):AST + 文本双 grep 全仓命中仅 workflows/match_listing.py:137/170(本批已改)与 tests/test_match_listing.py:46-47/68-69/88-89;三者都不写任何表(next_serial_start 只 SELECT ops.feed_items),不存在「它写的表还有别的补给线」这条活性;docs 里只有 docs/sku_plan.md 提到,属计划文本。同步删除 tests/test_match_listing.py:45-69 的 test_sku_autogen_format_and_serial_resume 整个用例(含其内嵌 :49-66 的 _Conn 类)与 :88-89 的 next_serial_start 桩。

**为什么**:conventions §六「每个能力只有一条实现路径」:跟卖 SKU 的生成从此只有 mint 一条路,留着旧生成器就是留一条随时会被误用的第二路(它还带日期,恰好是本改造要消灭的信息)。conventions §五要求判死前给证据,上面给了 AST + grep + 「它写哪张表」三条,不是靠 grep 不到调用者。旧 PHUMWMT 存量行不受影响:它们只在飞书表 B 列与 ops.feed_items 历史里,读路径全格式通吃。

**测试**:
- tests/test_match_listing.py 删除 test_sku_autogen_format_and_serial_resume(:45-69)与 :88-89 的桩
- tests/test_sku_guard.py::test_no_second_sku_generator_survives(全仓 grep:PHUMWMT 字面量与 make_sku / next_serial_start 名字在 services/ 与 workflows/ 下零命中)

**验收**:pytest tests/test_match_listing.py -q;grep -rn "PHUMWMT\|make_sku\|next_serial_start" --include=*.py services/ workflows/ 无命中

#### B2-21 · `tests/test_sku_guard.py` · **不新建文件**:该文件由批次 0a 建(全项目守门唯一之家,形态仿 tests/test_feishu_guard.py:33 的 ROOT、:42-52 的白名单节、:348-371 的「白名单不许烂掉」用例)。本批只往里增删白名单条目并补四组断言。若 0a 落地时用了别的文件名(如 test_sku_identity_guard.py),本批负责把它重命名/合并成 tests/test_sku_guard.py 并把 0b 的守门断言一并并进来。

**改动**:在既有的白名单 dict 结构里增删条目 + 补四组断言:(1) _ABANDON_CALLERS_OK 登记四个弃码点 = {services/sku_codec.py: 定义与自调, workflows/catalog_sync.py: 弃码点 1 delete_verified, workflows/sku_locked_heal.py: 弃码点 2 RETIRE 成功+冷却, services/listing_sheet.py: 弃码点 3 0101119(决策 B)};批次 3 的 workflows/sku_migrate.py 预留一行注释但当前不登记。(2) _ABANDON_FORBIDDEN 反向名单:workflows/product_clear.py / workflows/problem_product_cleanup.py / workflows/maintenance.py / services/walmart_catalog.py(mark_missing 所在)/ services/feed_track.py 五个文件里 abandon 零出现,每条写明该文件为什么绝不许弃码。(3) _ABANDONED_FILTER_OK:`abandoned_at IS NULL` 这段 SQL 文本在 **.py 里**只允许出现在 services/sku_codec.py(mint 的活行复用查询)、workflows/list_new.py(去重闸)、workflows/alloc_push.py(_SQL_ONLINE)三处;**refdata/schema.sql 的部分索引条件不计入扫描面**(它是 DDL 不是消费方过滤),tests/ 亦不计入;白名单值里写死收口批次,批次 3 起加第四条 workflows/sku_migrate.py。(4) _UPDATE_ABANDONED_OK:`UPDATE catalog.listing_sources` + abandoned_at 的 SQL 文本只允许在 services/sku_codec.py;同批把 **INSERT INTO catalog.listing_sources** 也纳入扫描,白名单只放 services/listing_sources.py(register)与 services/sku_codec.py(mint)。每条白名单配「白名单不许烂掉」用例(登记的文件/函数不存在即红)。文件 docstring 补上本批四道守门各自守的事故形态。

**为什么**:四个弃码点是本改造最危险的判据:多一个点 = 沃尔玛侧还活着的记录被我们当成死的,下一轮新码新 UPC 去上同一个 item(同店重复 listing,沃尔玛不会替你拦);少一个点 = 僵尸行永远挡着新码。而这类错误全是静默的 —— 没有任何回执会告诉你「你不该弃这个码」。守门是唯一能在三个月后还拦得住的机制(tests/test_feishu_guard.py 文件头的同款理由:收口只值钱一次,守不守得住才决定它还在不在)。**审查采纳三条**:① 四位审查者全部指出守门被拆进四个新文件(0a / 0b / 2 / 横切各一份),extract_asin 白名单重复三次、abandoned_at 白名单三种数目、abandon 白名单两份 —— 守门测试自己犯了它要守的「双轨禁止」,而白名单是会随批次缩短的清单,维护在多处必烂。本条改为「只增删唯一那份」,并接受合并/重命名的收尾工作。② `abandoned_at IS NULL` 允许几处在四个包里是三种口径(三处 / 含 schema.sql 四处 / 含 sku_migrate 四处),哪份先合另一份当天就红,而最省事的修法正是注释掉断言;本条按 conventions §九(B2-27)写死的权威口径执行:消费方 .py 三处,DDL 不计入,批次 3 加第四处。③ 原稿的守门只扫 UPDATE,登记簿的 INSERT 完全不设防(mint 内联 INSERT 之后就有两条写入路径),补上 INSERT 扫描。

**测试**:
- 本条即测试;其内含用例:test_abandon_callers_are_the_four_points_only、test_destructive_workflows_never_abandon、test_abandoned_at_is_null_appears_only_in_the_three_consumers、test_only_the_codec_updates_abandoned_at、test_registry_inserts_have_two_authors_only、test_the_whitelists_do_not_rot

**验收**:python -m pytest tests/test_sku_guard.py -q 全绿;人为在 workflows/product_clear.py 里加一行 sku_codec.abandon 后重跑必须变红(改完记得撤回);ls tests/ | grep -c sku.*guard 结果为 1

#### B2-22 · `tests/test_list_new.py` · _wire_execute_env 在 :932-1007(_GateState 构造 :946-948、mark_used 桩 :978、register 桩 :997、write_submit_cols 桩 :1001);另有十三处用例直接构造 _GateState:86、239、271、315、350、382、410、443、491、618、645、864、1321

**改动**:底座补三个桩并修全部 _GateState 构造:① monkeypatch.setattr(ln.sku_codec, "mint", lambda conn, store, st, key, *, workflow="": "CODE_" + key[-6:]),可观测容器记 seen["minted"].append((store, key));② monkeypatch.setattr(ln.sku_codec, "placeholder", lambda st: "XDRYRUN00000");③ :946-948 的 _GateState 位置参数由八个补到十个(末尾追加 {} 与 set()),**上列十三处直接构造也同改**(它们是位置参构造,少两个参数会 TypeError,这是本条最先炸出来的地方)。:997 既有的 listing_sources.register 桩保留(用来断言它零调用);:1001 的 write_submit_cols 桩改成记录 vals 以便断言 V 值(`lambda u: (seen["submit_vals"].extend(u), len(u))[1]`)。

**为什么**:底座是本批所有真跑用例的地基:_GateState 变长会一次打断二十多个既有用例,先修底座再加新用例,才分得清「红是因为新功能没写对」还是「红是因为底座没跟上」。mint 桩返回可预测的码(而不是随机),是为了让断言写得出具体值 —— 随机码只能断言「不等于 ASIN」,断言不了「载荷里那个就是登记簿里那个」。mint 桩签名带 `*, workflow` 是跟着 B2-02 定死的最终签名走,漏了它真代码调用会 TypeError 而桩不会,那种差异只在生产暴露。

**测试**:
- 底座本身;受影响的既有用例:test_list_new_dry_run_gate_chain、test_failed_rows_requeue_until_cap、test_deferred_rows_write_no_terminal_state_until_the_settle_round、test_submit_loop_is_cross_store_concurrent、test_failed_store_gets_one_serial_second_pass、test_second_pass_resubmits_a_byte_identical_payload 等全部真跑用例

**验收**:python -m pytest tests/test_list_new.py -q 全绿(这是本批最重要的回归信号)

#### B2-23 · `workflows/list_new.py` · MAX_LIST_ATTEMPTS 在 :659;_SQL_ATTEMPTS 在 :662-669;_retry_rows 在 :672-701(cur.execute :690-691,阈值比较 :695)

**改动**:本批以核验为主、必要时补齐,并新增一条对 abandon 的要求:① 确认批次 0a 交付的 _SQL_ATTEMPTS 已改成按 (店, ASIN) 经登记簿 JOIN 计数并按代际;若 0a 未落地此改法(仍按 (store, sku) 数,即现状 :662-669),本批必须补上 —— 因为本批之后 sku 每换一代就重新计数,等于把 3 次上限变成无限重试。② 代际过滤的判据**不得依赖 catalog.product_events.asin 列**:要求 sku_codec.abandon 在写 sku_abandoned 事件时,detail 里显式带 {"source_key": <该行的 ASIN/GTIN>};_SQL_ATTEMPTS 的代际 LATERAL 按 `e.detail->>'source_key' = t.asin` 取最近一次弃码时刻,而不是 `coalesce(e.asin, e.sku) = t.asin`。③ 退化方向写进注释:取不到弃码事件时跨码累计(判严),不是重新计数。

**为什么**:护栏跟码走是 synthesis 规则 5 的核心:重试上限、payload_key 防重、UPC 原号复用三条护栏原本都绑在「同一个品同一个 SKU」上;换码那一刻三条同时失效。代际计数的退化方向必须是「跨码累计」而不是「重新计数」—— 判不准就判严,这是破坏性动作侧的一贯方向(误拦一行的代价远小于无限重试烧配额)。**审查采纳**:一位审查者指出 0a 版 _SQL_ATTEMPTS 用 `coalesce(e.asin, e.sku) = t.asin` 认弃码事件,而 product_events.asin 要到批次 0b 才改成经登记簿反查(现状 services/product_events.py:167 仍是 `extract_asin(r["sku"])`)—— 在「0a 已合、0b 未合」的窗口里,abandon 写出的 sku_abandoned 事件 sku 是不透明码 ⇒ asin 恒 NULL ⇒ coalesce 回落成不透明码 ⇒ 代际过滤永不命中,静默退化。方向虽然是 fail-safe(跨码累计),但让判据依赖一个还没收口的列本身就不该;改读 detail->>'source_key' 后,正确性不再跨批次依赖。

**测试**:
- tests/test_list_new.py::test_attempts_count_restarts_after_an_abandon(同 (店, ASIN):弃码事件之前 3 次提交 + 之后 1 次 ⇒ 计数为 1,行仍可重试)
- tests/test_list_new.py::test_attempts_count_accumulates_across_codes_when_no_abandon(无弃码事件时两代码的提交次数相加达 3 ⇒ 判达上限)
- tests/test_list_new.py::test_generation_filter_reads_the_detail_not_the_asin_column(SQL 文本断言含 detail->>'source_key',不含 coalesce(e.asin)
- tests/test_list_new.py::test_failed_rows_requeue_until_cap(:176 既有用例,确认阈值语义未变)

**验收**:pytest tests/test_list_new.py -k attempts -q

#### B2-24 · `workflows/alloc_push.py(改)+ services/alloc_survey.py 与 services/risk_trace.py(只加注释,不改 SQL)` · alloc_push._SQL_ONLINE 在 :49-53(lifecycle 条件在 :52),其上注释 :46-48,消费处 :68-73(`online = {(s, sku_asin.extract_asin(sku)) ...}` 在 :72、`online = {(s, a) for s, a in online if a}` 在 :73);alloc_survey._SQL_ONLINE 在 :184-190(lifecycle 条件 :188),其上「必须排 RETIRED」注释 :174-183;risk_trace still_listed 条件说明 :14-25

**改动**:决策 C 默认(对齐去重闸):① 确认批次 0a 已把 alloc_push._SQL_ONLINE 改成 walmart_items LEFT JOIN listing_sources、判据 `missing_since IS NULL AND ls.abandoned_at IS NULL`、**去掉 :52 的 lifecycle 条件**、键取 coalesce(ls.source_key, sku);若 0a 未做,本批补上(本批是 abandoned_at 第一次有非空值,不对齐会立刻出现「分配链派工、list_new 每轮拦」的对打)。② **services/alloc_survey._SQL_ONLINE 一个字不改**:在 :183 之后补三行注释说破「alloc_push 答的是『该不该派工』,已按码是否活着判;本函数答的是『占用/冲突里这家店有没有活货位』,退市行不是活货位 —— 两个口径故意不同,不要顺手统一」,并显式驳回 synthesis required_changes #6 里「alloc_survey 一起改」的半条(理由见 why)。③ services/risk_trace.py:17-19 的注释里那句「services/alloc_survey._SQL_ONLINE 同款判法与教训」保留有效(alloc_survey 没改),另补一句「alloc_push 自 2026-09-02 起改按码是否弃用判,与本处不同」。若所有者选不对齐,本条只做注释说明,并在 list_new 的拦截理由里写明「退市未弃码,等删除核验后再上」。

**为什么**:去重闸(list_new)与派工闸(alloc_push)必须同一口径,否则退市且未弃码的 ASIN 会被分配链每天派一次、被上架链每天拦一次,运营看到的是一堆永远上不去的行。反向的坑更贵:alloc_push 排 RETIRED + mint 复用旧码 + 载荷自带 2028 endDate = 对退市档案批量走官方 unretire 通道(plan.md:166 事故重演),所以 lifecycle 条件必须从**派工侧**拿掉、由「码还活着」承担判据。**审查采纳(原稿此处自相矛盾)**:两位审查者指出原稿 decisions C 的 default 写「alloc_push 与 alloc_survey 一起改」,而同一 item 的 change 正文又写「alloc_survey 只改注释、不要跟着改」,执行者拿到两条相反指令没有判据可依。本次拍死:**只改 alloc_push,alloc_survey 不改**,并把 decisions C 的措辞与本条正文逐字对齐。理由是二者回答的不是一个问题 —— alloc_survey 的产物喂给占用/冲突判定,一条退市行确实不再是活货位(:176-183 的 2026-08-15 所有者质疑与查证结论仍成立),把它算成活货位会让占用与冲突组凭空多出一批;而 alloc_push 决定要不要给运营派新活,那里的危险是复活退市档案。两处都不触发破坏动作,判据分开是安全的,合并才会打破 2026-08-15 的定稿。

**测试**:
- tests/test_alloc_push.py::test_online_set_matches_the_dedup_gate_wording(两条 SQL 文本的判据部分逐字比对:missing_since IS NULL 与 abandoned_at IS NULL 同时出现、lifecycle 不出现)
- tests/test_alloc_push.py::test_retired_but_unabandoned_asin_is_not_dispatched(未弃码的 RETIRED 行不进待派工)
- tests/test_alloc_registry.py 或 tests/test_alloc_plan.py::test_alloc_survey_keeps_its_lifecycle_condition(SQL 文本断言 alloc_survey._SQL_ONLINE 仍含 coalesce(upper(lifecycle_status),'ACTIVE') = 'ACTIVE' —— 反向钉住「不许顺手统一」)
- tests/test_risk_trace.py::test_still_listed_keeps_its_lifecycle_condition(既有 SQL 文本用例,确认没被顺手改掉)

**验收**:pytest tests/test_alloc_push.py tests/test_risk_trace.py tests/test_alloc_plan.py -q;python cli.py alloc_push --dry-run 待派工条数与切换前对比(预期变化方向:退市未弃码的那部分不再出现)

#### B2-25 · `refdata/schema.sql` · catalog.listing_sources 段在 :208-236(建表 :213-221、listing_sources_key_idx :226-227、存量回填 :228-236);新索引追加在 :227 之后、:228 的回填注释之前

**改动**:追加一条幂等部分索引:`CREATE INDEX IF NOT EXISTS listing_sources_abandoned_idx ON catalog.listing_sources (store, source_type, source_key) WHERE abandoned_at IS NOT NULL;` 并加三行注释说明它服务的是 list_new 的代际上限闸(每轮一次 GROUP BY 计数),部分条件让索引只装已弃码的少数行。**本条只加这一条新索引,不碰任何既有索引**:活码部分唯一索引(名字与条件)归批次 0a 一处定义,本批不 DROP、不重建、不改条件 —— 若 0a 已建同名 abandoned 索引,本条为空操作(IF NOT EXISTS)。

**为什么**:代际上限闸每轮要按 (store, source_type, source_key) 数已弃码行数;不带索引就是每轮全表扫 listing_sources。用部分索引(WHERE abandoned_at IS NOT NULL)而不是全表索引:活码行是绝大多数,把它们装进这个索引没有任何查询会用到。条件写的是 IS NOT NULL,与守门那条「abandoned_at IS NULL 只许出现在三处 .py」不冲突(守门不扫 schema.sql,见 B2-21)。**审查采纳**:四位审查者中有三位把「活码唯一索引三个包三个名字、批次 3 的 DROP 打空、裸 CREATE UNIQUE 在存量重复行上失败会让 db_init 整份回滚」列为 blocker;本批据此明确**不参与索引名之争**,只加一条正交的新索引,并在 depends_on 里把「索引名与条件由 0a 一处定死」列为合并前置。

**测试**:
- tests/test_sku_guard.py::test_generation_index_exists_in_schema(读 refdata/schema.sql 文本,断言索引名 listing_sources_abandoned_idx 与部分条件 WHERE abandoned_at IS NOT NULL 在;这是本仓给 DDL 钉字面量的既有做法)
- tests/test_sku_guard.py::test_the_live_unique_index_is_named_once(schema.sql 全文里活码唯一索引名只出现一次 —— 防止三个包各建一条)

**验收**:python cli.py db_init(幂等,连跑两次都成功);psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='listing_sources';" 可见 listing_sources_abandoned_idx,且活码唯一索引只有一条

#### B2-26 · `docs/db_schema.md` · catalog.listing_sources 段在 :161-177(建表 :166-172、listing_sources_key_idx 注释 :173-176)

**改动**:在 :176 之后、:177 的 ``` 之前补 listing_sources_abandoned_idx 的同款注释行(索引名 + 部分条件 + 谁在用 + 为什么部分);同段确认批次 0a 已把 abandoned_at / abandoned_reason / replaced_by 三列与活码部分唯一索引写进来,若缺则补齐(索引名以 schema.sql 实际落地的那个为准,不另起名)。列注释里必须写死那句三义辨析:码弃用 ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用。同段另补一行:catalog.upc_pool 的 status 取值(:186)新增 burned_delete / burned_lock 两个值(与 conflict 区分,由 0a 交付,本批复验在位)。

**为什么**:CLAUDE.md 硬规:动了表同步 docs/db_schema.md。三义辨析写在列注释上而不是只写在代码 docstring 里,是因为查库排障的人(和 AI)第一眼看的是这份文档,而这三个同名异义正是最容易把人带沟里的地方。

**测试**:
- 无(文档);由 code review 覆盖

**验收**:git diff docs/db_schema.md 与 refdata/schema.sql 的改动一一对应

#### B2-27 · `docs/conventions.md(新增第九节)+ CLAUDE.md(工程规范区加三行指针)` · conventions.md 现有八节(§一 :7、§二 :20、§三 :37、§四 :54、§五 :92、§六 :115、§七 :136、§八 :149),文件共 200 行;新节追加在文件末尾(:200 之后)

**改动**:新增「九、SKU 码的生命周期(所有者定稿 2026-09-02)」,内容为 synthesis rule 全文压缩版:码的寿命定义;登记簿行永不删除、abandoned_at IS NULL 叫活码;弃码只有一个实现 sku_codec.abandon;四个弃码点逐条(观测/回执/错误码/观测)与各自烧不烧号;其余一切下架不弃码的五项清单;mint 的复用语义;三条护栏跟码走;消费方契约;跨店永不复用。**并在本节写死两句权威口径供守门测试逐字引用**:(a)「身份表达式唯一文本」—— SQL 侧 `LEFT JOIN catalog.listing_sources ls ON ls.store = <t>.store AND ls.sku = <t>.sku AND ls.source_type = 'amz'` + 键 `coalesce(ls.source_key, <t>.sku)`,Python 侧 services/sku_asin.pick_asin;(b)「`abandoned_at IS NULL` 允许出现的位置」—— 消费方 .py 三处(sku_codec.mint / list_new 去重闸 / alloc_push._SQL_ONLINE),批次 3 起加 sku_migrate 候选选取为第四处;refdata/schema.sql 的部分索引条件是 DDL 不是消费方过滤,不计入;tests/ 不计入。同步在 CLAUDE.md 的「工程规范」区加一条不超过三行的指针:SKU 码身份唯一出处 listing_sources、弃码唯一实现 sku_codec.abandon、四个弃码点(展开见 conventions §九)。

**为什么**:CLAUDE.md 是每次会话必读、conventions 是展开层,这是本仓既定分工。这条规则的性质与「处置建议按 action 分工」「在营判据唯一」完全同级(都是判据唯一出处),不写进这两份文件,下一个会话的 AI 会重新发明弃码点。**审查采纳**:两位审查者指出「abandoned_at IS NULL 只许三处」这句话在四个工作包里被数成三种口径、身份表达式在不同包里带不带 source_type='amz' 也不一致,而守门测试是按文本比对的 —— 规则文字与守门清单必须逐字对得上,否则某个包合并当天守门就假红/假绿。把两句权威口径写进 §九 并让守门引用它,是唯一能让四个批次收敛的地方。

**测试**:
- 无(文档);tests/test_sku_guard.py 的白名单理由里应引用 conventions §九 的条目编号

**验收**:人眼:新会话只读 CLAUDE.md 能否答出「product_clear 停用要不要弃码」;grep -n "abandoned_at IS NULL" docs/conventions.md 能读到那句权威口径

#### B2-28 · `docs/sku_plan.md + docs/listing_plan.md + docs/feishu_tables.md + docs/plan.md` · sku_plan.md:§5.2 硬约束 :256-265、§5.3 :267-318、§5.4 速查表 :320-331、批次 2 段 :391-407、§8 待定清单 :439-475;listing_plan.md:变体三件套记录 :168;feishu_tables.md:上架表(新)行 :63;plan.md:决策日志(文件末尾追加)

**改动**:① sku_plan.md 批次 2 段(:391-407)改写为已定稿的执行记录:四个弃码点、mint 在 _prep_rows 的硬约束与理由、两道新闸与常量出处、**新增的 limit 试点闸**、决策 A/B/C/D 的最终取值(所有者拍板后填)、四个来源字母的定值与定值日期、试点七步的实际执行结果;:400-401 那句「list_new 目前无 limit 参数,要么加 -p limit=,要么手工只留一行」改写为「-p limit=N 由批次 2 交付」。② §5.3 :305-308 关于 alloc_push/alloc_survey 口径的那段按 B2-24 的最终拍板改写(只改 alloc_push,alloc_survey 明确不改及其理由)。③ §4 的「退役后旧 UPC 永久烧号」与「24h 冷却」两处补注「本仓保守策略 / 旧仓实证(legacy_survey.md:1649),非官方规则」。④ §8 待定清单(:439-475)里已落地的项打勾并注明批次 2:码的寿命(:443 已勾)、决策 A/B/C(:446-455)、四个来源字母(:469-470)、UPC 池表 E 列口径(:468)、跟卖旧续号停用(:474)。⑤ listing_plan.md 的上架主链描述里「sku=asin 约定」改为「预备期 mint,登记簿权威」,:168 那行「groupId 用 SKU 占位」补一句现在占位的是不透明码(且变体品的组 ID 仍从 parent ASIN 派生,见决策 D)。⑥ feishu_tables.md:63 的上架表行补一句:V 列「SKU」由 list_new 提交时强制回写/覆盖(submitted/failed/unknown 三种结局都写),列数从 21 改 22。⑦ plan.md 决策日志追加一条:2026-09-02 批次 2 写侧切换定稿(四字母定值、决策 A/B/C/D 取值、feed_poll 认 --dry-run、试点结果)。

**为什么**:CLAUDE.md 硬规:改了 workflow 同步对应文档。sku_plan 的旧稿与本批实现直接矛盾的地方(旧稿 §5.3 早期版本说 RETIRE 成功就弃码;:400-401 说没有 limit),留着矛盾文本比没有文本更危险 —— 下一个人会照旧稿改代码或照旧稿以为功能不存在。**审查采纳**:一位审查者指出 required_changes #1 里「§4 的烧号与冷却要标注为本仓保守策略而非官方规则」这半条在原稿里只落到了代码注释、文档更正无人承载,补进本条 ③。

**测试**:
- 无(文档)

**验收**:grep -n "retired_at\|下次上架抽新码\|无 limit 参数" docs/sku_plan.md 无残留旧稿;git diff docs/ 与代码改动一一对应

#### B2-31 · `workflows/product_clear.py` · 20-21(`动作映射(2026-08-06 所有者定稿):停用/下架 → RETIRE_ITEM(可恢复);` / `删除或 **C 列留空 → DELETE_ITEM**(永久,仅自发货)。提交走 api/feeds 唯一通道`)

**改动**::20 的「(可恢复)」后补一句括注:「⚠ 可恢复窗口 ≈ 下一轮 problem_scan 之前 —— problem_scan._SQL_ITEMS(workflows/problem_scan.py:77-83)按 `published_status <> 'PUBLISHED' AND missing_since IS NULL` 扫,无 lifecycle 豁免,停用品一到两轮就会被自动链建议 DELETE;届时走弃码点 1 正常收尾。RETIRE 本身**不弃码**(决策 A 默认,conventions §九),码与 UPC 都还活着。」若所有者选决策 A 的替代分支(RETIRE 回执成功即弃码),本条改为把「(可恢复)」整个删掉并说明为什么。

**为什么**:**审查采纳的 missing 项**(原稿完全没有承载点)。synthesis required_changes #1 明确要求改这处注释,而原稿只在 decisions A 的 **alternative** 分支里提了一句「若选简化版…措辞必须同改」—— 但决策 A 取**默认值**(RETIRE 不弃码、不加豁免)时这条注释照样必须改:默认方案下「停用可恢复」实际只有一个到下一轮扫描为止的窗口,注释里那个不带限定的「(可恢复)」会让人以为它是长期可恢复态,而这正是决策 A 要所有者知情的核心事实。纯注释改动,零行为变化,但它是所有者做决策 A 的判据面。

**测试**:
- 无(纯注释)

**验收**:grep -n "可恢复窗口" workflows/product_clear.py 有命中

#### B2-32 · `services/product_events.py` · 事件码常量区 :94-109(_FEED_KIND 在 :90-92,EVENTS 在 :113-120)

**改动**:在 :109(AUDIT_REJECTED)之后、:111 的 EVENTS 注释之前追加两行回执码具名常量:`RETIRE_FEED_SUCCESS = f"{_FEED_KIND['RETIRE_ITEM']}_feed_success"` 与 `DELETE_FEED_SUCCESS = f"{_FEED_KIND['DELETE_ITEM']}_feed_success"`,行内注释「回执码由 {kind}_feed_{status} 派生,SQL 里需要它时引用本常量,不写字面量 —— _FEED_KIND 一改名,写字面量的那条 SQL 会静默返回空集」。EVENTS(:113-120)不变(它已由 :119-120 的推导式覆盖这两个码)。若批次 0a 已加同名常量,本条降级为核验。

**为什么**:**审查采纳**:一位审查者指出 B2-11 的冷却 SQL 里直接写 `e.event = 'retire_feed_success'` 字面量,与本文件 :94 自述的「事件码常量(唯一出处;新增先在此登记)」纪律矛盾,也与同一轮改造里 0a 对 sku_abandoned 的要求(用常量拼进 SQL)不一致。回执类事件码在仓内是 `f"{k}_feed_{st}"` 推导出来的、没有具名常量,所以纪律在这一处天然破功 —— 一旦 _FEED_KIND 的取值改名,冷却闸静默返回空集、闸形同虚设而没有任何报错。补两个具名常量是最小修法(不新增能力,只给已存在的字符串一个名字);顺带把 DELETE_FEED_SUCCESS 也命名,因为 services/product_events.py:218 的 _VERIFY_SQL 里也有同款字面量,批次 3 会再用一次。

**测试**:
- tests/test_product_events_registry.py::test_receipt_code_constants_match_the_derived_set(断言 RETIRE_FEED_SUCCESS 与 DELETE_FEED_SUCCESS 都在 EVENTS 里,且值等于 _FEED_KIND 推导出来的串)
- tests/test_sku_guard.py::test_no_receipt_code_literals_in_business_sql(services/ 与 workflows/ 下不出现 '_feed_success' / '_feed_failed' 字面量,白名单只放 services/product_events.py 自身)

**验收**:pytest tests/test_product_events_registry.py tests/test_sku_guard.py -q

### DDL

```sql
CREATE INDEX IF NOT EXISTS listing_sources_abandoned_idx ON catalog.listing_sources (store, source_type, source_key) WHERE abandoned_at IS NOT NULL;  -- 服务 list_new 代际上限闸的每轮 GROUP BY 计数;部分条件让索引只装已弃码的少数行(活码是绝大多数,装进来没有查询会用到)。幂等,批次 0a 若已建则为空操作。追加在 refdata/schema.sql:227(listing_sources_key_idx)之后、:228 的存量回填注释之前;同步 docs/db_schema.md:176 之后的索引注释行。⚠ 本批**只加这一条**:活码部分唯一索引的名字与局部条件归批次 0a 一处定义,本批不 DROP、不重建、不改条件(四位审查者一致把「三个包三个索引名 + 批次 3 裸 CREATE UNIQUE 让 db_init 整份回滚」列为 blocker)。
```

### 文档同步

- docs/conventions.md — 新增「九、SKU 码的生命周期」全节(码的寿命定义 / 弃码唯一实现 / 四个弃码点 / 五类不弃码 / mint 复用语义 / 三条护栏跟码走 / 消费方契约 / 跨店永不复用),**并写死两句供守门逐字引用的权威口径**:身份表达式唯一文本(SQL 侧 LEFT JOIN + source_type='amz' + coalesce(ls.source_key, t.sku);Python 侧 sku_asin.pick_asin)、abandoned_at IS NULL 允许出现的位置(消费方 .py 三处;schema.sql 的 DDL 索引条件与 tests/ 不计入;批次 3 起加 sku_migrate 为第四处)
- CLAUDE.md — 工程规范区加三行指针:身份唯一出处 listing_sources、弃码唯一实现 sku_codec.abandon、四个弃码点(展开见 conventions 九)
- docs/sku_plan.md — 批次 2 段(:391-407)改为执行记录并把 :400-401「无 limit 参数」改写为「-p limit=N 由批次 2 交付」;§5.3 :305-308 的 alloc_push/alloc_survey 口径按 B2-24 拍板改写;§4 的「退役后旧 UPC 永久烧号」与「24h 冷却」标注为本仓保守策略 / 旧仓实证(legacy_survey.md:1649)、非官方规则;§8 待定清单(:439-475)里来源字母、决策 A/B/C/D、码的寿命、UPC 池 E 列口径、跟卖旧续号五项打勾并填定值
- docs/listing_plan.md — 上架主链描述里的 sku=asin 约定改为「预备期 mint,登记簿权威」;:168 那行「groupId 用 SKU 占位」补一句现在占位的是不透明码,并点名变体品的组 ID 仍从 parent ASIN 派生(决策 D)
- docs/db_schema.md — :161-177 catalog.listing_sources 段补 listing_sources_abandoned_idx 注释;确认 abandoned_at / abandoned_reason / replaced_by 三列与三义辨析注释在位;:186 的 upc_pool.status 取值补 burned_delete / burned_lock
- docs/feishu_tables.md — :63 的「上架表(新)」行:列数 21 → 22,补一句 V 列「SKU」由 list_new 提交时强制回写/覆盖(submitted/failed/unknown 三种结局都写)
- docs/plan.md — 决策日志追加一条:2026-09-02 批次 2 写侧切换定稿(四字母定值、决策 A/B/C/D 取值、feed_poll 从此认 --dry-run、list_new 新增 limit 试点闸、试点结果)

### 守门测试

- tests/test_sku_guard.py::test_abandon_callers_are_the_four_points_only — abandon 的调用点白名单只有 sku_codec(定义)/ catalog_sync(delete_verified)/ sku_locked_heal(RETIRE 成功+冷却)/ listing_sheet(0101119,决策 B);新增调用点即红。⚠ 本批**不新建守门文件**,只往批次 0a 建的这一份唯一之家增删白名单(四位审查者一致指出原方案会长出四个守门文件、白名单重复三处)
- tests/test_sku_guard.py::test_destructive_workflows_never_abandon — product_clear / problem_product_cleanup / maintenance / services/walmart_catalog(mark_missing)/ services/feed_track 五处 abandon 零出现
- tests/test_sku_guard.py::test_abandoned_at_is_null_appears_only_in_the_three_consumers — 该 SQL 片段在 .py 里只允许出现在 sku_codec.mint、list_new 去重闸、alloc_push._SQL_ONLINE;refdata/schema.sql 与 tests/ 不计入扫描面(口径出处 conventions §九)
- tests/test_sku_guard.py::test_only_the_codec_updates_abandoned_at 与 test_registry_inserts_have_two_authors_only — UPDATE catalog.listing_sources … abandoned_at 只允许在 services/sku_codec.py;INSERT INTO catalog.listing_sources 只允许在 services/listing_sources.register 与 services/sku_codec(mint)
- tests/test_sku_guard.py::test_sku_update_reason_has_no_caller_yet — 批次 3 的接口留着但零调用
- tests/test_sku_guard.py::test_source_letters_are_distinct_and_in_the_codec_alphabet / test_source_letters_cover_exactly_the_registered_source_types / test_registry_holds_no_second_alphabet — 四字母互不相同、都在 sku_codec 的字母表内、键集合等于四个 SOURCE_* 常量;registry 里不出现第二份字母表
- tests/test_sku_guard.py::test_cooldown_and_generation_constants_have_one_home — 24h 冷却与代际阈值的字面量只许长在 sku_codec.py
- tests/test_sku_guard.py::test_mint_has_no_dry_run_switch — mint 签名里不含 dry_run(占位码走 placeholder,双轨禁止)
- tests/test_sku_guard.py::test_placeholder_code_can_never_collide_with_a_minted_one — 占位码含字母表外符号(0 与 U)
- tests/test_sku_guard.py::test_no_second_sku_generator_survives — PHUMWMT / make_sku / next_serial_start 在 services 与 workflows 下零命中
- tests/test_sku_guard.py::test_no_receipt_code_literals_in_business_sql — '_feed_success' / '_feed_failed' 字面量只许在 services/product_events.py(冷却闸的事件码必须走具名常量)
- tests/test_sku_guard.py::test_the_live_unique_index_is_named_once — refdata/schema.sql 里活码唯一索引名只出现一次(防三个包各建一条,db_init 整份回滚)
- tests/test_sku_guard.py::test_the_whitelists_do_not_rot — 白名单登记的文件与函数必须真实存在(仿 tests/test_feishu_guard.py:348-371)
- tests/test_list_new.py::test_feed_poll_dry_run_writes_nothing_anywhere — feed_poll --dry-run 全程 abandon / mark_conflict / mark_used / release / sheet_write_ranges 零调用(**本批新增的最重要一条**:弃码不可逆,而这条链此前完全不认 --dry-run)
- tests/test_list_new.py::test_mint_happens_in_prep_not_in_one_store — 串行补试不二次抽码(双上架的唯一拦网)
- tests/test_list_new.py::test_second_pass_resubmits_a_byte_identical_payload — 既有用例扩写:两次载荷的 sku 逐字相同且不等于 ASIN
- tests/test_list_new.py::test_payload_sku_is_the_minted_code_not_the_asin — 载荷 sku 就是登记簿里那个码
- tests/test_list_new.py::test_dry_run_uses_a_placeholder_code_and_writes_nothing — list_new dry-run 全程零 mint / 零 register / 零 claim
- tests/test_list_new.py::test_limit_truncates_after_the_gates_not_before — 试点上限闸在闸门之后截断(被淘汰行不占名额)
- tests/test_list_new.py::test_cooldown_sql_scopes_the_registry_join_to_amz — 冷却闸的登记簿 JOIN 限 amz(否则跟卖行按 GTIN 建键,闸恒不生效且不报错)
- tests/test_listing_sku_col.py::test_write_submit_cols_eight_values_is_byte_identical 与 test_nobody_recomputes_the_row_sku — 批次 1 的两条护栏在本批必须仍绿(原方案会把它们打红)
- tests/test_sku_locked_heal.py::test_failed_receipt_never_abandons — 回执失败绝不弃码(不信回执信观测的边界:这一点是唯一绑回执的,所以失败分支必须钉死)
- tests/test_catalog_sync.py::test_delete_not_effective_never_abandons — 观测未生效不弃码
- tests/test_alloc_push.py::test_online_set_matches_the_dedup_gate_wording — 派工口径与去重闸口径 SQL 文本对齐,任何人加回 lifecycle 条件即红
- tests/test_alloc_plan.py(或 test_alloc_registry.py)::test_alloc_survey_keeps_its_lifecycle_condition — 反向钉住 alloc_survey 的排 RETIRED 口径**不许**跟着改(2026-08-15 定稿仍成立)

### 验收命令

```bash
python -m pytest -q  # 全量必须全绿;先修 tests/test_list_new.py 底座(_GateState 补两字段,共十四处构造点)再看新用例
```
```bash
python -m pytest tests/test_sku_guard.py tests/test_list_new.py tests/test_match_listing.py tests/test_sku_locked_heal.py tests/test_catalog_sync.py tests/test_product_events.py tests/test_product_events_registry.py tests/test_alloc_push.py tests/test_listing_sku_col.py -q
```
```bash
ls tests/ | grep -i 'sku.*guard'  # 必须只有一个文件名(守门唯一之家)
```
```bash
crontab -l | grep -Ei 'auto_listing|retire_and_relist|product_clear|daily_cleanup' ; launchctl list | grep -i erp  # 【所有者机器,合并前硬前置】CLAUDE.md 安全红线:确认旧仓的破坏性任务调度已停,输出贴进 PR。旧仓还在发 RETIRE/DELETE 时,新仓的观测会把旧仓的动作当成自己的,四个弃码点会在错误时刻触发
```
```bash
python cli.py db_init  # 幂等建表 + 新部分索引;连跑两次都必须成功
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT indexname FROM pg_indexes WHERE tablename='listing_sources';"  # 活码唯一索引只有一条 + listing_sources_abandoned_idx 在
```
```bash
python cli.py feed_poll --dry-run  # 【新增,试点第 0 步】全部输出带 [DRY-RUN],PG 与飞书零写 —— 弃码点 3 在这条链上,这一步不过就地停
```
```bash
python cli.py list_new --dry-run -p check_spec=1 -p limit=1 -p store=<试点店>  # 试点第 1 步:载荷 sku 是占位码(首位来源字母 + DRYRUN00000),其余字段正常;摘要不出现任何写库动作。⚠ check_spec=1 会真调 DeepSeek(走 llm_cache,同批重复预检不重复计费),试点期只跑一次;不看 spec 预检时用不带 check_spec 的 dry-run
```
```bash
python cli.py match_listing --dry-run  # 首条 Item 载荷的 sku 是占位码,不是 PHUMWMT 日期串
```
```bash
python cli.py list_new -p store=<试点店> -p limit=1  # 试点第 2 步:真跑 1 个品。limit 由 B2-30 交付,不再需要人去生产上架表删行
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select store, sku, source_type, source_key, workflow, abandoned_at from catalog.listing_sources where store='<试点店>' order by created_at desc limit 5;"  # 新码已登记、abandoned_at 为 NULL
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select f.store, f.sku from ops.feed_items f left join catalog.listing_sources ls on ls.store=f.store and ls.sku=f.sku where f.feed_type='MP_ITEM' and f.submitted_at>=current_date and ls.sku is null;"  # 体检:今日提交的每个 sku 都在登记簿里,必须 0 行
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select store, source_type, source_key, count(*) from catalog.listing_sources where abandoned_at is null and source_key is not null group by 1,2,3 having count(*)>1;"  # 体检:活码唯一,必须 0 行(部分唯一索引在守,这条是复核)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select count(*) from catalog.listing_sources where created_at>=current_date and workflow in ('list_new','match_listing') and sku !~ '^[<四个字母>][23456789ABCDEFGHJKMNPQRSTVWXYZ]{11}$';"  # 体检:今日新码形态全部合规,必须 0
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select upc, asin, sku, status from catalog.upc_pool where store='<试点店>' and used_at>=current_date;"  # sku 列是 12 位码、asin 列是 ASIN、status=used
```
```bash
python cli.py catalog_sync -p store=<试点店>  # 试点第 3 步:在线产品总表看到新 SKU 与来源码;上架表 V 列有值
```
```bash
python cli.py maintenance_scan -p preview=1 -p store=<试点店>  # 试点第 4 步:必须能看见这个新品 —— 批次 0a 的 SQL 收口做没做对的唯一实测,看不见就地停止切换并 revert
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select l.po_id, l.sku, l.asin from orders.order_lines l where l.sku='<新码>';"  # 试点第 5 步:该品出一单后 asin 列有值;飞书销售订单表 ASIN 列同步有值
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select store, sku, event, occurred_at, detail from catalog.product_events where store='<试点店>' order by occurred_at desc limit 10;"  # list_submitted 的 sku 是新码、detail->>'asin' 是 ASIN
```
```bash
# 试点第 6 步:对该行人为制造一次 FAILED(清 K/L、O 写 FAILED)后重跑 python cli.py list_new -p store=<试点店> -p limit=1,确认复用同一 SKU、同一 UPC:psql -c \"select count(distinct sku) from ops.feed_items where feed_type='MP_ITEM' and store='<试点店>' and submitted_at>=current_date;\" 结果为 1
```
```bash
python cli.py sku_locked_heal --dry-run  # 弃码点 2 的空跑:只报将退役/将核验条数,不动台账不轮询
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select ls.store, ls.sku, ls.abandoned_reason, ls.abandoned_at from catalog.listing_sources ls where ls.abandoned_at is not null order by ls.abandoned_at desc limit 20;"  # 弃码只应出现 delete_verified / sku_locked / upc_conflict 三种 reason(决策 A 默认下不该有 product_clear 的 RETIRE)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "select count(*) from catalog.listing_sources ls join catalog.product_events e on e.store=ls.store and e.sku=ls.sku where e.event='retire_feed_success' and e.source='product_clear' and ls.abandoned_at is not null;"  # 决策 A 默认下必须 0:停用不弃码
```
```bash
# 试点第 7 步:以上全过 → -p limit=10 跑一轮 → 再全店按常规节奏上;任何一步不过就地停,按 risks 末条的回滚清单处置(revert 前必须先落盘已 mint 且已提交成功的 (店, 新码) 清单)
```

### 决策点

- **A|product_clear 停用(RETIRE)要不要给 problem_scan 加豁免、要不要弃码**
  - 默认:RETIRE 不弃码(登记簿保持活码、UPC 不烧);problem_scan 的豁免另议,本批不实现。代码上体现为:守门测试反向钉死 product_clear 不得调 abandon;sku_locked_heal 的 RETIRE 弃码是「SKU_LOCKED 自愈链」这一条专用路径,与 product_clear 的停用不是一回事(两者都发 RETIRE_ITEM,判据在调用方不在 feedType)。**本批新增 B2-31**:无论选默认还是替代分支,workflows/product_clear.py:20-21 的「(可恢复)」注释都必须补上可恢复窗口的限定——已核实 problem_scan._SQL_ITEMS(:77-83)按 published_status <> 'PUBLISHED' AND missing_since IS NULL 扫、无 lifecycle 豁免。
  - 备选:(1) 加豁免:给 problem_scan 加「lifecycle=RETIRED 且本仓提交过 retire_submitted(source=product_clear)」的豁免,让停用真正可恢复,并另建显式恢复动作(同 SKU + 同 UPC + 未来 endDate 的 MP_MAINTENANCE 最小载荷)—— 推翻 2026-08-28「非 PUBLISHED 一律删除」的一部分,需单品实测。(2) 简化版:改采「RETIRE 回执成功即弃码」,分支更少,但永久失去可恢复、每次停用烧一个 UPC,且违背「不信回执信观测」(RETIRE 没有观测核验事件)。
  - 影响:默认方案零代码代价、保留选项:停用后码还活着,下次同店同 ASIN 再上会复用同码同 UPC(对沃尔玛 = 同一记录再提交/unretire 语义,不撞 Product ID)。风险是现行 problem_scan 策略下停用品一到两轮就会被自动链 DELETE 掉,届时走弃码点 1 正常收尾 —— 也就是说默认方案下「停用可恢复」实际只有一个到下一轮扫描为止的窗口(B2-31 把这句写进注释)。选 (2) 则 product_clear.py:20-21 的「停用(可恢复)」措辞必须整个删掉,且守门测试的反向名单要移除 product_clear 一项。三条路都能跑,判据差异只在 abandon 的调用点数量。
- **B|UPC 撞库 ERR_EXT_DATA_0101119 时,码是否与 UPC 一起换**
  - 默认:一起换(弃码点 3):listing_sheet._mark_upc_conflicts 烧号处改为一次 sku_codec.abandon(reason=upc_conflict)(烧号由 abandon 内部完成,不再另调 mark_conflict),下一轮 mint 给新码、claim 给新号。无论选哪支,B2-16 的 execute 形参与 row_sku(r) 两项修正都要做(前者是安全红线,后者是批次 1 的守门)。
  - 备选:不换码:只烧 UPC、码继续活着,下一轮同码换新号重发(维持 2026-08-09 口径「撞库只说明这个号被占,照常领新号重试」)。此时 B2-16 只做类型修正、row_sku 收口、登记簿反查与 execute 形参,不加 abandon 调用,并在 docstring 写明为什么不换;守门测试改为反向断言 abandon 零调用。
  - 影响:换的代价:多耗一个免费的码(空间 30^11,累计百万码撞码概率 3e-5)。不换的风险:重演「撞库 → 同 SKU 换 UPC → ERR_EXT_DATA_0101211(SKU 绑死旧 UPC)→ 进 SKU_LOCKED 自愈链」的死循环。唯一的反向证据是 services/upc_pool.py:132-135 记的 2026-08-19 实证(O=FAILED 换新号重发同一 SKU 必败),但那次 FAILED 是不是 0101119 没有记录。代价不对称,故默认换;两种选择的分叉只在一个 if 与一条测试断言。
- **C|alloc_push 派工口径是否对齐 list_new 去重闸(**本次修订已拍平原稿的自相矛盾**)**
  - 默认:**只对齐 alloc_push,alloc_survey 明确不改**:workflows/alloc_push._SQL_ONLINE(:49-53)改为 missing_since IS NULL AND 登记簿该 (store, sku) 未弃码,去掉 :52 的 lifecycle 条件;services/alloc_survey._SQL_ONLINE(:184-190)一个字不改(它答的是占用/冲突里「这家店有没有活货位」,2026-08-15 所有者质疑并查证过的「必须排 RETIRED」结论仍成立,见其 :174-183 注释);services/risk_trace.still_listed 保持原有 lifecycle 条件不动。三处各加一行注释说破彼此的区别。据此**显式驳回 synthesis required_changes #6 里「alloc_survey 一起改」的半条**。
  - 备选:(1) 全对齐(alloc_survey 也改):要另给一份行为差额清单——占用与冲突组会因为「退市未弃码行重新算成活货位」而变多,直接影响 resolve_conflicts 与 claim_audit 的判定,这是 2026-08-15 定稿的反向。(2) 全不对齐:分配链继续按 lifecycle 排 RETIRED、照常派工,list_new 每轮用去重闸拦并写 N 理由;此时必须把 N 列理由改得更准(例如「退市未弃码,等删除核验后再上」),否则运营看不懂。
  - 影响:默认方案下 RETIRED 且未弃码的 ASIN 从「该派工」变成「等 delete_verified 后再派工」,延迟 = 删除链的一到三天(48h 宽限 + 下一轮观测)。alloc_survey 不跟着改不会造成对打:两者都不触发破坏动作,只是分别喂给「派工」与「占用/冲突」两条判定。反向的坑最贵且必须避免:alloc_push 排 RETIRED + mint 复用旧码 + 载荷自带 2028 endDate = 对退市档案批量走官方复活通道(plan.md:166 事故重演),所以无论选哪支,都不许在派工侧「排 RETIRED」与「mint 复用」同时成立。**原稿此处的 change 正文与 decisions 措辞相反,两位审查者点名,本次按上述拍死并逐字对齐。**
- **D|变体品的 variantGroupId 仍从 parent ASIN 派生(货源隐匿的剩余漏洞)——新增决策项**
  - 默认:本批不改,作为独立议题交所有者定,并由 B2-28 写进 docs/sku_plan.md §8 待决清单(不能只活在 risks 文本里)。默认假设:暂不改;单品口径反而变好(B2-04 之后单品的占位组 ID 从 ASIN 变成不透明码),变体品的组 ID 仍可倒推货源。
  - 备选:(1) 组 ID 也换成不透明码:需要一个跨轮稳定、同族一致的派生源(例如给每个变体家族在登记簿/新表里分配一个不透明 family_id),并处理存量已在架变体组的迁移(改组 ID 等于重组变体,风险远高于改 SKU)。(2) 折中:只对**新建**变体家族用不透明组 ID,存量家族保持现状——代价是两套并存,与「单一实现路径」冲突,需要显式登记为过渡态并给终止条件。
  - 影响:不改的代价是改造的核心目标(货源隐匿)对变体品只完成了一半:services/variant_group.group_id(:125-155,由 parent_asin 或 min(家族 ASIN) 派生)经 services/mp_conform 写进 Visible.variantGroupId 随载荷发给沃尔玛,任何拿到我们变体组 ID 的人都能直接倒推 ASIN。改的代价是一轮独立调研 + 变体组重组的实测风险。**四位审查者中有三位把这条列为「已识别但无人认领」的目标级漏洞**(原稿只在 risks 第 1 条提了一句、没有 item、没有决策编号、没进待决清单),本次立为决策 D,确保交付后有人回来处理。

### 依赖

- **跨包前置(必须在写第一行代码前拍平,四位审查者一致列为 blocker)**:① 活码部分唯一索引的**名字与最终条件**由批次 0a 一处定死(建议 listing_sources_live_uidx,条件含 abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL AND 不透明码形态 + 至少一个字母),批次 2/3 一律不 DROP、不重建;② 不透明码字母表 / 长度 / 「至少一个字母」判据唯一出生地 = services/sku_codec.py(横切 D4),registry 与 services/sku_asin 都不留第二份;③ 守门测试唯一之家 = tests/test_sku_guard.py,后续批次只增删白名单;④ mint 的最终签名 = mint(conn, store, source_type, source_key, *, workflow),无 dry_run kwarg。这四条不定,0a 一合就会有测试红,而最省事的修法正是注释掉断言。
- 批次 0a(必须已合并):services/sku_codec.py 的 mint / abandon / is_opaque / source_of;catalog.listing_sources 的 abandoned_at / abandoned_reason / replaced_by 三列 + 活码部分唯一索引(并发双 mint 靠它拦,没有它 B2-03 的顺序 mint 也挡不住两个进程同轮抢同一个 key);upc_pool 的 burned_delete / burned_lock 两个状态值;services/sku_asin.resolve / resolve_many / pick_asin;维护链与审核链的六处 SQL 硬等号收口(否则试点第 4 步 maintenance_scan preview 看不见新品,切换必须就地停);product_events 的 SKU_ABANDONED / SKU_REPLACED 事件码登记(未登记时 record_many 会 fail loud,services/product_events.py:156-159)
- 批次 0b(必须已合并):order_lines / product_events / blacklist / order_audit / alloc 四处 / feed_track / product_refresh 等按 SKU 形态倒推 ASIN 的调用点收口(否则新码上架后订单审核每单判待人工);飞书销售/售后订单表 ASIN 列。**特别点名 services/blacklist.py:207-215 的 _LATEST_CTE**(`DISTINCT ON (coalesce(asin, sku))` 推黑名单键,:260-268 的 rebuild_asin_blacklist 会整表重灌)——一位审查者查出 sku_plan §3.3 与四个工作包全都漏了它;不收口的话本批之后 product_events.asin 为 NULL 的行会让 coalesce 回落成 12 位不透明码,**随机码被写成黑名单键**,正是 §3.3 点名的三大最危险失效之一。本批把它列为硬前置:0b 若不收,批次 2 不许上生产。
- 批次 1(必须已合并,且本批要复验):上架表 V 列「SKU」+ registry columns 22 列(registry/resources.py:514-524,当前 21 列、"upc_match" 在 :523)+ _COLS=22(services/listing_sheet.py:41 当前 `_COLS = 21          # A~U`)+ 单列写函数 + row_sku() 单一回落表达式 + write_submit_cols 的**可选**第 9 值(B1-07);listing_sheet.sync_from_ledger:542 的回执找行、heal_unknown:417/438、sku_locked_heal:79/92/125 的 skus 取值全部改为 row_sku(r)。本批 B2-13 的 test_retire_payload_carries_the_code_not_the_asin 就是这条依赖的验收 —— 批次 1 若漏了 sku_locked_heal:79,切换后 RETIRE 发出去的是 ASIN,退不到东西且不报错。另需复验 B1-09 把 _mark_upc_conflicts 的池反查状态条件与 services/upc_pool.burn_for_retire:202 的 `status IN ('claimed','used')` 对齐(现状是 `status <> 'conflict'`,两个烧号出口口径不一致本身就违反单一路径)。
- 批次 0a 的 _SQL_LISTED_ASINS 去重闸改法(workflows/list_new.py:304-306 当前是裸 `SELECT DISTINCT store, sku FROM catalog.walmart_items WHERE missing_since IS NULL`;改为 walmart_items LEFT JOIN listing_sources、missing_since IS NULL AND abandoned_at IS NULL、键 coalesce(source_key, sku)、**不加 lifecycle 条件**)必须已在位:本批是 abandoned_at 第一次有非空值,去重闸没改的话已弃码的僵尸行会永久挡住新码
- **所有者前置(代码之外,试点真跑前必须完成)**:四个来源字母定值(B2-01 的占位值替换 —— 一旦有码 mint 出来就改不回去,必须是硬前置不是边跑边定);决策 A / B / C / D 拍板;synthesis required_changes #13 的批次 2 前单品实测 (a)-(e) 全部跑完并记录结果;**沃尔玛 spec 里 Orderable.sku 的长度上限与字符集核实**(docs/sku_plan.md:464 待办 —— 本批就要按 12 位真跑,一位审查者指出四个包都没把它列成阻塞前置,本批补上)
- **调度层前置(CLAUDE.md 红线,已升级为可执行的合并硬闸)**:确认旧仓 auto_listing / retire_and_relist / product_clear / daily_cleanup 的调度已停 —— 新旧系统对同一破坏性任务并跑会让弃码点在错误时刻触发(旧仓发的 RETIRE/DELETE 会被新仓的观测当成自己的动作)。synthesis open_questions #16 明说这几条旧 cron 是否已停不在证据内,合并前必须在所有者机器上现场核一遍并把 `crontab -l | grep -Ei ...` 的输出贴进 PR(见 acceptance_commands 第 4 条)。**未采纳**审查建议的「加 -p legacy_stopped=<日期> 参数,缺省即拒跑」:list_new 每天 20:00 由调度自动跑,加一个必填参数会让整条链每天拒跑,而且这个参数只能靠人自己填日期、伪造成本为零 —— 用可执行的合并前置检查 + plan.md 决策日志留痕更实在。

### 风险

- **变体品的 variantGroupId 仍把 ASIN 递给沃尔玛**(本批之外的隐匿漏洞):services/variant_group.group_id(:125-155)用 parent_asin 或 min(家族 ASIN) 派生组 ID,mp_conform 把它写进 Visible.variantGroupId 发出去。变体品换成不透明 SKU 之后,货源仍可从变体组 ID 直接倒推;单品口径反而变好(B2-04 之后占位组 ID 从 ASIN 变成不透明码)。**本次修订已把它从「risks 里的一句话」升级为决策 D**,并由 B2-28 写进 docs/sku_plan.md §8 待决清单 —— 三位审查者都指出「一条已识别的目标级漏洞只活在 risks 文本里,交付后没人会回来处理它」。
- **dry-run 里看到的 sku 是占位码,不代表真跑会发的串**:有活码行的产品真跑会复用登记簿里的旧码。所有者用 dry-run 验收时要知道这一点,否则会以为「每次都抽新码」。要看真串只能查登记簿(acceptance 里那条 SQL)。
- 预备期 mint 之后、提交之前失败的行(必填缺失 / 标题不足 / 出参失败 / UPC 池不足 / 整店异常)会留下活码行但上架表 V 列为空:登记簿里有码、表上看不见。下一轮复用同一个码并在提交时写 V,自愈;但在此之前运营从表上查不到这行的码。可接受(不影响任何自动判据),但排障时要知道「V 空不等于没有码」。
- 同一轮里同 (店, ASIN) 出现两行(运营在上架表贴重了)时,两行会拿到同一个码 —— mint 的第二次调用命中活行复用。这与切换前的行为一致(切换前两行的 sku 都是同一个 ASIN),同一个 feed 里两条同 sku 条目由沃尔玛侧 REPLACE 语义吞掉。不是本批引入的新风险,但换码之后更不容易被人眼发现(两个随机码相同不像两个 ASIN 相同那样扎眼),**B2-03 已据此在 mint 段加了一行「本轮 N 行复用同一个码」的日志计数**。
- sku_locked_heal 路径会经历一次「双冷却」:自愈链自己等 24h(从 RETIRE 提交时刻起算),清列后 list_new 的新冷却闸又按 retire_feed_success 的回执时刻等 24h,叠加后实际多等一个回执延迟(通常几分钟到几小时,list_new 每天 20:00 一轮,多数情况下不多等一轮)。默认接受(判不准就判严),这正是 sku_plan 批次 4「有了退役就换新码之后 24h 冷却是否还必要」要回答的问题;若试点发现多等一整轮不可接受,再给冷却闸加「该 (店, ASIN) 在 retire 之后已有 sku_abandoned 事件则不等」的例外。
- 四个来源字母一旦有码 mint 出来就改不回去:改字母只影响之后新抽的码,已发出去的码留在沃尔玛侧。所以「所有者定值」必须是试点真跑的硬前置,不能边跑边定(已写进 depends_on)。
- 登记簿行永不删除 + 每次弃码留一行,长期看行数随「上架过的 (店, 产品) 次数」增长。代际上限 3 把单个 (店, 产品) 的行数封在 4 行以内,总量可控;但 catalog.listing_sources 会从「每个在架品一行」变成「每个上架过的品若干行」,alloc / risk_trace 等按 source_key 反查的地方要确认没有隐含的一对一假设(批次 0a 收口时应已处理,本批复验)。
- abandon 是单向推进,没有撤销弃码的路径(有意为之)。中间窗口内运营在 Seller Center 手工改 Site End Date 或手删,会让登记簿与沃尔玛侧短暂不一致,只能靠 catalog_sync 观测 + 人工 UPDATE 一行修正 —— 而守门测试禁止 sku_codec 之外的 UPDATE,所以人工修正必须走 psql 直连并在 plan.md 留痕。是否要一条人工「撤销弃码」的登记入口,留给所有者(synthesis open_questions #14)。
- 本批依赖的批次 0a/0b/1 若有任何一处漏改,表现全部是静默失效而不是报错(sku_plan §3.1 的总判断)。所以试点第 4 步(maintenance_scan preview 必须看得见新品)是不可跳过的闸:它是批次 0a 那六处 SQL 收口做没做对的唯一实测手段。**特别提示 services/blacklist.py:207-215 的黑名单键推导若被 0b 漏掉**,本批之后会把随机码灌进 catalog.asin_blacklist(rebuild 路径是一次性把好键换成坏键),而黑名单闸从此拦不住违禁品 —— 已列进 depends_on 作为硬前置。
- 单品实测 (a)-(e) 未做就切换的风险:(a) 停用后该 SKU 在 GET items 里是缺席还是 RETIRED 可见 —— 决定弃码点 1 与去重闸的实际行为;(b) 同码同 UPC 重发能否复活缺席记录 —— 官方与六家第三方都说能,本仓与旧仓零实证,mint 的复用语义押在这上面;(c) DELETE 经 delete_verified 后同店同 ASIN 重上能否新码新号过闸;(d) 人为制造 0101119 看码与 UPC 是否同换、重试计数是否跨码累计、代际上限是否生效;(e) sku_locked_heal 清列后能否过闸重上、新码新号是否仍需 RETIRE + 24h。(f) RETIRE_ITEM feed 是否仍被受理(plan.md:56/165 待办)属并行风险,不做也能切,但弃码点 2 的整条路依赖它。
- **回滚方案(原稿缺失,三位审查者点名;横切包散文版在此收编并补一条原稿漏掉的危害)**:代码可 `git revert` 本批 PR,新列不 DROP(conventions §五:DROP COLUMN 不可回滚,未连库核对一律不执行),飞书 V 列留空不删。但 revert 之前**必须先落盘已 mint 且已提交成功的 (店, 新码) 清单**:`select store, sku from catalog.listing_sources where workflow='list_new' and abandoned_at is null and created_at>=<切换日>;`。原因是 revert 之后 _apply_submit_result(:259-285)回到写 r["asin"],而这批已用不透明码上架成功的品,只要将来出现一次 O=FAILED 走 _retry_rows(:672-701,按 r["asin"] 重排队),list_new 就会用 sku=ASIN 重发 MP_ITEM —— **同一产品在同一店多出第二条 listing,烧一个 UPC,而且不报错**。处置:revert 后把这批行在上架表上标 O 终态或从待上队列摘掉,并跑 `python cli.py list_new --dry-run` 确认这批 ASIN 全部落在去重闸(counts['dedup'])而不是候选里。已 mint 的码留在登记簿里不必回收(下轮复用)。
- **行号会漂**:本工作包的每一条 file:line 都在 2026-09-02 逐个打开文件核对过(list_new.py 1891 行、listing_sheet.py 565 行、match_listing.py 311 行、sku_locked_heal.py 258 行、product_events.py 267 行、catalog_sync.py 276 行、alloc_push.py 114 行、resources.py 1057 行、schema.sql 1700 行),但依赖批次 0a/0b/1 先合并 —— 它们会插入新行。执行前请对每条 item 用 lines 字段里给的**锚点字符串** grep -n 重新定位,不要照抄行号做 sed/patch。
- **未采纳的审查建议(逐条说明)**:① 「给 list_new 加 -p legacy_stopped=<日期>,缺省即拒跑」—— 未采纳,理由见 depends_on 调度层前置那条(每日调度会被拒跑,且参数伪造成本为零);改为可执行的合并前置检查。② 「把 alloc_survey._SQL_ONLINE 一起对齐」(synthesis required_changes #6 的半条)—— 显式驳回,理由见决策 C(它答的是占用/冲突口径,2026-08-15 定稿仍成立)。③ 「write_submit_cols 第 9 值改必填」—— 未采纳,改为可选,理由见 B2-10(必填会打红批次 1 的守门)。④ 「把整个 match_listing 的 for r in todo 循环包进一个事务」—— 未采纳,改为两趟,理由见 B2-18(循环体 :155 是逐行的沃尔玛 API 调用)。⑤ 「本批新建 tests/test_sku_codec_guard.py」—— 未采纳,改为并入唯一的 tests/test_sku_guard.py,理由见 B2-21。⑥ 关于批次 0a 的 pick_asin 大小写/形态校验、0b 的 resolve_pairs 按店收窄、批次 1 的 _COLS 派生打通 A2:V、批次 3 的 SkuUpdate 过不了 mp_conform.strip_unknown 等审查意见:均不属本批范围,已在本条留档,请分别转交对应批次的工作包(其中批次 3 的 strip_unknown 一条会让形态 B 每一行都变成「建第二条 listing」,建议优先处置)。

### PR 切分

建议**一个 PR** 交付(约 900-1100 行改动,其中测试约一半 —— 比原稿估的 700-900 多出的部分是新增的 B2-29 feed_poll execute 贯通、B2-30 limit 闸、B2-31/32 两条小修,以及守门从「新建文件」改成「并入既有文件」后要一并搬迁的 0b 断言),不拆。理由是拆开会出现三种半切换状态,每一种都是真实的钱:(1) 只上 mint、不上四个弃码点 —— delete_verified 之后码不弃,下一轮 mint 复用旧码 + claim 复用旧 UPC 去上一个沃尔玛侧已经删掉的 item,撞官方的 48h 等待期,而且看起来像「运气差」;(2) 只上弃码点、不上两道新闸 —— SKU_LOCKED 品每轮弃一码烧一号,没有代际上限兜底;(3) **上了弃码点 3 却没上 B2-29** —— `cli.py feed_poll --dry-run` 会真弃码真烧号,而 feed_poll 挂在 product_chain 里每轮自动跑,这是本次修订新识别的、必须与弃码点同批的一条。PR 内部按提交拆成七段便于 review:c1 registry 四字母 + sku_codec 常量/placeholder/签名对齐;c2 list_new 写侧(mint/载荷/mark_used/事件/V 列/去 register);c3 match_listing 写侧两趟 + 删 make_sku 一族;c4 四个弃码点(catalog_sync / sku_locked_heal / listing_sheet / sku_update 接口)+ **feed_poll execute 贯通**;c5 两道新闸 + limit 试点闸 + _GateState + 索引 + product_events 回执码常量;c6 守门白名单增删(并入 tests/test_sku_guard.py);c7 文档 + product_clear 注释。\n\n**工时与关键路径(原稿缺失,一位审查者点名)**:写代码约 5-6 人日;但关键路径不是写代码,是三件「人的等待」——(a) 四个来源字母定值 + 决策 A/B/C/D 拍板(所有者,可与 0a/0b 并行启动);(b) synthesis required_changes #13 的 (a)-(e) 五项单品实测 + Orderable.sku 长度/字符集核实(所有者机器,每项要等一轮 catalog_sync,合计 3-5 个自然日,**建议在批次 0a 开工当天就启动**);(c) 旧仓四条破坏性 cron 已停的现场核实。三件全部完成前,代码可以合并到分支但一行都不许真跑。\n\n合并后**不要立刻放全店**:按 acceptance 的试点七步走 —— 第 0 步 `feed_poll --dry-run` 零写、第 1 步 dry-run 看占位码、第 2 步 `-p limit=1` 真跑一个品、第 4 步 `maintenance_scan -p preview=1` 必须看得见这个品(不过就地停并按 risks 末条回滚)、第 6 步人为 FAILED 验复用、第 7 步 `-p limit=10` 一轮后再全店。limit 闸由 B2-30 交付,不再需要人去生产上架表删行。
