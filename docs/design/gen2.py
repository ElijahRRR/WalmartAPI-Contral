# -*- coding: utf-8 -*-
"""第二轮画板:业务域主页面全集(所有者 2026-08-18 反馈:覆盖面要全,
非定时工作流也要呈现,分配/类目映射/订单全貌等本项目独有链路补齐)。"""
import json, pathlib
from gen import (CSS, FONTS, DOTS, TONES, ICONS, ic, dot, pill, tag, card, kpi,
                 sidebar, topbar, page, shell, scrollbar)

ATLAS = json.loads(pathlib.Path("atlas.json").read_text(encoding="utf-8"))
BY = {a["name"]: a for a in ATLAS}

# ════════════════════════ 工作流全景(66 条,含非定时) ════════════════════════
ATLAS_GROUPS = [
    ("采集与目录", ["catalog_sync", "product_ingest", "product_refresh", "scrape_missing",
                    "brand_scrape", "product_query", "catalog_health", "node_backfill",
                    "pt_backfill", "pt_census", "sku_normalize", "taxonomy_import", "taxonomy_derive"]),
    ("产品审核", ["product_audit", "audit_why", "audit_calibrate", "audit_import", "audit_history_fold"]),
    ("类目映射", ["catmap_gap", "catmap_mine", "catmap_align", "catmap_suggest", "catmap_promote",
                  "catmap_fix", "catmap_prune", "catmap_export", "catmap_import"]),
    ("上架", ["list_new", "match_listing", "upc_sync", "sku_locked_heal", "variant_probe"]),
    ("分配与占用", ["alloc_stores", "alloc_products", "alloc_plan", "alloc_backfill",
                    "alloc_audit", "claim_audit", "store_release"]),
    ("订单与售后", ["order_sync", "order_audit", "returns_sync", "settlement_sync", "perf_problems",
                    "order_asin_normalize", "order_center_push", "order_center_cleanup", "order_history_import"]),
    ("黑名单与风控", ["risk_sync", "blacklist_push", "asin_blacklist_import"]),
    ("维护与清理", ["maintenance_scan", "maintenance", "problem_scan", "problem_product_cleanup", "product_clear"]),
    ("报表与备份", ["daily_report", "backup", "kpi_history_import", "cleanup_history_import"]),
    ("基础设施", ["feed_poll", "ping_stores", "db_init", "init_data_root", "launchd_install", "skill_export"]),
]

def atlas_board():
    cols = [[], [], []]
    order = [0, 5, 2, 3, 1, 4, 6, 7, 8, 9]  # 按高度手排三列
    col_of = {0: 0, 5: 1, 2: 1, 3: 2, 1: 2, 4: 0, 6: 2, 7: 0, 8: 2, 9: 1}
    for gi in order:
        gname, names = ATLAS_GROUPS[gi]
        rows = []
        for n in names:
            a = BY[n]
            marks = (tag("red", "危") if a["dangerous"] else "") + \
                    (tag("sky", "调") if a["scheduled"] else "")
            doc = a["doc"].split("——")[-1].split("—")[-1].strip().rstrip("。")
            doc = doc.replace("**危险:缺省即真跑,空跑用 `--dry-run`。**", "").replace("**", "")
            if len(doc) > 34: doc = doc[:33] + "…"
            rows.append(f'''<div style="display:flex;align-items:center;gap:8px;padding:5px 12px;border-bottom:1px solid #f4f4f5">
              <span class="id" style="width:168px;flex:none;font-weight:500;color:#18181b">{n}</span>
              <span style="display:inline-flex;gap:4px;flex:none;width:66px">{marks}</span>
              <span style="flex:1;min-width:0;font-size:11.5px;color:#71717a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{doc}</span>
            </div>''')
        n_d = sum(1 for n in names if BY[n]["dangerous"])
        head = f'{gname} · {len(names)}' + (f'(危 {n_d})' if n_d else "")
        cols[col_of[gi]].append(card(head, "".join(rows)))
    grid = "".join(f'<div style="flex:1;display:flex;flex-direction:column;gap:16px">{"".join(c)}</div>' for c in cols)
    legend = (f'<div class="card" style="padding:12px 16px;display:flex;align-items:center;gap:18px;flex:none">'
              f'<span style="font-size:13px;color:#27272a"><b>66 条全部可从这里触发</b> —— 定时只是其中 21 条的“自动挡”,其余 45 条本来就是手动跑的,同样是前端的一等公民</span>'
              f'<span style="flex:1"></span>{tag("red", "危")}<span style="font-size:12px;color:#71717a">写沃尔玛/写库,强制两步(20 条)</span>'
              f'{tag("sky", "调")}<span style="font-size:12px;color:#71717a">在自动调度里(21 条)</span><span style="font-size:11px;color:#a1a1aa;margin-left:12px">⚠ 两套缺省并存:危险类缺省真跑(空跑 --dry-run);回填类(sku_normalize / node_backfill / pt_backfill / order_asin_normalize)缺省预览(apply=1 才写)—— 按钮文案按各自语义</span></div>')
    content = legend + f'<div style="display:flex;gap:16px;align-items:flex-start">{grid}</div>'
    return shell(page("wf", ["工作流", "全景 66 条"], content, 1340), 1440, 1340)

# ════════════════════════ 运行记录 + 调度(只读) ════════════════════════
def runs_board():
    jobs = [
        ("launchd", "feed_poll", "每小时 :00/:30", "feed_poll", ("emerald", "成功 14:30")),
        ("launchd", "order_chain", "每小时 :20", "order_sync · order_audit · returns_sync", ("emerald", "成功 14:20")),
        ("gpt", "backup", "每天 02:00", "backup", ("emerald", "成功 02:03")),
        ("gpt", "daily_report", "每天 06:40", "daily_report", ("emerald", "成功 06:52")),
        ("gpt", "order_daily", "每天 07:30", "perf_problems · order_asin_normalize", ("emerald", "成功 07:35")),
        ("gpt", "product_chain", "每天 13:00", "catalog_sync · product_refresh · product_ingest · maintenance_scan · maintenance · problem_scan · problem_product_cleanup", ("emerald", "成功 14:48")),
        ("gpt", "blacklist", "每天 15:00", "risk_sync · blacklist_push", ("emerald", "成功 15:03")),
        ("gpt", "product_clear", "每天 15:00", "product_clear", ("red", "失败 15:00")),
        ("gpt", "audit_sheet", "每天 18:10", "product_audit", (None, "今晚")),
        ("gpt", "list_new", "每天 20:00", "list_new", (None, "今晚")),
        ("gpt", "settlement", "每周三 08:00", "settlement_sync", (None, "08-19")),
    ]
    jrows = []
    for runner, label, when, wfs, (tone, note) in jobs:
        state = pill(tone, note) if tone else f'<span style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#a1a1aa">{ic("Clock", 12, "#a1a1aa")}{note}</span>'
        rtag = tag("gray", "launchd") if runner == "launchd" else tag("violet", "智能体")
        jrows.append(f'<tr class="rz"><td class="td" style="width:86px">{rtag}</td>'
                     f'<td class="td id" style="width:110px;font-weight:500">{label}</td>'
                     f'<td class="td" style="width:110px;font-size:12px;color:#52525b">{when}</td>'
                     f'<td class="td" style="font-size:11.5px;color:#71717a;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{wfs}</td>'
                     f'<td class="td" style="width:110px">{state}</td></tr>')
    sched = card("调度一览 · 11 条(只读 —— 权威在 registry/schedule.py;同一条链绝不许 launchd 与智能体两个泳道都挂,撞了后到的每轮空跑且报成功)",
                 f'<table><tr><th class="th">跑在哪</th><th class="th">任务</th><th class="th">时间</th><th class="th">工作流链</th><th class="th">上次结果</th></tr>{"".join(jrows)}</table>')

    runs = [
        ("#18492", "list_new", "web", "20:00:58", "5m28s", ("emerald", False, "成功 0"), "9 店提交 442 条…"),
        ("#18491", "product_clear", "launchd", "15:00:02", "22s", ("red", False, "失败 1"), "PG 连接数触顶(129/100)"),
        ("#18490", "blacklist", "launchd", "15:00:01", "2m33s", ("emerald", False, "成功 0"), "品牌黑名单 +3;已推飞书"),
        ("#18489", "feed_poll", "launchd", "14:30:00", "1m02s", ("emerald", False, "成功 0"), "2 feed 落定 301 SKU"),
        ("#18488", "order_chain", "launchd", "14:20:00", "9m17s", ("emerald", False, "成功 0"), "42/43 店 3,368 订单行"),

        ("#18486", "product_chain", "manual", "13:00:00", "1h48m", ("emerald", False, "成功 0"), "7 步全过;建议 214"),
        ("#18485", "prodct_audit", "manual", "12:41:07", "0s", ("violet", False, "配置错 2"), "工作流名写错(应为 product_audit)"),
        ("#18484", "audit_why", "web", "11:32:44", "3s", ("emerald", False, "成功 0"), "B0BSNKKR6T:R2 禁售大类 -100 → rejected;R4 黑名单命中记为证据"),
    ]
    rrows = "".join(
        f'<tr class="rc"><td class="td id" style="color:#71717a">{rid}</td>'
        f'<td class="td id" style="font-weight:500">{wf}</td>'
        f'<td class="td">{tag("gray", op)}</td><td class="td id" style="color:#71717a">{t}</td>'
        f'<td class="td num" style="color:#71717a">{dur}</td><td class="td">{pill(st[0], st[2], hollow=st[1])}</td>'
        f'<td class="td" style="font-size:12px;color:#52525b;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{summ}</td></tr>'
        for rid, wf, op, t, dur, st, summ in runs)
    runsc = card("运行记录 · ops.runs(点行进详情;⚠ 退出码 3「没抢到锁」那一轮不写 ops.runs —— 这里查不到是正常的,连续两次退 3 才值得追)",
                 f'''<div style="padding:10px 16px;display:flex;gap:8px;border-bottom:1px solid #f4f4f5">
                   <span class="btn sm bs">工作流:全部</span><span class="btn sm bs">结局:全部</span>
                   <span class="btn sm bs">触发方:全部</span><span class="btn sm bs">今天</span>
                   <span style="flex:1"></span><span style="font-size:12px;color:#a1a1aa;align-self:center">今天 46 次运行 · 失败 1</span></div>
                 <table><tr><th class="th">run</th><th class="th">工作流</th><th class="th">触发</th><th class="th">开始</th><th class="th" style="text-align:right">耗时</th><th class="th">结局</th><th class="th">摘要首行</th></tr>{rrows}</table>''')
    content = sched + runsc
    return shell(page("runs", ["运行记录与调度"], content, 1100), 1440, 1100)


