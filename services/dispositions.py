"""处置建议台账(ops.dispositions)的读写积木 —— "建议/执行"分界面。

两条链共用本模块(2026-08-16 起):
    problem_scan     → problem_product_cleanup   反补/删除/顽固停用
    maintenance_scan → maintenance               标题/价格/库存/删除
**必须落在 services**:铁律 1 规定任何层不准 import workflows,
两条链的四个工作流之间不能互相取用。

状态机(与 refdata/schema.sql 的 ops.dispositions 头注一致):
    suggested → executing → confirmed / ineffective / withdrawn

函数各管一段,谁也不越界:
    suggest_many()      扫描件写建议(幂等:同 (店铺,SKU,动作) 已有未落定行则刷新)
    withdraw_stale()    本轮不再建议的 suggested 行置 withdrawn
    claim()             执行件领取待执行建议(只读,不改状态——提交成功才改)
    mark_executing()    提交成功后落 feed_id 并转 executing
    settle()            按观测事件把 executing 判成 confirmed / ineffective
    settle_maintenance() 同上,但判的是"值改过来了没有"(标题/价格/库存)
    expire_executing()  超期没等到观测的 executing 行放行,免得永久堵住同一 SKU

⚠ **生效判定不在本模块实现**。settle() 读的是 catalog.product_events 里
catalog_sync 经 services/product_events.verify_deletions 落的
delete_verified / delete_not_effective ——"不信回执信观测"那套规则(含 46h
宽限期(2026-08-29 由 48h 下调,理由见 product_events.DELETE_VERIFY_GRACE_HOURS)、
RETIRED/缺席算 gone)已经在跑,这里再写一份判定只会产生两份会漂移的
真相。本模块只做"把已有判决登记到建议行上"。

⚠ **两条链共用一张表,交界处五条纪律**(2026-08-16 定,2026-08-24 大修)。
起因是所有者在 08-19 的维护记录里翻到一行「删除 | 审核判拒仍在架:(理由未留存)」
——那句措辞只有 problem_scan 造得出,而维护记录表只有 maintenance 写得进。
病根是旧形态的三个毛病凑在一起:按来源领取、按来源记原因、压制只活在一轮内存里。

  ① **按动作领,不按来源领**(`claim(actions=...)`,必须传)。source 答
     "为什么建议",action 答"该谁干"。拿前者当后者用,归属就取决于"谁先落库"
     而显示的原因取决于"谁后覆写",两边对不上。
  ② **破坏动作只有一个出口** problem_product_cleanup。maintenance 从
     2026-08-24 起不发 DELETE_ITEM —— 两个出口意味着配额、在途防重、病历口径
     各有一套,同一个 SKU 被两条链先后删两次是生产实证过的(提交期防重按
     **整批载荷指纹**算,两条链批次内容不同就撞不上)。
  ③ **破坏组存在即压制同 SKU 的维护组**,实现在 `claim()` 的 _SUPPRESS_CLAUSE
     ——按库里所有未落定的破坏类建议判,**与两个扫描件谁先跑无关**。
     压制条数由 `count_suppressed()` 报,不许静默。
     ⚠ 破坏组内部**不合并**:retire + delete 同 SKU 是 problem_scan 对顽固件
     的有意设计(双 feed 齐发),合成一条会让一个的落定覆盖另一个。
  ④ **多来源支撑**:`sources` 列按来源分格记 {action, code, reason, at}。
     部分唯一索引 `(store, sku, action)` 跨来源,同一条被两条链命中时合成
     一行 —— 但两条理由都留在 sources 里,`reason`/`category` 由 claim() 现算
     (单来源逐字不变,多来源拼成「维护:… | 审核:…」)。
     `source`/`reason`/`category` 三列**不再被后写方覆盖**,它们是病历。
  ⑤ 每条链只撤销/只统计**自己那一格**(withdraw_stale/count_open 的 source
     参数):撤销删掉自己那格,**全空才 withdrawn**;另一条链还在支撑的行照旧
     待执行。整行撤会把对方的建议一起干掉,而对方永远撤不掉自己那条。

单店破坏类上限(限额表「下架限制」)只在执行件领取时施加一次
(`cap_destructive`)。此前两条扫描件各截一次同一张表,每店最多 N 条实际变成
最多 2N。
"""

import logging

logger = logging.getLogger("services.dispositions")

# ⚠ **本模块每个 SQL 参数都必须显式写 `::类型`**(2026-08-14 定,有 lint 式
# 测试 test_every_sql_param_is_cast 挡着)。不是风格洁癖 —— 同一段 SQL 因为
# PG 推不出参数类型,在生产上连炸三次,而每次 pytest 都是全绿:
#   ① `record <> ALL(text[][])`                     类型不匹配
#   ② `%(store)s IS NULL OR store = %(store)s`      IS NULL 不提供类型信息
#   ③ `jsonb_build_object('k', %(why)s)`            该函数收 any,无从推断
# 本仓的 SQL 用例只断言**文本子串**,PG 的类型推断根本跑不到 —— 靠"下次小心"
# 是没用的,只有"每个参数都带 cast"这条机械规则能被测试执行。

# 动作取值。破坏组(delete/retire)与反补归 problem_product_cleanup,
# 维护三类归 maintenance —— **按动作分工,不按来源**(理由见下面 PROBLEM_ACTIONS)。
# ⚠ relist(反补)2026-08-28 所有者定稿退役:「非 PUBLISHED 一律删除,不再改
# End Date 救商品」。库里存量 relist 行不删:suggested 由扫描件 withdraw_stale
# 自然撤掉,executing 由 settle 的 _SETTLE_RELIST_SQL 照旧落定 —— 但 claim
# 不再领取(不在任何领取集),不会再有新的反补 feed 发出。
ACTIONS = ("delete", "retire", "title", "price", "inventory")

