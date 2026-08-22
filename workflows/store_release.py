"""store_release — 释放占用(整店 / 点名品牌 / 点名 ASIN)。**危险:缺省即真跑,空跑用 `--dry-run`。**

用法:
  python cli.py store_release -p store=A085 --dry-run       # 预览该店全部占用
  python cli.py store_release -p store=A085                 # 真释放
  python cli.py store_release -p brand=vtopmart             # 点名释放一个品牌
  python cli.py store_release -p asin=B08LHF7VLT            # 点名释放一个产品
  python cli.py store_release -p brand=vtopmart -p store=A085
        # 点名 + **限定占用店**:只有这个品牌此刻确实占在 A085 才放
        #(不动在线快照 —— 那是个别归属调整,店还在正常经营)
  python cli.py store_release -p from_csv=<路径>/alloc_该释放占用.csv
        # 批量:吃 claim_audit 出的清单,逐条按 (类型, 占用键, **占用店**) 释放
  python cli.py store_release -p store=A085 -p mark_offline=0
        # 只放占用、不动在线快照(默认整店释放会同步标缺席,见下)
  python cli.py store_release -p dead=1 --dry-run
        # **批量清死店**:凭证表里没有的店,整店释放 + 全店标缺席(见下)

**这是全系统唯一的释放路径**(docs/allocation_plan.md §六):没有任何代码会
自动释放占用——店铺暂停不释放、商品下架不释放、KPI 报 TERMINATED 也不释放
(那只是"可释放资格",审计告警列清单,人来跑这条命令)。理由:KPI 是外部
观测,误报触发自动释放的话,品牌被别的店占走就再也撤不回来了。

**整店释放同时校正在线快照**(§九.4):catalog_sync 只扫在册店,店铺从凭证表
删掉后它的 walmart_items 行会永久冻结成"在架",污染在线表投影、list_new
全局去重闸与 maintenance。店铺终止 = 商品事实上已全部下架,所以整店释放时
顺手把该店在架行标 missing_since——**这是校正观测不是伪造**,dry-run 会先
把行数列出来;不想动就 `-p mark_offline=0`。
点名释放(brand/asin)不碰快照:那是个别产品的归属调整,店还在正常经营。

**`-p dead=1` 批量清死店**(所有者 2026-08-22:"产品库中有些店已经不在了…
这些店以外的产品全部作为已下线状态,让我其他的店上架时不会被拦截,也能进入
分配队列")。它就是把上面那条整店释放**对每一家不在册的店各跑一遍**,
不是另一套逻辑 —— 同一个能力只有一条实现路径。

名录的权威是**凭证表的「启用」勾选**(`stores.enabled_names()`,所有者定稿
2026-08-22),不写进代码:店铺名录是会变的业务数据,写死在这里,下次开一家
新店就得改代码(铁律 3)。所以用法是**把不再运营的店的「启用」取消勾选**
(不必删行,历史凭证留着),再跑这条;dry-run 会把"保留 / 清理"两张名单都
列出来,拿它跟手上的在营名单逐个对。

⚠ **规划外 ≠ 不在营。** 判定只看在不在凭证表,**不看** `alloc_excluded_stores`
(「谭总」那些)—— 它们不参与分配,但货是真在卖的,扫掉就是把在售商品
凭空标成下架。
⚠ **店名对不上比店没了更常见。** 库里的 `walmart_items.store` 与凭证表店名
若差一个空格/大小写,这条命令会把一家在营店整店下线。所以扫之前做一次
**近似名核对**:归一化后撞上在册店的,一律**拒跑并报出来**,让人先把名字对齐。
⚠ 一家都不保留时**拒跑**:那几乎一定是名字格式对不上,不是店真的全没了。
"""

import logging
from collections import Counter

from registry import db
from services import claims

DANGEROUS = True

logger = logging.getLogger("workflows.store_release")

_MARK_OFFLINE_SQL = """
UPDATE catalog.walmart_items
   SET missing_since = now(), published_status = NULL, lifecycle_status = NULL,
       updated_at = now()
 WHERE store = %s AND missing_since IS NULL
"""