# ════════════════════════ 产品列表 ════════════════════════
def productlist_board():
    filters = "".join(f'<span class="btn sm bs">{f}</span>' for f in
        ["店铺:全部", "审核:全部", "在架:全部", "品牌", "类目", "有无采集数据", "货期 ≤", "价格区间"])
    head = f'''<div style="display:flex;gap:8px;align-items:center;flex:none">
      {filters}<span style="flex:1"></span>
      <span class="btn sm bghost">紧凑</span><span class="btn sm bs">舒适</span><span class="btn sm bs">列</span></div>'''
    rows_data = [
        ("B0CHXNPXVX", "Anker 65W USB C Charger, 3-Port Foldable…", "Anker", "$23.99", "$6.99", "$30.98", "2", ("emerald", "approved", False), "A085 在架", "08-18 09:12"),
        ("B0BSNKKR6T", "Sony WH-1000XM5 Wireless Noise Canceling…", "SONY", "$328.00", "$0.00", "$328.00", "1", ("red", "rejected", False), "—", "08-18 07:44"),
        ("B0CJM1WPXR", "Building Blocks Set 1080 Pcs Classic…", "未知", "$35.99", "$8.49", "$44.48", "4", ("amber", "pending", True), "—", "08-18 06:02"),
        ("B09XM4NP2Q", "Kitchen Storage Rack 3-Tier Standing…", "HomeKit", "$42.50", "$11.99", "$54.49", "3", ("emerald", "approved", False), "A117 在架", "08-17 22:15"),
        ("B0D1KPLM88", "LED Strip Lights 100ft Music Sync…", "Daybetter", "$18.99", "$5.49", "$24.48", "2", ("gray", "未审", False), "—", "08-17 21:40"),
        ("B08HJKL223", "Pet Grooming Kit Vacuum Suction 99%…", "oneisall", "$89.99", "$0.00", "$89.99", "6", ("amber", "pending", True), "—", "08-17 20:11"),
        ("B0CQW2RT45", "Air Fryer 5.8QT Digital Touchscreen…", "COSORI", "$99.99", "$14.20", "$114.19", "2", ("red", "rejected", False), "—", "08-17 18:33"),
        ("B07YMN6P31", "Yoga Mat Non-Slip Eco Friendly TPE…", "Gaiam", "$21.98", "$4.99", "$26.97", "1", ("emerald", "approved", False), "A102 在架", "08-17 15:27"),
        ("B0BPQ88XYZ", "Car Phone Holder Mount Dashboard…", "无快照", "—", "—", "—", "—", ("gray", "未审", False), "—", "缺采集"),
        ("B0CC71GH02", "Stainless Steel Water Bottle 32oz…", "IRON °FLASK", "$27.95", "$6.50", "$34.45", "3", ("emerald", "approved", False), "A140 在架", "08-17 11:08"),
        ("B09ZZKL776", "Baby Monitor 2K Pan-Tilt-Zoom Camera…", "eufy", "$159.99", "$0.00", "$159.99", "5", ("amber", "pending", True), "—", "08-17 09:52"),
        ("B0BXY44Q19", "Electric Toothbrush Sonic 8 Brush Heads…", "Aquasonic", "$29.99", "$5.99", "$35.98", "2", ("red", "rejected", False), "—", "08-16 23:44"),
    ]
    trs = []
    for asin, title, brand, price, ship, landed, lead, (tone, alab, dash), listed, snap in rows_data:
        miss = 'color:#b45309' if snap == "缺采集" else 'color:#71717a'
        listed_html = pill("emerald", listed) if "在架" in listed else '<span style="color:#a1a1aa;font-size:12px">—</span>'
        trs.append(f'''<tr class="rc"><td class="td id">{asin}</td>
          <td class="td" style="max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{title}</td>
          <td class="td" style="font-size:12px">{brand}</td>
          <td class="td num">{price}</td><td class="td num">{ship}</td><td class="td num" style="font-weight:600">{landed}</td>
          <td class="td num">{lead}</td><td class="td">{tag(tone, alab, dashed=dash)}</td>
          <td class="td">{listed_html}</td><td class="td id" style="{miss}">{snap}</td></tr>''')
    table = card("产品 · catalog.products ⋈ latest_snapshot ⋈ walmart_items", f'''
      <table><tr><th class="th">ASIN</th><th class="th">标题(亚马逊侧)</th><th class="th">品牌</th>
      <th class="th" style="text-align:right">采集价</th><th class="th" style="text-align:right">运费</th>
      <th class="th" style="text-align:right">落地价</th><th class="th" style="text-align:right">货期</th>
      <th class="th">审核</th><th class="th">沃尔玛侧</th><th class="th">最新快照</th></tr>{"".join(trs)}</table>
      <div style="display:flex;align-items:center;gap:12px;padding:10px 16px">
        <span style="font-size:12px;color:#71717a">128,455 行 · 服务端分页 · 第 1 / 6,423 页</span>
        <span style="flex:1"></span>
        <span class="btn sm bs">上一页</span><span class="btn sm bs">下一页</span></div>''',
      action='<span style="font-size:12px;color:#a1a1aa">虚拟滚动 + 服务端分页,筛选都走索引</span>')
    note = (f'<div class="card" style="padding:12px 16px;display:flex;align-items:center;gap:10px;flex:none">'
            f'{ic("Play", 14, "#3f3f46")}<span style="font-size:12.5px;color:#3f3f46">当前筛选结果可直接作为工作流入参:'
            f'</span><span class="btn sm bs">缺采集的 → scrape_missing</span><span class="btn sm bs">未审的 → product_audit</span>'
            f'<span style="font-size:11px;color:#a1a1aa">(跳到工作流面板,参数已带上;危险的照旧走两步)</span></div>')
    content = head + table + note
    return shell(page("products", ["产品", "列表"], content, 1000), 1440, 1000)

