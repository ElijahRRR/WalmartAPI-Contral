"""item_id_sync — 用沃尔玛 ITEM 报表补齐 catalog.walmart_items.item_id(首轮全量,日常按缺口)。

用法:
  python cli.py item_id_sync --dry-run                 # 只报候选店 / 缺口 / 台账在途,不建报表不写库
  python cli.py item_id_sync                           # 有缺口的店各拿一份报表补齐(每天 05:00 调度)
  python cli.py item_id_sync -p all=1                  # 全部能调 API 的店都拿(首轮 / 对账)
  python cli.py item_id_sync -p store=A085朱丽霖         # 单店
  python cli.py item_id_sync -p store=X -p probe=1     # 探针:拿报表,打印表头/行数/状态分布/样本 + 原件留存与体检,**不写 item_id**
  python cli.py item_id_sync -p wait_min=60 -p poll_secs=300   # 等待上限(分钟)/ 轮询间隔(秒),缺省即此
  python cli.py item_id_sync -p store=X -p data_days=729       # 报表数据范围天数(缺省 365;官方上限两年,代码夹到 729)
  python cli.py item_id_sync -p store=X -p renew=1             # 台账在途行作废,重新创建(改了请求形状时用)

为什么单独一条工作流(所有者定稿 2026-09-07):数字 itemId 只有 On-request ITEM
报表能批量给(GET /v3/items 与 catalog/search 都不返回,2026-08-05 实证);报表要等
15–45 分钟、每店每小时只能创建一次,挂在 catalog_sync 每店流程里会把产品链第一步
拖长一倍,而且报表失败会和目录同步的失败混在一起。此前 catalog_sync 的 `-p item_ids=1`
接线已摘掉(双轨禁止);它当年"配额极低"的真相是轮询桶配成 55/分钟而官方状态
查询是 20/小时(api/_client 桶登记处有注)。

每店一轮(services/item_reports 是判据与台账的唯一出处):
  ① 台账 ops.report_requests 取本店最近一条未落定行 → 有 requestId 就接着等/接着下载,
     **不重建**(`renew=1` 例外:在途行记 error 作废,重新创建 —— 改了请求形状时用);
     没有才「先落 pending 再 POST」创建(POST 不自动重试;429 = 这小时额度没了,本店
     本轮放弃、明天再来,不补试)。请求体带 dataStartTime/dataEndTime = 近 data_days 天
     (缺省 365):不带日期的 ITEM 报表只回 1 行(2026-09-07 22:05 C021 探针实证,后台
     不设时间同样只显示很少);范围记进台账 note。
  ② 轮询:先睡后查,用列表接口按 requestId 找自己那一份、找到即停(不带日期参数,
     带了回 400;nextCursor 是完整 query 串直接拼 URL;官方表 200/min 但生产实见是小时级
     桶,与单查共用 18/hour —— 均 2026-09-07 实证);五分钟一问,上限 wait_min,超时把
     requestId 留在台账下轮接着等(官方保留 30 天)。
  ③ READY → 取下载 URL(20/hour)→ 下载 → 解析(zip 内 CSV 或裸 CSV)→ 表头守门
     (SKU / Item ID / Item Page URL 缺一即拦,其余列增减只报不拦)。
  ④ 「Item ID」列与 URL 尾段互校 → 与本店在架行比对 → 写库:NULL 填、已有值且不同
     **按报表改**(所有者定稿「冲突以报表为准」)、报表里没有的在架行计未匹配。
  ⑤ 覆盖率 < 95%(在售行 ≥ 20)在首行点名「疑似不全」:当轮照填已匹配的,明天再拿一份。
     **分母只算在售行**(在架且 ACTIVE / PUBLISHED,所有者定稿 2026-09-08):报表不给
     RETIRED / SYSTEM_PROBLEM,库里还有列表接口翻回来的幽灵(A109:235 行"在售"里 232
     行单查 404),对真正在售的品报表覆盖 3355/3358。
  ⑥ 报表行整店落 catalog.item_report_rows(真跑才写):catalog_sync 扫店后用它兜底 ——
     报表里 PUBLISHED 而扫描没见到的 SKU 单查补入,在线品不许因分页 / 切片漏掉。

候选店:缺省 = 能调 API 的店里、在售行有 item_id 为空的店;`all=1` 全店;`store=X` 单店。
新上架的品要等沃尔玛 published 才有 itemId,所以「上架后补齐」不挂在 list_new 尾巴上,
靠每天一轮自然覆盖;缺席后复现的行由 catalog_sync 把 item_id 重置为 NULL,同样自然回来。
飞书「在线产品总表」的 itemId 列不在这里写:catalog_sync 每轮把 PG 投影回飞书,05:00 填好、
06:40 日报链的 catalog_sync 一跑就带上了。

**不复用**后台(Seller Center)/ Scheduler 生成的报表(所有者定稿 2026-09-07):只认自己
POST 的 requestId。全量靠对账不靠参数:请求体不传行过滤器,「拿全没有」用报表 SKU 集合 ×
catalog_sync 扫回来的在架集合来证明;数据范围按哪个日期列筛官方没写,老品掉出窗口会
体现为「疑似不全」,那时把 data_days 放到 729(官方两年上限留一天余量)。

失败处理走店级重试标准(conventions §四):跨店并发 → 凭证失效跳店 → 其余失败店跑完
别人后串行补试一次(补试进来先查台账,已建的报表接着等,不会二次创建)→ 仍失败按
diagnose 归缺席、首行点名、不炸整轮;零店完成才判失败。限流(429)与超时不是异常:
它们是本轮的正常结局,写在摘要里,下轮再来。

`--dry-run` 的边界:本工作流 DANGEROUS=False(创建报表不改任何商品),cli 恒给
execute=True、另传 dry_run —— 空跑**不创建报表、不下载、不写库**,只报候选与台账。
`probe=1` 要真跑(它会创建并下载一份报表),只是不写 item_id;台账行停在 ready,
紧接着的真跑直接下载,不再创建。
"""

