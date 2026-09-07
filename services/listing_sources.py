"""产品来源登记簿积木(catalog.listing_sources;所有者定稿 2026-08-07)。

背景:sku=asin 约定只对 amz 搬运品成立——跟卖 SKU 是人工/自动编号,
未来还有自建、1688 等来源。旧系统靠"SKU 格式不像 ASIN 就全排除"防误伤,
代价是这些产品成了自动化盲区。新规矩:**谁上架谁登记,自动化按出身路由,
手动通道全格式通吃**。

路由铁律(消费方契约):任何由"源数据查不到/缺失"驱动的自动破坏动作
(如 amz 采集不到 → 清库存/删除),必须 JOIN 本表限定 source_type 匹配;
source_type='unknown' 的行一律不自动动。maintenance 的 price/inventory/
title provider 做实时必须带上这条(services/maintenance_intents.py 契约)。

调整成本:出身是数据不是代码——改归类 = UPDATE 一行;新增来源 = 新
source_type 取值 + 对应 provider,管道零改动。

消费方契约(SKU 改造批次 0a,2026-09-02):
  ① 本模块的 `register` 只负责**首次登记**(存量 backfill 与跟卖 B 列人工号);
     **自动抽码一律走 services/sku_codec.mint** —— 抽码与登记必须同一函数同一
     事务,不存在"抽了没登记"。
  ② `abandoned_at` / `abandoned_reason` / `replaced_by` 三列**只准由
     services/sku_codec 写**(0a 的 abandon;批次 3 的改码替换),本模块与任何
     工作流都不得 UPDATE 它们。
  ③ 本表的 INSERT 只有**两个**合法出口:本模块的 register 与 sku_codec.mint
     家族。新增第三个即违规(conventions §六:一个能力一条实现路径),守门
     tests/test_sku_guard.py 全仓扫本表的 INSERT / UPDATE 语句钉死 ② 与 ③。
"""

import logging
import re

from services import sku_asin

logger = logging.getLogger("services.listing_sources")

# source_type 取值登记(新来源在此登记,消费方禁止散落字符串字面量)
SOURCE_AMZ = "amz"          # 亚马逊搬运(source_key=asin;sku=asin 约定适用)
SOURCE_MATCH = "match"      # 跟卖(source_key=匹配 GTIN;sku 为人工/自动编号)
SOURCE_SELF = "self"        # 自建产品库(预留)
SOURCE_1688 = "1688"        # 1688 货源(预留)
SOURCE_UNKNOWN = "unknown"  # 存量格式回填未能归类;不参与任何自动破坏动作


def register(conn, rows: list[dict]) -> int:
    """输入:连接 + [{store, sku, source_type, source_key?, workflow}]
    → 输出:写入数。首次登记优先(ON CONFLICT 不覆盖);改归类走人工 UPDATE。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.listing_sources "
            "(store, sku, source_type, source_key, workflow) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (store, sku) DO NOTHING",
            [(r["store"], r["sku"], r["source_type"],
              r.get("source_key"), r.get("workflow")) for r in rows])
    return len(rows)


def replacement_map(conn, store: str) -> dict[str, str]:
    """输入:连接 + 店 → 输出:{新码: 旧码}(在途改码的反向指针,catalog_sync 用)。

    只读。回答的是「本轮**新出现**的这个 sku 是不是某个旧码的替身」——
    是的话它不该被记成 item_appeared(那是"这个店多了一个品"),而是改码的另一面。
    改码前本店恒返回空字典 ⇒ 消费方的分支一次都不会走到(零行为变化)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT sku, replaces FROM catalog.listing_sources "
                    "WHERE store = %s AND replaces IS NOT NULL", (store,))
        return {sku: old for sku, old in cur.fetchall()}