# ════════════════════════ Feed 追踪 ════════════════════════
def feedtracker_board():
    kpis = "".join([
        kpi("在途", "7", "pending 1 + submitted 6", tone="amber"),
        kpi("今日落定", "2", "301 SKU · feed_poll :00/:30"),
        kpi("今日失败 SKU", "17", "PROHIBITED ×14 · PRICE ×3", tone="red"),
        kpi("pending 超 6h", "1", "告警升级:人工核对后处理", tone="amber"),
    ])
    frows = []
    for fid, ftype, store, wf, st, n, res, age, warn in [
        ("6f9c41d2", "inventory", "A085", "maintenance", ("amber", "pending", True), "301", "—", "3d 2h", True),
        ("8f31c2d9", "MP_ITEM", "A085", "list_new", ("sky", "submitted", False), "61", "—", "42m", False),
        ("b7e04a11", "MP_ITEM", "A102", "list_new", ("red", "failed", False), "55", "41 ✓ / 14 ✗", "38m", False),
        ("c94d7f02", "MP_ITEM", "A117", "list_new", ("sky", "submitted", False), "52", "—", "35m", False),
        ("d2c807aa", "price", "A093", "maintenance", ("emerald", "done", False), "148", "148 ✓", "6h", False),
        ("e2a91b6d", "MP_MAINTENANCE", "A140", "maintenance", ("emerald", "done", False), "153", "150 ✓ / 3 missing(回执查无此 SKU,独立终态非错误)", "7h", False),
        ("f4b3a980", "DELETE_ITEM", "A128", "problem_product_cleanup", ("sky", "submitted", False), "23", "—", "1h", False),
    ]:
        style = 'background:#fffbeb' if warn else ''
        frows.append(f'''<tr class="rz" style="{style}"><td class="td id" style="font-weight:500">{fid}</td>
          <td class="td id">{ftype}</td><td class="td id">{store}</td><td class="td id" style="color:#71717a">{wf}</td>
          <td class="td">{tag(st[0], st[1], dashed=st[2])}</td><td class="td num">{n}</td>
          <td class="td" style="font-size:12px">{res}</td><td class="td num" style="{'color:#b45309;font-weight:600' if warn else 'color:#71717a'}">{age}</td></tr>''')
    feeds = card("feed 台账 · ops.feed_log(行点开 → feed_items SKU 级 → feed_item_errors 错误码)", f'''
      <div style="padding:10px 16px;display:flex;gap:8px;border-bottom:1px solid #f4f4f5">
        <span class="btn sm bs">状态:全部</span><span class="btn sm bs">类型:全部</span><span class="btn sm bs">店铺:全部</span>
        <span class="btn sm bdghost" style="border:1px dashed #fecaca;border-radius:6px">⏱ pending 超 6h · 1</span>
        <span style="flex:1"></span></div>
      <table><tr><th class="th">feedId</th><th class="th">类型</th><th class="th">店铺</th><th class="th">提交方</th>
      <th class="th">状态</th><th class="th" style="text-align:right">SKU</th><th class="th">SKU 级结果</th>
      <th class="th" style="text-align:right">在途时长</th></tr>{"".join(frows)}</table>
      <div style="padding:10px 16px;font-size:11px;color:#b45309;background:#fffbeb;border-top:1px solid #fde68a">
      ⚠ pending 的 feed 是「提交结局不确定」:系统不自动补交(写操作宁停不重)。它的症状是那批 SKU 每轮被报「在途防重跳过 N 个」而 N 一直不变 —— 看着像正常防重,实际是再也发不出去了。这里把它单独标黄(超 6 小时即告警)。feed 已终态但个别 SKU 仍 INPROGRESS 时 feed_log 保持 submitted 留待下轮再查 —— 是设计不是卡死。诊断入口:feed_poll -p feed_id=… 只打印补明细,不动状态不回写飞书。</div>''')
    errs = card("错误码排行 · ops.v_feed_error_stats(近 7 天)", '''<div style="padding:12px 16px;display:flex;flex-direction:column;gap:8px">''' +
        "".join(f'''<div style="display:flex;align-items:center;gap:10px">
          <span class="id" style="width:230px;flex:none">{code}</span>
          <div style="flex:1;height:16px;border-radius:3px;background:#f4f4f5"><div style="height:16px;border-radius:3px;background:{c};width:{w}%"></div></div>
          <span class="id" style="width:40px;text-align:right">{n}</span>
          <span style="width:280px;flex:none;font-size:11px;color:#71717a">{note}</span></div>'''
        for code, n, w, c, note in [
            ("PROHIBITED_PRODUCT", "14", 100, "#ef4444", "违禁判定 → problem_scan 会归类,品牌可能进黑名单"),
            ("INVALID_PRICE", "3", 21, "#f59e0b", "价格越界 → 查限价规则"),
            ("SKU_LOCKED", "2", 14, "#f59e0b", "锁死 → sku_locked_heal 自愈链"),
            ("MISSING_ATTRIBUTE", "1", 7, "#a1a1aa", "规格缺属性 → mp_conform 回看"),
        ]) + "</div>")
    content = f'<div style="display:flex;gap:16px;flex:none">{kpis}</div>' + feeds + errs
    return shell(page("feeds", ["Feed 追踪"], content, 1000), 1440, 1000)

# ════════════════════════ 黑名单中心 ════════════════════════
def blacklist_board():
    query = f'''<div class="card" style="padding:16px;display:flex;align-items:center;gap:12px;flex:none">
      <div style="position:relative;flex:1;max-width:520px">
        <span style="position:absolute;left:10px;top:10px">{ic("Search", 14, "#a1a1aa")}</span>
        <div class="mono" style="height:36px;border:1px solid #d4d4d8;border-radius:6px;display:flex;align-items:center;padding:0 10px 0 30px;font-size:13px">SONY</div>
      </div>
      <span class="btn bp">查是否被拦</span>
      <div style="display:flex;align-items:center;gap:8px;padding:0 12px;height:36px;background:#fef2f2;border:1px solid #fecaca;border-radius:6px">
        {dot("red")}<span style="font-size:13px;color:#b91c1c;font-weight:500">命中 brand_blacklist(casefold 归一后精确匹配)</span>
        <span style="font-size:12px;color:#b91c1c">2026-05-12 入 · 来源:飞书人工归拢 · 今天拦下 12 次上架</span></div>
    </div>'''
    def bl(title, direction, dtone, count, note, rows):
        rr = "".join(f'<div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid #f4f4f5">'
                     f'<span class="id" style="flex:1">{a}</span><span style="font-size:11px;color:#71717a">{b}</span></div>' for a, b in rows)
        return card(f'{title} · {count}', f'''<div style="padding:8px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #f4f4f5">
          {tag(dtone, direction)}<span style="font-size:11px;color:#71717a">{note}</span></div>{rr}''')
    cards = f'''<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      {bl("品牌总清单 brand_blacklist", "飞书 → 库 · 只增改", "sky", "42,318", "人工归拢;R4 扫描证据 + 上架拦截;与渠道表不去重",
          [("SONY", "05-12 · 知产风险"), ("LEGO", "03-02 · 知产风险"), ("Nike", "02-14 · 品牌方投诉史")])}
      {bl("后台报错自产 brand_err_hits", "库 → 飞书 · 投影", "violet", "1,207(今天 +3)", "程序自产渠道,是另一张表不是总表的视图;pushed_at 空 = 未投影",
          [("LEGO(err PROHIBITED)", "今天 · b7e04a11 回执"), ("Disney(err IP_CLAIM)", "昨天"), ("Marvel(err PROHIBITED)", "昨天")])}
      {bl("永久禁售 asin_blacklist", "库自产 + 导入", "gray", "18,442", "只收永久类 B禁售/C品牌/E知产/F限类/G药品/K审查(+LEGACY);biz_cn 单独开关",
          [("B09XM4NP2Q · 知产", "07-19 · problem_scan 归类"), ("B0BSNKKR6T · 品牌", "05-12 · 随品牌入"), ("B07QQ81MZL · 药品", "04-30 · 导入")])}
      {bl("卖家与类目 seller_blacklist / amazon_cat_blacklist", "飞书 → 库 · 全量重灌", "sky", "312 + 47", "TRUNCATE 重灌(飞书删行跟着消失);骤缩 >50% ⛔停手保旧数据,不是失败",
          [("SellerID A2K8…QX", "黑名单卖家"), ("Health › Vitamins", "整类不上"), ("Weapons & Knives", "整类不上")])}
    </div>'''
    stat = card("今天的拦截 · 86 次(上架七道闸的黑名单档)", '''<div style="padding:12px 16px;font-size:12.5px;color:#52525b;line-height:2">
      brand_blacklist 61 · asin_blacklist 14 · brand_err_hits 8 · amazon_cat_blacklist 3 —— 点数字进上架漏斗的被拦明细。</div>''')
    warn = ('<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#b45309;background:#fffbeb;'
            'border:1px solid #fde68a;border-radius:6px;padding:10px 12px;flex:none">'
            + ic("AlertTriangle", 13, "#b45309") +
            '<b>入库那一刻拦截就生效了</b>(上架和审核读的是数据库);飞书表格只是投影,晚两小时更新 —— 「表格没更新」不等于「还没生效」。'
            '</div>')
    content = query + warn + cards + stat
    return shell(page("blacklist", ["黑名单中心"], content, 1000), 1440, 1000)

# ════════════════════════ 采集监控 ════════════════════════
def scrape_board():
    kpis = "".join([
        kpi("今天推送", "3 批 · 14,208 ASIN", "插队只有两档:订单/审核走 10,补采走 0"),
        kpi("已回", "12,384", "87.2%"),
        kpi("超时批次", "1", "zip=10019 · 1,208 未回", tone="amber"),
        kpi("今日失败 ASIN", "1,162", "验证码占 35%", tone="red"),
    ])
    brows = []
    for bid, zipc, wf, st, total, back, t in [
        ("b-4471", "97035", "product_refresh", ("emerald", "completed"), "9,800", "9,800", "13:02 → 14:31"),
        ("b-4472", "10019", "product_refresh", ("amber", "timeout"), "3,200", "1,992", "13:02 → 15:32 · 已回的照常入库,只是不再等 —— 别当失败重推"),
        ("b-4473", "60614", "product_audit 同轮闭环", ("sky", "running"), "820", "413", "18:14 →"),
        ("b-4474", "97035", "scrape_missing", ("sky", "pushed"), "388", "0", "18:40 →"),
        ("b-4468", "33101", "brand_scrape", ("red", "failed"), "150", "0", "昨天 · 服务端 500"),
    ]:
        brows.append(f'<tr class="rz"><td class="td id" style="font-weight:500">{bid}</td><td class="td id">{zipc}</td>'
                     f'<td class="td" style="font-size:12px;color:#71717a">{wf}</td><td class="td">{tag(st[0], st[1], dashed=st[1] in ("running", "pushed"))}</td>'
                     f'<td class="td num">{total}</td><td class="td num">{back}</td><td class="td id" style="color:#71717a">{t}</td></tr>')
    batches = card("采集批次 · ops.scrape_batches(全项目共用台账,按批次名前缀圈自己的链;completed ≠ 数据到位 —— 还隔「增量导出 → product_ingest」两跳,数据到没到看 snapshots)",
        f'<table><tr><th class="th">批次</th><th class="th">邮编</th><th class="th">推送方</th><th class="th">状态</th>'
        f'<th class="th" style="text-align:right">推送</th><th class="th" style="text-align:right">已回</th><th class="th">时间</th></tr>{"".join(brows)}</table>')
    fr = [
        ("captcha 验证码", 412, 100, "#f59e0b", "换时段重采能好;冷却 14 天不是可选项", "emerald", "可重采"),
        ("timeout 超时", 388, 94, "#f59e0b", "看采集端负载", "emerald", "可重采"),
        ("network 网络", 201, 49, "#a1a1aa", "抖动", "emerald", "可重采"),
        ("blocked 被拦", 87, 21, "#ef4444", "403/503 异常流量(非验证码)", "amber", "换时段"),
        ("parse_error 解析失败", 44, 11, "#ef4444", "页面拿到但解不出 → 修采集器", "red", "别重采"),
        ("variant_offset 变体偏移", 9, 2, "#ef4444", "成因非确定性,立即重试结果一样", "red", "别重采"),
        ("unknown 兜底桶", 21, 5, "#a1a1aa", "采集端新增错误类先落这;拿不准别下终局结论", "gray", "不归档"),
    ]
    fails = card("失败原因分布 · ops.scrape_failures(error_type 封闭集 11 类 + unknown;处置各不同 —— 别统一显示成「采集失败」。not_found 不在这里:404 走成功路径,在 snapshots.outcome 里)",
        '<div style="padding:12px 16px;display:flex;flex-direction:column;gap:8px">' +
        "".join(f'''<div style="display:flex;align-items:center;gap:10px">
          <span style="width:110px;flex:none;font-size:13px">{name}</span>
          <div style="flex:1;height:16px;border-radius:3px;background:#f4f4f5"><div style="height:16px;border-radius:3px;background:{c};width:{w}%"></div></div>
          <span class="id" style="width:44px;text-align:right">{n}</span>
          <span style="width:70px;flex:none">{tag(t2, lab)}</span>
          <span style="width:250px;flex:none;font-size:11px;color:#71717a">{note}</span></div>'''
        for name, n, w, c, note, t2, lab in fr) + "</div>")
    content = f'<div style="display:flex;gap:16px;flex:none">{kpis}</div>' + batches + fails
    return shell(page("scrape", ["采集监控"], content, 1000), 1440, 1000)