import logging
import time
from collections import Counter

from api import reports
from registry import db, paths
from services import item_reports as ir, notify_fmt as nf, store_retry, \
    stores as stores_svc, walmart_catalog
from services.params import flag

DANGEROUS = False       # 只读沃尔玛(创建报表不改商品);写库只有 item_id 与台账
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

logger = logging.getLogger("workflows.item_id_sync")

WORKFLOW = "item_id_sync"
_PROBE_SAMPLE = 3


def _result(name: str, outcome: str, **kw) -> dict:
    out = {"store": name, "outcome": outcome, "note": "", "counters": {}}
    out.update(kw)
    return out


def _one_store(store: dict, wait_min: int, poll_secs: int, probe: bool,
               data_days: int = ir.DATA_RANGE_DAYS, renew: bool = False) -> dict:
    """输入:店铺 + 等待参数 + 是否探针(+ 数据范围天数 + 是否作废在途行重建)
    → 输出:该店结果 dict(outcome ∈ applied/probe/quota/timeout/error)。

    网络类异常**抛出**交店级补试;补试进来先查台账,已建的报表接着等,不二次创建。
    """
    name = store["name"]
    t0 = time.monotonic()
    window = None
    with db.pg_conn() as conn:
        ir.expire_stale(conn, name)
        row = ir.open_request(conn, name)
        if row is not None and renew:
            # 改了请求形状(如数据范围),在途的那份报表已没用:作废重建,不接着等
            ir.mark_error(conn, row["id"], f"superseded: renew=1,原 requestId={row['request_id']}")
            logger.info("店铺 %s 台账在途报表 %s(%s)按 renew=1 作废,重新创建",
                        name, row["request_id"], row["status"])
            row = None
        if row is None:
            window = ir.data_window(data_days)
            row_id = ir.record_pending(conn, name, ir.REPORT_TYPE, ir.REPORT_VERSION,
                                       WORKFLOW, note=f"dataStartTime={window[0]} "
                                                      f"dataEndTime={window[1]}")
            request_id, status, submitted_at = None, "pending", None
        else:
            row_id, request_id = row["id"], row["request_id"]
            status, submitted_at = row["status"], row["submitted_at"]
            logger.info("店铺 %s 台账有未落定报表 %s(%s),接着用,不重建",
                        name, request_id, status)

    if not request_id:
        # 先落 pending 再调接口(CLAUDE.md 安全红线);POST 不自动重试
        try:
            data = reports.create_report_request(store, ir.REPORT_TYPE, ir.REPORT_VERSION,
                                                 data_start=window[0], data_end=window[1])
        except reports.ReportQuotaError as e:
            # 沃尔玛 429,或本地桶已记过这小时那一枚:本轮结局,不抛、不补试
            # (2026-09-07 生产实见:抛出去进串行补试,在创建桶里睡 3595 秒)
            with db.pg_conn() as conn:
                ir.mark_error(conn, row_id, f"quota: {e}")
            return _result(name, "quota", note=str(e))
        except reports.ReportRequestError as e:
            with db.pg_conn() as conn:
                ir.mark_error(conn, row_id, f"create: {e.status}: {e}")
            if e.status is not None and 400 <= e.status < 500:
                # 请求形状被拒(415/400…):确定性错误,重试只会再被拒一次
                return _result(name, "error",
                               note=f"创建被沃尔玛拒绝({e.status},请求形状问题,重试无用):{e}")
            raise                       # 5xx / 网络未达:交串行补试
        except Exception as e:
            # 网络类异常上抛给串行补试;补试进来若 POST 其实已到沃尔玛(响应丢了),
            # requestId 我们不知道 —— 本地桶已记一枚,补试会得到 quota 结局;
            # 台账那行转 error,下一小时(或明天)重建一份
            with db.pg_conn() as conn:
                ir.mark_error(conn, row_id, f"create: {e.__class__.__name__}: {e}")
            raise
        request_id = str(data["requestId"])
        with db.pg_conn() as conn:
            ir.mark_submitted(conn, row_id, request_id)
        status, submitted_at = "submitted", None
        logger.info("店铺 %s ITEM 报表已提交 requestId=%s(数据范围 %s ~ %s)",
                    name, request_id, window[0], window[1])

    if status in ("pending", "submitted"):
        state, polls = ir.wait_ready(store, request_id, wait_min=wait_min,
                                     poll_secs=poll_secs)
        if state == "ERROR":
            with db.pg_conn() as conn:
                ir.mark_error(conn, row_id, "walmart: requestStatus=ERROR")
            return _result(name, "error", note="沃尔玛报表生成失败(ERROR),明天重建")
        if state == "TIMEOUT":
            return _result(name, "timeout",
                           note=f"等 {wait_min} 分钟未就绪(轮询 {polls} 次),"
                                f"requestId 留在台账,下轮接着等")
        with db.pg_conn() as conn:
            ir.mark_ready(conn, row_id)

    url, _exp = reports.get_download_url(store, request_id)
    blob = reports.download_report(url, store["proxy"])
    blob_info = None
    if probe:
        # 探针原件留存 + 体检:「报表只有 1 行」要拿原件才分得清是沃尔玛只给了 1 行
        # 还是解析吞了(2026-09-07 C021:55 列对上、在架 1490 行却只解析出 1 行)
        dump = paths.item_report_dump_file(name, request_id,
                                           ".zip" if blob[:2] == b"PK" else ".csv")
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(blob)
        blob_info = {**reports.report_blob_info(blob), "dump": str(dump)}
        logger.info("探针 %s 报表原件已存 %s(%d 字节)", name, dump, len(blob))
    rows = reports.parse_report_csv(blob)
    header = list(rows[0].keys()) if rows else []
    if not rows:
        with db.pg_conn() as conn:
            ir.mark_error(conn, row_id, "empty: 报表 0 行")
        return _result(name, "error", note="报表 0 行(表头都没有),不写库")
    err, drift = ir.check_header(header)
    if err:
        with db.pg_conn() as conn:
            ir.mark_error(conn, row_id, f"header: {err}")
        return _result(name, "error", note=err)

    mapping, mcount = ir.map_item_ids(rows)
    with db.pg_conn() as conn:
        ir.mark_downloaded(conn, row_id, len(rows))
        current = walmart_catalog.item_id_map(conn, name)
        # 覆盖率分母 = 在售行(所有者定稿 2026-09-08);写入仍按全部在架行
        updates, pcount = ir.plan_updates(current, mapping, live=walmart_catalog.live_skus(conn, name))
        counters = {**mcount, **pcount}
        if probe:
            # 探针不写 item_id;台账停在 ready,紧接着的真跑直接下载不重建
            outcome = "probe"
            recon = ir.reconcile_breakdown(walmart_catalog.in_catalog_profile(conn, name), rows)
        else:
            walmart_catalog.set_item_ids(conn, name, updates)
            # 报表行整店落库:catalog_sync 的报表兜底(在线品不许因分页漏掉)读它
            walmart_catalog.replace_report_rows(conn, name, request_id,
                                                ir.report_rows(rows, mapping), submitted_at)
            ir.mark_applied(conn, row_id, counters, note=drift)
            outcome = "applied"
    res = _result(name, outcome, counters=counters, note=drift,
                  elapsed_min=(time.monotonic() - t0) / 60, request_id=request_id,
                  window=window)
    if probe:
        res["blob"] = blob_info
        res["header"] = header
        res["sample"] = [(reports.report_row_sku(r), reports.item_id_from_column(r),
                          reports.item_id_from_url(r)) for r in rows[:_PROBE_SAMPLE]]
        res["publish"] = dict(Counter(str(r.get("Publish Status") or "") for r in rows))
        res["dates"] = {c: ir.date_span(rows, c) for c in ir.DATE_COLUMNS}
        res["recon"] = recon
        res["lifecycle"] = dict(Counter(str(r.get("Lifecycle Status") or "") for r in rows))
    return res


