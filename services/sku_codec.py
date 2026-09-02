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
   mint 家族;abandoned_at / abandoned_reason / replaced_by 三列**只准本模块写**。
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

# ── 弃码原因词表(**只有这四个**;新增即改动四个弃码点的定义)────────────────
ABANDON_DELETE_VERIFIED = "delete_verified"   # DELETE 经观测核验
ABANDON_SKU_LOCKED = "sku_locked"             # SKU_LOCKED 自愈退役 + 冷却期满
ABANDON_UPC_CONFLICT = "upc_conflict"         # ERR_EXT_DATA_0101119 撞库(决策 B)
ABANDON_SKU_UPDATE = "sku_update"             # 改码 SkuUpdate 观测确认后的旧行
ABANDON_REASONS = frozenset({
    ABANDON_DELETE_VERIFIED, ABANDON_SKU_LOCKED,
    ABANDON_UPC_CONFLICT, ABANDON_SKU_UPDATE,
})

#: 弃码原因 → 烧号状态。**在表里 = 这个原因要烧号**;不在表里的两个各有理由:
#:   upc_conflict —— 号已由 listing_sheet 的撞库处置标成 conflict,再烧一次是
#:                   重复动作(而且会把「撞库」这个真相盖掉);
#:   sku_update   —— 码换了但 item 还在、UPC 还绑着,烧号等于白烧。
#: ⚠ 状态值本身在批次 2 才真正写进库:upc_pool.burn_for_retire 现在仍写
#: conflict(0a 是零行为变化批次),批次 2 接线时它改成 burn(conn, pairs, status)
#: 并由本表传值 —— 到那时改的是一处,不留双轨(决策 D)。
_BURN_STATUS = {
    ABANDON_DELETE_VERIFIED: upc_pool.BURN_DELETE,
    ABANDON_SKU_LOCKED: upc_pool.BURN_LOCK,
}

# 模块级计数(排障用:两个分支各记各的,合成一条就分不清并发与随机源故障)
_concurrent_mint = 0            # 撞的是别的进程刚发的活码键 ⇒ 复用对方的码
_sku_redraws = 0                # 撞的是主键/全局唯一 ⇒ 真·随机撞码,重抽

#: 复用查询:同 (店, 来源, 源头键) 已有活码就复用它。**不按形态过滤** ——
#: 存量 sku=asin 的活行照样复用(存量迁码是批次 3 的事);条件与
#: listing_sources_live_uidx / listing_sources_live_key_idx 的局部条件逐字对齐。
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


def abandon(conn, store: str, sku: str, reason: str, *, replaced_by=None) -> bool:
    """输入:连接 + 店铺 + 旧码 + 弃码原因 → 输出:是否本次真的弃掉了(幂等)。

    同一事务内三件事:① UPDATE 登记簿(只动活行,已弃或不存在返 False,不重复
    记事件);② 该弃码原因要烧号且是 amz 行 ⇒ 烧掉该 (店, ASIN) 名下的 UPC
    (码与 UPC 同寿命;match 行的 source_key 是 GTIN,只标不烧);③ 记一条码级
    事件 sku_replaced(replaced_by 非空)或 sku_abandoned,**detail 必带
    source_key** —— 新码在 product_events.asin 列里提不出来,代际过滤读的是它。

    reason 不在 ABANDON_REASONS 里直接抛 ValueError:弃码点只有四个,第五个
    出现时应该是有人在讨论后加进词表,而不是随手传个字符串进来。
    """
    if reason not in ABANDON_REASONS:
        raise ValueError(
            f"未登记的弃码原因 {reason!r}:只有 {sorted(ABANDON_REASONS)} 四个"
            f"弃码点(先在 services/sku_codec.py 的 ABANDON_REASONS 登记)")
    with conn.cursor() as cur:
        cur.execute(_SQL_ABANDON, (reason, replaced_by, store, sku))
        row = cur.fetchone()
    if not row:
        return False
    source_type, source_key = row[0], row[1]
    burned = 0
    if _BURN_STATUS.get(reason) and source_type == listing_sources.SOURCE_AMZ \
            and source_key:
        burned = upc_pool.burn_for_retire(conn, [(store, source_key)])
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