# ════════════════════════ 维护与清理中心 ════════════════════════
def maint_board():
    steps = f'''<div class="card" style="padding:14px 16px;display:flex;align-items:center;gap:0;flex:none">
      <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("sky", "suggested", dashed=True)}<span class="kpival" style="font-size:20px">214</span><span style="font-size:12px;color:#71717a">已建议,待 maintenance 执行</span></div>
      <div style="width:36px;text-align:center;color:#d4d4d8">{ic("ChevronRight", 16, "#d4d4d8")}</div>
      <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("amber", "executing", dashed=True)}<span class="kpival" style="font-size:20px">37</span><span style="font-size:12px;color:#71717a">已提交 feed,等生效核验</span></div>
      <div style="width:36px;text-align:center;color:#d4d4d8">{ic("ChevronRight", 16, "#d4d4d8")}</div>
      <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("emerald", "confirmed")}<span class="kpival" style="font-size:20px">51</span><span style="font-size:12px;color:#71717a">观测落定(不信回执信重扫)</span></div>\n      <div style="width:36px;text-align:center;color:#d4d4d8"></div>\n      <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("red", "ineffective")}<span class="kpival" style="font-size:20px">7</span><span style="font-size:12px;color:#71717a">回执说成了、线上没变 → 下轮重建议</span></div>
      <div style="width:36px;text-align:center;color:#d4d4d8"></div>
      <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("gray", "withdrawn")}<span class="kpival" style="font-size:20px">12</span><span style="font-size:12px;color:#71717a">撤销 —— 商品自己恢复了</span></div>
    </div>'''
    rows = [
        ("B0D1KPLM88", "A085", ("red", "删除"), "商品在亚马逊已下架", "—", ("sky", "suggested", True)),
        ("B08HJKL223", "A102", ("red", "删除"), "标题相似度 42% · 疑似换品", "—", ("sky", "suggested", True)),
        ("B0CQW2RT45", "A117", ("amber", "改库存→0"), "no_buybox · 无 Buy Box", "12 → 0", ("sky", "suggested", True)),
        ("B09ZZKL776", "A093", ("amber", "改库存→0"), "unavailable · 商品不可售", "8 → 0", ("sky", "suggested", True)),
        ("B0BXY44Q19", "A128", ("amber", "改库存→0"), "out_of_stock · 缺货", "15 → 0", ("amber", "executing", True)),
        ("B07YMN6P31", "A140", ("amber", "改库存→0"), "lead_days · 货期 9 天超限 · <b style=color:#b91c1c>executing 4 天未落定</b>", "20 → 0", ("amber", "executing", True)),
        ("B0CHXNPXVX", "A085", ("sky", "改价"), "亚马逊涨价 $19.99 → $23.99", "$45.99 → $47.99", ("emerald", "confirmed", False)),
        ("B0CC71GH02", "A076", ("emerald", "反补上架"), "曾缺货删除,现已恢复供货(2 次/30 天封顶)", "—", ("emerald", "confirmed", False)),
        ("B0BPQ88XYZ", "A111", ("gray", "停用"), "连续 30 天零动销 + 亚马逊涨价", "—", ("gray", "withdrawn", False)),
    ]
    trs = "".join(
        f'''<tr class="rz"><td class="td id">{a}</td><td class="td id">{s}</td>
        <td class="td">{tag(act[0], act[1])}</td>
        <td class="td" style="font-size:12.5px;font-weight:500;color:#27272a">{reason}</td>
        <td class="td id">{delta}</td><td class="td">{tag(st[0], st[1], dashed=st[2])}</td></tr>'''
        for a, s, act, reason, delta, st in rows)
    table = card("处置台账 · ops.dispositions —— 原因码就在列表上,不藏进详情", f'''
      <div style="padding:10px 16px;display:flex;gap:8px;border-bottom:1px solid #f4f4f5">
        <span class="btn sm bs">状态:全部</span><span class="btn sm bs">动作:全部</span><span class="btn sm bs">原因码:全部</span>
        <span style="flex:1"></span><span class="btn bdanger sm">{ic("Zap", 12, "#fff")}去执行 maintenance(214 条建议)</span></div>
      <table><tr><th class="th">ASIN</th><th class="th">店铺</th><th class="th">动作</th><th class="th">原因码</th>
      <th class="th">变更</th><th class="th">状态</th></tr>{trs}''')
    note = ('<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#71717a;background:#fafafa;'
            'border:1px solid #f4f4f5;border-radius:6px;padding:10px 12px;flex:none">'
            + ic("AlertTriangle", 13, "#b45309") +
            '四条清零判据表里长得一模一样(库存 N → 0),只有原因码 unavailable / no_buybox / out_of_stock / lead_days 分得清;两类删除正确性判断完全不同。维护三类 executing 超 3 天自动放行,问题链的删除/反补**故意不放行** —— 卡住必须在这页看得见(上表红字那行)。')
    content = steps + table + note + '</div>'
    # 上一行拼接误差防御:note 已闭合,末尾多的 </div> 去掉
    content = steps + table + note
    return shell(page("maint", ["维护与清理"], content, 1000), 1440, 1000)