def _store_line(r: dict) -> str:
    c = r["counters"]
    line = (f"  {r['store']}:报表 {c.get('total', 0)} 行,匹配在架 {c.get('matched', 0)},"
            f"填 {c.get('filled', 0)},改 {c.get('overwritten', 0)},"
            f"未匹配 {c.get('unmatched', 0)},无 ID {c.get('no_id', 0)}")
    for k, label in (("url_mismatch", "两列不一致跳过"), ("dup_conflict", "同 SKU 多 ID 跳过"),
                     ("extra", "报表多出(非在架)")):
        if c.get(k):
            line += f",{label} {c[k]}"
    if r.get("elapsed_min") is not None:
        line += f",耗时 {r['elapsed_min']:.0f} 分钟"
    cov = ir.coverage_note(c)
    if cov:
        line += f";{cov}"
    if r.get("note"):
        line += f";{r['note']}"
    return line


def _probe_lines(r: dict) -> list[str]:
    out = [f"探针 {r['store']}(requestId={r.get('request_id')}):表头 {len(r['header'])} 列"
           + ("(与 specs 原件一致)" if not r.get("note") else f";{r['note']}")]
    if r.get("window"):
        out.append(f"  本轮新建,数据范围 {r['window'][0]} ~ {r['window'][1]}")
    for col, d in (r.get("dates") or {}).items():
        # 哪一列的最早值贴着 dataStartTime,数据范围就是按哪列筛的
        out.append(f"  {col}:最早 {d['min']} 最晚 {d['max']},按年 {d['by_year']}"
                   + (f",无法解析 {d['unparsed']} 行(样本 {d['sample']!r})" if d["unparsed"] else ""))
    out.append(f"  Publish Status 分布:{r['publish']}")
    out.append(f"  Lifecycle Status 分布:{r['lifecycle']}")
    out.append(f"  样本(SKU, Item ID 列, URL 尾段):{r['sample']}")
    rc = r.get("recon")
    if rc:
        # 对账:覆盖率缺口是什么,拿两边名单分组看,不猜(所有者 2026-09-07)
        out.append(f"  对账·报表覆盖的在架行 {rc['matched']} 行:按库里 lifecycle/published {rc['matched_by_status']}")
        out.append(f"  对账·在架不在报表 {rc['unmatched']} 行:按库里 lifecycle/published {rc['unmatched_by_status']};"
                   f"样本 {rc['unmatched_sample']}")
        out.append(f"  对账·报表有但在架名单没有 {rc['extra']} 行:按报表 Lifecycle/Publish {rc['extra_by_status']};"
                   f"样本 {rc['extra_sample']}")
    b = r.get("blob") or {}
    if b:
        out.append(f"  原件 {b['bytes']} 字节,zip 成员 {b['members'] or '无(裸 CSV)'},取 {b['member']};"
                   f"CSV {b['csv_bytes']} 字节 / {b['lines']} 个换行,解析 {b['rows']} 行,"
                   f"首行最长字段 {b['longest_field']} 字符;原件已存 {b['dump']}")
        if b["rows"] <= 1 and b["lines"] > b["rows"] + 1:
            out.append(f"  ⚠ 换行 {b['lines']} 个却只解析出 {b['rows']} 行:多半是某个字段引号没闭合"
                       f"把后面全吞了(看首行最长字段),不是沃尔玛只给了 {b['rows']} 行")
        if len(b["members"]) > 1:
            out.append(f"  ⚠ zip 里有 {len(b['members'])} 个成员,只解析了 {b['member']}:可能是分片")
    out.append(_store_line({**r, "note": ""}).replace("  ", "  将写:", 1)
               + "(探针未写 item_id;台账停在 ready,去掉 probe 重跑直接下载)")
    return out


