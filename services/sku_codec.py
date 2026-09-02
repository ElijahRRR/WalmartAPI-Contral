"""12 位不透明 SKU 的编码规则与码的生命周期(**编码规则的唯一之家**)。

⚠ 本模块在 **SKU 改造批次 0a 只建不接线**,调用点在批次 2(mint 在 list_new /
match_listing 的预备期,abandon 在四个弃码点)。conventions §五说得明白:
「从未跑过、但在批次待办里」= 活,不是死代码 —— 下一次死代码盘点别把它判死。

码长什么样(sku_plan §2 定稿 2026-09-02):

    <来源字母><11 位随机码>       共 12 位,无分隔符
    AK7QM2X9RT4W                  A = amz(字母映射在 registry.SKU_SOURCE_LETTERS)

五件必须钉死的事:

① **「码弃用」≠「沃尔玛 lifecycle RETIRED」≠「product_clear 停用」** —— 三个
   同名异义,登记簿的列名故意用 abandoned 不用 retired。abandoned_at IS NULL
   的行叫**活码**;码的寿命 = 沃尔玛侧那条 (店, SKU) 记录对我们还有用的寿命,
   不是上架/下架次数。

② **弃码点只有四个**(sku_plan §5.3):
     1. DELETE 经 catalog_sync 观测核验(`delete_verified`)—— 不是回执:
        「回执成功但后台没删」是所有者实证过的故障模式,按回执弃码 =
        下次拿新码新 UPC 去上一个还活着的 item = 同店重复 listing;
     2. SKU_LOCKED 自愈链 RETIRE 回执成功 + 冷却期满(唯一绑回执的弃码点:
        锁死的 SKU 可能从未进过 walmart_items,无观测可等);
     3. UPC 撞库 ERR_EXT_DATA_0101119,码与 UPC 一起换(决策 B);
     4. 改码 SkuUpdate 经观测确认后,旧行 abandoned_at + replaced_by。
   **其余一切「下架」都不弃码**:product_clear 停用(RETIRE)、库存归零、缺席
   missing_since、被沃尔玛 unpublish、提交失败/被拒/Unknown/PROHIBITED ——
   沃尔玛侧记录仍在、仍绑着我们的 UPC,抽新码 = 同店两条同内容记录 + 白烧一个
   UPC。反向清单(守门测试钉住):product_clear / problem_product_cleanup /
   maintenance / catalog_sync.mark_missing / feed_track **不得**调 abandon。

③ **消费方契约**:resolve / 维护链 JOIN / 事件归并 / 订单反查一律**不按
   abandoned_at 过滤**(旧码带着订单、售后回来时必须还查得到)。全仓 .py 里
   `abandoned_at IS NULL` 只允许出现在三处:本模块的 mint、list_new 去重闸、
   alloc_push._SQL_ONLINE(批次 3 起增 sku_migrate 的候选选取为第四处)。
   refdata/schema.sql 的部分索引条件是 DDL,不计入这张白名单。

④ **本模块是 12 位不透明码编码规则的唯一之家**:字母表 / 长度 / 随机段长 /
   重抽上限 / 占位码 / is_opaque 判据都在这里出生。registry 只登记
   SKU_SOURCE_LETTERS(所有者要拍的取值,属外部配置);schema.sql 两条部分唯一
   索引的字符类由 tests/test_sku_guard.py 与本模块常量逐字对齐。**任何人在别处
   再抄一份即违规**(三处并存会配出互斥的守门断言,谁也绿不了)。

⑤ 登记簿 catalog.listing_sources 的 INSERT 只有两个合法出口:
   services/listing_sources.register(backfill 与跟卖人工号的首次登记)与本模块的
   mint 家族;abandoned_at / abandoned_reason / replaced_by / replaces /
   replaced_at 五列**只准本模块写**(abandon / mint_replacement /
   settle_replacement 三个函数,别处一律不得 UPDATE 登记簿)。

⑥ **改码(批次 3)的三个动作各有一个函数,状态只有三态**:
     mint_replacement    —— 先落库:新码行 replaces=旧码 + 旧行 replaced_by=新码
                            (pending;同一事务,commit 归调用方)
     settle_replacement('confirmed')   —— 观测确认:旧行 abandon('sku_update'),
                            **不烧 UPC**(GTIN 还挂在同一个 item 上,烧了下次
                            claim 会去领新号,等于自造 SKU_LOCKED)
     settle_replacement('rolled_back') —— 观测反证/回执失败:清旧行 replaced_by,
                            新码 abandon('sku_update_failed')
   三个函数都**只动身份与病历**;UPC 改标、上架表 SKU 列、处置建议、节点库存归
   调用方在同一事务里各调各的积木(本模块不做跨域编排)。
"""