# ⚠ **动作优先级,全项目唯一出处**(所有者定稿 2026-08-24)。
# 序:删除 > 停用 > 库存 > 标题 > 价格。三处依赖它:合并建议时谁压过谁、
# claim() 的取件顺序、摘要的列序。
# 这条规则本来就在跑,只是关在 maintenance_intents 的**一轮内存**里
# (`_ACTION_RANK`(已删)/ `collect_all` 的 `doomed` 集合)——那份只看得见本轮
# 自己算出来的删除,看不见另一条链挂在库里的建议,于是跨链重复删了两次
# (所有者 2026-08-24 实证)。提升作用域到"库里所有未落定建议"就是本常量。
ACTION_RANK = {"delete": 0, "retire": 1,
               "inventory": 2, "title": 3, "price": 4}
ACTION_ORDER = tuple(sorted(ACTIONS, key=lambda a: ACTION_RANK[a]))

# 破坏组:不可逆。**存在即压制该 SKU 的维护组建议** —— 要删的东西没必要再花
# 配额去改(批次 E 踩过:先花配额救活再花配额删掉)。压制在 claim() 里判。
# ⚠ 组内**不合并**:同 SKU 同时挂 retire 与 delete 是 problem_scan 对顽固件的
# 有意设计(双 feed 齐发,能删的删、删不掉的至少停用),两条是两个 feed、
# 两次独立的生效判定,合成一条会让其中一个的落定结果覆盖另一个。
DESTRUCTIVE_ACTIONS = ("delete", "retire")
# 单店单轮破坏类上限的缺省值:限额表「下架限制」缺该店时的退路(会告警)。
# **唯一出处**(2026-08-24 归一):此前 maintenance_intents.DELETE_PER_STORE 与
# problem_scan._AUDIT_DELETE_PER_STORE 各写一个 300,两条链各截一次 ⇒ 每店 600。
DESTRUCTIVE_PER_STORE = 300

# ── 执行件按**动作**领取,不按来源(2026-08-24 定)────────────────────────
# source 回答"为什么建议",action 回答"该谁干"。拿前者当后者用就会错位:
# 2026-08-19 生产实见一行 —— 维护链先落 delete 建议(source='maint'),审核链
# 后来覆写了它的 reason,那行仍归维护链执行,于是维护记录表里写着维护链的
# 「建议」、问题链的「原因」,谁也说不清是哪条链干的。按动作领之后不可能再错位。
PROBLEM_ACTIONS = ("delete", "retire")   # relist 已退役(2026-08-28,见 ACTIONS 注)
# 维护链专属动作:它们的"生效"没有对应的核验事件,由 settle_maintenance()
# 直接比对 catalog.walmart_items 的现值判定。**同时是 maintenance 的领取集**
MAINT_ACTIONS = ("title", "price", "inventory")

# 来源(tro 是预留:侵权投诉链将来也走同一张建议表)。按动作领之后来源只剩
# 两个用途:withdraw_stale(每条链只撤自己建的)与 count_open(各报各的账)
SOURCES = ("scan", "audit", "tro", "maint")
PROBLEM_SOURCES = ("scan", "audit", "tro")
MAINT_SOURCES = ("maint",)

# 幂等写:同 (店铺,SKU,动作) 已有未落定行 → 刷新依据与时间,不新增
# (扫描件按调度反复跑,每轮堆一行会让建议表变成流水账)。
# ⚠ 只更新 suggested 行:**executing 行绝不能被覆盖**——它已经提交了 feed,
# 把它的 suggested_at 刷新会让"等观测多久了"失真,更严重的是若同轮把
# feed_id 洗掉,这条提交就永远等不到落定判决了。
# 合并规则。⚠ 三个键**不再被后写方覆盖**(2026-08-24 改):
#   source   = 首个支撑来源(与"谁先落库"一致,不再与 reason 自相矛盾)
#   reason   = 首次建议时的原因,留给病历
#   category = 同上
# 当前全貌在 sources 里,由 claim() 现算 —— 旧形态让后写方覆盖 reason,结果是
# 一行显示着 A 链的建议、B 链的原因(08-19 生产实见那条维护记录)。
# detail 改成**合并**而不是替换:替换会把维护链删除行的 label/旧值/新值 洗掉
#(那正是 08-19 那行「建议」列只剩光秃秃"删除"、旧值新值全空的原因)。
_UPSERT_SQL = """
INSERT INTO ops.dispositions
    (store, sku, asin, source, action, category, reason, detail, sources)
VALUES (%(store)s::text, %(sku)s::text, %(asin)s::text, %(source)s::text,
        %(action)s::text, %(category)s::text, %(reason)s::text,
        %(detail)s::jsonb, %(sources)s::jsonb)
ON CONFLICT (store, sku, action) WHERE status IN ('suggested', 'executing')
DO UPDATE SET asin = COALESCE(EXCLUDED.asin, ops.dispositions.asin),
              detail = ops.dispositions.detail || EXCLUDED.detail,
              sources = ops.dispositions.sources || EXCLUDED.sources,
              suggested_at = now()
WHERE ops.dispositions.status = 'suggested'
"""