def run(params: dict) -> str:
    """输入:params(store/all/probe/wait_min/poll_secs/data_days/renew;cli 注入 dry_run)→ 输出:补齐摘要。"""
    dry_run = bool(params.get("dry_run"))
    want_all = flag(params, "all")
    probe = flag(params, "probe")
    renew = flag(params, "renew")
    only = params.get("store")
    wait_min = int(params.get("wait_min", 0) or ir.DEFAULT_WAIT_MIN)
    poll_secs = int(params.get("poll_secs", 0) or ir.DEFAULT_POLL_SECS)
    data_days = int(params.get("data_days", 0) or ir.DATA_RANGE_DAYS)
    if probe and not only:
        return "⛔ probe=1 必须带 store=X(探针是单店的:一份报表、一次人眼核对)"
    if probe and dry_run:
        return "⛔ probe 要真跑(它会创建并下载一份报表,只是不写 item_id);去掉 --dry-run"

    store_list = stores_svc.load_stores([only] if only else None)
    if not store_list:
        return f"店铺凭证未找到:{only or '(任一)'}"
    with db.pg_conn() as conn:
        gaps = walmart_catalog.stores_missing_item_id(conn)
        # 空跑要看台账在途,真跑里各店自己查(_one_store)
        inflight = {s["name"]: ir.open_request(conn, s["name"]) for s in store_list} \
            if dry_run else {}
    if want_all or only:
        cands = list(store_list)
    else:
        cands = [s for s in store_list if gaps.get(s["name"])]
    n_gap_rows = sum(gaps.get(s["name"], 0) for s in cands)

    if dry_run:
        lines = [f"🧪 [DRY-RUN] item_id_sync:候选 {len(cands)}/{len(store_list)} 店"
                 f"(缺口 {n_gap_rows} 行),真跑将各创建一份 ITEM 报表(v{ir.REPORT_VERSION[1:]},"
                 f"数据范围近 {data_days} 天)、等 ≤{wait_min} 分钟、只填/改这些店的 item_id;"
                 f"不建报表不写库" + (";renew=1:在途行作废重建" if renew else "")]
        for s in cands:
            n = s["name"]
            fl = inflight.get(n)
            lines.append(f"  {n}:缺口 {gaps.get(n, 0)} 行"
                         + (f";台账在途 {fl['status']} requestId={fl['request_id']}"
                            f"(真跑接着用,不重建)" if fl else ""))
        if not cands:
            lines.append("  无缺口:全部在架行已有 item_id;要对账整店用 -p all=1")
        return "\n".join(lines)

    if not cands:
        return ("item_id_sync:无缺口(全部在架行已有 item_id),本轮不请求报表;"
                "要对账整店用 -p all=1")

    workers = min(stores_svc.STORE_WORKERS, len(cands))
    results, dead, absent, gate_note = store_retry.fan_out(
        cands, lambda s: _one_store(s, wait_min, poll_secs, probe, data_days, renew), workers,
        log_label="报表补 item_id")

    by = {k: [r for r in results if r["outcome"] == k]
          for k in ("applied", "probe", "quota", "timeout", "error")}
    filled = sum(r["counters"].get("filled", 0) for r in by["applied"])
    over = sum(r["counters"].get("overwritten", 0) for r in by["applied"])
    unmatched = sum(r["counters"].get("unmatched", 0) for r in by["applied"])
    # ⚠ 首行 = 结论 + 最要紧的数;缺席点名也在首行(链通知只发成功步骤的首行)
    head = (f"item_id_sync:{len(by['applied'])}/{len(cands)} 店补齐,"
            f"填 {filled},改 {over}(报表为准),在架未匹配 {unmatched}"
            + (f",限流放弃 {len(by['quota'])} 店" if by["quota"] else "")
            + (f",超时待续 {len(by['timeout'])} 店" if by["timeout"] else "")
            + (f",⚠ 报表失败 {len(by['error'])} 店" if by["error"] else "")
            + (f",探针 {len(by['probe'])} 店(未写库)" if by["probe"] else "")
            + nf.absent_tail(absent, gate_note, tail="台账保留 requestId,下轮接着"))
    lines = [head]
    if gate_note:
        lines.append(gate_note)
    for r in sorted(by["applied"], key=lambda r: stores_svc.sort_key(r["store"])):
        lines.append(_store_line(r))
    for r in sorted(by["probe"], key=lambda r: stores_svc.sort_key(r["store"])):
        lines.extend(_probe_lines(r))
    for label, key in (("限流放弃(每类型每小时一次,明天再来)", "quota"),
                       ("超时待续", "timeout"), ("报表失败", "error")):
        for r in sorted(by[key], key=lambda r: stores_svc.sort_key(r["store"])):
            lines.append(f"  ⚠ {r['store']}:{label} —— {r['note']}")
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")
    if not results:
        # 零店完成不许报成功(与 catalog_sync 同款闸):全部店都没跑通意味着
        # 凭证表整体出了问题或接口形状变了,报成功没人会来看
        lines.append("⚠ 零店完成 —— 本轮没有拿到任何报表")
        raise RuntimeError("\n".join(lines))
    lines.append("飞书「在线产品总表」的 itemId 列由 catalog_sync 下一轮投影(06:40 / 13:00)")
    return "\n".join(lines)
