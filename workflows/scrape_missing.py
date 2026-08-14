"""scrape_missing — 给库里缺亚马逊数据的产品补采(危险,默认 dry-run)。

用法:
  python cli.py scrape_missing                       # 预览:缺什么、几类、能不能采
  python cli.py scrape_missing -p only=never_tried   # 只看/只推某一类
  python cli.py scrape_missing -p limit=20000 --execute
  python cli.py scrape_missing -p check=1            # 只查在途批次(同 product_refresh)

**与既有两条推采集的分工**(每个能力只有一条实现路径):
  product_refresh  在架产品全量重推(维护链的新鲜度源头,只看 walmart_items)
  list_new         上架时发现数据源缺席,顺手补采那几个 ASIN
  **本工作流**     `catalog.products` 里**有行但没有亚马逊数据**的存量补采
                   ——上面两条都不覆盖它:没上架过、也没走到上架流程。

缺数据分三层(体检口径与 catalog_health 一致):
  无标题   审核链直接跳过(采集降级/占位行),等于这行完全不可用
  无类目   L1 ②级失明,只能落到候选+LLM,判得慢且准头差
  无类目ID 同上,且喂不进 catmap_mine(挖不出映射)

**推之前先分清来路**(所有者 2026-08-14 要求补采的正是这批):
  never_tried   一次都没采过 —— 补采命中率最高,优先推
  had_snapshot  采过但落的是降级快照(outcome≠ok)—— 值得再试
  hard_failed   最近一次是终局失败(not_found/gone 这类)—— **默认不推**:
                多半是亚马逊已下架的历史 ASIN(库里 8 万空壳大头来自
                pt_backfill 用删除历史建的占位行),推了也是白烧配额
  retryable     最近失败但类型可重试(captcha/timeout/blocked)—— 值得再推

防重:同一天同一类只建一个批次(日界批次名),撞名沿用既有批次(v4 语义:
409 带 batch_id,绝不静默合并)。台账与 product_refresh 共用
`services/scrape_batches`,`-p check=1` 的落定逻辑也共用。
"""

import logging
import re
from datetime import datetime

from api import scraper
from registry import db
from services import kpi, scrape_batches as batches

DANGEROUS = True        # 会给采集器压几万个任务,默认 dry-run

logger = logging.getLogger("workflows.scrape_missing")

BATCH_PREFIX = "missing-"
BATCH_SIZE = 100_000     # 与 product_refresh 同口径(上传上限 50MB,远够)
TIMEOUT_HOURS = 2        # 比在架重推(1h)宽:这批量大且多是冷门老 ASIN

# 合法 ASIN 形态(与采集侧 common/core/idents.ASIN_RE 同口径)。products.asin
# 绝大多数是真 ASIN,但 pt_backfill 从旧库 SKU 归一时会带进个别非标形态
# ——推给采集器只会白占任务位,过滤掉并计数(不静默丢)
_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")

# 缺数据分层 × 采集履历分类。履历取每个 ASIN 的**最新一条**证据:
# 有 ok 快照 = 采到过(那它只是字段缺,不算缺数据,不进候选);
# 有非 ok 快照 = 降级;有失败台账 = 按 error_type 分可重试/终局。
_TARGETS_SQL = """
WITH miss AS (
    SELECT asin,
           (title IS NULL OR btrim(title) = '')            AS no_title,
           (amazon_category IS NULL
              OR btrim(amazon_category) = '')              AS no_path,
           (browse_node_id IS NULL)                        AS no_node
    FROM catalog.products
    WHERE marketplace = 'US'
), cand AS (
    SELECT * FROM miss WHERE no_title OR no_path OR no_node
), snap AS (
    SELECT DISTINCT ON (s.asin) s.asin, s.outcome, s.scraped_at
    FROM catalog.snapshots s
    JOIN cand ON cand.asin = s.asin
    WHERE s.marketplace = 'US'
    ORDER BY s.asin, s.scraped_at DESC
), fail AS (
    SELECT DISTINCT ON (f.asin) f.asin, f.error_type, f.recorded_at
    FROM ops.scrape_failures f
    JOIN cand ON cand.asin = f.asin
    ORDER BY f.asin, f.recorded_at DESC
)
SELECT c.asin, c.no_title, c.no_path, c.no_node,
       s.outcome, f.error_type,
       -- 冷却期判据取"这条证据本身有多旧",失败台账优先(与 classify 同序)
       EXTRACT(day FROM now() - coalesce(f.recorded_at, s.scraped_at))::int
FROM cand c
LEFT JOIN snap s ON s.asin = c.asin
LEFT JOIN fail f ON f.asin = c.asin
"""