# 观测判决登记:executing 行 × 提交之后落的核验事件。
# gone 侧(delete_verified)= 生效;still 侧(delete_not_effective)= 没生效。
# 反补(relist)的生效信号不同:商品重新 PUBLISHED —— 直接看 walmart_items
# 现状,不看事件(反补没有对应的核验事件流)。
# ⚠ relist 动作已退役(2026-08-28,见 ACTIONS 注):_SETTLE_RELIST_SQL 保留
# 只为给**存量** executing 行收尾,新建议不会再产生这个动作。
_SETTLE_DELETE_SQL = """
UPDATE ops.dispositions d
SET status = CASE WHEN e.event = 'delete_verified'
                  THEN 'confirmed' ELSE 'ineffective' END,
    settled_at = now(),
    detail = d.detail || jsonb_build_object('settled_by', e.event)
FROM (
    SELECT DISTINCT ON (store, sku) store, sku, event, occurred_at
    FROM catalog.product_events
    WHERE event IN ('delete_verified', 'delete_not_effective')
    ORDER BY store, sku, occurred_at DESC
) e
WHERE d.status = 'executing' AND d.action IN ('delete', 'retire')
  AND d.store = e.store AND d.sku = e.sku
  AND e.occurred_at > d.executed_at
RETURNING d.status
"""

# 反补生效 = 该 SKU 已不在问题清单里(published_status 回到正常或已缺席);
# 仍在问题清单 = 没生效。**必须等 catalog_sync 重新观测过**
# (last_seen_at > executed_at),否则拿提交前的旧快照判,永远判成"没生效"。
_SETTLE_RELIST_SQL = """
UPDATE ops.dispositions d
SET status = CASE
        WHEN w.sku IS NULL OR w.missing_since IS NOT NULL
             OR w.published_status NOT IN ('UNPUBLISHED', 'SYSTEM_PROBLEM')
        THEN 'confirmed' ELSE 'ineffective' END,
    settled_at = now(),
    detail = d.detail || jsonb_build_object(
        'settled_by', coalesce(w.published_status, 'absent'))
FROM catalog.walmart_items w
WHERE d.status = 'executing' AND d.action = 'relist'
  AND w.store = d.store AND w.sku = d.sku
  AND w.last_seen_at > d.executed_at
RETURNING d.status
"""


def suggest_many(conn, rows: list[dict]) -> int:
    """输入:连接 + 建议行(store/sku/action 必填)→ 输出:写入行数。幂等。

    detail 传 dict,本函数负责序列化;asin/category/reason 可缺省。
    非法 action/source 直接抛 —— 拼错一个字符串会静默落一批永远没人领的
    建议行(执行件按 action 分桶,不认识的桶不会被消费),宁炸不吞。

    每行都往 sources 里写自己那一格(来源 → 动作/原因码/原因/时间),同
    (店铺,SKU,动作) 被两条链命中时两格并存 —— 这是"查当初为什么删要翻两条链
    日志"的解法。
    """
    import json
    from datetime import datetime, timezone
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for r in rows:
        if r["action"] not in ACTIONS:
            raise ValueError(f"未知 action={r['action']!r}(可用:{ACTIONS})")
        src = r.get("source", "scan")
        if src not in SOURCES:
            raise ValueError(f"未知 source={src!r}(可用:{SOURCES})")
        reason = (r.get("reason") or "")[:500]
        payload.append({
            "store": r["store"], "sku": r["sku"], "asin": r.get("asin"),
            "source": src, "action": r["action"],
            "category": r.get("category"), "reason": reason,
            # default=str:detail 里常有 datetime(删除建议带 first_seen/
            # last_seen)。不给 default 的话 json.dumps 直接抛,而抛的位置在
            # 扫描件末尾 —— 一整轮扫描白跑,且看起来像"建议表坏了"
            "detail": json.dumps(r.get("detail") or {}, ensure_ascii=False,
                                 default=str),
            "sources": json.dumps(
                {src: {"action": r["action"], "code": r.get("category"),
                       "reason": reason, "at": now}},
                ensure_ascii=False, default=str),
        })
    with conn.cursor() as cur:
        # ⚠ 报**实际落库行数**,不是 len(payload)。两者会差:同 (店铺,SKU,动作)
        # 被两个来源同时命中时,部分唯一索引把它们合成一条。首版报 len(payload)
        # ——生产实测扫描件说"落账 521 条"、执行件只领到 472 条,对不上账。
        # executemany 的 rowcount 在 psycopg3 里是各次之和,正是我们要的。
        cur.executemany(_UPSERT_SQL, payload)
        return cur.rowcount if cur.rowcount is not None else len(payload)


# ⚠ 用**多参数 unnest + NOT EXISTS**,不要写成
#   `(store, sku, action) <> ALL(%(keep)s::text[][])`
# 那样是错的:左边是 record 类型,右边二维数组的元素类型是 text,PG 直接报
# 类型不匹配(2026-08-14 首版就这么写的,靠复查发现)。
# 三个平行数组 + unnest(a,b,c) 是 PG 的标准写法,三列一一对位。
#
# ⚠ 每个参数必须带 `::类型` 的事故史(连炸三次)见模块头注 —— 改动本段后
# 别信 pytest 绿,这类 SQL 的唯一验证手段是连库 dry-run 跑一次。
# 撤销 = **只删自己那一格**,全空才 withdrawn(2026-08-24 多来源支撑之后)。
# 旧写法按标量 source 整行撤:一行只能记一个来源,另一条链既撤不掉它、也不
# 知道自己那条理由还成不成立 —— 合并之后照旧写就会出现"维护链不再建议了,
# 但因为 source 记的是 audit,这行永远撤不掉"。
#
# ⚠ 用 jsonb_exists(...) 函数形式而不是 `?` 运算符:`?` 在若干驱动/工具链里
# 会被当占位符,炸的时候只在生产上炸。
# ⚠ 兼容存量:sources 还是 '{}' 的老行按标量 source 匹配(schema.sql 有一次性
# 回填,但别让这条 SQL 依赖它跑过 —— 漏回填的行会永远撤不掉且不报错)。
# ⚠ keep 比对用**该来源自己记的动作**:破坏组撞车时行上的 action 可能已被
# 升格(retire → delete),拿升格后的动作去比对方的 keep 清单必然对不上。
_WITHDRAW_SQL = """
UPDATE ops.dispositions d
SET sources = d.sources - %(source)s::text,
    status = CASE WHEN (d.sources - %(source)s::text) = '{}'::jsonb
                  THEN 'withdrawn' ELSE d.status END,
    settled_at = CASE WHEN (d.sources - %(source)s::text) = '{}'::jsonb
                      THEN now() ELSE d.settled_at END,
    detail = d.detail || jsonb_build_object('withdrawn_reason', %(why)s::text)
WHERE d.status = 'suggested'
  AND (jsonb_exists(d.sources, %(source)s::text)
       OR (d.sources = '{}'::jsonb AND d.source = %(source)s::text))
  AND (%(store)s::text IS NULL OR d.store = %(store)s::text)
  AND NOT (d.store = ANY(%(exclude)s::text[]))
  AND NOT EXISTS (
      SELECT 1 FROM unnest(%(stores)s::text[], %(skus)s::text[],
                           %(actions)s::text[]) AS k(store, sku, action)
      WHERE k.store = d.store AND k.sku = d.sku
        AND k.action = COALESCE(d.sources -> %(source)s::text ->> 'action',
                                d.action))
RETURNING d.id, d.status
"""