import logging
import secrets

from registry import resources
from services import listing_sources, product_events, upc_pool

logger = logging.getLogger("services.sku_codec")

# ── 编码规则常量(**全仓唯一之家**,别处不许再抄)────────────────────────────
#: 30 个符号:剔掉 0/O、1/I/L(手抄与 OCR 的经典混淆对)与 U(避免脏词)。
#: schema.sql 两条部分唯一索引的字符类与本常量**逐字相同**,守门测试钉住。
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_LEN = 11                # 随机段位数;空间 30^11 ≈ 1.77×10^16
_LEN = 12                       # 来源字母 1 位 + 随机段 11 位
_MAX_DRAWS = 5                  # 撞码重抽上限:仍撞 = 随机源坏了,不是运气
#: 空跑占位码:12 位但含 `0`(不在字母表里)⇒ is_opaque 恒 False,永远不会被
#: 当成真码,也永远落不进那两条部分唯一索引。与 list_new 的 UPC 占位同纪律。
DRYRUN_PLACEHOLDER = "DRYRUN000000"
#: `is_opaque()` 在 SQL 侧的**等价表达,唯一出处**(批次 3 地基)。
#: 由 `_ALPHABET` 与 `_LEN` **派生**,不是手打的第二份正则 —— 手打就是第二个
#: 字母表之家,而两份一漂,索引/SQL 判据与 Python 判据会对「什么是新码」给出
#: 不同答案,且全程不报错。消费方这样用:
#:     OPAQUE_SQL_PREDICATE.format(col="w.sku")
#: 拼进去的是我们自己的常量(不是外部输入),无注入面。
#: 任何 .py 里再出现第二处 12 位字符集正则即违规(守门 tests/test_sku_guard.py);
#: refdata/schema.sql 两条部分唯一索引的条件与本常量逐字同源,由守门测试对齐。
OPAQUE_SQL_PREDICATE = (
    "({col} ~ '^[" + _ALPHABET + "]{{" + str(_LEN) + "}}$' AND {col} ~ '[A-Z]')"
)

# ── 码的生命周期常量(**全仓唯一之家**;workflows 只许写常量名,不写字面量)──
#: 退役后的冷却小时数。官方无明文,取旧仓实证(legacy_survey.md:1649):
#: RETIRE 回执成功之后沃尔玛侧那条记录不是立刻可复用,不等就重上必然再失败。
#: 两个消费方共用同一个数:SKU_LOCKED 自愈链(RETIRE→冷却→清列重上)与
#: list_new 的退役冷却闸。**此前它长在 sku_locked_heal 的 params 默认值里**,
#: 泛化成闸门后必然出现第二份,两份一漂就没人说得清冷却到底几小时。
RETIRE_COOLDOWN_HOURS = 24
#: 同 (店, 来源, 源头键) 允许的**已弃码代数上限**:达到它就不再自动换码重上,
#: 交人工看。堵的是「弃码 → 新码 → 再弃码」这个闭环 —— 每转一圈白烧一个 UPC
#: 与一个 MP_ITEM 配额名额,而且每换一次码,重试上限/在途防重/原号复用三条
#: 护栏都跟着重新计数(护栏跟码走,见 list_new._SQL_ATTEMPTS 头注)。
MAX_SKU_GENERATIONS = 3