# ════════════════════════ 分配中心(全链手动,前端是唯一入口) ════════════════════════
def alloc_board():
    steps = []
    chain = [
        ("alloc_audit", "审计", "只读", None, ("emerald", "08-12 跑过")),
        ("填表 & 下架", "人工", "飞书限额表", None, ("amber", "3 店类目列未填")),
        ("alloc_backfill", "回填", "一次性", "危", ("emerald", "08-13 落 1,872 占用")),
        ("alloc_stores / products", "体检", "只读", None, ("emerald", "08-17")),
        ("alloc_plan", "方案", "落占用", "危", ("gray", "待跑")),
        ("list_new", "上架", "按方案", "危", ("gray", "每天 20:00")),
        ("claim_audit", "对账", "只读", None, ("amber", "41 条该释放")),
        ("store_release", "释放", "唯一路径", "危", ("gray", "—")),
    ]
    for i, (name, role, note, danger, (tone, st)) in enumerate(chain):
        arrow = f'<div style="width:22px;text-align:center;flex:none;color:#d4d4d8">{ic("ChevronRight", 14, "#d4d4d8")}</div>' if i else ""
        d = tag("red", "危") if danger else ""
        steps.append(arrow + f"""<div style="flex:1;min-width:0;border:1px solid #e4e4e7;border-radius:6px;background:#fff;padding:8px 10px">
          <div style="display:flex;align-items:center;gap:6px"><span class="id" style="font-size:11.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{name}</span>{d}</div>
          <div style="font-size:10px;color:#a1a1aa;margin-top:2px">{role} · {note}</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:5px">{dot(tone)}<span style="font-size:10.5px;color:#52525b">{st}</span></div>
        </div>""")
    pipeline = card("分配链 · 全部手动工作流,这个页面是它们唯一的触发与呈现入口",
        f'<div style="padding:12px 16px;display:flex;align-items:center">{"".join(steps)}</div>')

    kpis = "".join([
        kpi("active 占用 · 品牌", "1,872", "catalog.claims · kind=brand"),
        kpi("active 占用 · 产品", "23,410", "kind=product · ASIN 全局排他"),
        kpi("对账:当初就不该占", "41", "claim_audit · 全踩类目/渠道闸", tone="amber"),
        kpi("待你做的事", "5", "填类目列 ×3 · 冲突确认 ×2", tone="amber"),
    ])

    crow = lambda kind, key, store, src, at, st, extra: (
        f'<tr class="rc"><td class="td">{tag("violet" if kind == "brand" else "gray", kind)}</td>'
        f'<td class="td id" style="font-weight:500">{key}</td><td class="td id">{store}</td>'
        f'<td class="td id" style="color:#71717a">{src}</td><td class="td id" style="color:#71717a">{at}</td>'
        f'<td class="td">{st}</td><td class="td" style="font-size:11px;color:#71717a">{extra}</td></tr>')
    claims = card("占用台账 · catalog.claims —— 占用是决策不是观测:下架 / 店暂停 / KPI 报 TERMINATED 都不释放;唯一释放路径是 store_release", f"""
      <table><tr><th class="th">类型</th><th class="th">占用键</th><th class="th">占用店</th><th class="th">来源</th>
      <th class="th">占用时间</th><th class="th">状态</th><th class="th">备注</th></tr>
      {crow("brand", "anker", "A085", "alloc_backfill", "08-13", tag("emerald", "active"), "在线 61 · 决策时快照 pt=Wall Chargers")}
      {crow("brand", "gaiam", "A102", "alloc_plan", "08-14", tag("emerald", "active"), "在线 12")}
      {crow("product", "B0CC71GH02", "A140", "alloc_plan", "08-14", tag("emerald", "active"), "占用在 A140,货在 A140 ✓")}
      {crow("product", "B0BPQ88XYZ", "A111", "alloc_backfill", "08-13", tag("emerald", "active"), '<span style="color:#b45309">占用在 A111,货在 A128 —— A128 该下架</span>')}
      {crow("brand", "cosori", "A117", "store_release", "08-16", tag("gray", "released"), "released 行永不删 · 原因:类目不符")}
      </table>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa">「占用店」与「在线店」是两列两个事实,不合成一列;此页无任何「自动清理失效占用」按钮 —— 那不该存在。</div>""",
      action='<span class="btn sm bs">状态:active</span><span class="btn sm bs">类型:全部</span><span class="btn sm bs">按店条形榜</span>')

    planrow = lambda s, cap, quota, got, direct, left, ratio, bad: (
        f'<tr class="rc"{" style=background:#fffbeb" if bad else ""}><td class="td id" style="font-weight:500">{s}</td>'
        f'<td class="td num">{cap}</td><td class="td num">{quota}</td><td class="td num" style="font-weight:600">{got}</td>'
        f'<td class="td num">{direct}</td><td class="td num">{left}</td>'
        f'<td class="td num" style="{"color:#b45309;font-weight:600" if bad else ""}">{ratio}</td></tr>')
    plan = card("方案审阅 · alloc_plan(dry-run 输出,execute 前必审;真跑落占用,不碰沃尔玛)", f"""
      <div style="padding:10px 16px;display:flex;gap:14px;border-bottom:1px solid #f4f4f5;font-size:12px;color:#52525b">
        <span>未发出:{tag("gray", "no_gate 34")} 要开大类</span><span>{tag("gray", "no_room 58")} 等下架腾位</span>
        <span>{tag("gray", "no_quota 27")} 等下一批</span><span style="flex:1"></span>
        <span>候选切口 <b class="id">60</b> ↔ 实际入场线 <b class="id">74</b>(两个数,故意并列)</span></div>
      <table><tr><th class="th">店</th><th class="th" style="text-align:right">剩余容量</th><th class="th" style="text-align:right">本轮配额</th>
      <th class="th" style="text-align:right">分到</th><th class="th" style="text-align:right">其中定向</th>
      <th class="th" style="text-align:right">分完还剩</th><th class="th" style="text-align:right">顶层比值</th></tr>
      {planrow("A085", "39", "24", "26", "8", "13", "1.12", False)}
      {planrow("A102", "45", "24", "24", "3", "21", "0.94", False)}
      {planrow("A117", "48", "24", "22", "0", "26", "1.48", True)}
      {planrow("A140", "55", "24", "24", "5", "31", "—", False)}
      </table>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa">「分到 26 > 配额 24」不是 bug:一张牌 = 一个品牌组,组是原子的;真正不许越的是剩余容量硬闸。顶层比值出 [0.7, 1.3] 标黄;自由流 &lt; 20 件的店显示 —。</div>""",
      action=f'<span class="btn sm bs">方案表 csv</span><span class="btn sm bs">未入选 csv</span><span class="btn bdanger sm">{ic("Zap", 12, "#fff")}execute 落占用</span>')

    audit_rel = card("对账与释放 · claim_audit → store_release(带拼好的命令)", f"""
      <table><tr><th class="th">类型</th><th class="th">占用键</th><th class="th">占用店</th><th class="th">踩闸原因</th><th class="th">释放命令</th></tr>
      <tr class="rc"><td class="td">{tag("violet", "brand")}</td><td class="td id">sony</td><td class="td id">A093</td>
        <td class="td" style="font-size:12px;color:#b45309">在架 4 行全踩类目闸(Electronics 未准入)</td>
        <td class="td id" style="font-size:11px;color:#71717a">cli.py store_release -p brand=sony -p store=A093</td></tr>
      <tr class="rc"><td class="td">{tag("gray", "product")}</td><td class="td id">B07QQ81MZL</td><td class="td id">A076</td>
        <td class="td" style="font-size:12px;color:#b45309">渠道闸(店不接自由流)</td>
        <td class="td id" style="font-size:11px;color:#71717a">cli.py store_release -p asin=B07QQ81MZL -p store=A076</td></tr>
      </table>
      <div style="padding:10px 16px;display:flex;align-items:center;gap:10px;border-top:1px solid #f4f4f5">
        <span class="btn bs sm">导出 41 条 csv</span>
        <span class="btn bdanger sm">{ic("Zap", 12, "#fff")}store_release -p from_csv 批量释放</span>
        <span style="font-size:11px;color:#b91c1c">确认弹窗必须写:释放后品牌可被下一轮分给别的店,那一步不可逆。</span></div>""")

    gotcha = ('<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#71717a;background:#fafafa;'
              'border:1px solid #f4f4f5;border-radius:6px;padding:10px 12px;flex:none">'
              + ic("AlertTriangle", 13, "#b45309") +
              '「参与分配」三态不压成两态:限额表填 0 = 不接货 · 未填 = 待补(点名) · &gt;0 = 参与;它与店铺 SUSPENDED 状态无关。'
              '回填的 as_of/sales_days 必须与出清单那轮审计完全一致 —— 页面顶部常驻显示这对值。</div>')
    content = pipeline + f'<div style="display:flex;gap:16px;flex:none">{kpis}</div>' + claims + plan + audit_rel + gotcha
    return shell(page("alloc", ["分配", "占用与方案"], content, 1400), 1440, 1400)