def withdraw_stale(conn, source: str, keep: list[tuple], why: str,
                   store: str | None = None,
                   exclude_stores: list[str] | None = None) -> int:
    """输入:连接 + 来源 + 本轮仍建议的 (店铺,SKU,动作) + 扫描范围 → 输出:撤销行数。

    exclude_stores(2026-08-26 店级重试标准③配套):**本轮没扫**的缺席店。
    它们的行既不在 keep 里(扫描件避让了),也**不许撤** —— 缺席 ≠ 恢复正常,
    撤了会把待执行建议记成「商品自己恢复正常了」(错误取证),下轮又重建。
    与 store 参数是两个正交的范围轴:store 答"这轮只扫了谁",
    exclude_stores 答"这轮谁没被扫到"。

    **建议是有时效的**:今天建议删 A,明天 A 自己恢复正常了、扫描件不再建议它
    —— 但昨天那条 suggested 行还挂着,执行件照样会删。这个函数把"本轮不再
    建议、但还挂着 suggested"的行置 withdrawn。

    只动**本来源那一格**(source 参数):扫描件那一轮不该碰审核来源的建议,
    反之亦然 —— 两个来源各跑各的闸,互相看不见对方为什么建议。
    合并行(两条链都在支撑)撤掉自己那一格之后**照旧待执行**,返回值不计它:
    报"已撤销"而另一条链还在建议、执行件照样会做,那是谎话。

    executing 及已落定的行一根手指都不碰:那些已经提交出去了,撤销无意义
    (feed 已经在沃尔玛队列里),它们的归宿是 settle() 按观测判决。

    ⚠ **store 参数是必需的安全边界,不是可选优化**(2026-08-14 加):
    撤销的判据是"不在本轮 keep 清单里",而扫描件支持 `-p store=X` 只扫一个店
    —— 那一轮的 keep 里只有该店的行,不限范围就会把**其余全部店铺**的待执行
    建议一次清空。调用方扫了哪个范围就必须传哪个范围;全量扫传 None。
    """
    with conn.cursor() as cur:
        if not keep:
            # 本轮一条都不建议 = 该来源(该范围内)的 suggested 全撤
            cur.execute(
                "UPDATE ops.dispositions d "
                "SET sources = d.sources - %(source)s::text, "
                "    status = CASE WHEN (d.sources - %(source)s::text) "
                "                       = '{}'::jsonb "
                "                  THEN 'withdrawn' ELSE d.status END, "
                "    settled_at = CASE WHEN (d.sources - %(source)s::text) "
                "                           = '{}'::jsonb "
                "                      THEN now() ELSE d.settled_at END "
                "WHERE d.status = 'suggested' "
                "  AND (jsonb_exists(d.sources, %(source)s::text) "
                "       OR (d.sources = '{}'::jsonb "
                "           AND d.source = %(source)s::text)) "
                "  AND (%(store)s::text IS NULL "
                "       OR d.store = %(store)s::text) "
                "  AND NOT (d.store = ANY(%(exclude)s::text[])) "
                "RETURNING d.status",
                {"source": source, "store": store,
                 "exclude": list(exclude_stores or [])})
            return sum(1 for (st,) in cur.fetchall() if st == "withdrawn")
        cur.execute(_WITHDRAW_SQL, {
            "source": source, "why": why, "store": store,
            "exclude": list(exclude_stores or []),
            "stores": [k[0] for k in keep],
            "skus": [k[1] for k in keep],
            "actions": [k[2] for k in keep]})
        # 报的是**真撤掉的**条数,不是"少了一个支撑来源"的条数:后者里有一批
        # 另一条链还在建议、执行件照样会做,说成"已撤销"就是谎话
        return sum(1 for _id, st in cur.fetchall() if st == "withdrawn")