# 冷却天数(所有者定稿 2026-08-14 + 采集侧证据)。variant_offset / not_found
# 的成因是非确定性的(A/B 分桶、地域缓存、库存差异、卖家重新上架),隔一段时间
# 才会重新掷骰子;采集侧实测"立刻 rotate 重试结果一样",所以不设冷却 = 每轮
# 全额重推、烧配额且命中率约等于 0。14 天是保守起点,看回收率再调。
COOLDOWN_DAYS = 14

_CLASSES = ("never_tried", "had_snapshot", "retryable", "retry_later",
            "cooling", "hard_failed", "ok_but_thin")
# 默认推哪几类:命中率高的优先,终局失败与"采到过但字段薄"不推。
# retry_later 进默认集(所有者定稿 2026-08-14:"这种都可以再重采,全局适用"),
# 但只有过了冷却期的才在这一档,没过的落 cooling 不推
_DEFAULT_PUSH = ("never_tried", "had_snapshot", "retryable", "retry_later")


def classify(outcome: str | None, error_type: str | None,
             age_days: int | None = None,
             cooldown: int = COOLDOWN_DAYS) -> str:
    """输入:最新快照 outcome + 最新失败类型 + 该证据的天龄 → 输出:采集履历分类。纯函数。

    顺序有讲究:**失败台账优先于快照**——采集失败的 ASIN 压根不产出快照行
    (没采到就没有可导出的记录),有快照又有失败台账时,失败是更新的证据。

    ⚠ `variant_offset` 与 `not_found` 归 retry_later 而不是终局(2026-08-14
    按采集仓库源码订正,详见 services/scrape_batches.RETRY_LATER):
      · variant_offset —— 成因是 Amazon A/B test / 地域缓存 / 库存差异,
        非确定性;采集侧 cap=1 不重试是配额保护+防数据中毒的策略,不是
        "重试也没用"。它自己永远不会再试(force=true 也不越过),**只有本侧
        提交新批次这一条出路**。
      · not_found —— 采集侧**不是** error_type,是 outcome='not_found' 走
        成功路径(worker/engine.py:1418-1434,success=True、任务状态 done);
        其注释明写"下一轮定时采集自动纠正"。商品下架后又上架是常事。
    两者都要过 cooldown 才推,没过的落 `cooling`(计数但不推)——立刻重推
    与非确定性成因的重掷周期不匹配,纯烧配额。
    """
    def _cooled(cls: str) -> str:
        # 天龄取不到(证据没时间戳)时按"已冷却"放行:宁可多推一次,
        # 也不要因为缺个时间戳把一整类永久卡死在 cooling 里
        if age_days is None or age_days >= cooldown:
            return cls
        return "cooling"

    if error_type:
        if error_type in batches.RETRYABLE:
            return "retryable"
        if error_type in batches.RETRY_LATER:
            return _cooled("retry_later")
        return "hard_failed"
    if outcome is None:
        return "never_tried"
    if outcome == "ok":
        return "ok_but_thin"
    if outcome == "not_found":
        return _cooled("retry_later")
    return "had_snapshot"