# ════════════════════════ 订单中心(全貌:不只审核) ════════════════════════
def orders_board():
    tabs = """<div style="display:flex;gap:2px;border-bottom:1px solid #e4e4e7;flex:none">
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;font-weight:500;color:#18181b;border-bottom:2px solid #18181b">订单行</span>
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;color:#52525b">售后</span>
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;color:#52525b">绩效</span>
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;color:#52525b">对账</span>
    </div>"""
    SALE = {"Created": ("gray", True), "Acknowledged": ("sky", True), "Shipped": ("sky", False),
            "Delivered": ("emerald", False), "Cancelled": ("gray", False)}
    AUD = {"未审": ("gray", False), "✓ 通过": ("emerald", False), "建议拒绝": ("red", False), "待人工": ("amber", True)}
    rows = [
        ("08-18 13:02", "108934567890123", "PHUMWMT202606110034", "A102", "1", "$52.49", "Shipped", "待人工", "", "USPS · 9400 1112…"),
        ("08-18 11:47", "108934561177452", "PHUMWMT202605280081", "A085", "2", "$63.96", "Acknowledged", "✓ 通过", "", "—"),
        ("08-18 10:15", "108934558812007", "PHUMWMT202607050012", "A085", "1", "$47.99", "Created", "✓ 通过", "", "—"),
        ("08-18 09:38", "108934552290873", "PHUMWMT202604220019", "A117", "1", "$19.99", "Shipped", "建议拒绝", "钓鱼", "FedEx · 7712 0034…"),
        ("08-17 22:03", "108934522903318", "PHUMWMT202603140007", "A093", "3", "$204.00", "Delivered", "✓ 通过", "", "UPS · 1Z 999 AA1…"),
        ("08-17 20:44", "108934519984401", "PHUMWMT20260801056", "A128", "1", "$24.99", "Cancelled", "未审", "", "—"),
        ("08-17 18:20", "108934488251170", "PHUMWMT202512190022", "A140", "1", "$89.99", "Delivered", "✓ 通过", "", "USPS · 9400 1187…"),
    ]
    trs = []
    for t, po, sku, store, qty, amt, sale, aud, phish, ship in rows:
        st, sd = SALE[sale]; at, ad = AUD[aud]
        ph = tag("violet", "钓鱼 · note") if phish else ""
        trs.append(f'<tr class="rc"><td class="td id" style="color:#71717a">{t}</td>'
                   f'<td class="td id" style="font-weight:500">{po}</td><td class="td id">{sku}</td>'
                   f'<td class="td id">{store}</td><td class="td num">{qty}</td><td class="td num">{amt}</td>'
                   f'<td class="td">{tag(st, sale, dashed=sd)}</td>'
                   f'<td class="td"><span style="display:inline-flex;gap:4px">{tag(at, aud, dashed=ad)}{ph}</span></td>'
                   f'<td class="td" style="font-size:11.5px;color:#71717a">{ship}</td></tr>')
    main = card("订单行 · orders.order_lines(行身份 = 订单号 + SKU;审核只出建议,不自动拒单)", f"""
      <div style="padding:10px 16px;display:flex;gap:8px;border-bottom:1px solid #f4f4f5">
        <span class="btn sm bs">店铺:全部</span><span class="btn sm bs">近 7 天</span>
        <span class="btn sm bs">销售状态:全部</span><span class="btn sm bs">审核:全部</span>
        <span class="btn sm bs" style="border-style:dashed">已隐藏 source=历史数据</span>
        <span style="flex:1"></span><span style="font-size:12px;color:#a1a1aa;align-self:center">今天 3,368 行 · 42/43 店</span></div>
      <table><tr><th class="th">下单时间</th><th class="th">PO</th><th class="th">SKU</th><th class="th">店</th>
      <th class="th" style="text-align:right">件</th><th class="th" style="text-align:right">金额</th>
      <th class="th">销售状态</th><th class="th">审核</th><th class="th">物流</th></tr>{"".join(trs)}</table>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa">审核徽章四值:未审(灰)/ ✓ 通过 / 建议拒绝 / 待人工;「钓鱼」不在状态里,在 audit_detail.note —— 钓鱼行状态就是建议拒绝,且后续轮次不可覆盖。电话 84% 被沃尔玛打码成全 0,展示为「已打码」。</div>""")

    seg = lambda title, inner: f'<div style="flex:1;min-width:0">{card(title, inner)}</div>'
    returns = seg("售后 · return_lines(同一订单行可多次售后)", f"""
      <div style="padding:10px 16px;display:flex;flex-direction:column;gap:8px;font-size:12px">
        <div style="display:flex;gap:6px;align-items:center"><span class="id">RMA 2482…07</span>{tag("sky", "INITIATED", dashed=True)}{tag("gray", "NOT_REFUNDED")}<span style="color:#71717a">SHIPPED_TO_RETURN_CENTER</span></div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">RMA 2481…88</span>{tag("emerald", "CLOSED")}{tag("emerald", "REFUNDED")}<span style="color:#71717a">KEEP_ITEM · $24.99</span></div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">RMA 2480…12</span>{tag("sky", "DELIVERED", dashed=True)}{tag("gray", "NOT_REFUNDED")}<span style="color:#71717a">退款模式 FIRST_SCAN(订单级)</span></div>
      </div>""")
    perf = seg("绩效 · perf_event_spans(仍拖分 = still_active)", f"""
      <div style="padding:10px 16px;display:flex;flex-direction:column;gap:8px;font-size:12px">
        <div style="display:flex;gap:8px;align-items:center">{tag("red", "otd · 仍拖分 3")}{tag("red", "vtr · 2")}{tag("gray", "returns · 滚出 7")}</div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">PO …90123 · otd</span><span style="color:#71717a">首见 W31 · 已 3 期 · accountable</span>{pill("red", "仍拖分")}</div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">PO …51170 · vtr</span><span style="color:#71717a">首见 W29 · 已 5 期</span>{pill("gray", "已滚出窗口")}</div>
        <div style="font-size:11px;color:#a1a1aa">历史累计口径 = COUNT(DISTINCT (店, PO, 指标)),直接数行会虚高。</div>
      </div>""")
    settle = seg("对账 · settlement_by_line(四态,别自己拿 net 判)", f"""
      <div style="padding:10px 16px;display:flex;flex-direction:column;gap:8px;font-size:12px">
        <div style="display:flex;gap:6px">{tag("emerald", "已入账")}{tag("amber", "已冲销")}{tag("sky", "已退款")}{tag("gray", "待入账", dashed=True)}</div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">PO …03318</span><span class="id">net $178.44</span><span style="color:#71717a">2 账期 · 佣金 8%</span>{tag("emerald", "已入账")}</div>
        <div style="display:flex;gap:6px;align-items:center"><span class="id">PO …84401</span><span class="id">net $0 · gross $49.98</span><span style="color:#71717a">Sale/Refund 相消</span>{tag("sky", "已退款")}</div>
        <div style="font-size:11px;color:#a1a1aa">net=0 有两义:gross&gt;0 是全额退款,gross=0 是无金额 —— 用视图 settle_status,别自算。</div>
      </div>""")
    chains = ('<div class="card" style="padding:10px 16px;display:flex;align-items:center;gap:16px;flex:none;font-size:12px;color:#52525b">'
              + pill("emerald", "order_chain :20 每小时") + pill("emerald", "order_daily 07:30") + pill("gray", "settlement 周三 08:00")
              + '<span style="flex:1"></span><span class="btn sm bs">order_center_push 补推</span>'
              + '<span class="btn sm bdghost">order_center_cleanup(危 · 一次性)</span></div>')
    content = tabs + main + f'<div style="display:flex;gap:16px;flex:none">{returns}{perf}{settle}</div>' + chains
    return shell(page("orders", ["订单", "全貌"], content, 1100), 1440, 1100)

# ════════════════════════ 审核中心(L0-L4 分层 + 同轮补采 + 重审三通道) ════════════════════════
def audit_board():
    today = card("今日 audit_sheet · 18:10(上架表驱动;审核权威在库,E 列只是投影)", f"""
      <div style="padding:14px 16px;display:flex;align-items:center;gap:0">
        <div style="flex:1"><div class="kpilabel">表 E 列空 · 领取</div><div class="kpival" style="margin-top:6px">9,402</div></div>
        <div style="width:30px;color:#d4d4d8">{ic("ChevronRight", 16, "#d4d4d8")}</div>
        <div style="flex:1"><div class="kpilabel">库里已有结论 · 零 LLM 直接投影</div><div class="kpival" style="margin-top:6px">8,140</div></div>
        <div style="width:30px;color:#d4d4d8">{ic("ChevronRight", 16, "#d4d4d8")}</div>
        <div style="flex:1"><div class="kpilabel">真待审(未审 + pending)</div><div class="kpival" style="margin-top:6px">1,262</div></div>
        <div style="width:30px"></div>
        <div style="flex:1;background:#fafafa;border:1px solid #f4f4f5;border-radius:6px;padding:10px 12px">
          <div style="font-size:11px;color:#71717a">limit 撞满时必须给三个数</div>
          <div class="id" style="font-size:13px;margin-top:4px">总量 1,262 · 本轮 1,262 · 还剩 0</div></div>
      </div>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa;border-top:1px solid #f4f4f5">E 列写回值是小写 <span class="id">pass / reject</span>(不是 approved);缺数据行只写 F 列原因、E 列必须留空 —— E 一有值该行就退出审核通道。</div>""")

    def layer(name, judge, n, tone, note):
        return f"""<div style="display:flex;align-items:center;gap:12px;padding:6px 0">
          <span style="width:170px;flex:none;font-size:13px;color:#27272a">{name}</span>
          <span style="width:88px;flex:none">{tag(tone, judge, dashed=judge == "pending")}</span>
          <div style="flex:1;height:18px;border-radius:3px;background:#f4f4f5"><div style="height:18px;border-radius:3px;background:{DOTS[tone] if tone != "gray" else "#d4d4d8"};width:{max(2, n / 1262 * 100):.0f}%"></div></div>
          <span class="id" style="width:52px;text-align:right;flex:none">{n:,}</span>
          <span style="width:320px;flex:none;font-size:11px;color:#71717a">{note}</span></div>"""
    funnel = card("分层判定漏斗 · audit.audit_runs(统计一律排除 stage_stopped_at='SHORTCUT' 影子行)", f"""
      <div style="padding:12px 16px">
        {layer("L0 Phase0 四硬规则", "reject", 214, "red", "R0 中国卖家类目硬禁 / R1 准入双白名单 / R2 十八条禁售大类 / R3a 硬认证;串行短路,单条 hit ≠ 只违反一条")}
        {layer("L1 类目解析", "pending", 37, "amber", "类目解不出 → pending(中间态不是结论),隔日退避重试")}
        {layer("L2 硬规则复核", "reject", 96, "red", "结论只由 -100 硬规则决定;R3b/R3c/R4 黑名单/R5 商标/R7/R8 penalty=0 纯证据,不画成拒因")}
        {layer("L3 语义(LLM)", "pending", 6, "amber", "LLM 故障 → pending;http_429 才算撞限流,5xx 是对端故障")}
        {layer("L4 视觉", "reject", 0, "gray", "故障回落 pass 按 rule_code 计数 —— 全故障 = 层未生效,要亮出来")}
        {layer("最终", "pass", 909, "emerald", "写回 products 五列 + 投影上架表 E 列")}
      </div>""")

    closure = card("同轮补采闭环 · 审核不等下一天", f"""
      <div style="padding:12px 16px;display:flex;align-items:center;gap:8px;font-size:12.5px;color:#27272a">
        <span>审不了 <b class="id">118</b>(不在库/无标题)</span>{ic("ChevronRight", 13, "#d4d4d8")}
        <span>推 <span class="id">audit_gap_20260818</span>(插队成功)</span>{ic("ChevronRight", 13, "#d4d4d8")}
        <span>轮询等采(默认 20 分钟)</span>{ic("ChevronRight", 13, "#d4d4d8")}
        <span>就地摄取</span>{ic("ChevronRight", 13, "#d4d4d8")}
        <span style="color:#047857">救回 <b class="id">81</b> 本轮判掉</span>{ic("ChevronRight", 13, "#d4d4d8")}
        <span style="color:#b45309">仍缺 <b class="id">37</b> 写 F 列理由</span>
      </div>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa;border-top:1px solid #f4f4f5">产品审核补采走 ops.scrape_batches 的 audit_gap_ 前缀;订单审核的按邮编台账是 ops.audit_scrape —— 两套台账,别画进同一块。</div>""")

    pend = card("pending 队列 · 1,207(两来源分开,不混)", f"""
      <div style="padding:12px 16px;display:flex;gap:16px;align-items:center">
        <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("amber", "L1 · 类目解不出", dashed=True)}<span class="kpival" style="font-size:20px">1,144</span><span style="font-size:11px;color:#71717a">类目映射补上自然消化</span></div>
        <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("amber", "L3 · LLM 故障", dashed=True)}<span class="kpival" style="font-size:20px">63</span><span style="font-size:11px;color:#71717a">隔日退避重试</span></div>
        <span class="btn bs sm">mode=pending 专刷(无退避 · 手动)</span>
      </div>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa;border-top:1px solid #f4f4f5">audited_at 是审核动作时刻,不是进入 pending 的时刻 —— 页面只报 pending 总量,不算龄期。</div>""")

    why = card("单产品「为什么被拒」· audit_why 的界面化(产品详情页可跳)", f"""
      <div style="padding:12px 16px;display:flex;flex-direction:column;gap:8px;font-size:12.5px">
        <div style="display:flex;gap:8px;align-items:center"><span class="id" style="font-weight:600">B0BSNKKR6T</span>{tag("red", "rejected")}<span style="color:#71717a">现行结论(products 五列)· pt_source=walmart_confirmed 的 PT 一个字不许动</span></div>
        <div style="display:flex;gap:8px;align-items:center;padding-left:12px">{dot("red")}<span class="id">L2 · R2 禁售大类</span><span style="color:#71717a">penalty −100 —— 结论由它决定</span></div>
        <div style="display:flex;gap:8px;align-items:center;padding-left:12px">{dot("gray")}<span class="id">L2 · R4 品牌黑名单 sony</span><span style="color:#71717a">penalty 0 —— 纯证据,不是拒因</span></div>
        <div style="display:flex;gap:8px;align-items:center;padding-left:12px">{dot("gray")}<span class="id">读数实况</span><span style="color:#71717a">walmart_pt_meta.access_state / zh_can_do / pt_spec 认证字段 / 黑名单命中,让人一眼看到规则读的格子</span></div>
      </div>""")

    redo = card("重审三通道(危险,各自确认;「清空表 E 列」不是重审入口)", f"""
      <div style="padding:12px 16px;display:flex;gap:12px">
        <div style="flex:1;border:1px solid #e4e4e7;border-radius:6px;padding:10px 12px">
          <div class="id" style="font-size:12px;font-weight:600">-p asins=…</div>
          <div style="font-size:11px;color:#71717a;margin-top:4px;line-height:1.6">点名强审,无视现有结论。rejected 永不自动重审,这是唯一救活通道。</div></div>
        <div style="flex:1;border:1px solid #e4e4e7;border-radius:6px;padding:10px 12px">
          <div class="id" style="font-size:12px;font-weight:600">-p rerule=R2</div>
          <div style="font-size:11px;color:#71717a;margin-top:4px;line-height:1.6">改规则后定点翻案:只翻被该规则拒过的;显示 总量/本轮/还剩。</div></div>
        <div style="flex:1;border:1px solid #fecaca;border-radius:6px;padding:10px 12px;background:#fef2f2">
          <div class="id" style="font-size:12px;font-weight:600;color:#b91c1c">-p force_rerun=v3</div>
          <div style="font-size:11px;color:#b91c1c;margin-top:4px;line-height:1.6">全量重审,最重 —— LLM 费用警示,必走预览。</div></div>
      </div>""")

    conflict = card("审核 × 上架冲突 · catalog.audit_listing_conflicts(两列分开,不合并)", f"""
      <div style="padding:12px 16px;display:flex;gap:16px">
        <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("red", "rejected_still_listed")}<span class="kpival" style="font-size:20px">4</span><span style="font-size:11px;color:#71717a">已拒仍在架 —— 该下架</span></div>
        <div style="flex:1;display:flex;align-items:center;gap:10px">{tag("amber", "rejected_after_listing", dashed=True)}<span class="kpival" style="font-size:20px">2</span><span style="font-size:11px;color:#71717a">上架后才被拒 —— 闸漏拦,查规则时序</span></div>
        <div style="flex:1;display:flex;align-items:center;gap:10px"><span style="font-size:11px;color:#a1a1aa">工具区:audit_calibrate 四桶报告 · audit_import 体检 · audit_history_fold(全手动)</span></div>
      </div>""")

    content = today + funnel + f'<div style="display:flex;gap:16px"><div style="flex:1;display:flex;flex-direction:column;gap:16px">{closure}{pend}</div><div style="flex:1;display:flex;flex-direction:column;gap:16px">{why}</div></div>' + redo + conflict
    return shell(page("audit", ["审核", "审核中心"], content, 1400), 1440, 1400)