def count_open(conn, status: str = "suggested",
               sources: tuple | None = None) -> int:
    """输入:连接(+状态/来源)→ 输出:库里该状态的建议行条数。

    ⚠ 与 suggest_many 的返回值**不是一回事**,摘要里别混用:
      suggest_many → 本轮**写了多少次**(每条 upsert 各算一次)
      count_open   → 库里**现在有多少条**
    两者会差,差额 = 同 (店铺,SKU,动作) 被多个来源命中、被部分唯一索引合并的
    条数(本轮实测 519 次写入 → 库里 470 条,差 49)。执行件领走的是后者,
    所以摘要要报的也是后者 —— 首版报前者,人对不上账。

    ⚠ **sources 要传自己那条链的**(2026-08-16 两条链共用本表之后):不传就把
    另一条链的待执行也算进来,摘要报的数和自家执行件领到的数对不上。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ops.dispositions WHERE status = %(st)s::text"
            " AND (%(sources)s::text[] IS NULL"
            "      OR jsonb_exists_any(sources, %(sources)s::text[])"
            "      OR (sources = '{}'::jsonb"
            "          AND source = ANY(%(sources)s::text[])))",
            {"st": status,
             "sources": list(sources) if sources is not None else None})
        return cur.fetchone()[0]


#: 来源 → 人读标签。多来源合并时给每条理由挂上出处,否则
#: 「标题相似度 62% | 审核判拒仍在架」读不出哪句话是谁说的。
# 破坏组压制维护组的**唯一实现**。为什么在 claim 而不在写入期:压制必须与
# 两个扫描件谁先跑无关(本仓吃过"顺序即语义"的亏),而写入期只能压住后写的
# 那一方。被压制的行留在 suggested 不撤 —— 删除若最终没生效,它们还在,
# 不用等下一轮扫描件重算。
_SUPPRESS_CLAUSE = """
  AND (d.action = ANY(%(destructive)s::text[]) OR NOT EXISTS (
        SELECT 1 FROM ops.dispositions x
        WHERE x.store = d.store AND x.sku = d.sku
          AND x.status IN ('suggested', 'executing')
          AND x.action = ANY(%(destructive)s::text[])))
"""

_CLAIM_SQL = """
SELECT d.id, d.store, d.sku, d.asin, d.source, d.action, d.category,
       d.reason, d.detail, d.sources
FROM ops.dispositions d
WHERE d.status = 'suggested'
  AND (%(actions)s::text[] IS NULL OR d.action = ANY(%(actions)s::text[]))
""" + _SUPPRESS_CLAUSE + """
ORDER BY d.store, array_position(%(rank)s::text[], d.action), d.suggested_at
"""

# 被压制了多少条 —— 必须报出来。静默压制读起来就是"今天没有维护建议",
# 而其实是"这批 SKU 都等着被删"(本仓口诀:静默的闸没人记得它关着)。
_SUPPRESSED_SQL = """
SELECT count(*)
FROM ops.dispositions d
WHERE d.status = 'suggested'
  AND (%(actions)s::text[] IS NULL OR d.action = ANY(%(actions)s::text[]))
  AND NOT (d.action = ANY(%(destructive)s::text[]))
  AND EXISTS (
        SELECT 1 FROM ops.dispositions x
        WHERE x.store = d.store AND x.sku = d.sku
          AND x.status IN ('suggested', 'executing')
          AND x.action = ANY(%(destructive)s::text[]))
"""

_MARK_SQL = """
UPDATE ops.dispositions
SET status = 'executing', feed_id = %(feed_id)s::text, executed_at = now(),
    executed_by = %(by)s::text
WHERE id = ANY(%(ids)s::bigint[]) AND status = 'suggested'
"""

SOURCE_LABEL = {"maint": "维护", "scan": "问题", "audit": "审核", "tro": "投诉"}


def _merge_view(row: dict) -> dict:
    """输入:一行建议(带 sources)→ 输出:同一行,reason/category 按全部来源现算。

    **单来源时逐字不变**(不加前缀):维护记录表的「原因」列、以及任何拿这段
    文本做匹配的地方,不该因为这次改造而变样。多来源才拼接,并按各来源写入
    时间排序 —— 08-19 那行的病根正是"只看得到后写的那一方"。
    """
    srcs = row.get("sources") or {}
    row["merged_from"] = tuple(sorted(srcs))
    if len(srcs) <= 1:
        return row
    ordered = sorted(srcs.items(),
                     key=lambda kv: (str((kv[1] or {}).get("at") or ""), kv[0]))
    parts = [f"{SOURCE_LABEL.get(src, src)}:{(v or {}).get('reason')}"
             for src, v in ordered if (v or {}).get("reason")]
    if parts:
        row["reason"] = " | ".join(parts)[:500]
    for _src, v in ordered:                 # 原因码取最早那个非空的
        if (v or {}).get("code"):
            row["category"] = v["code"]
            break
    return row


def claim(conn, actions: tuple | None = None) -> list[dict]:
    """输入:连接 + 本执行件能干的动作 → 输出:这些动作的 suggested 行。**只读**。

    领取与转态分开是有意的:提交 feed 可能失败、可能被在途防重拦下,只有
    真提交成功(拿到 feed_id)才该转 executing。先转态再提交 = 提交失败的行
    卡在 executing 永远等不到判决,而下轮扫描因部分唯一索引还建不出新建议。

    ⚠ **actions 必须传**(默认 None = 全领,只留给排查用)。传的是
    `PROBLEM_ACTIONS` / `MAINT_ACTIONS`,别在工作流里手写字符串。
    领错动作的后果是白炸一轮:维护链的 'price' 落进
    problem_product_cleanup.group_by_store 会直接抛,反过来 'delete' 落进
    维护执行件同理。库里的存量 'relist' 行不在任何领取集里,永远领不走
    (退役动作,见 ACTIONS 注)。

    ⚠ **按动作领,不按来源领**(2026-08-24 改)。旧口径按 source 领,后果见
    PROBLEM_ACTIONS 的注释:一条被两条链先后建议过的删除,归谁执行取决于
    "谁先落库",而表里显示的原因是"谁后覆写"。

    返回顺序 = (店铺, 动作优先级, 建议时间)。破坏组排在维护组前面,单店上限
    截断(cap_destructive)因此总是先保住优先级高的那些。

    ⚠ **破坏组压制维护组**:同一 SKU 挂着未落定的 delete/retire 时,它的
    title/price/inventory 行一条都不返回 —— 要删的东西没必要再花配额去
    改(批次 E 踩过:先花配额救活、再花配额删掉)。被压制的行留在 suggested
    不撤:删除若最终没生效,它们还在,不用等扫描件重算。压制了多少条由
    count_suppressed() 报,别让它静默。

    reason/category 由 sources 现算(见 _merge_view):多来源支撑时两条理由
    都要看得见,不能只显示后写的那一方。
    """
    with conn.cursor() as cur:
        cur.execute(_CLAIM_SQL, {
            "actions": list(actions) if actions is not None else None,
            "destructive": list(DESTRUCTIVE_ACTIONS),
            "rank": list(ACTION_ORDER)})
        cols = [d.name for d in cur.description]
        return [_merge_view(dict(zip(cols, r))) for r in cur.fetchall()]


def count_suppressed(conn, actions: tuple | None = None) -> int:
    """输入:连接 + 动作集 → 输出:因同 SKU 挂着破坏类建议而被压制的条数。

    **必须有人报它**:压制是静默的(claim 少返回几行,谁也不报错),不报的话
    摘要写着"没有待执行的维护建议",人会以为扫描件没算出东西来。
    """
    with conn.cursor() as cur:
        cur.execute(_SUPPRESSED_SQL, {
            "actions": list(actions) if actions is not None else None,
            "destructive": list(DESTRUCTIVE_ACTIONS)})
        return cur.fetchone()[0]


_EXECUTED_TODAY_SQL = """
SELECT store, count(*) FROM ops.dispositions
WHERE action = ANY(%(destructive)s::text[])
  AND executed_at IS NOT NULL
  AND executed_at > now() - make_interval(hours => %(hours)s::int)