# ── 弃码原因词表(**弃码点仍然只有四个**;新增即改动弃码点的定义)──────────────
ABANDON_DELETE_VERIFIED = "delete_verified"   # DELETE 经观测核验
ABANDON_SKU_LOCKED = "sku_locked"             # SKU_LOCKED 自愈退役 + 冷却期满
ABANDON_UPC_CONFLICT = "upc_conflict"         # ERR_EXT_DATA_0101119 撞库(决策 B)
ABANDON_SKU_UPDATE = "sku_update"             # 改码 SkuUpdate 观测确认后的旧行
#: 第五个**原因**,但**不是第五个弃码点**(批次 3 地基):改码没成(回执失败或
#: 观测反证)时,把那个从未上过沃尔玛的**新码**弃掉。它弃的是我们自己刚抽的码,
#: 不是沃尔玛侧任何活着的记录 —— 「弃码点只有四个」那条纪律说的是"哪些情形
#: 允许让一条**在架记录**的码退休",与本原因是两件事。
#: 为什么弃而不是删行:登记簿的行**永不 DELETE**,而全局 UNIQUE(sku) 含已弃码行,
#: 弃掉的码从此不会被任何人抽到 —— 这正是"码是免费的、失败就换一个"的落地方式。
ABANDON_SKU_UPDATE_FAILED = "sku_update_failed"
ABANDON_REASONS = frozenset({
    ABANDON_DELETE_VERIFIED, ABANDON_SKU_LOCKED,
    ABANDON_UPC_CONFLICT, ABANDON_SKU_UPDATE, ABANDON_SKU_UPDATE_FAILED,
})

#: 弃码原因 → 烧号状态(批次 2 接线,决策 D:唯一写入函数 upc_pool.burn)。
#: **在表里 = 这个原因要烧号**,状态值由本表给,别处不许写状态字面量:
#:   delete_verified —— DELETE 经观测核验,号随码一起死(burned_delete);
#:   sku_locked      —— SKU_LOCKED 自愈退役,不烧就会被 claim 原号复用回来,
#:                      "清列重上领新号"成空话(burned_lock);
#:   upc_conflict    —— 撞库(决策 B:码与 UPC 一起换)。状态仍写 conflict,
#:                      因为那个值的语义就是「全站已存在该 UPC」—— 写成
#:                      burned_* 反而把"是谁先占了号"这条排障线索盖掉。
#: 不在表里的两个:
#:   sku_update        —— 码换了但 item 还在、UPC 还绑着,烧号等于白烧。
#:   sku_update_failed —— 被弃的是那个从未上过沃尔玛的新码,它名下从来没有号。
_BURN_STATUS = {
    ABANDON_DELETE_VERIFIED: upc_pool.BURN_DELETE,
    ABANDON_SKU_LOCKED: upc_pool.BURN_LOCK,
    ABANDON_UPC_CONFLICT: upc_pool.CONFLICT,
}

# 模块级计数(排障用:两个分支各记各的,合成一条就分不清并发与随机源故障)
_concurrent_mint = 0            # 撞的是别的进程刚发的活码键 ⇒ 复用对方的码
_sku_redraws = 0                # 撞的是主键/全局唯一 ⇒ 真·随机撞码,重抽