def replaced_skus(conn, store: str) -> set[str]:
    """输入:连接 + 店 → 输出:{正在被替换的旧码}(在途改码的旧码集合)。

    只读。回答的是「本轮**缺席**的这个 sku 是不是正在被替换」—— 是的话它的缺席
    是我们自己造成的,不该记 item_missing、不该产删除建议。
    改码前本店恒返回空集合 ⇒ 消费方的分支一次都不会走到(零行为变化)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT sku FROM catalog.listing_sources "
                    "WHERE store = %s AND replaced_by IS NOT NULL", (store,))
        return {r[0] for r in cur.fetchall()}


# ══════════════════════════════════════════════════════════════════════════════
#  来源码人工归类(所有者 2026-09-03;**全仓唯一一条改 source_type / source_key
#  的路径**,守门 tests/test_sku_guard.py 钉死)
#
#  背景:存量里有一批行的 sku 不是标准 ASIN(`CMSQ-B0CLCX3Q1Z-169.99` 应为
#  `B0CLCX3Q1Z`、`B0822D9QQKS59` 应为 `B0822D9QQK`,还有别的形态),
#  sources_backfill 一律把它们登记成 source_type='unknown' + source_key=NULL。
#  按路由铁律它们**被排除在全部自动维护之外**,也进不了 sku_migrate 的候选 ——
#  这不是 bug(防误伤设计),但也不该是终局:出身是数据不是代码,人认出来了
#  就该能改。register 的 docstring 早就写着「改归类走人工 UPDATE」,今天补上
#  那条 UPDATE 的**唯一实现**。
# ══════════════════════════════════════════════════════════════════════════════

#: 归类允许写成的四个来源类型 → (人读名, 键闸)。**闸按类型分**:登记簿的键
#: 错了下游一声不吭,而四种出身的"键"根本不是同一种东西 ——
#: amz 的键是 ASIN、match 的键是匹配 GTIN、1688/self 的键是货源侧自己的货号。
#: 用 amz 那把尺子去量 1688 货号,结果是把它整批拒收;反过来不设闸,则是把
#: 一个非 amz 的品登记成 amz ⇒ 它的价格/标题/库存从此跟着某个亚马逊页面走,
#: 断货窗口一到还会被建议**永久删除**(所有者 2026-09-03 当场问出来的口子)。
#: ⚠ SOURCE_UNKNOWN **不在表内**:归类的定义就是"离开 unknown",允许写回去
#: 等于给自己开一条把已归类行打回盲区的路。
RECLASSIFY_TYPES: dict[str, tuple[str, str]] = {
    SOURCE_AMZ: ("亚马逊搬运", "标准 ASIN(10 位含字母)"),
    SOURCE_MATCH: ("跟卖", "GTIN 数字串(8~14 位)"),
    SOURCE_1688: ("1688 货源", "货源侧货号(非空,≤64 字符)"),
    SOURCE_SELF: ("自建", "自建货号(非空,≤64 字符)"),
}

#: 默认类型:清单没有「确认来源类型」这一列时按它算(存量绝大多数是搬运品)。
#: 调用方**必须**在摘要里把这件事喊出来 —— 静默默认正是这条通道原来的毛病。
RECLASSIFY_DEFAULT_TYPE = SOURCE_AMZ

_GTIN = re.compile(r"^\d{8,14}$")


def normalize_source_key(source_type: str, key) -> str:
    """输入:来源类型 + 人填的键 → 输出:归一后的键(不做合法性判断)。

    ⚠ **只有 amz / match 归一成大写**:ASIN 与 GTIN 都是大写字母数字的约定,
    大小写不敏感;1688 / 自建的货号是货源侧的原文,大小写可能有意义,
    动它等于把键改错 —— 而键错了下游一声不吭(§九)。
    """
    v = str(key or "").strip()
    return v.upper() if source_type in (SOURCE_AMZ, SOURCE_MATCH) else v


def source_key_ok(source_type: str, key: str) -> bool:
    """输入:来源类型 + 已归一的键 → 输出:这个键配不配得上这个出身。

    **写入前的最后一道闸**,四类各判各的(闸的语义见 RECLASSIFY_TYPES 头注)。
    """
    if source_type == SOURCE_AMZ:
        return sku_asin.is_standard_asin(key)
    if source_type == SOURCE_MATCH:
        return bool(_GTIN.fullmatch(key))
    if source_type in (SOURCE_1688, SOURCE_SELF):
        return bool(key) and len(key) <= 64 and not any(c.isspace() for c in key)
    return False


#: 归类改写在 workflow 列留下的记号(登记簿上唯一的一处痕迹,见 reclassify 头注)。
RECLASSIFY_WORKFLOW = "sources_reclassify"

#: 待归类 = 「自动链看不见」的那个条件本身(消费方一律 JOIN 本表限定
#: source_type='amz' 且键非空)。**不按 abandoned_at 过滤**(§九②):弃码行同样
#: 可能等着人认,而按它过滤是那条危险谓词的经典误用。
_PENDING_SQL = """
SELECT ls.store, ls.sku, ls.source_type, ls.source_key,
       w.product_name, w.published_status, w.missing_since,
       (w.store IS NOT NULL) AS in_items