GROUP BY store
"""


def destructive_executed_today(conn, hours: int = 20) -> dict[str, int]:
    """输入:连接(+窗口小时)→ 输出:{店铺: 窗口内已放行的破坏类条数}。

    「下架限制」是**按天**的语义,而 cap_destructive 只看单次运行 —— 同一天
    第二次运行(链尾重赛缺席店、人工重跑)会把每店上限翻倍(2026-08-24 归一
    消灭过"按来源翻倍",重赛把它换成"按轮次翻倍"带了回来)。本函数给
    cap_destructive 提供当日已放行数,把上限做成真·按天。窗口 20h 与
    drop_recent 同源(链一天一轮留余量)。已 executed 的行无论后来落定成
    什么状态都占额度 —— 配额花掉了就是花掉了。
    """
    with conn.cursor() as cur:
        cur.execute(_EXECUTED_TODAY_SQL, {
            "destructive": list(DESTRUCTIVE_ACTIONS), "hours": int(hours)})
        return {s: int(n) for s, n in cur.fetchall()}


def cap_destructive(rows: list[dict], caps: dict, default: int,
                    executed_today: dict[str, int] | None = None
                    ) -> tuple[list[dict], dict]:
    """输入:已领取的建议行 + {店铺:上限} + 缺省上限(+当日已放行数)→ 输出:(截后的行, {店铺:超额})。

    **单店删除上限的唯一施加点**(2026-08-24 归一)。此前两条链各自在扫描期
    按同一张限额表「下架限制」截一次 —— 应用两遍的结果是"每店最多 N 条"
    实际变成了最多 2N。现在扫描期一律不截(建议表如实反映待办),执行期截一次。

    executed_today(2026-08-26,链尾重赛配套):同店**当日**已放行的破坏类
    条数,先从上限里扣掉 —— 否则重赛/人工重跑一次,上限就翻一倍
    (取数 destructive_executed_today,执行件领取时查一次传入)。

    只截破坏组:维护三类(标题/价格/库存)不烧下架配额,不该被这个上限管。
    超出的**留在 suggested 不动**,下轮继续领 —— 丢弃会让它们永远轮不到。
    """
    used = dict(executed_today or {})
    kept, over, per_store = [], {}, {}
    for r in rows:
        if r["action"] not in DESTRUCTIVE_ACTIONS:
            kept.append(r)
            continue
        store = r["store"]
        cap = max(0, int(caps.get(store, default)) - int(used.get(store, 0)))
        per_store[store] = per_store.get(store, 0) + 1
        if per_store[store] > cap:
            over[store] = over.get(store, 0) + 1
            continue
        kept.append(r)
    if over:
        logger.warning("破坏类建议超单店上限(含当日已放行 %s),本轮留到下轮:%s",
                       {k: v for k, v in used.items() if k in over} or "0",
                       over)
    return kept, over


def group_by_store(rows: list[dict], *, key: str, order: tuple,
                   id_field: str) -> dict[str, dict]:
    """输入:领取到的行 + 分桶键名/桶内动作顺序/报错用 id 列名 → 输出:{店铺: {动作: [行]}}。

    纯函数,可测。两个执行件共用(2026-08-27 上移):maintenance 按
    `kind` × MAINT_ACTIONS 分桶,problem_product_cleanup 按 `action` ×
    (relist, retire, delete) 分桶 —— 同一个 `claim()` 出来的同一份数据的两种
    分桶写法,各写一份迟早只改一处(算法、判据、docstring 措辞本来就一字不差)。

    ⚠ **未知动作即抛,宁炸不吞**(conventions §三的安全闸,抽取时不许顺手改成
    静默丢弃):建议表里冒出一个不认识的动作 = 路由口径已经对不上了。静默丢掉
    它,那条建议每轮都会被领走又消失,`claim()` 的取件数与实际提交数长期对不上
    而两边都不报错 —— 破坏动作走的正是这条路。
    """
    out: dict[str, dict] = {}
    for r in rows:
        bucket = out.setdefault(r["store"], {k: [] for k in order})
        if r[key] in bucket:
            bucket[r[key]].append(r)
        else:               # 建议表里出现了不认识的动作:宁炸不吞
            raise ValueError(f"未知 {key}={r[key]!r}"
                             f"(建议行 id={r.get(id_field)})")
    return out


def mark_executing(conn, ids: list[int], feed_id, by: str = "") -> int:
    """输入:连接 + 建议行 id 列表 + feed_id + 执行者 → 输出:转态行数。

    `by` 是工作流名,落 executed_by 列 —— 建议合并之后"这条最终是谁干的"
    在库里必须有答案,否则又回到 08-19 那种从表面推不出执行者的状态。
    """
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(_MARK_SQL, {"ids": list(ids), "by": str(by or ""),
                                "feed_id": str(feed_id or "")})
        return cur.rowcount


def settle(conn) -> dict:
    """输入:连接 → 输出:{confirmed: n, ineffective: n}(本轮落定的建议行)。

    只登记**已有**的观测判决,不自己判生效(见模块头注)。还没等到
    catalog_sync 重新观测的行保持 executing,不落判 —— 与
    product_events.verify_deletions 的 'wait' 语义对齐。
    """
    out = {"confirmed": 0, "ineffective": 0}
    with conn.cursor() as cur:
        for sql in (_SETTLE_DELETE_SQL, _SETTLE_RELIST_SQL):
            cur.execute(sql)
            for (st,) in cur.fetchall():
                out[st] = out.get(st, 0) + 1
    if out["ineffective"]:
        logger.warning("处置建议落定:%d 条**未生效**(回执成功但观测显示没动)"
                       "——下轮扫描会重新建议", out["ineffective"])
    return out


# ── 维护链的落定(标题/价格/库存)────────────────────────────────────────────
# 与删除/反补不同,这三个动作**没有对应的核验事件**:catalog_sync 只是把新值
# 扫回 catalog.walmart_items,没人为"改价生效了"记一条事件。所以这里的判据就是
# 最朴素的那条 —— **重新观测之后,线上的值是不是我们要的值**。
#
# ⚠ 比对放在 Python 里做,不写进 SQL 的 CASE。理由写在本模块头注:同一段 SQL
# 因为 PG 推不出参数类型在生产上连炸三次,而 `(detail->>'new')::numeric` 这种
# 从 jsonb 里取值再强转,遇到一条脏 detail 就炸掉**整条 UPDATE**(不是跳过那
# 一行,是整轮落定失败)。取回来在 Python 里逐行比,脏行只影响它自己。
_MAINT_OPEN_SQL = """
SELECT d.id, d.action, d.detail, w.price, w.avail_qty, w.product_name
FROM ops.dispositions d
JOIN catalog.walmart_items w ON w.store = d.store AND w.sku = d.sku
WHERE d.status = 'executing'
  AND d.action = ANY(%(actions)s::text[])
  AND w.last_seen_at > d.executed_at + make_interval(hours => %(grace)s::int)