_COUNT_ONLINE_SQL = """
SELECT count(*) FROM catalog.walmart_items
WHERE store = %s AND missing_since IS NULL
"""

# 逐店在架行数。判"在架"与 alloc_survey._SQL_ONLINE 有意不同:那边还要排
# RETIRED(它问"这是不是一个活货位"),这边问的是"这一行还在冒充在架吗"
# —— 死店的 RETIRED 行同样在污染 list_new 的去重闸,一起标掉才干净。
_SQL_ONLINE_BY_STORE = """
SELECT store, count(*) FROM catalog.walmart_items
WHERE missing_since IS NULL AND store IS NOT NULL AND btrim(store) <> ''
GROUP BY store
"""


def _name_key(s) -> str:
    """输入:店名 → 输出:近似名比对用的键(去掉所有空白 + casefold)。

    **只用于"报警",不用于匹配**:归一化后撞上在册店的会让整条命令拒跑,
    而不是"那就当它是同一家"。自动对齐等于替所有者决定两个不同的字符串
    是同一家店 —— 猜错一次就是把在营店整店下线,而 missing_since 一旦打上,
    在线表投影、list_new 去重闸、maintenance 三处同时开始按"已下架"办事。
    """
    return "".join(str(s or "").split()).casefold()


def _reason(params: dict, store, brand, asin) -> str:
    if params.get("reason"):
        return str(params["reason"]).strip()
    if not brand and not asin:
        return "store_release:整店释放"
    return (f"store_release:点名释放 {'品牌 ' + brand if brand else 'ASIN ' + asin}"
            + (f"(限 {store})" if store else ""))


def _read_csv(path: str) -> tuple[list, str | None]:
    """输入:claim_audit 落的 csv 路径 → 输出:([(kind, key, store)], 错误)。

    只认 `claim_audit` 的表头(类型/占用键/占用店)。**认死表头而不是按列号取**:
    按列号取的话,以后往 csv 中间插一列,这条命令就会拿着「原因」当占用键去释放,
    而且不会报错 —— 一次误释放要人肉查回来。
    """
    import csv
    from pathlib import Path
    p = Path(path).expanduser()
    if not p.exists():
        return [], f"文件不存在:{p}"
    with p.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        need = {"类型", "占用键", "占用店"}
        if not need <= set(rd.fieldnames or []):
            return [], (f"表头对不上(要有 {'、'.join(sorted(need))}),"
                        f"实际是 {rd.fieldnames} —— 这份 csv 由 claim_audit 生成")
        out = []
        for r in rd:
            kind = (r["类型"] or "").strip()
            key = (r["占用键"] or "").strip()
            st = (r["占用店"] or "").strip()
            if kind in claims.KINDS and key and st:
                out.append((kind, key, st))
    return out, None