#: 复用查询:同 (店, 来源, 源头键) 已有活码就复用它。**不按形态过滤** ——
#: 存量 sku=asin 的活行照样复用;条件与 listing_sources_live_uidx /
#: listing_sources_live_key_idx 的局部条件逐字对齐。
#: ⚠ `AND replaced_by IS NULL` 不是可选项(批次 3):在途改码期间,同一个
#: (店, 来源, 源头键) 会**同时**有旧行(replaced_by 非空)与新码行(活)两条。
#: 不带这一条,list_new 会拿回那个已经宣告要退休的旧码,再发一次 MP_ITEM。
#: **索引条件与本查询条件必须逐字相同,改一处必须改另一处** —— 不同的话,
#: 「索引允许插入的状态,代码查得出两行」,而这类不一致在本仓一律是静默的
#: (守门 tests/test_sku_guard.py::test_mint_live_row_filter_matches_the_index_condition)。
_SQL_LIVE = """
SELECT sku FROM catalog.listing_sources
WHERE store = %s AND source_type = %s AND source_key = %s
  AND abandoned_at IS NULL AND replaced_by IS NULL
ORDER BY created_at LIMIT 1
"""

#: 抽码与登记**同一函数同一事务**——不存在「抽了没登记」这种中间态。
#: ON CONFLICT 不带 target 是故意的:要同时兜住主键 (store, sku) 与全局唯一
#: listing_sources_opaque_sku_uidx 两种冲突,而 target 只能指一个。
#: 拿不到行之后由调用处分两个明确分支各记各的(见 mint)。
_SQL_MINT = """
INSERT INTO catalog.listing_sources (store, sku, source_type, source_key, workflow)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING sku
"""

#: 改码抽码:与 mint 的 INSERT 同款(ON CONFLICT 不带 target 兜住主键与全局唯一
#: 两种冲突),只多一列 replaces —— 新码行从出生起就指着它替换的那个旧码。
#: 认领唯一索引的条件是 (store, replaces) WHERE replaces IS NOT NULL AND
#: abandoned_at IS NULL:回滚作废的新码行留着 replaces 当病历,但不再占认领位。
_SQL_MINT_REPLACEMENT = """
INSERT INTO catalog.listing_sources
    (store, sku, source_type, source_key, workflow, replaces)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING sku
"""

#: 旧行的现状:一次查清"能不能改"与"是不是已经在改了"(幂等的判据)。
_SQL_OLD_ROW = """
SELECT replaced_by, abandoned_at, source_type, source_key
FROM catalog.listing_sources WHERE store = %s AND sku = %s
"""

#: 在途改码的既有新码:旧行指着谁、那个码还活着 ⇒ 直接返回它(崩溃重入不换码)。
_SQL_PENDING_NEW = """
SELECT n.sku FROM catalog.listing_sources o
JOIN catalog.listing_sources n ON n.store = o.store AND n.sku = o.replaced_by
WHERE o.store = %s AND o.sku = %s
  AND o.replaced_by IS NOT NULL AND o.abandoned_at IS NULL
  AND n.abandoned_at IS NULL
"""

#: 旧行进入 pending:只动"活着且还没在改"的行,返 0 行 = 前提没了 ⇒ 调用方 fail loud。
_SQL_MARK_REPLACED = """
UPDATE catalog.listing_sources
SET replaced_by = %s, replaced_at = now()
WHERE store = %s AND sku = %s AND abandoned_at IS NULL AND replaced_by IS NULL
RETURNING sku
"""

#: 回滚:把旧行从 pending 放回活码(只清自己那一对指针,不碰别人的)。
_SQL_CLEAR_REPLACED = """
UPDATE catalog.listing_sources
SET replaced_by = NULL, replaced_at = NULL
WHERE store = %s AND sku = %s AND replaced_by = %s
RETURNING sku
"""

#: 弃码:只动活行(abandoned_at IS NULL),返 0 行 = 已弃或不存在 ⇒ 幂等 no-op。
_SQL_ABANDON = """
UPDATE catalog.listing_sources
SET abandoned_at = now(), abandoned_reason = %s,
    replaced_by = coalesce(%s, replaced_by)
WHERE store = %s AND sku = %s AND abandoned_at IS NULL
RETURNING source_type, source_key
"""