"""

# 落定宽限(2026-08-26,链尾重赛引入的时间压缩):此前"提交"与"下一次重新
# 观测"天然隔 16~24 小时,判据只需 last_seen_at > executed_at;链尾重赛把
# 这个间隔压到十几分钟 —— 主链 14:50 刚提交的 MP_MAINTENANCE 还在沃尔玛
# 队列里,15:05 重赛的 catalog_sync 刷新观测、15:15 重赛的 maintenance 落定,
# 会把"太早看"谎报成 ineffective(销案后行卡 executing,该 SKU 白丢一天)。
# 2 小时 > 价格 feed SLA(15 分钟)与 MP_MAINTENANCE 常规处理时长;正常
# 隔日节奏(16~24h)完全不受影响。破坏侧本就有 46h 宽限(verify_deletions)。
MAINT_SETTLE_GRACE_HOURS = 2

_MAINT_SETTLE_SQL = """
UPDATE ops.dispositions
SET status = %(status)s::text, settled_at = now(),
    detail = detail || jsonb_build_object('settled_by', %(by)s::text)
WHERE id = ANY(%(ids)s::bigint[]) AND status = 'executing'
"""

# 改价比对的容差:沃尔玛回读的价格是 numeric,我们算出来的是 float,
# 0.01 的一半足够区分"改过来了"和"没改"
_PRICE_EPS = 0.005


def maint_effective(action: str, want, price, qty, name) -> bool:
    """输入:动作 + 建议的新值 + 线上现值三件 → 输出:是否已生效。纯函数,可测。

    ⚠ 值转不动(None / 脏数据)一律判**未生效**,不判生效:判错成生效的后果是
    这条建议被销案、下轮不再建议,那个商品就永远停在错的值上而且没人知道。
    """
    if action == "price":
        try:
            return want is not None and abs(float(price) - float(want)) < _PRICE_EPS
        except (TypeError, ValueError):
            return False
    if action == "inventory":
        try:
            return int(qty) == int(want)
        except (TypeError, ValueError):
            return False
    return bool(name) and str(name) == str(want or "")


def settle_maintenance(conn) -> dict:
    """输入:连接 → 输出:{confirmed: n, ineffective: n}(维护三动作的落定)。

    只判**提交后过了宽限期、且已被 catalog_sync 重新观测过**的行
    (w.last_seen_at > d.executed_at + 2h,见 MAINT_SETTLE_GRACE_HOURS)。
    没重新观测的保持 executing —— 拿提交前的旧快照判,永远判成"没生效";
    观测得太早同理 —— feed 还在沃尔玛队列里,判了就是把"太早看"报成"没执行"。
    """
    with conn.cursor() as cur:
        cur.execute(_MAINT_OPEN_SQL, {"actions": list(MAINT_ACTIONS),
                                      "grace": int(MAINT_SETTLE_GRACE_HOURS)})
        rows = cur.fetchall()
    ok, bad = [], []
    for rid, action, detail, price, qty, name in rows:
        want = (detail or {}).get("new")
        (ok if maint_effective(action, want, price, qty, name) else bad).append(rid)
    with conn.cursor() as cur:
        for ids, status, by in ((ok, "confirmed", "observed"),
                                (bad, "ineffective", "value_unchanged")):
            if ids:
                cur.execute(_MAINT_SETTLE_SQL,
                            {"ids": ids, "status": status, "by": by})
    if bad:
        logger.warning("维护建议落定:%d 条**未生效**(catalog_sync 重新扫过,"
                       "线上值仍不是我们提交的值)——下轮扫描会重新建议", len(bad))
    return {"confirmed": len(ok), "ineffective": len(bad)}


# 超期放行。**这条不是洁癖,是防死锁**:部分唯一索引只允许同 (店铺,SKU,动作)
# 有一条未落定行,executing 行永远不落定 = 那个 SKU 的那类维护**永久停摆**,
# 而且完全静默(扫描件每轮算出意图,upsert 撞索引写不进去,rowcount 0)。
# 等不到观测的常见原因:商品下架了(walmart_items 缺席,JOIN 不上)、
# catalog_sync 连着几轮没扫到那家店。
_EXPIRE_SQL = """
UPDATE ops.dispositions
SET status = 'ineffective', settled_at = now(),
    detail = detail || jsonb_build_object('settled_by', 'expired')