def run(params: dict) -> str:
    """输入:params(store/brand/asin/from_csv/reason/mark_offline/execute)→ 输出:摘要。"""
    execute = bool(params.get("execute"))
    store = (params.get("store") or "").strip() or None
    brand = (params.get("brand") or "").strip() or None
    asin = (params.get("asin") or "").strip().upper() or None
    from_csv = (params.get("from_csv") or "").strip() or None
    mark_offline = str(params.get("mark_offline", "1")).lower() not in {"0", "false", "no"}
    dead = str(params.get("dead", "")).lower() in {"1", "true", "yes"}

    if dead:
        return _run_dead(params, execute, mark_offline)
    if from_csv:
        return _run_csv(params, from_csv, execute)

    named = [x for x in (brand, asin) if x]
    if len(named) > 1:
        return "⛔ `-p brand=` 与 `-p asin=` 二选一,不能同时给"
    if not named and not store:
        return ("⛔ 至少给一个:-p store=<店铺> / -p brand=<品牌> / -p asin=<ASIN>"
                " / -p dead=1 / -p from_csv=<claim_audit 的 csv>"
                "(全空会清空整个台账,不允许)")
    # ★ `-p store=` 与 brand/asin **同时给** = 点名释放**并限定占用店**
    #   (2026-08-22)。为什么要有:按 (类型, 键) 无条件释放的话,占用如果在你
    #   出清单之后换了店(别处释放过、重新分配过),这条命令会把**新店的好占用**
    #   一起放掉 —— `_run_csv` 早就按三条件释放,而 claim_audit 拼给人手跑的
    #   单条命令却没带 store,两条路径口径不一致。
    whole_store = bool(store) and not named
    # ⚠ **限定店的点名释放绝不许动快照**:那是个别归属调整,店还在正常经营。
    #   不加这道判断的话 `-p brand=X -p store=A085` 会把 A085 整店标缺席
    mark_offline = mark_offline and whole_store

    kind = claims.BRAND if brand else (claims.PRODUCT if asin else None)
    key = brand or asin
    if brand:
        # 品牌键必须与占用时同一套归一算法,否则大小写/空格差一点就释放不到
        from services import brand_key as bk
        key = bk.normalize(brand)
        if bk.is_placeholder(brand):
            return (f"⛔ 「{brand}」是占位符(无品牌),按设计它从不占用品牌,"
                    f"没有可释放的行;要放产品用 -p asin=")
    reason = _reason(params, store, brand, asin)

    with db.pg_conn() as conn:
        rows = claims.preview_release(conn, store=store, kind=kind, key=key)
        online = 0
        if mark_offline:
            with conn.cursor() as cur:
                cur.execute(_COUNT_ONLINE_SQL, (store,))
                online = int(cur.fetchone()[0])

        by_kind = Counter(k for k, _, _ in rows)
        what = ("整店 " + store if whole_store else
                ("品牌 " + key if brand else "ASIN " + key)
                + (f"(限 {store})" if store else ""))
        head = (f"{what}:"
                f"active 占用 {len(rows)} 条"
                + (f"(品牌 {by_kind.get(claims.BRAND, 0)} / 产品 "
                   f"{by_kind.get(claims.PRODUCT, 0)})" if rows else ""))
        sample = "; ".join(f"{k}:{v}→{s}" for k, v, s in rows[:15])

        if not execute:
            lines = [f"🧪 将释放 {head}"]
            if sample:
                lines.append(f"   样例:{sample}" + (" …" if len(rows) > 15 else ""))
            if mark_offline:
                lines.append(f"   同时把该店 {online} 行在架商品标缺席"
                             f"(校正观测:店终止后商品事实上已下架;"
                             f"-p mark_offline=0 可只放占用不动快照)")
            if not rows and not online:
                lines.append("   本次无任何可释放的行——占用台账里没有它,"
                             "或已经释放过了(released 行不重复释放)")
            lines.append("   确认后去掉 --dry-run 重跑")
            return "\n".join(lines)

        freed = claims.release(conn, reason=reason, store=store, kind=kind, key=key)
        marked = 0
        if mark_offline:
            with conn.cursor() as cur:
                cur.execute(_MARK_OFFLINE_SQL, (store,))
                marked = cur.rowcount or 0

    logger.warning("store_release 已执行:%s,释放 %d 条,标缺席 %d 行,原因=%s",
                   store or key, len(freed), marked, reason)
    return (f"✅ 已释放 {head.replace('active 占用', '实际释放')}"
            + (f";该店 {marked} 行在架商品已标缺席" if marked else "")
            + f";原因={reason}"
            + ";released 行永久保留(回答『当初属于谁』只能靠它)")