def is_opaque(sku) -> bool:
    """输入:任意 sku → 输出:是不是本系统抽的 12 位不透明码。

    三个条件缺一不可:长度 12、每一位都在字母表里、**至少含一个 A-Z 字母**。
    最后那条不能省:12 位纯数字的沃尔玛 item id 若恰好不含 0/1,前两条会放它
    过去,于是一个平台侧编号被当成我们抽的码。schema.sql 两条部分唯一索引的
    条件与这三条逐字对齐(守门测试钉住)。
    """
    s = str(sku or "").strip().upper()
    return (len(s) == _LEN
            and all(ch in _ALPHABET for ch in s)
            and any("A" <= ch <= "Z" for ch in s))


def source_of(sku) -> str | None:
    """输入:sku → 输出:来源 source_type(不是新码/字母未登记一律返 None)。

    只认首位字母,且只对不透明码回答 —— 存量 SKU 的首位字母什么都不代表。
    字母表未定或首字母没登记时返 None,**不猜**(缺省不猜是安全红线)。
    """
    if not is_opaque(sku):
        return None
    letter = str(sku).strip().upper()[0]
    for source_type, mapped in resources.SKU_SOURCE_LETTERS.items():
        if mapped == letter:
            return source_type
    return None


def _live_sku(cur, store: str, source_type: str, source_key) -> str | None:
    """输入:游标 + (店, 来源, 源头键) → 输出:该品当前的活码(没有返 None)。"""
    cur.execute(_SQL_LIVE, (store, source_type, source_key))
    row = cur.fetchone()
    return row[0] if row else None


def mint(conn, store: str, source_type: str, source_key, *, workflow: str) -> str:
    """输入:连接 + 店铺 + 来源 + 源头键 → 输出:该品的活码(复用或新抽,同事务登记)。

    ① 先查活行:有就复用同一码 —— 依据是沃尔玛官方约束「一个 Product ID 只能挂
       一个 SKU」(抽新码去上同一个 Product ID 必撞),不是「同码重发能复活」。
    ② 没有:按 registry.SKU_SOURCE_LETTERS 取来源字母 + 11 位密码学随机段,
       INSERT 并 RETURNING。**抽码与登记同一次调用同一事务**。
    ③ INSERT 拿不到行时分两个明确分支(conventions §六真兜底三要件,不做
       catch-all):(a) 重跑一次 ① 查到 ⇒ 另一个进程刚给同一个品发了码,返回
       对方那个码(warning + _concurrent_mint;这与 mint 的复用语义一致,不是
       失败);(b) 仍查不到 ⇒ 撞的是主键或全局唯一索引 = 真·随机撞码,
       info + _sku_redraws 后重抽,至多 _MAX_DRAWS 次仍撞抛 RuntimeError。
       合成一条会把并发双 mint 诊断成随机源故障,排障方向全错。

    **没有 dry_run 形参**:写库函数不设「这次不写」模式(conventions §六)。
    空跑由调用方决定不调 mint、直接用 DRYRUN_PLACEHOLDER 表达。
    """
    global _concurrent_mint, _sku_redraws
    letter = resources.SKU_SOURCE_LETTERS.get(source_type)
    with conn.cursor() as cur:
        live = _live_sku(cur, store, source_type, source_key)
        if live:
            return live
        if not letter:
            raise ValueError(
                f"source_type={source_type!r} 没有来源字母:先在 "
                f"registry.resources.SKU_SOURCE_LETTERS 登记(缺省不猜)")
        if letter not in _ALPHABET or not letter.isalpha():
            raise ValueError(
                f"来源字母 {letter!r} 不合法:必须取自 services.sku_codec._ALPHABET "
                f"里的字母(纯数字首位会让 is_opaque 判否)")
        for _ in range(_MAX_DRAWS):
            code = letter + "".join(secrets.choice(_ALPHABET)
                                    for _ in range(_RANDOM_LEN))
            cur.execute(_SQL_MINT, (store, code, source_type, source_key, workflow))
            row = cur.fetchone()
            if row:
                return row[0]
            other = _live_sku(cur, store, source_type, source_key)
            if other:
                _concurrent_mint += 1
                logger.warning(
                    "并发 mint:%s/%s/%s 已被另一进程发码 %s,复用之(累计 %d 次)",
                    store, source_type, source_key, other, _concurrent_mint)
                return other
            _sku_redraws += 1
            logger.info("随机撞码重抽(%s 已被占用,累计 %d 次)", code, _sku_redraws)
    raise RuntimeError(
        f"连抽 {_MAX_DRAWS} 次都撞码({store}/{source_type}/{source_key}):"
        f"30^{_RANDOM_LEN} 的空间下这不是运气问题,查随机源")