def run(params: dict) -> str:
    """输入:params(only/limit/execute/check)→ 输出:缺口分层 + 推送摘要。"""
    if params.get("check"):
        # 落定语义与 product_refresh 共用 services 那一份,按自己的前缀圈
        return "\n".join(["在途补采批次(check:只读采集侧,结果同步进台账):"]
                         + batches.check_open(BATCH_PREFIX, TIMEOUT_HOURS))
    only = str(params.get("only", "")).strip()
    if only and only not in _CLASSES:
        raise ValueError(f"only 可选:{sorted(_CLASSES)}")
    limit = int(params.get("limit", 0)) or None

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_TARGETS_SQL)
            rows = cur.fetchall()
    if not rows:
        return "scrape_missing:库里没有缺亚马逊数据的产品行 ✓"

    by_class: dict = {c: [] for c in _CLASSES}
    lack = {"no_title": 0, "no_path": 0, "no_node": 0}
    for asin, no_title, no_path, no_node, outcome, err, age in rows:
        by_class[classify(outcome, err, age)].append(asin)
        lack["no_title"] += bool(no_title)
        lack["no_path"] += bool(no_path)
        lack["no_node"] += bool(no_node)

    want = (only,) if only else _DEFAULT_PUSH
    picked: list[str] = []
    for c in want:
        picked.extend(by_class[c])
    targets = sorted({a for a in picked if _ASIN_RE.fullmatch(a or "")})
    dropped = len(set(picked)) - len(targets)
    if limit:
        targets = targets[:limit]

    est = len(targets) / 2500        # 所有者口径:2000~3000/分钟
    lines = [
        f"scrape_missing:缺数据产品行 {len(rows)}"
        f"(无标题 {lack['no_title']} / 无类目 {lack['no_path']} / "
        f"无类目ID {lack['no_node']};一行可同时缺多项)",
        "采集履历分类:" + " / ".join(
            f"{c} {len(by_class[c])}" for c in _CLASSES if by_class[c]),
        f"本次目标 {len(targets)} 个 ASIN"
        f"(类别 {'/'.join(want)}{f',limit {limit}' if limit else ''};"
        f"按 2500/分钟估算约 {est:.0f} 分钟"
        + (f";非 ASIN 形态已过滤 {dropped} 个" if dropped else "") + ")",
    ]
    if by_class["hard_failed"] and not only:
        lines.append(f"  ⚠ 终局失败 {len(by_class['hard_failed'])} 个默认不推"
                     f"(多为已下架的历史 ASIN;要试加 -p only=hard_failed)")
    if by_class["cooling"]:
        lines.append(f"  ⏳ 冷却中 {len(by_class['cooling'])} 个不推"
                     f"(variant 偏移/已下架,上次证据不足 {COOLDOWN_DAYS} 天;"
                     f"成因非确定性,太早重推命中率约等于 0)")
    if by_class["ok_but_thin"]:
        lines.append(f"  ⚠ 采到过但字段仍缺 {len(by_class['ok_but_thin'])} 个"
                     f"默认不推——重采大概率还是这样,先查采集侧字段契约")
    if not targets:
        return "\n".join(lines + ["本次无可推 ASIN"])
    if not params.get("execute"):
        return "\n".join(lines + ["(dry-run:未推采集;确认无误加 --execute)"])

    stamp = datetime.now(kpi.CN_TZ).strftime("%Y%m%d")
    tag = only or "mix"
    chunks = [targets[i:i + BATCH_SIZE]
              for i in range(0, len(targets), BATCH_SIZE)]
    pushed = 0
    for i, chunk in enumerate(chunks, 1):
        name = (f"{BATCH_PREFIX}{tag}-{stamp}" if len(chunks) == 1
                else f"{BATCH_PREFIX}{tag}-{stamp}-{i:02d}")
        try:
            res = scraper.submit_batch(name, chunk)
            batches.record(name, res.get("batch_id"), len(chunk), "pushed",
                           f"inserted={res.get('inserted')}")
            pushed += len(chunk)
            lines.append(f"  {name}:推送 {len(chunk)} 个"
                         f"(inserted={res.get('inserted')})")
        except scraper.BatchExistsError as e:
            # 日界批次名撞名 = 今天这一类已经推过:沿用,不重复烧配额
            batches.record(name, e.batch_id, len(chunk), "pushed",
                           "同日同类已推,沿用既有批次")
            lines.append(f"  {name}:今天已推过,沿用既有批次 {e.batch_id}")
        except Exception as e:  # noqa: BLE001 —— 单批失败不连坐其余批次
            batches.record(name, None, len(chunk), "failed", str(e)[:200])
            lines.append(f"  {name}:❌ 推送失败 {e}")
            logger.exception("批次 %s 推送失败", name)
    lines.append(f"推送完成 {pushed} 个 ✓(采集落库走 product_ingest;"
                 f"`-p check=1` 查在途,回来后再跑 catalog_health 看补了多少)")
    return "\n".join(lines)