def _run_dead(params: dict, execute: bool, mark_offline: bool) -> str:
    """输入:params → 输出:批量清死店摘要。**逐店走整店释放那条路,不另写。**

    **在不在营**是唯一判据,来源是 `stores.enabled_names()`(在册 ∧ 勾了启用)。
    另外两个都不能用:`load_stores()` 还筛 ClientId/代理,会把「在营但代理没配
    的店」误判成死店(§九.4 的原话);`registered_names()` 连「启用」都不看,
    勾了停用的店会被当成还在营 —— 而这条命令直通整店下线。

    三道拒跑闸,每一道都对应一种"看起来像死店、其实是我们自己错了":
      1. 凭证表读不到 / 读回空 —— 拿不到真值时**不许降级**,更不许当成"全死了";
      2. 近似名撞车 —— 库里的店名与在册店只差空白/大小写,那是名字漂了不是店没了;
      3. 一家都不保留 —— 几乎一定是店名格式整体对不上。
    """
    from services import stores as stores_svc

    try:
        registered = stores_svc.enabled_names()
    except Exception as e:                          # noqa: BLE001
        return (f"⛔ 凭证表读不到({e}):分不清在营店与死店,拒绝清理。"
                f"**不拿旧快照兜底** —— 这道判定最不能承受的就是这种降级")
    if not registered:
        return "⛔ 凭证表一家店都没读到:这不可能是真值,拒绝清理"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE_BY_STORE)
            online = {s: int(n) for s, n in cur.fetchall()}

        keep = {s: n for s, n in online.items() if s in registered}
        dead = {s: n for s, n in online.items() if s not in registered}

        # 闸 2:近似名撞车
        reg_keys = {_name_key(s): s for s in registered}
        drift = [(s, reg_keys[_name_key(s)]) for s in dead
                 if _name_key(s) in reg_keys]
        if drift:
            return ("⛔ 有 %d 家店的名字与在册店只差空白/大小写,拒绝清理 ——\n"
                    "   这是**名字漂了**,不是店没了。自动对齐等于替你决定两个不同的\n"
                    "   字符串是同一家店,猜错一次就是把在营店整店下线。\n"
                    "   先把库里或凭证表的名字改成一致,再跑:\n%s"
                    % (len(drift), "\n".join(f"     库里「{a}」 ↔ 在册「{b}」"
                                              for a, b in sorted(drift))))
        # 闸 3:一家都不保留
        if online and not keep:
            return ("⛔ %d 家有在架行的店**没有一家**在凭证表里 —— 拒绝清理。\n"
                    "   店名格式整体对不上的可能性,远大于所有店同时终止。\n"
                    "   库里前几家:%s\n   在册前几家:%s"
                    % (len(online), "、".join(sorted(online)[:5]),
                       "、".join(sorted(registered)[:5])))
        if not dead:
            return (f"✓ 没有需要清理的店:{len(keep)} 家有在架行的店全部在册"
                    f"(在册共 {len(registered)} 家)")

        held = {s: claims.preview_release(conn, store=s) for s in dead}
        n_claim = sum(len(v) for v in held.values())
        n_rows = sum(dead.values())
        # ⚠ 名单两边都列全,不截断:这条命令的唯一人工控制点就是这两张名单,
        #   截断等于让人在看不全的情况下按确认
        body = [
            f"清理 {len(dead)} 家不在册的店:{n_rows:,} 行在架商品标缺席"
            + (f",释放 {n_claim} 条占用" if n_claim else ",无占用可释放"),
            "  要清理的:" + "、".join(f"{s}({n:,})"
                                      for s, n in sorted(dead.items())),
            f"  保留的 {len(keep)} 家:" + "、".join(f"{s}({n:,})"
                                                    for s, n in sorted(keep.items())),
            f"  (凭证表共 {len(registered)} 家;其中 {len(registered) - len(keep)} 家"
            f"没有在架行,本次无事可做)",
        ]
        if not mark_offline:
            body.append("  ⚠ `-p mark_offline=0`:只释放占用,**不动在线快照** ——"
                        "那些行还会继续冒充在架,拦着别的店上架")

        if not execute:
            return "\n".join(
                ["🧪 将" + body[0]] + body[1:]
                + ["  ⚠ **拿上面两张名单对一遍你手上的在营店名单再执行。**",
                   "     标了 missing_since 之后,在线表投影 / list_new 去重闸 /",
                   "     maintenance 三处会同时按「已下架」办事;要恢复只能靠",
                   "     catalog_sync 重新扫到它 —— 而死店根本不会被扫。",
                   "  确认后去掉 --dry-run 重跑"])

        reason = (str(params.get("reason") or "").strip()
                  or "store_release:批量清死店(不在凭证表)")
        freed = marked = 0
        for st in sorted(dead):
            freed += len(claims.release(conn, reason=reason, store=st))
            if mark_offline:
                with conn.cursor() as cur:
                    cur.execute(_MARK_OFFLINE_SQL, (st,))
                    marked += cur.rowcount or 0

    logger.warning("store_release 清死店:%d 家,释放 %d 条,标缺席 %d 行,原因=%s",
                   len(dead), freed, marked, reason)
    return "\n".join([f"✅ 已清理 {len(dead)} 家不在册的店:"
                      f"标缺席 {marked:,} 行,释放占用 {freed} 条",
                      "  " + "、".join(sorted(dead)),
                      f"  保留 {len(keep)} 家在册店的 {sum(keep.values()):,} 行",
                      "  released 行永久保留(回答『当初属于谁』只能靠它)"])