WHERE status = 'executing'
  AND action = ANY(%(actions)s::text[])
  AND executed_at < now() - make_interval(days => %(days)s::int)
RETURNING id
"""

EXPIRE_DAYS = 3         # 维护按日跑,3 天还没等到观测就别再等了


def expire_executing(conn, actions: tuple = MAINT_ACTIONS,
                     days: int = EXPIRE_DAYS) -> int:
    """输入:连接(+动作集合/天数)→ 输出:超期放行的行数。

    只放行**本链自己的动作**:删除类有它自己的 48h 宽限 + 观测判定
    (product_events.verify_deletions),不该被这条粗暴的时限抢先判掉。
    """
    with conn.cursor() as cur:
        cur.execute(_EXPIRE_SQL, {"actions": list(actions), "days": int(days)})
        n = len(cur.fetchall())
    if n:
        logger.warning("处置建议超期放行 %d 条(超 %d 天没等到 catalog_sync "
                       "重新观测;不放行会把这些 SKU 的该类维护永久堵住)", n, days)
    return n


_STUCK_SQL = """
SELECT store, action, count(*) AS n, min(executed_at) AS oldest
FROM ops.dispositions
WHERE status = 'executing'
  AND executed_at < now() - make_interval(days => %(days)s::int)
  AND (%(sources)s::text[] IS NULL
       OR jsonb_exists_any(sources, %(sources)s::text[])
       OR (sources = '{}'::jsonb AND source = ANY(%(sources)s::text[])))
GROUP BY store, action
ORDER BY n DESC, store
"""


def stuck_executing(conn, sources: tuple | None = None,
                    days: int = EXPIRE_DAYS) -> list[dict]:
    """输入:连接(+来源/天数)→ 输出:卡在 executing 超期的行,按 (店铺,动作) 聚合。

    **只读,只为了让人看见**——不改状态、不放行(放行归 expire_executing)。

    为什么必须有人报它:部分唯一索引 `dispositions_open_uidx` 约束的是
    `status IN ('suggested','executing')`,所以一条 executing 会**挡住**同
    (店铺, SKU, 动作) 的任何新建议。它靠观测落定,而观测全部来自 catalog_sync
    —— **店铺不被扫,观测就永远不来**(凭证坏掉的店正是这样:换 token 400 →
    catalog_sync 跳过它 → 它的 executing 行等不到判决)。于是那些 SKU 的这类
    处置永久停摆,而扫描件每轮照常报"建议 N 条",**完全看不出少了谁**。

    ⚠ 两条链在放行上不对称,这是有意的:维护三类由 `expire_executing` 3 天
    放行;删除/反补**不自动放行**(它们有自己的观测判定,粗暴时限会抢先判掉
    真正在途的删除)。所以问题商品链尤其需要这一句 —— 它没有任何兜底,
    卡住就是一直卡着。自动放行也不是好答案:店铺是死的,放行 → 下轮重新建议
    → 执行件再提交一次 feed → 凭证坏着照样失败,白烧配额。卡着是**真有原因**
    卡着,该做的是让人看见。
    """
    with conn.cursor() as cur:
        cur.execute(_STUCK_SQL,
                    {"days": int(days),
                     "sources": list(sources) if sources is not None else None})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def stuck_note(rows: list[dict], days: int = EXPIRE_DAYS) -> str:
    """输入:stuck_executing 的返回 → 输出:摘要用的一行(没卡住返回空串)。

    两个扫描件共用同一句措辞 —— 各写一份迟早一边改了另一边没改。
    """
    if not rows:
        return ""
    total = sum(r["n"] for r in rows)
    by_store = ",".join(f"{r['store']}×{r['n']}" for r in rows[:8])
    more = f" 等 {len(rows)} 组" if len(rows) > 8 else ""
    return (f"⚠ {total} 条建议卡在 executing 超 {days} 天({by_store}{more}):"
            f"这些 (店铺,SKU,动作) **本轮建不出新建议**(部分唯一索引挡着)。"
            f"成因多为该店没被 catalog_sync 扫到(凭证坏了?)⇒ 观测永远不来。"
            f"查:SELECT * FROM ops.dispositions WHERE status='executing' "
            f"AND executed_at < now() - interval '{days} days'")