# ════════════════════════ 类目映射中心(九条工作流的界面化) ════════════════════════
def catmap_board():
    kpis = "".join([
        kpi("映射覆盖率(产品侧)", "64.4%", "15,538 node 已映射 10,011 · 自增强回路在涨"),
        kpi("映射表", "13,349 node", "audit.walmart_category_map · 高置信行直出"),
        kpi("类目树", "28,495 node", "重导后应达 32,147(中间层已修解析)"),
        kpi("三方 JOIN 命中", "82.2% / 99.9%", "产品侧 / 映射表侧 —— 导入前必看这两个数"),
    ])
    def bucket(name, n, items, tone, verdict, note):
        return f"""<div style="flex:1;border:1px solid #e4e4e7;border-radius:6px;padding:12px 14px;background:#fff">
          <div style="display:flex;align-items:center;gap:8px"><span style="font-size:13px;font-weight:600">{name}</span>{tag(tone, verdict)}</div>
          <div class="id" style="font-size:15px;font-weight:600;margin-top:6px">{n}<span style="font-size:11px;color:#a1a1aa;font-weight:400"> · {items}</span></div>
          <div style="font-size:11px;color:#71717a;margin-top:6px;line-height:1.6">{note}</div></div>"""
    buckets = card("四桶缺口 · catmap_gap(按 node 分:该 node 下有没有任何一个产品带实证 PT)", f"""
      <div style="padding:12px 16px;display:flex;gap:12px">
        {bucket("A 桶 · 有实证没映射", "1,740 node", "16.6 万件", "emerald", "catmap_mine 常态重跑",
                "瓶颈是实证太稀;每跑一轮 product_audit,walmart_pt 就多一批,下一轮 mine 就多挖一批 —— 自增强回路")}
        {bucket("B 桶 · 零实证", "3,787 node", "4.5 万件", "gray", "⛔ 不排期",
                "所有者 08-17 裁决:真实收益仅约 1.37 万件(84% 停在 L0 压根不查类目);解不出走 L1 LLM 兜底,慢些贵些但判得了")}
        {bucket("C 桶 · 树里没有", "2,759 node", "—", "amber", "采集侧补抓",
                "产品带着这个 node 但类目树没有 → catmap_gap -p only=not_in_tree 出清单")}
        {bucket("D 桶 · 没货", "12,386 node", "—", "gray", "不处理",
                "亚马逊有我们没货 —— 别浪费 LLM")}
      </div>""")
    life = card("置信度生命周期 · 升档自动,降档只走人工", f"""
      <div style="padding:12px 16px;display:flex;gap:16px">
        <div style="flex:1;display:flex;flex-direction:column;gap:8px;font-size:12.5px">
          <div style="display:flex;gap:8px;align-items:center">{tag("emerald", "高")}<span style="color:#52525b">审核 ②级闸<b>直出</b>,不经 LLM(硬筛 confidence='高')</span></div>
          <div style="display:flex;gap:8px;align-items:center">{tag("amber", "中")}<span style="color:#52525b">只进候选,LLM 拿它当参考自己判</span></div>
          <div style="display:flex;gap:8px;align-items:center">{tag("gray", "低")}<span style="color:#52525b">凭空推测 —— 弱证据永远绕不过 LLM。中/低是设计内档位,不是待修复的错误</span></div>
          <div style="font-size:11px;color:#71717a;margin-top:4px;line-height:1.7">升档:catmap_mine -p promote=1,数据攒够低→中→高自动走,重跑幂等。<br>
          降档<b>不自动</b>(证据可能只是暂时变薄;高置信可能是人定的)—— 只走 catmap_fix:旧行降「低」留痕不删,新行插「高」。</div>
        </div>
        <div style="flex:1">
          <table><tr><th class="th">mine 产出桶</th><th class="th">判据</th><th class="th">落档</th></tr>
          <tr class="rc"><td class="td id">mined_trusted</td><td class="td" style="font-size:12px">≥5 票且优势 ≥70~80%</td><td class="td">{tag("emerald", "高")}</td></tr>
          <tr class="rc"><td class="td id">mined_review</td><td class="td" style="font-size:12px">2~4 票、优势达标</td><td class="td">{tag("amber", "中")}</td></tr>
          <tr class="rc"><td class="td id">map_conflict</td><td class="td" style="font-size:12px">实证与旧映射相左(旧行不动)</td><td class="td">{tag("amber", "中")}</td></tr>
          <tr class="rc"><td class="td id">mined_mixed</td><td class="td" style="font-size:12px">票分流,首选是多数派非共识</td><td class="td">{tag("gray", "低")}</td></tr>
          <tr class="rc"><td class="td id">map_ambiguous</td><td class="td" style="font-size:12px;color:#b91c1c;font-weight:500">映射表自己挂多条高置信 PT —— ②级直出对该 node 失明且无报错,必须高亮</td><td class="td" style="font-size:12px;color:#71717a">只报不写;裁剪走 catmap_fix</td></tr>
          </table></div>
      </div>""")
    maps = card("映射明细 · audit.walmart_category_map(键 = browse_node_id;名字会漂 ID 不会;PG 是权威,飞书只是镜子 —— 在飞书改格子不影响判定。哨兵行「无对应Walmart PT」是信息不是脏数据)", f"""
      <table><tr><th class="th">node_id</th><th class="th">代表路径(非唯一真相 —— 树是 DAG,走父链查 amazon_node_paths)</th>
      <th class="th">沃尔玛 PT</th><th class="th">置信</th><th class="th">来源</th><th class="th">票(只数 pt_source=walmart_confirmed,偏少正常)</th></tr>
      <tr class="rc"><td class="td id">172456</td><td class="td" style="font-size:12px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Electronics › Chargers &amp; Power Adapters</td><td class="td id">Wall Chargers</td><td class="td">{tag("emerald", "高")}</td><td class="td id" style="color:#71717a">mined_trusted</td><td class="td num">38 · 92%</td></tr>
      <tr class="rc"><td class="td id">3741411</td><td class="td" style="font-size:12px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Sports &amp; Outdoors › Golf › Cart Accessories</td><td class="td id">Golf Cart Parts</td><td class="td">{tag("amber", "中")}</td><td class="td id" style="color:#71717a">mined_review</td><td class="td num">3 · 100%</td></tr>
      <tr class="rc"><td class="td id">228013</td><td class="td" style="font-size:12px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Tools &amp; Home Improvement › Power Tools</td><td class="td id">Power Drills</td><td class="td">{tag("emerald", "高")}</td><td class="td id" style="color:#71717a">catmap_fix(人工)</td><td class="td num">—</td></tr>
      <tr class="rc"><td class="td id">1055398</td><td class="td" style="font-size:12px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Home &amp; Kitchen › Home Décor(旧行 · 降「低」留痕)</td><td class="td id">Home Decor</td><td class="td">{tag("gray", "低")}</td><td class="td id" style="color:#71717a">mined_conflict_fix 前身</td><td class="td num">—</td></tr>
      </table>""",
      action='<span class="btn sm bs">查 node</span><span class="btn sm bs">只看冲突</span>')
    wfs = card("九条工作流(全部手动;两条危)", f"""
      <div style="padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px">
        <span class="btn sm bp">catmap_mine 常态重跑</span><span class="btn sm bs">catmap_promote 升档</span>
        <span class="btn sm bdghost">catmap_fix 定点修 危</span><span class="btn sm bdghost">catmap_prune 清死 PT 危</span>
        <span class="btn sm bs">catmap_gap 缺口</span><span class="btn sm bs" style="text-decoration:line-through;color:#a1a1aa">catmap_suggest(⛔不排期)</span>
        <span class="btn sm bs">catmap_align 无 ID 老行兜底</span><span class="btn sm bs">catmap_export 推飞书(缩量>2% ⛔停手护栏 —— 08-17 曾误删 1,847 行)</span><span class="btn sm bs">catmap_import 收飞书</span>
        <span style="flex:1"></span><span style="color:#a1a1aa">树:taxonomy_import(预览强制先看三方命中率)· taxonomy_derive 零采集补树外 node</span>
      </div>""")
    content = f'<div style="display:flex;gap:16px;flex:none">{kpis}</div>' + buckets + life + maps + wfs
    return shell(page("catmap", ["类目映射"], content, 1400), 1440, 1400)