def _run_csv(params: dict, path: str, execute: bool) -> str:
    """输入:csv 路径 → 输出:批量释放摘要。**逐条都带 store 条件。**

    ⚠ 每条释放都把 (kind, key, **store**) 三个条件一起传给 `claims.release`。
    只按 (kind, key) 放的话,占用如果在你出这份 csv 之后换了店(别处释放过、
    重新分配过),这条命令会把**新店**的占用一起放掉 —— 而它是好的。
    三条件不匹配时那一行自然放不到,归进"跳过"计数,人能看见。
    ⚠ 不动在线快照:这是逐条归属调整,货还在架上(要不要下架看 csv 的
    「要下架的 SKU」列),整店释放那套 mark_offline 与这里无关。
    """
    items, err = _read_csv(path)
    if err:
        return f"⛔ 读不了 {path}:{err}"
    if not items:
        return f"⛔ {path} 里没有可释放的行(类型/占用键/占用店 三列都要有值)"
    reason = str(params.get("reason") or "").strip() or f"store_release:批量 {path}"

    with db.pg_conn() as conn:
        hit, miss = [], []
        for kind, key, st in items:
            got = claims.preview_release(conn, store=st, kind=kind, key=key)
            (hit if got else miss).append((kind, key, st))
        by_store = Counter(s for _k, _key, s in hit)
        head = (f"csv {len(items)} 行 → 命中 active 占用 {len(hit)} 条"
                f"(品牌 {sum(1 for k, _, _ in hit if k == claims.BRAND)} / "
                f"产品 {sum(1 for k, _, _ in hit if k == claims.PRODUCT)})"
                + (f";**{len(miss)} 行没命中**(已释放过、或占用此刻不属于"
                   f"csv 里那家店 —— 后者说明 csv 过期了,重跑 claim_audit)"
                   if miss else ""))
        if not execute:
            return ("\n".join(
                [f"🧪 将批量释放:{head}",
                 "   涉及 " + "、".join(f"{s}×{n}" for s, n in by_store.most_common(8)),
                 "   样例:" + "; ".join(f"{k}:{key}→{s}" for k, key, s in hit[:10])
                 + (" …" if len(hit) > 10 else ""),
                 "   ⚠ 释放本身可逆(released 行永不删),但释放后这些品牌会被",
                 "     下一轮分配给别的店,**那一步不可逆** —— 先确认这份 csv 是",
                 "     刚跑出来的,不是几天前的。",
                 "   确认后去掉 --dry-run 重跑"]))
        freed = 0
        for kind, key, st in hit:
            freed += len(claims.release(conn, reason=reason, store=st,
                                        kind=kind, key=key))
    logger.warning("store_release 批量:csv=%s,释放 %d 条,未命中 %d,原因=%s",
                   path, freed, len(miss), reason)
    return (f"✅ 批量释放完成:{head};实际释放 {freed} 条;原因={reason}"
            + "\n   货还在架上 —— 按 csv 的「要下架的 SKU」列去下架,那是另一件事")