def _pending_new_sku(cur, store: str, old_sku: str) -> str | None:
    """输入:游标 + (店, 旧码) → 输出:在途改码的既有新码(没有返 None)。"""
    cur.execute(_SQL_PENDING_NEW, (store, old_sku))
    row = cur.fetchone()
    return row[0] if row else None


def mint_replacement(conn, store: str, old_sku: str, source_type: str,
                     source_key, *, workflow: str = "sku_migrate") -> str:
    """输入:连接 + 店 + 旧码 + 出身 → 输出:新抽的不透明码(同事务写完两条指针,先落库)。

    ① **幂等**:该 (店, 旧码) 的登记行已有 replaced_by 且那个新码行还活着 ⇒ 直接
       返回既有新码,**不换码**。这是「防重状态先落库再调接口」的必然要求:
       pending 行已经写了、feed 还没发就崩了,下轮必须拿回同一个码 —— 换码 =
       载荷变了 = feeds.payload_key 防重不命中 = 同一个 item 被改两次码。
    ② 否则:抽码(复用 mint 的字母表 / 随机段 / 重抽上限)→ INSERT 新行
       {store, sku=新码, source_type, source_key, workflow, replaces=旧码}
       → UPDATE 旧行 SET replaced_by=新码, replaced_at=now()。
    ③ 全程**同一个 conn 同一事务**:不自己开连接、不自己 commit(commit 归调用方 ——
       调用方必须在调 feeds 之前把这个事务提交掉,那才叫"先落库再调接口")。
    ④ **不复用活行**(与 mint 的语义正好相反,这正是它单独一个函数而不是给 mint
       加开关的原因:一个能力一条实现路径,一个函数一种语义)。
    ⑤ INSERT 拿不到行时分两个明确分支(与 mint 同款,不做 catch-all):
       (a) 重跑一次 ① 查到 ⇒ 另一个进程刚给同一个旧码发了新码,返回**对方那个码**;
       (b) 仍查不到 ⇒ 撞的是主键/全局唯一 = 真·随机撞码,重抽,至多 _MAX_DRAWS 次。
       两支的正确处置完全相反,一锅端会把并发误诊成"随机源坏了"。

    前提(不满足一律 fail loud,不猜):旧码必须已登记、必须还是活码。
    ⚠ 射程只有**一跳**:旧码改码后立即弃码、永不再改码(catalog.sku_aliases 的
    继承链就是按一跳写的)。对一个不透明活码再改一次码不在本函数射程内。
    """
    global _concurrent_mint, _sku_redraws
    letter = resources.SKU_SOURCE_LETTERS.get(source_type)
    with conn.cursor() as cur:
        cur.execute(_SQL_OLD_ROW, (store, old_sku))
        old = cur.fetchone()
        if not old:
            raise ValueError(
                f"旧码 {store}/{old_sku} 不在登记簿里:改码只能改**登记过出身**的码"
                f"(先 sources_backfill 补登记,缺省不猜)")
        if old[1] is not None:
            raise ValueError(
                f"旧码 {store}/{old_sku} 已弃码(reason 见 abandoned_reason):"
                f"弃了的码不再改码 —— 沃尔玛侧那条记录我们已经当它不存在了")
        pending = _pending_new_sku(cur, store, old_sku)
        if pending:
            return pending
        if old[0]:
            raise RuntimeError(
                f"旧码 {store}/{old_sku} 的 replaced_by={old[0]!r} 指向一个已弃码/"
                f"不存在的行:状态自相矛盾,交人工(自动清指针会把真在途的改码抹掉)")
        if not letter:
            raise ValueError(
                f"source_type={source_type!r} 没有来源字母:先在 "
                f"registry.resources.SKU_SOURCE_LETTERS 登记(缺省不猜)")
        for _ in range(_MAX_DRAWS):
            code = letter + "".join(secrets.choice(_ALPHABET)
                                    for _ in range(_RANDOM_LEN))
            cur.execute(_SQL_MINT_REPLACEMENT,
                        (store, code, source_type, source_key, workflow, old_sku))
            row = cur.fetchone()
            if row:
                cur.execute(_SQL_MARK_REPLACED, (code, store, old_sku))
                if not cur.fetchone():
                    raise RuntimeError(
                        f"旧行 {store}/{old_sku} 在同一事务里变成了不可改状态"
                        f"(被弃码或已被别人改码):本次改码作废,让事务回滚")
                return code
            other = _pending_new_sku(cur, store, old_sku)
            if other:
                _concurrent_mint += 1
                logger.warning(
                    "并发改码:%s/%s 已被另一进程发新码 %s,复用之(累计 %d 次)",
                    store, old_sku, other, _concurrent_mint)
                return other
            _sku_redraws += 1
            logger.info("随机撞码重抽(%s 已被占用,累计 %d 次)", code, _sku_redraws)
    raise RuntimeError(
        f"连抽 {_MAX_DRAWS} 次都撞码({store}/{old_sku} 的替换码):"
        f"30^{_RANDOM_LEN} 的空间下这不是运气问题,查随机源")