# ════════════════════════ 店铺(列表 + 单店详情) ════════════════════════
def stores_board():
    srows = []
    for name, cred, ctone, ping, ss, sales, n in [
        ("A085", "ok", "emerald", "ok · 14:20", "ACTIVE", "可售", "14,203"),
        ("A093", "ok", "emerald", "ok", "ACTIVE", "可售", "13,877"),
        ("A102", "ok", "emerald", "ok", "ACTIVE", "可售", "15,012"),
        ("A117", "ok", "emerald", "ok", "ACTIVE", "可售", "14,455"),
        ("A128", "ok", "emerald", "ok", "ACTIVE", "可售", "13,209"),
        ("A140", "ok", "emerald", "ok", "ACTIVE", "可售", "14,988"),
        ("谭总10", "换 token 失败", "red", "401 · 14:20", "ACTIVE", "不可售(推导)", "12,371"),
        ("谭总12", "在册但被过滤:启用=否", "gray", "—", "(未知)", "(未知)", "—"),
    ]:
        on = ' style="background:#fafafa;outline:1px solid #18181b;outline-offset:-1px"' if name == "A085" else ""
        srows.append(f'<tr class="rc"{on}><td class="td id" style="font-weight:500">{name}</td>'
                     f'<td class="td">{tag(ctone, cred)}</td><td class="td id" style="color:#71717a">{ping}</td>'
                     f'<td class="td" style="font-size:12px">{ss}</td><td class="td" style="font-size:12px">{sales}</td>'
                     f'<td class="td num">{n}</td></tr>')
    left = card("店铺 · 45(排序:字母 → 数字 → 中文,前缀+数字自然序 —— 不许用 SQL ORDER BY)", f"""
      <table><tr><th class="th">店铺</th><th class="th">凭证</th><th class="th">连通(ping_stores)</th>
      <th class="th">store_status</th><th class="th">sales_status</th><th class="th" style="text-align:right">在架</th></tr>{"".join(srows)}</table>
      <div style="padding:8px 16px;font-size:11px;color:#a1a1aa">store_status 空 = 未知不是停用(判不准就判活);sales_status 绝不回填旧值;「查无此店」与「在册但被过滤(启用=否 / 缺 ClientId / 缺代理)」分开显示。</div>""",
      style="flex:1;min-width:0")

    def kv(k, v, warn=False):
        return (f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f4f4f5;font-size:12px">'
                f'<span style="color:#71717a">{k}</span><span class="id" style="{"color:#b45309" if warn else ""}">{v}</span></div>')
    right = f"""<div style="width:430px;flex:none;display:flex;flex-direction:column;gap:16px">
      {card("A085 · 状态 / 商品 / 订单", '<div style="padding:8px 16px">'
        + kv("store_status / payment / sales", "ACTIVE · 正常 · 可售")
        + kv("在线商品(PG 复用,非实时)", "14,203 · 有库存 13,911 · 无库存 292")
        + kv("昨日出单 / 销售额(锚 06:30)", "82 单 · $2,470.15")
        + kv("绩效", "OTD 96.2% · 取消 0.4% · 追踪 99.3% · 退款 1.8%") + "</div>")}
      {card("结算", '<div style="padding:8px 16px">'
        + kv("账期销售额 / 佣金", "$31,240 · $2,499")
        + kv("期末余额 / 回款", "$8,112 · $7,904(08-15 · Payoneer)")
        + kv("无 Hold", "true(ACTIVE 且 payout ≥ closing)") + "</div>")}
      {card("配额与限制(飞书限额表;0 或负 = 回默认,不是停摆)", '<div style="padding:8px 16px">'
        + kv("上架限制 / 下架配额 / 删除配额", "100 · 200 · 200")
        + kv("配送时长上限", "7 天(默认)—— 上架超限不上;维护超限库存写 0")
        + kv("库存特殊要求", "—(填 0 才是整店清零的显式路径)")
        + kv("四区间改价倍率", "1.9 / 1.8 / 1.7 / 1.6") + "</div>")}
      {card("API 配额观测 · ops.rate_events(近 48h 稀缺桶)", '<div style="padding:8px 16px">'
        + kv("feeds.post(价格三件套共享桶)", "4 / 6 今日", True)
        + kv("prices.put(单品 100/小时)", "61 / 100 峰时")
        + kv("reports.request / insights", "3 · 1(1/分钟)") + "</div>")}
    </div>"""
    content = f'<div style="display:flex;gap:16px;flex:1;min-height:0">{left}{right}</div>'
    return shell(page("stores", ["店铺"], content, 1100), 1440, 1100)

DOMAIN_BOARDS = {
    "CatmapCenter.dc.html": (catmap_board, "类目映射中心", 1440, 1400),
    "Stores.dc.html": (stores_board, "店铺", 1440, 1100),
    "AuditCenter.dc.html": (audit_board, "审核中心", 1440, 1400),
    "AllocCenter.dc.html": (alloc_board, "分配中心", 1440, 1400),
    "OrdersCenter.dc.html": (orders_board, "订单中心 · 全貌", 1440, 1100),
    "ProductList.dc.html": (productlist_board, "产品列表", 1440, 1000),
    "FeedTracker.dc.html": (feedtracker_board, "Feed 追踪", 1440, 1000),
    "BlacklistCenter.dc.html": (blacklist_board, "黑名单中心", 1440, 1000),
    "ScrapeMonitor.dc.html": (scrape_board, "采集监控", 1440, 1000),
    "MaintCenter.dc.html": (maint_board, "维护与清理", 1440, 1000),
    "WorkflowAtlas.dc.html": (atlas_board, "工作流全景 · 66 条", 1440, 1340),
    "RunsSchedule.dc.html": (runs_board, "运行记录与调度", 1440, 1100),
}
DOMAIN_POS = {
    # 三列(x=0/1560/3120)四行,按使用流:产品→审核→类目映射 / 分配→订单→维护 /
    # 黑名单→Feed→采集 / 店铺→运行调度→全景
    "ProductList.dc.html": (0, 0),
    "AuditCenter.dc.html": (1560, 0),
    "CatmapCenter.dc.html": (3120, 0),
    "AllocCenter.dc.html": (0, 1520),
    "OrdersCenter.dc.html": (1560, 1520),
    "MaintCenter.dc.html": (3120, 1520),
    "BlacklistCenter.dc.html": (0, 3040),
    "FeedTracker.dc.html": (1560, 3040),
    "ScrapeMonitor.dc.html": (3120, 3040),
    "Stores.dc.html": (0, 4160),
    "RunsSchedule.dc.html": (1560, 4160),
    "WorkflowAtlas.dc.html": (3120, 4160),
}