FROM catalog.listing_sources ls
LEFT JOIN catalog.walmart_items w ON w.store = ls.store AND w.sku = ls.sku
WHERE ls.source_type = %s OR ls.source_key IS NULL
ORDER BY ls.store, ls.sku
"""

#: 导入行的现状(判"能不能改"要看库里此刻是什么,不能信 csv 里那两列 ——
#: 清单可能是上周导出的)。
_CURRENT_SQL = """
SELECT store, sku, source_type, source_key FROM catalog.listing_sources
JOIN unnest(%s::text[], %s::text[]) AS t(s, k) ON store = t.s AND sku = t.k
"""

#: ⚠ WHERE 里那个入口条件**与 plan_reclassify 判的是同一件事,而且不许省**:
#: 计划与写入之间隔着人看摘要的时间,并发的 mint / 另一次导入都可能把行改掉;
#: 没有这一条,一次 overwrite=0 的导入照样能盖掉刚刚归好类的行,而且不报错。
_RECLASSIFY_SQL = """
UPDATE catalog.listing_sources SET source_type = %s, source_key = %s, workflow = %s
WHERE store = %s AND sku = %s
  AND (%s OR source_type = %s OR source_key IS NULL)
"""


def pending_reclassify(conn) -> list[dict]:
    """输入:连接 → 输出:待人工归类的登记行 [{store, sku, source_type, source_key,
    product_name, published_status, missing_since, in_items}](只读,按店+SKU 排序)。

    待归类的判据只有一条:`source_type='unknown' OR source_key IS NULL` ——
    它就是路由铁律把这些行挡在自动维护之外的那个条件,不是另立的一套口径。
    左连 walmart_items 只为给人配上商品名与在架状态(判"这个码到底是什么品"
    要靠它);登记簿有行而在架表没有是正常的(下架过的品行永不 DELETE),
    `in_items` 如实标出来,不静默丢行。
    """
    with conn.cursor() as cur:
        cur.execute(_PENDING_SQL, (SOURCE_UNKNOWN,))
        cols = ("store", "sku", "source_type", "source_key", "product_name",
                "published_status", "missing_since", "in_items")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def plan_reclassify(conn, rows: list[dict],
                    overwrite: bool = False) -> tuple[list[dict], dict]:
    """输入:连接 + [{store, sku, source_key, source_type?}] + overwrite
    → 输出:(可改行, 跳过点名)。

    **只读**;`reclassify` 真改之前走的就是本函数,所以预览报的"本次会改多少行"
    与真跑改出来的行数同口径 —— 判据一处出生,不许在工作流里另算一遍
    (conventions §六)。

    跳过点名是 {理由: [店/SKU…]},四类各自点名、不合并:拒收(码不是标准
    ASIN)、登记簿里没有这一行、已归类而没传 overwrite、已经是这个值(幂等
    重跑)。**默默丢行是这类导入最难查的故障**:所有者拿回来的摘要里只会少
    几行,而他没法知道少的是哪几行、为什么。
    """
    skipped: dict[str, list[str]] = {}

    def _skip(why: str, what: str):
        skipped.setdefault(why, []).append(what)

    want, seen = [], set()
    for r in rows:
        store, sku = str(r.get("store") or "").strip(), str(r.get("sku") or "").strip()
        stype = str(r.get("source_type") or "").strip() or RECLASSIFY_DEFAULT_TYPE
        key = normalize_source_key(stype, r.get("source_key"))
        if not store or not sku:
            _skip("店铺或 SKU 为空(拒收)", f"{store or '(空)'}/{sku or '(空)'}")
            continue
        if (store, sku) in seen:
            _skip("清单内重复,只认第一条", f"{store}/{sku}")
            continue
        seen.add((store, sku))
        if stype not in RECLASSIFY_TYPES:
            _skip(f"来源类型不认识(只收 {'/'.join(RECLASSIFY_TYPES)};拒收)",
                  f"{store}/{sku}→{stype}")
            continue
        # 形态闸在**写入之前**,而且**按类型分**:登记簿的键错了下游一声不吭
        # (采不到源数据 → 判成"源头没了" → 清库存/删除),灌垃圾键比不归类
        # 危险得多。四种出身的键不是同一种东西,闸也就不能是同一把尺子。
        if not source_key_ok(stype, key):
            _skip(f"来源码不合 {stype} 的形态"
                  f"({RECLASSIFY_TYPES[stype][1]};拒收)",
                  f"{store}/{sku}→{key or '(空)'}")
            continue
        want.append({"store": store, "sku": sku,
                     "source_type": stype, "source_key": key})
    if not want:
        return [], skipped

    with conn.cursor() as cur:
        cur.execute(_CURRENT_SQL, ([w["store"] for w in want],
                                   [w["sku"] for w in want]))
        now = {(s, k): (t, key) for s, k, t, key in cur.fetchall()}

    todo = []
    for w in want:
        cur_state = now.get((w["store"], w["sku"]))
        if cur_state is None:
            _skip("登记簿里没有这一行(先 sources_backfill)", f"{w['store']}/{w['sku']}")
            continue
        cur_type, cur_key = cur_state
        if cur_type == w["source_type"] and cur_key == w["source_key"]:
            _skip("已经是这个值(幂等重跑)", f"{w['store']}/{w['sku']}")
            continue
        if not (cur_type == SOURCE_UNKNOWN or not cur_key) and not overwrite:
            _skip("已归类,未传 overwrite 不覆盖",
                  f"{w['store']}/{w['sku']}({cur_type}:{cur_key})")
            continue
        todo.append(w)
    return todo, skipped


def reclassify(conn, rows: list[dict],
               overwrite: bool = False) -> tuple[int, dict]:
    """输入:连接 + [{store, sku, source_key, source_type?}] + overwrite
    → 输出:(改写行数, 跳过点名)。

    **全仓唯一一条 UPDATE source_type / source_key 的路径**(register 只负责首次
    登记,ON CONFLICT DO NOTHING;弃码/改码五列另有唯一写者 services/sku_codec)。
    按 (store, sku) 定位,写成人给的 `source_type` + `source_key`,并在 workflow
    列留下 RECLASSIFY_WORKFLOW 的记号。类型缺省 `RECLASSIFY_DEFAULT_TYPE`(amz),
    取值限 `RECLASSIFY_TYPES` 四档,键按类型各过各的闸。

    ⚠ **类型不是摆设**(所有者 2026-09-03 当场问出来的口子):此前本函数写死
    `amz`,于是一个 1688 或自建的品只要填了个形态合法的 ASIN,就会被登记成
    搬运品 —— 它的价格/标题/库存从此跟着那个亚马逊页面走,断货窗口一到还会
    被建议**永久删除**,而且全程不报错。非 amz 的行归类之后照样有身份、
    照样进不了 amz 那三条 provider,这正是分类型的意义。

    ⚠ **归成 amz 的那一批,后果是把商品交还自动链**:它们从此满足消费方那条
    `source_type='amz' AND source_key IS NOT NULL` 的 JOIN,于是被 amz 快照驱动的
    改价 / 清库存 / **删除**管到 —— 盲区变辖区,破坏面对这批行第一次打开。
    归成 match / 1688 / self 的行只是拿到身份,没有任何 provider 认领它们。
    调用方必须让人先看清单再放行(纪律与 sources_backfill 同款:改完先
    `maintenance_scan --dry-run` 看意图量,尤其删除段)。

    **三个同名异义,别混**(§九⑥同款纪律):
      · **归类(本函数)** = 改这一行的**出身**(它一直是哪个 ASIN 的搬运品,
        我们此前不知道)。SKU 不变、沃尔玛侧一根手指都不碰。
      · **首次登记(register)** = 这一行此前**根本没有**登记行,补一条。
        ON CONFLICT DO NOTHING,永不覆盖既有归类。
      · **改码(sku_codec.mint_replacement / settle_replacement)** = 换掉沃尔玛
        侧的 SKU 本身(要发 feed、要观测定案、要弃旧码)。它动的是身份的另一端,
        与归类没有任何交集 —— 谁也不许拿本函数去实现改码。

    为什么不记 product_events:账本记的是**产品生死与观测事实**,事件码全集
    (services/product_events.EVENTS)里没有一个说得清"我们改了对这一行出身的
    认识";硬套一个已登记码 = 往账本里灌一支消费方读不懂的分叉,自造未登记码
    则被 record_many 当场拒收。归类的痕迹留在 workflow 列 + 报告目录里那份
    csv,要审计走这两处。
    """
    todo, skipped = plan_reclassify(conn, rows, overwrite)
    changed = 0
    with conn.cursor() as cur:
        for w in todo:
            cur.execute(_RECLASSIFY_SQL,
                        (w["source_type"], w["source_key"], RECLASSIFY_WORKFLOW,
                         w["store"], w["sku"], overwrite, SOURCE_UNKNOWN))
            if cur.rowcount:
                changed += 1
            else:
                # 计划与写入之间行被别人改了(并发 mint / 另一次导入)
                skipped.setdefault("入口条件已不满足,未改(重跑即可)", []).append(
                    f"{w['store']}/{w['sku']}")
    if changed:
        logger.info("来源码归类改写 %d 行(overwrite=%s)", changed, overwrite)
    return changed, skipped