def settle_replacement(conn, store: str, old_sku: str, new_sku: str,
                       verdict: str, reason: str = "") -> None:
    """输入:连接 + 新旧码 + 判词 → 输出:无(同事务把身份两端一次改到位)。

    verdict ∈ {'confirmed', 'rolled_back'},两支都**幂等**(已定案再调 = no-op):

      confirmed  —— 观测确认(新码在架且旧码缺席):旧行走**唯一的弃码出口**
        `abandon(reason='sku_update')`(**不烧 UPC**:GTIN 仍挂在同一个 item 上,
        烧了下次 claim 会去领新号 = 自造 SKU_LOCKED),再给**新码**记一条
        `sku_replaced` 出生事件 —— 新码在事件账本里一条历史都没有,不给它记,
        「这个 ASIN 用过哪些码」的时间线上就只有旧码单方面的一条。
      rolled_back —— 回执失败或观测反证:清掉旧行的 replaced_by/replaced_at
        (旧行回到活码,下一轮可以重来),新码走同一个 abandon 出口弃掉
        (reason='sku_update_failed';新码从未配 UPC,烧号分支天然空转)。

    **本函数只动身份与病历**:UPC 改标(upc_pool.retag_sku)、上架表 SKU 列、
    处置建议迁键、节点库存清行,全部归调用方在同一事务里各调各的积木 ——
    弃码只有一个实现,跨域编排不是弃码的一部分。
    """
    if verdict not in ("confirmed", "rolled_back"):
        raise ValueError(
            f"未登记的改码判词 {verdict!r}:只有 confirmed / rolled_back 两支"
            f"(stalled 是过程账的状态,不是身份的状态,不进本函数)")
    if verdict == "confirmed":
        done = abandon(conn, store, old_sku, ABANDON_SKU_UPDATE,
                       replaced_by=new_sku)
        if not done:
            return                       # 已定案:不重复记事件(幂等)
        product_events.record_many(conn, [{
            "sku": new_sku, "store": store, "event": product_events.SKU_REPLACED,
            "source": "sku_codec",
            "detail": {"old_sku": old_sku, "reason": reason or "sku_update"},
        }])
        return
    with conn.cursor() as cur:
        cur.execute(_SQL_CLEAR_REPLACED, (store, old_sku, new_sku))
        cleared = cur.fetchone() is not None
    burned = abandon(conn, store, new_sku, ABANDON_SKU_UPDATE_FAILED)
    if cleared or burned:
        logger.info("改码回滚 %s/%s→%s(旧行复活=%s,新码弃码=%s,原因=%s)",
                    store, old_sku, new_sku, cleared, burned, reason or "-")


def abandon(conn, store: str, sku: str, reason: str, *, replaced_by=None) -> bool:
    """输入:连接 + 店铺 + 旧码 + 弃码原因 → 输出:是否本次真的弃掉了(幂等)。

    同一事务内三件事:① UPDATE 登记簿(只动活行,已弃或不存在返 False,不重复
    记事件);② 该弃码原因要烧号且是 amz 行 ⇒ 烧掉该 (店, ASIN) 名下的 UPC
    (码与 UPC 同寿命;match 行的 source_key 是 GTIN,只标不烧);③ 记一条码级
    事件 sku_replaced(replaced_by 非空)或 sku_abandoned,**detail 必带
    source_key** —— 新码在 product_events.asin 列里提不出来,代际过滤读的是它。

    reason 不在 ABANDON_REASONS 里直接抛 ValueError:弃码点只有四个(词表里第五个
    取值 sku_update_failed 弃的是我们自己刚抽、从未上过沃尔玛的新码,不是弃码点),
    新原因出现时应该是有人在讨论后加进词表,而不是随手传个字符串进来。

    **烧不烧号、烧成什么状态,只看 `_BURN_STATUS` 这一张分派表**(决策 D):
    三个原因各配一个状态、sku_update 不烧;写入走 upc_pool.burn 唯一函数。
    只烧 amz 行:match 行的 source_key 是 GTIN,而 UPC 池的领号键是
    (店, ASIN),拿 GTIN 去烧匹配恒空 —— 那不是"烧不到",是"根本不该烧"。

    ⚠ `ABANDON_SKU_UPDATE` / `ABANDON_SKU_UPDATE_FAILED`(改码两支)在
    **workflows 侧仍然零调用**:本模块的 settle_replacement 是它们唯一的入口,
    而 settle_replacement 的唯一调用方是批次 3 的 workflows/sku_migrate.py。
    守门 tests/test_sku_guard.py::test_sku_update_reason_has_no_caller_yet 钉住
    "sku_codec 之外零出现" —— 接线那天那条断言必须显式改掉,改不掉就说明有人
    绕过 settle_replacement 自己弃了码(双轨禁止)。
    """
    if reason not in ABANDON_REASONS:
        raise ValueError(
            f"未登记的弃码原因 {reason!r}:词表只有 {sorted(ABANDON_REASONS)}"
            f"(先在 services/sku_codec.py 的 ABANDON_REASONS 登记)")
    with conn.cursor() as cur:
        cur.execute(_SQL_ABANDON, (reason, replaced_by, store, sku))
        row = cur.fetchone()
    if not row:
        return False
    source_type, source_key = row[0], row[1]
    burned = 0
    if _BURN_STATUS.get(reason) and source_type == listing_sources.SOURCE_AMZ \
            and source_key:
        burned = upc_pool.burn(conn, [(store, source_key)],
                               _BURN_STATUS[reason])
    detail = {"old_sku": sku, "reason": reason, "source_type": source_type,
              "source_key": source_key, "burned_upcs": burned}
    if replaced_by:
        detail["new_sku"] = replaced_by
    product_events.record_many(conn, [{
        "sku": sku, "store": store,
        "event": (product_events.SKU_REPLACED if replaced_by
                  else product_events.SKU_ABANDONED),
        "source": "sku_codec", "detail": detail,
    }])
    return True
