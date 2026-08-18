# -*- coding: utf-8 -*-
"""生成 WalmartAPI 运营台设计画布的 8 块画板(.dc.html)+ canvas.json。

设计词汇 100% 取自旧仓 erp-core(/workspace/erpapi/erp-core/handoff-design):
zinc 中性色系 + 黑色主按钮 + emerald/amber/red/sky/violet/gray 状态点,
Inter + Noto Sans SC + JetBrains Mono,行高 32/40 两档,240px 白侧栏。
"""

FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700'
         '&family=JetBrains+Mono:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;600;700'
         '&display=swap" rel="stylesheet">')

# ---- erp-core 精确取值 ----
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter','Noto Sans SC',system-ui,sans-serif; -webkit-font-smoothing: antialiased;
       background: #fafafa; color: #18181b; }
a { color: #0369a1; text-decoration: none; } a:hover { color: #075985; }
.mono { font-family: 'JetBrains Mono',ui-monospace,monospace; }
.tab { font-variant-numeric: tabular-nums; }
.card { background: #fff; border: 1px solid #e4e4e7; border-radius: 8px; }
.chead { display: flex; align-items: center; justify-content: space-between; padding: 0 16px; height: 44px; border-bottom: 1px solid #f4f4f5; }
.ctitle { font-size: 13px; font-weight: 500; color: #18181b; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 99px; flex: none; }
.dothollow { display: inline-block; width: 9px; height: 9px; border-radius: 99px; background: #fff; border: 2px solid #f59e0b; flex: none; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: #27272a; white-space: nowrap; }
.tag { display: inline-flex; align-items: center; gap: 4px; padding: 0 6px; height: 20px; border-radius: 4px; font-size: 11px; font-weight: 500; border: 1px solid; white-space: nowrap; }
.tagdash { border-style: dashed; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; white-space: nowrap; border-radius: 6px;
       font-weight: 500; font-size: 14px; height: 36px; padding: 0 12px; border: 1px solid transparent; cursor: default; }
.btn.sm { height: 28px; padding: 0 8px; font-size: 12px; }
.btn.lg { height: 40px; padding: 0 16px; }
.bp { background: #18181b; color: #fff; }
.bs { background: #fff; color: #18181b; border-color: #d4d4d8; }
.bghost { background: transparent; color: #3f3f46; }
.bdanger { background: #dc2626; color: #fff; }
.bdghost { background: transparent; color: #b91c1c; }
table { border-collapse: separate; border-spacing: 0; width: 100%; }
.th { background: #fafafa; text-align: left; padding: 0 12px; height: 36px; font-size: 11px; font-weight: 500; color: #71717a;
      text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid #e4e4e7; white-space: nowrap; }
.td { padding: 0 12px; font-size: 13px; color: #27272a; border-bottom: 1px solid #f4f4f5; vertical-align: middle; }
.rz .td { height: 40px; } .rc .td { height: 32px; }
.num { text-align: right; font-family: 'JetBrains Mono',ui-monospace,monospace; font-size: 12.5px; font-variant-numeric: tabular-nums; }
.id { font-family: 'JetBrains Mono',ui-monospace,monospace; font-size: 12.5px; letter-spacing: -0.01em; color: #3f3f46; }
.kbd { font-family: 'JetBrains Mono',monospace; font-size: 10px; padding: 1px 5px; border: 1px solid #e4e4e7;
       border-bottom-width: 2px; border-radius: 4px; background: #fafafa; color: #52525b; }
.grouptitle { padding: 0 16px; margin: 12px 0 4px; font-size: 10px; font-weight: 500; text-transform: uppercase; letter-spacing: .05em; color: #a1a1aa; }
.navitem { display: flex; align-items: center; gap: 8px; height: 32px; padding: 0 16px; font-size: 13px; color: #3f3f46; }
.navitem.on { background: #18181b; color: #fff; }
.navbadge { font-family: 'JetBrains Mono',monospace; font-variant-numeric: tabular-nums; font-size: 10px; padding: 0 6px; height: 16px;
            display: inline-flex; align-items: center; border-radius: 4px; background: #f4f4f5; color: #3f3f46; margin-left: auto; }
.navbadge.amber { background: #fffbeb; color: #b45309; }
.kpilabel { font-size: 12px; color: #71717a; }
.kpival { font-size: 24px; font-weight: 600; font-family: 'JetBrains Mono',ui-monospace,monospace; font-variant-numeric: tabular-nums; color: #18181b; }
.log { font-family: 'JetBrains Mono',ui-monospace,monospace; font-size: 12px; line-height: 1.7; color: #d4d4d8;
       background: #18181b; border-radius: 6px; padding: 14px 16px; white-space: pre; overflow: hidden; }
.log .ok { color: #34d399; } .log .warn { color: #fbbf24; } .log .err { color: #f87171; } .log .dim { color: #71717a; }
"""

TONES = {
    "emerald": ("#ecfdf5", "#047857", "#a7f3d0"),
    "amber":   ("#fffbeb", "#b45309", "#fde68a"),
    "red":     ("#fef2f2", "#b91c1c", "#fecaca"),
    "sky":     ("#f0f9ff", "#0369a1", "#bae6fd"),
    "violet":  ("#f5f3ff", "#6d28d9", "#ddd6fe"),
    "gray":    ("#f4f4f5", "#3f3f46", "#e4e4e7"),
}
DOTS = {"emerald": "#10b981", "amber": "#f59e0b", "red": "#ef4444",
        "sky": "#0ea5e9", "violet": "#8b5cf6", "gray": "#a1a1aa"}

ICONS = {
 "LayoutDashboard": '<rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>',
 "Package": '<path d="m7.5 4.27 9 5.15"/><path d="M21 8 12 2 3 8v8l9 6 9-6V8z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
 "ListChecks": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
 "AlertTriangle": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
 "Store": '<path d="m2 7 2-3h16l2 3"/><path d="M4 7v13a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V7"/><path d="M2 7a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0"/>',
 "ShoppingCart": '<circle cx="8" cy="21" r="1"/><circle cx="19" cy="21" r="1"/><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12"/>',
 "Search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
 "ChevronRight": '<polyline points="9 18 15 12 9 6"/>',
 "X": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 "Check": '<polyline points="20 6 9 17 4 12"/>',
 "Copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
 "ExternalLink": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
 "Play": '<polygon points="6 3 20 12 6 21 6 3"/>',
 "Clock": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
 "Ban": '<circle cx="12" cy="12" r="9"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
 "SlidersHorizontal": '<line x1="3" y1="6" x2="13" y2="6"/><line x1="17" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="9" y2="12"/><line x1="13" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="13" y2="18"/><line x1="17" y1="18" x2="21" y2="18"/><circle cx="15" cy="6" r="2"/><circle cx="11" cy="12" r="2"/><circle cx="15" cy="18" r="2"/>',
 "Download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
 "Zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
 "ScrollText": '<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
 "Send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
 "Inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"/>',
}

def ic(name, size=15, color="currentColor", sw=1.75):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" height="{size}" '
            f'fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" '
            f'style="flex:none">{ICONS[name]}</svg>')

def dot(tone):
    return f'<span class="dot" style="background:{DOTS[tone]}"></span>'

def pill(tone, label, hollow=False):
    d = '<span class="dothollow"></span>' if hollow else dot(tone)
    return f'<span class="pill">{d}<span>{label}</span></span>'

def tag(tone, label, dashed=False):
    bg, fg, bd = TONES[tone]
    extra = " tagdash" if dashed else ""
    style = f"background:{bg};color:{fg};border-color:{bd}"
    if dashed:
        style = f"background:#fff;color:{fg};border-color:{bd}"
    return f'<span class="tag{extra}" style="{style}">{label}</span>'

# ---- 应用骨架:侧栏 + 顶栏(erp-core Sidebar/Topbar 逐值复刻) ----
NAV = [
    ("每天看", [
        ("overview",  "LayoutDashboard", "总览", None, None),
        ("oq",        "Inbox",           "订单待人工", "23", "amber"),
        ("runs",      "ScrollText",      "运行记录", None, None),
    ]),
    ("按需查", [
        ("products",  "Package",           "产品", None, None),
        ("listing",   "ListChecks",        "上架", None, None),
        ("maint",     "SlidersHorizontal", "维护与清理", "214", None),
        ("orders",    "ShoppingCart",      "订单", None, None),
        ("blacklist", "Ban",               "黑名单中心", None, None),
        ("feeds",     "Send",              "Feed 追踪", "7", None),
        ("scrape",    "Download",          "采集监控", None, None),
        ("stores",    "Store",             "店铺", None, None),
    ]),
    ("操作", [
        ("wf",        "Play",  "工作流", None, None),
        ("schedule",  "Clock", "调度", None, None),
    ]),
]

def sidebar(active):
    rows = []
    for title, items in NAV:
        rows.append(f'<div class="grouptitle">{title}</div>')
        for key, icon, label, badge, btone in items:
            on = " on" if key == active else ""
            color = "#fff" if key == active else "currentColor"
            b = ""
            if badge:
                bcls = "navbadge amber" if btone == "amber" else "navbadge"
                bstyle = ' style="background:rgba(255,255,255,.2);color:#fff"' if key == active else ""
                b = f'<span class="{bcls}"{bstyle}>{badge}</span>'
            rows.append(f'<div class="navitem{on}">{ic(icon, 15, color)}<span style="flex:1">{label}</span>{b}</div>')
    return f'''<aside style="width:240px;flex:none;background:#fff;border-right:1px solid #e4e4e7;display:flex;flex-direction:column">
  <div style="height:48px;border-bottom:1px solid #f4f4f5;display:flex;align-items:center;gap:8px;padding:0 16px">
    <div class="mono" style="height:24px;width:24px;border-radius:4px;background:#18181b;color:#fff;display:grid;place-items:center;font-size:11px;font-weight:700">W</div>
    <div style="line-height:1.25"><div style="font-size:13px;font-weight:600;color:#18181b">WalmartAPI 运营台</div>
    <div style="font-size:10px;color:#a1a1aa">45 店 · 生产 · macOS 本机</div></div>
  </div>
  <nav style="flex:1;padding:8px 0">{"".join(rows)}</nav>
  <div style="padding:12px 16px;border-top:1px solid #f4f4f5;font-size:11px;color:#a1a1aa">
    <div style="display:flex;align-items:center;gap:6px">{dot("emerald")} launchd 2 条 · 智能体 9 条在班</div>
    <div class="mono" style="margin-top:4px">Asia/Shanghai · 2026-08-18</div>
  </div>
</aside>'''

def topbar(crumbs):
    parts = []
    for i, c in enumerate(crumbs):
        if i:
            parts.append(f'<span style="color:#d4d4d8">{ic("ChevronRight", 13, "#d4d4d8")}</span>')
        w = "500" if i == len(crumbs) - 1 else "400"
        col = "#18181b" if i == len(crumbs) - 1 else "#71717a"
        parts.append(f'<span style="font-size:13px;font-weight:{w};color:{col}">{c}</span>')
    return f'''<header style="height:48px;flex:none;background:#fff;border-bottom:1px solid #e4e4e7;display:flex;align-items:center;gap:12px;padding:0 16px 0 24px;">
  <div style="display:flex;align-items:center;gap:6px">{"".join(parts)}</div>
  <div style="flex:1"></div>
  <div style="position:relative;width:320px">
    <span style="position:absolute;left:10px;top:9px">{ic("Search", 14, "#a1a1aa")}</span>
    <div style="height:32px;border:1px solid #d4d4d8;border-radius:6px;background:#fff;display:flex;align-items:center;padding:0 8px 0 30px;font-size:13px;color:#a1a1aa">输 ASIN / 工作流名 / 店铺名 直达…</div>
    <span class="kbd" style="position:absolute;right:8px;top:8px">⌘K</span>
  </div>
</header>'''

def page(active, crumbs, content, h):
    return (f'<div style="width:1440px;height:{h}px;display:flex;background:#fafafa;overflow:hidden">'
            f'{sidebar(active)}'
            f'<div style="flex:1;display:flex;flex-direction:column;min-width:0">{topbar(crumbs)}'
            f'<main style="flex:1;padding:24px;display:flex;flex-direction:column;gap:16px;overflow:hidden">{content}</main>'
            f'</div></div>')

def shell(body, w, h):
    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  {FONTS}
  <style>{CSS}</style>
</helmet>
{body}
</x-dc>
<script data-dc-script data-props='{{"$preview": {{"width": {w}, "height": {h}}}}}'>
class Component extends DCLogic {{
  renderVals() {{ return {{}}; }}
}}
</script>
</body>
</html>'''

def card(title, inner, action="", style=""):
    act = f'<div style="display:flex;align-items:center;gap:8px">{action}</div>' if action else ""
    head = f'<header class="chead"><h2 class="ctitle">{title}</h2>{act}</header>' if title else ""
    return f'<section class="card" style="{style}">{head}{inner}</section>'

def kpi(label, value, sub="", tone=None):
    v_extra = f';color:{TONES[tone][1]}' if tone else ""
    s = f'<div style="margin-top:4px;font-size:11px;color:#a1a1aa">{sub}</div>' if sub else ""
    return (f'<div class="card" style="padding:16px;flex:1">'
            f'<div class="kpilabel">{label}</div>'
            f'<div class="kpival" style="margin-top:8px{v_extra}">{value}</div>{s}</div>')

# ════════════════════════ 1. 设计系统总览 ════════════════════════
def swatch(hexv, name, big=False):
    h = "56px" if big else "40px"
    return (f'<div style="flex:1;min-width:0"><div style="height:{h};border-radius:6px;background:{hexv};'
            f'border:1px solid #e4e4e7"></div><div style="margin-top:6px;font-size:12px;color:#3f3f46">{name}</div>'
            f'<div class="id" style="font-size:11px;color:#a1a1aa">{hexv}</div></div>')

def ds_board():
    base = "".join([swatch("#fafafa", "底"), swatch("#ffffff", "面板"), swatch("#f4f4f5", "内边框"),
                    swatch("#e4e4e7", "边框"), swatch("#18181b", "文字主 / 主按钮"), swatch("#71717a", "文字次"),
                    swatch("#a1a1aa", "文字弱")])
    status = "".join([swatch(DOTS["emerald"], "成功 emerald"), swatch(DOTS["red"], "失败 red"),
                      swatch(DOTS["gray"], "忙 gray"), swatch(DOTS["amber"], "不确定 amber"),
                      swatch(DOTS["sky"], "在途 sky"), swatch(DOTS["violet"], "特例 violet"),
                      swatch("#dc2626", "危险按钮 red-600")])
    colors = card("色板 · 逐值取自旧系统 erp-core", f'''<div style="padding:16px;display:flex;flex-direction:column;gap:16px">
      <div style="display:flex;gap:12px">{base}</div>
      <div style="display:flex;gap:12px">{status}</div>
      <div style="font-size:12px;color:#71717a">中性色 = Tailwind zinc;状态色 emerald / amber / red / sky / violet;主按钮是 <b>zinc-900 黑</b>,不引入品牌蓝(界面上的按钮真的会写沃尔玛,不能染官方色)。</div>
    </div>''')

    type_rows = [
        ("24 / 600 / mono tab", '<span class="kpival">128,455</span>', "KPI 大数"),
        ("14 / 400", '<span style="font-size:14px">订单行按「订单号 + SKU」合并,同订单同 SKU 必为一行。</span>', "正文 / 表格单元"),
        ("13 / 500", '<span style="font-size:13px;font-weight:500">卡片标题 · 导航项</span>', "组件标题"),
        ("12.5 / mono", '<span class="id">B0CHXNPXVX · PHUMWMT202608180042 · 6f9c41d2-8e31</span>', "ID:ASIN / SKU / feedId,要逐字符比对"),
        ("11 / 500 / 全大写", '<span class="th" style="border:0;background:none;padding:0">store · audit_status · exit</span>', "表头"),
        ("10 / mono", '<span class="navbadge">23</span> <span class="kbd">⌘K</span>', "徽标 / 快捷键"),
    ]
    trs = "".join(f'<tr class="rz"><td class="td" style="width:170px;color:#71717a;font-size:12px">{a}</td>'
                  f'<td class="td">{b}</td><td class="td" style="color:#a1a1aa;font-size:12px">{c}</td></tr>'
                  for a, b, c in type_rows)
    typo = card("字阶 · Inter + Noto Sans SC / JetBrains Mono", f'<table>{trs}</table>')

    outcome = card("语义① 运行结局 —— 五种,不是两种", f'''<div style="padding:16px;display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;align-items:center;gap:24px">
        {pill("emerald", "成功 · 退出码 0")}{pill("red", "失败 · 退出码 1")}{pill("gray", "忙 · 退出码 3,上一轮还在跑")}
        {pill("amber", "结局不确定 · feed Unknown", hollow=True)}{pill("violet", "配置错 · 退出码 2")}
      </div>
      <div style="font-size:12px;color:#71717a;line-height:1.7">忙用<b>石板灰、不许红</b> —— 染红的话「它正忙」和「它坏了」混为一谈,人会天天去查一个没坏的东西。<br>
      「结局不确定」用<b>琥珀空心点</b>(实心 = 已落定):它既不是成功也不是失败,系统的处理是不重复提交、等自愈链收尾。</div>
    </div>''')

    btns = card("语义② 按钮三档 + 危险的形状", f'''<div style="padding:16px;display:flex;flex-direction:column;gap:14px">
      <div style="display:flex;align-items:center;gap:10px">
        <span class="btn bghost">查询</span><span class="btn bs">导出 CSV</span><span class="btn bp">同步 UPC</span>
        <span class="btn bdanger">{ic("Zap", 14, "#fff")}确认执行</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="btn sm bghost">查询</span><span class="btn sm bs">导出</span><span class="btn sm bp">运行</span>
        <span class="btn sm bdanger">{ic("Zap", 12, "#fff")}执行</span>
        <span style="width:24px"></span>
        <span style="font-size:12px;color:#71717a">对照:危险按钮(红<b>实心块</b> + ⚡)vs 失败徽章(红<b>点</b> + 文字)→</span>
        {pill("red", "失败")}{tag("red", "拒绝")}
      </div>
      <div style="font-size:12px;color:#71717a;line-height:1.7">危险按钮与失败徽章同色相,靠<b>形状 + ⚡ 图标</b>区分。危险按钮永远先出现「预览」那一步,不给直达;只读动作用幽灵按钮,常规写入用黑实心。</div>
    </div>''')

    midfinal = card("语义③ 中间态 vs 终态", f'''<div style="padding:16px;display:flex;flex-direction:column;gap:14px">
      <div style="display:flex;align-items:center;gap:16px">
        <span style="font-size:12px;color:#a1a1aa;width:64px">终态(实心)</span>
        {tag("emerald", "approved")}{tag("red", "rejected")}{tag("emerald", "done")}{tag("gray", "withdrawn")}{tag("emerald", "通过")}{tag("red", "拒绝")}
      </div>
      <div style="display:flex;align-items:center;gap:16px">
        <span style="font-size:12px;color:#a1a1aa;width:64px">中间态(虚线)</span>
        {tag("amber", "pending", dashed=True)}{tag("amber", "executing", dashed=True)}{tag("amber", "Unknown", dashed=True)}{tag("sky", "suggested", dashed=True)}{tag("amber", "待人工", dashed=True)}
      </div>
      <div style="font-size:12px;color:#71717a;line-height:1.7">pending / executing / Unknown 都是<b>还没完</b>,不配与 approved / done 相同的视觉重量:浅底 + 虚线描边,且列表里可一键筛出。</div>
    </div>''')

    def minitable(cls, label):
        rows = "".join(f'<tr class="{cls}"><td class="td id">B0CHXNPX{i}X</td><td class="td">Anker 65W 充电器 …</td>'
                       f'<td class="td num">$23.99</td><td class="td">{tag("emerald", "approved")}</td></tr>' for i in "VW")
        return (f'<div style="flex:1"><div style="font-size:12px;color:#71717a;margin-bottom:6px">{label}</div>'
                f'<div style="border:1px solid #e4e4e7;border-radius:6px;overflow:hidden"><table>'
                f'<tr><th class="th">ASIN</th><th class="th">标题</th><th class="th" style="text-align:right">采集价</th><th class="th">审核</th></tr>{rows}</table></div></div>')
    density = card("表格两档密度", f'''<div style="padding:16px;display:flex;gap:16px">
      {minitable("rc", "紧凑 32px —— 几十万行的产品/订单表默认档")}
      {minitable("rz", "舒适 40px —— 队列与详情内嵌表")}
    </div>''')

    body = f'''<div style="width:1440px;height:1200px;background:#fafafa;padding:32px;display:flex;flex-direction:column;gap:16px;overflow:hidden">
      <div style="display:flex;align-items:baseline;gap:16px">
        <h1 style="font-size:20px;font-weight:600">设计系统 · WalmartAPI 运营台</h1>
        <span style="font-size:12px;color:#71717a">配色与组件词汇对齐旧系统 erp-core · 桌面 1440 · 单用户全天盯的工具,信息密度高但有层次</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">{colors}{typo}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">{outcome}{btns}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">{midfinal}{density}</div>
    </div>'''
    return shell(body, 1440, 1200)

# ════════════════════════ 2. 总览首页(Main) ════════════════════════
def main_board():
    kpis = "".join([
        kpi("在架商品", "128,455", "9/9 店 ACTIVE · catalog_sync 13:00"),
        kpi("待审产品", "1,262", "audit_status 为空或 pending"),
        kpi("待处置建议", "214", "ops.dispositions · suggested"),
        kpi("在途 feed", "7", "含 1 条 pending 超 3 天", tone="amber"),
        kpi("UPC 池余量", "412", "低于 3 天用量,需补池", tone="amber"),
    ])
    kpirow = f'<div style="display:flex;gap:16px">{kpis}</div>'

    def todo(tone, hollow, text, dest):
        d = '<span class="dothollow"></span>' if hollow else dot(tone)
        return (f'<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid #f4f4f5">'
                f'{d}<span style="flex:1;font-size:13px;color:#27272a">{text}</span>'
                f'<span style="font-size:12px;color:#0369a1;display:inline-flex;align-items:center;gap:2px">{dest}{ic("ChevronRight", 12, "#0369a1")}</span></div>')
    todos = card("需要我管的事 · 5", "".join([
        todo("red", False, "<b>谭总10</b> 凭证失效 —— 该店今天整体被跳过(订单/上架/维护全停)", "店铺"),
        todo("red", False, "product_clear 15:00 失败(退出码 1)· 摘要:PG 连接数触顶,已钳制并发", "运行记录"),
        todo("amber", True, '<span class="id">6f9c41d2</span> 在途 3 天未落定(pending)· 301 SKU 每轮被防重跳过且 N 不变', "Feed 追踪"),
        todo("amber", False, "UPC 池余量 412,按日均领用 180 只够 2.3 天", "上架 · UPC 池"),
        todo("amber", False, "订单待人工 23 单,最早滞留 08-16(商品一致性闸 14 单)", "订单待人工"),
    ]), style="flex:1")

    chain = card("当天次序 · 硬约束", f'''<div style="padding:12px 16px;display:flex;flex-direction:column;gap:0">
      <div style="font-size:12px;color:#71717a;padding-bottom:8px">谁提前,谁就是拿昨天的数据做今天的判断</div>
      {"".join(f'<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid #f4f4f5"><span class="id" style="width:44px">{t}</span><span style="font-size:13px;font-weight:500">{n}</span><span style="font-size:12px;color:#a1a1aa">{d}</span></div>' for t, n, d in [
        ("13:00", "产品链", "采集→入库→维护→问题"), ("15:00", "黑名单 / 清仓", "先拦后清"),
        ("18:10", "产品审核", "拿当天新数据判"), ("20:00", "上架", "只上今天过审的")])}
    </div>''')

    runs_rows = []
    for t, label, wfs, st, dur, summary in [
        ("02:00", "backup", "backup", ("emerald", False, "成功"), "3m08s", "全库 pg_dump 4.2GB → 保留 14 份"),
        ("06:40", "daily_report", "daily_report", ("emerald", False, "成功"), "11m42s", "42 店日报已发飞书;影刀 KPI 腿沿用旧值(唤起失败已另报)"),
        ("07:30", "order_daily", "perf_problems · order_asin_normalize", ("emerald", False, "成功"), "4m51s", "绩效问题 3 单入台账;ASIN 归一 217 行"),
        ("14:20", "order_chain", "order_sync · order_audit · returns_sync", ("emerald", False, "成功"), "9m17s", "42/43 店 3,368 订单行;审核 通过 3,201 / 拒 96 / 钓鱼 2 / 待人工 23"),
        ("14:30", "feed_poll", "feed_poll", ("emerald", False, "成功"), "1m02s", "2 feed 落定 301 SKU;在途 7"),
        ("13:00", "product_chain", "catalog_sync … problem_product_cleanup(7 步)", ("emerald", False, "成功"), "1h48m", "在架 128,455;维护建议 214;问题归类 37"),
        ("15:00", "blacklist", "risk_sync · blacklist_push", ("emerald", False, "成功"), "2m33s", "品牌黑名单 +3(err_hits 自产);已推飞书"),
        ("15:00", "product_clear", "product_clear", ("red", False, "失败"), "22s", "PG 连接数触顶(129/100)—— 已钳制并发,待重跑"),
        ("18:10", "audit_sheet", "product_audit", None, "", "今晚 18:10"),
        ("20:00", "list_new", "list_new", None, "", "今晚 20:00"),
        ("周三 08:00", "settlement", "settlement_sync", None, "", "下次 08-19"),
    ]:
        if st is None:
            state = f'<span style="display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#a1a1aa">{ic("Clock", 13, "#a1a1aa")}未到点</span>'
            dim = ";color:#a1a1aa"
        else:
            state = pill(st[0], st[2], hollow=st[1])
            dim = ""
        runs_rows.append(f'<tr class="rz"><td class="td id" style="width:84px{dim}">{t}</td>'
                         f'<td class="td id" style="width:120px;font-weight:500{dim}">{label}</td>'
                         f'<td class="td" style="width:280px;font-size:12px;color:#71717a">{wfs}</td>'
                         f'<td class="td" style="width:110px">{state}</td>'
                         f'<td class="td num" style="width:80px;color:#71717a">{dur}</td>'
                         f'<td class="td" style="font-size:12px;color:#52525b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:390px">{summary}</td></tr>')
    runs = card("今天的自动任务 · 11 条(launchd 2 + 智能体 9)",
                f'<table><tr><th class="th">时间</th><th class="th">任务</th><th class="th">工作流</th><th class="th">结局</th><th class="th" style="text-align:right">耗时</th><th class="th">摘要首行</th></tr>{"".join(runs_rows)}</table>',
                action='<span class="btn sm bghost">运行记录</span>')

    content = (kpirow +
               f'<div style="display:grid;grid-template-columns:2fr 1fr;gap:16px;align-items:stretch">{todos}{chain}</div>' +
               runs)
    return shell(page("overview", ["总览", "2026-08-18 周二"], content, 1100), 1440, 1100)

# ════════════════════════ 3. 工作流触发 · 选择与参数 ════════════════════════
DOMAINS = ["采集与目录", "产品审核", "上架", "价格与库存", "订单与售后", "黑名单", "维护与清理", "报表与备份", "运维工具"]

WF_LISTING = [
    ("list_new", True, "上架新品:七道闸 → 领 UPC → LLM 属性 → 提交 feed"),
    ("match_listing", True, "跟卖上架:三道闸(风控/黑名单/防呆)→ 提交"),
    ("sku_locked_heal", True, "SKU 锁死自愈:RETIRE → 24h → 清列重上"),
    ("store_release", True, "店铺释放:清空该店在架再解绑"),
    ("upc_sync", False, "同步 UPC 池与沃尔玛后台占用"),
    ("variant_probe", False, "变体组探测(纯只读)"),
]

def wfselect_board():
    doms = "".join(
        f'<div style="display:flex;align-items:center;height:32px;padding:0 12px;border-radius:6px;font-size:13px;'
        f'{"background:#18181b;color:#fff;font-weight:500" if d == "上架" else "color:#3f3f46"}">{d}</div>'
        for d in DOMAINS)
    left = card("业务域", f'<div style="padding:8px;display:flex;flex-direction:column;gap:2px">{doms}</div>', style="width:200px;flex:none")

    items = []
    for name, danger, desc in WF_LISTING:
        on = name == "list_new"
        border = "border:1px solid #18181b" if on else "border:1px solid transparent"
        dtag = tag("red", "危") if danger else ""
        items.append(f'''<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:6px;{border};{"background:#fafafa" if on else ""}">
          <span class="id" style="font-weight:500;font-size:13px;color:#18181b;width:130px">{name}</span>{dtag}
          <span style="flex:1;font-size:12px;color:#71717a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{desc}</span></div>''')
    mid = card("上架 · 6 条", f'<div style="padding:8px;display:flex;flex-direction:column;gap:2px">{"".join(items)}</div>',
               style="flex:1")

    def field(label, inner, note=""):
        n = f'<div style="font-size:11px;color:#a1a1aa;margin-top:4px">{note}</div>' if note else ""
        return f'<div><div style="font-size:12px;color:#52525b;margin-bottom:6px">{label}</div>{inner}{n}</div>'
    inp = lambda v, ph="": (f'<div class="mono" style="height:36px;border:1px solid #d4d4d8;border-radius:6px;background:#fff;'
                            f'display:flex;align-items:center;padding:0 10px;font-size:13px;color:{"#18181b" if v else "#a1a1aa"}">{v or ph}</div>')
    right = card(f'list_new {tag("red", "危 · 写沃尔玛,不可逆")}', f'''<div style="padding:16px;display:flex;flex-direction:column;gap:16px">
      <div style="font-size:12.5px;color:#52525b;line-height:1.8;background:#fafafa;border:1px solid #f4f4f5;border-radius:6px;padding:10px 12px">
        从飞书上架表取过审行,依次过七道闸(非 ACTIVE 店 / 超配额 / 风控 / 黑名单 / 全局去重 / 防呆 / 数据不全),
        领 UPC → LLM 生成属性 → 规格一致化 → 按店提交 MP_ITEM feed。调度每天 20:00。</div>
      {field("limit — 本轮候选上限", inp("3000"), "整数;默认吃完整个待上架队列")}
      {field("store — 限单店(可空)", inp("", "空 = 全部 ACTIVE 店"), "店铺代号,如 A085")}
      {field("l3 — LLM 语义层", inp("on"), "off = 只走规则闸,不调用 LLM")}
      <div style="border-top:1px solid #f4f4f5;padding-top:14px;display:flex;flex-direction:column;gap:10px">
        <span class="btn bp" style="width:100%">{ic("Eye", 14, "#fff") if "Eye" in ICONS else ""}预览这一轮会做什么(--dry-run)</span>
        <div style="font-size:11px;color:#a1a1aa;line-height:1.6">危险工作流必须先预览:服务端不持有同参数的 dry-run 预览票就拒绝真跑(409)。预览本身就是一次真实空跑,输出即破坏面。</div>
      </div>
    </div>''', style="width:400px;flex:none")

    content = f'<div style="display:flex;gap:16px;flex:1;min-height:0">{left}{mid}{right}</div>'
    return shell(page("wf", ["工作流", "上架", "list_new"], content, 900), 1440, 900)

# ════════════════════════ 4. 工作流触发 · 预览破坏面 ════════════════════════
def wfpreview_board():
    stores = [
        ("A085", "61", "39", None), ("A102", "55", "45", None), ("A117", "52", "48", None),
        ("A093", "49", "51", None), ("A128", "48", "52", None), ("A140", "45", "55", None),
        ("A076", "44", "56", None), ("A111", "44", "56", None), ("A099", "44", "56", None),
        ("谭总10", "0", "—", "凭证失效 · 跳过"),
    ]
    srows = "".join(
        f'<tr class="rc"><td class="td id" style="font-weight:500">{s}</td>'
        f'<td class="td num">{n}</td><td class="td num" style="color:#71717a">{q}</td>'
        f'<td class="td">{tag("red", note) if note else tag("emerald", "就绪")}</td></tr>'
        for s, n, q, note in stores)
    log = '''<span class="dim">20:00:14</span> [DRY-RUN] 候选 1,262(limit=3000,实取 1,262)
<span class="dim">20:00:31</span> [DRY-RUN] 闸门:非ACTIVE店 -38 · 超配额 -310 · 风控 -124 · 黑名单 -86 · 去重 -41 · 防呆 -7 · 数据不全 -214
<span class="dim">20:00:31</span> [DRY-RUN] 过闸 442 条,分到 9 店
<span class="dim">20:00:52</span> [DRY-RUN] A085: 61 items → MP_ITEM feed(不提交,不领 UPC)
<span class="dim">20:00:52</span> [DRY-RUN] 谭总10: <span class="warn">凭证失效,整店跳过</span>
<span class="dim">20:00:53</span> [DRY-RUN] 完成:本轮将提交 9 feed / 442 SKU,预计领用 UPC 442'''
    modal = f'''<div style="position:absolute;inset:0;background:rgba(24,24,27,.3);display:flex;align-items:center;justify-content:center">
      <div style="width:760px;background:#fff;border:1px solid #e4e4e7;border-radius:8px;box-shadow:0 20px 40px rgba(24,24,27,.18)">
        <header style="padding:0 20px;height:48px;border-bottom:1px solid #f4f4f5;display:flex;align-items:center;gap:10px">
          <h3 style="font-size:14px;font-weight:500">预览 · list_new</h3>{tag("gray", "DRY-RUN")}{tag("red", "危")}
          <span style="flex:1"></span><span class="id" style="color:#a1a1aa">-p limit=3000 · 20:00:53 生成</span>{ic("X", 16, "#a1a1aa")}
        </header>
        <div style="padding:16px 20px;display:flex;flex-direction:column;gap:14px;max-height:560px">
          <div style="display:flex;gap:12px">
            <div style="flex:1;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:12px">
              <div style="font-size:12px;color:#b91c1c">破坏面</div>
              <div style="font-size:15px;font-weight:600;color:#b91c1c;margin-top:4px">向 9 家店提交 442 条上架 feed · 领用 442 个 UPC</div>
            </div>
            <div style="width:220px;background:#fafafa;border:1px solid #f4f4f5;border-radius:6px;padding:12px">
              <div style="font-size:12px;color:#71717a">候选 1,262 → 七道闸拦 820</div>
              <div style="font-size:12px;color:#71717a;margin-top:4px">最大闸:超配额 -310 · 数据不全 -214</div>
            </div>
          </div>
          <div style="border:1px solid #e4e4e7;border-radius:6px;overflow:hidden;max-height:190px">
            <table><tr><th class="th">店铺</th><th class="th" style="text-align:right">本轮提交</th><th class="th" style="text-align:right">配额余量</th><th class="th">状态</th></tr>{srows}</table>
          </div>
          <div class="log" style="max-height:130px">{log}</div>
        </div>
        <footer style="padding:0 20px;height:56px;border-top:1px solid #f4f4f5;display:flex;align-items:center;gap:10px">
          <span style="font-size:12px;color:#a1a1aa">预览即真实空跑输出 —— 人眼过一遍再执行</span><span style="flex:1"></span>
          <span class="btn bs">取消</span><span class="btn bdanger">{ic("Zap", 14, "#fff")}确认执行(真跑)</span>
        </footer>
      </div>
    </div>'''
    doms = card("业务域", '<div style="padding:8px"><div style="height:240px"></div></div>', style="width:200px;flex:none")
    base = f'<div style="display:flex;gap:16px;flex:1;min-height:0;filter:blur(0)">{doms}' \
           f'{card("上架 · 6 条", chr(60) + "div style=" + chr(34) + "height:300px" + chr(34) + chr(62) + chr(60) + "/div" + chr(62), style="flex:1")}' \
           f'{card("list_new", chr(60) + "div style=" + chr(34) + "height:300px" + chr(34) + chr(62) + chr(60) + "/div" + chr(62), style="width:400px;flex:none")}</div>'
    inner = f'<div style="position:relative;flex:1;display:flex;min-height:0">{base}{modal}</div>'
    content = f'<div style="position:relative;display:flex;flex-direction:column;flex:1;min-height:0">{inner}</div>'
    return shell(page("wf", ["工作流", "上架", "list_new", "预览"], content, 900), 1440, 900)

# ════════════════════════ 5. 工作流触发 · 执行与结局 ════════════════════════
def wflive_board():
    log = '''<span class="dim">20:00:58</span> [EXECUTE] 预览票核验通过(limit=3000 · 20:00:53)
<span class="dim">20:01:02</span> flock 单实例锁 OK → ops.runs #18492(operator=web)
<span class="dim">20:01:40</span> A085: 领 UPC 61 → LLM 属性 61/61 → mp_conform OK
<span class="dim">20:02:04</span> A085: MP_ITEM feed <span class="ok">提交成功</span> feedId=8f31c2d9 · feed_log 先落 pending 再改 submitted
<span class="dim">20:02:47</span> A102: 55 items → feedId=b7e04a11 <span class="ok">提交成功</span>
<span class="dim">20:03:58</span> A117: 52 items → feedId=c94d7f02 <span class="ok">提交成功</span>
<span class="dim">20:04:41</span> A093: 网络超时 1 次 → 反查 feed_log 三态:<span class="warn">未达</span> → 同一方法补交 <span class="ok">成功</span>
<span class="dim">20:05:36</span> A140: 45 items → feedId=e2a91b6d <span class="ok">提交成功</span> · x-current-token-count=4 → 退避 90s
<span class="dim">20:06:20</span> 9/9 店完成:442 SKU 已提交,失败 0,Unknown 0
<span class="dim">20:06:26</span> <span class="ok">exit 0</span> · 飞书通知已发 · feed_poll 将在 20:30 收结果'''
    head = f'''<div class="card" style="padding:16px;display:flex;align-items:center;gap:14px">
      <span class="btn bp sm">{ic("Play", 12, "#fff")}list_new</span>
      <span class="id" style="color:#71717a">ops.runs #18492</span>{tag("gray", "EXECUTE")}
      <span class="id" style="color:#71717a">-p limit=3000</span>
      <span style="flex:1"></span>
      {pill("emerald", "成功 · 退出码 0")}
      <span class="id" style="color:#71717a">20:00:58 → 20:06:26 · 5m28s · operator=web</span>
    </div>'''
    summary = card("结果摘要 · run() 返回原文", '''<div style="padding:16px;font-size:13px;line-height:1.9;color:#27272a">
      9 店提交 442 条(A085 61 / A102 55 / A117 52 / A093 49 / A128 48 / A140 45 / A076 44 / A111 44 / A099 44);
      谭总10 凭证失效整店跳过。UPC 领用 442(池余 412 → 补池提醒已挂)。LLM 属性 442/442,规格一致化零回退。
      feed 9 条全部 submitted,等 feed_poll 收终态。</div>''')
    nxt = f'''<div style="display:flex;gap:10px">
      <span class="btn bs">{ic("Send", 14)}去 Feed 追踪看 9 条在途</span>
      <span class="btn bs">{ic("ScrollText", 14)}运行记录</span>
      <span class="btn bghost">下载完整日志</span>
      <span style="flex:1"></span>
      <span style="font-size:12px;color:#a1a1aa;align-self:center">失败不给「自动重试」—— 写操作只走 feed_log 三态反查后同法补交</span>
    </div>'''
    content = head + summary + card("实时输出(子进程 stderr 管道,非日志文件)", f'<div style="padding:16px"><div class="log">{log}</div></div>') + nxt
    return shell(page("wf", ["工作流", "上架", "list_new", "运行 #18492"], content, 900), 1440, 900)

# ════════════════════════ 6. 产品详情 ════════════════════════
def product_board():
    def drow(k, v, mono=False):
        cls = ' class="id"' if mono else ' style="font-size:13px;color:#27272a"'
        return (f'<div style="display:flex;align-items:baseline;gap:12px;padding:8px 0;border-bottom:1px solid #f4f4f5">'
                f'<span style="width:88px;flex:none;font-size:12px;color:#71717a">{k}</span><span{cls}>{v}</span></div>')

    amazon = card("亚马逊侧 · catalog.products + latest_snapshot", f'''<div style="padding:8px 16px 12px">
      {drow("标题", "Anker 65W USB C Charger, 3-Port Foldable Compact Fast Charger…")}
      {drow("品牌", "Anker(不在黑名单 · 制造商 Anker Innovations)")}
      {drow("价格", '<span class="id">$23.99</span> · In Stock')}
      {drow("运费", '<span class="id">$6.99</span>(fast.shipping;null 时不当 0)')}
      {drow("落地价", '<span class="id" style="font-weight:600">$30.98</span> = 单价 + 运费')}
      {drow("货期", "2 天 · 变体 3(主变体本行)")}
      {drow("类目", "Electronics › Chargers & Power Adapters")}
      {drow("最新快照", '<span class="id">2026-08-18 09:12</span> · 24h 内,审核可用')}
    </div>''', style="flex:1")

    walmart = card("沃尔玛侧 · catalog.walmart_items", f'''<div style="padding:8px 16px 12px">
      {drow("店铺 / SKU", '<span class="id">A085 · PHUMWMT202607050012</span>')}
      {drow("UPC", '<span class="id">194735092345</span>(池:used)')}
      {drow("在架状态", pill("emerald", "PUBLISHED · ACTIVE"))}
      {drow("售价", '<span class="id">$47.99</span>(工作流按规则算出,人改的是规则)')}
      {drow("库存", '<span class="id">12</span>')}
      {drow("Buy Box", "持有 · 无跟卖竞争")}
      {drow("最近核对", '<span class="id">2026-08-18 13:52</span> · problem_scan')}
      {drow("维护建议", "无 suggested 挂起")}
    </div>''', style="flex:1")

    def ev(date, tone, label, dashed, detail, src):
        return f'''<div style="display:flex;gap:14px;padding:10px 0;border-bottom:1px solid #f4f4f5">
          <span class="id" style="width:118px;flex:none;color:#71717a">{date}</span>
          <span style="width:6px;flex:none;display:flex;justify-content:center"><span class="dot" style="background:{DOTS[tone]};margin-top:5px"></span></span>
          <span style="width:110px;flex:none">{tag(tone, label, dashed=dashed)}</span>
          <span style="flex:1;font-size:13px;color:#27272a">{detail}</span>
          <span class="id" style="flex:none;color:#a1a1aa">{src}</span></div>'''
    timeline = card("产品病历 · catalog.product_events —— 「它当初为什么被删/被拒」的唯一答案", "".join([
        ev("07-02 13:41", "gray", "入库", False, "从采集快照入库;来源批次 zip=10019", "catalog_sync"),
        ev("07-03 18:22", "emerald", "审核通过", False, "L1 规则全过;L3 语义未触发;类目映射 Electronics → Walmart『Wall Chargers』", "product_audit"),
        ev("07-05 20:03", "sky", "上架提交", False, 'MP_ITEM feed <span class="id">8f31c2d9</span> · 领 UPC 194735092345', "list_new"),
        ev("07-06 13:47", "emerald", "商品出现", False, "沃尔玛侧首次观测到,PUBLISHED", "problem_scan"),
        ev("08-11 13:55", "amber", "维护提交", False, "改价 $45.99 → $47.99;原因码:亚马逊涨价 $19.99 → $23.99", "maintenance"),
        ev("08-12 14:00", "gray", "状态变更", False, "价格生效核验通过,建议 #4471 executing → done", "feed_poll"),
    ]) + '<div style="padding:10px 16px;font-size:11px;color:#a1a1aa">事件全集:入库 / 审核通过 / 审核拒绝 / 上架提交 / 删除提交 / 停用提交 / 维护提交 / 问题归类 / 商品出现 / 商品消失 / 状态变更 / 删除已核验 / 删除未生效</div>')

    feeds_rows = "".join([
        f'<tr class="rz"><td class="td id">8f31c2d9</td><td class="td id">MP_ITEM</td><td class="td">{tag("emerald", "done")}</td><td class="td">success · 07-05 21:12 落定</td><td class="td id" style="color:#a1a1aa">list_new</td></tr>',
        f'<tr class="rz"><td class="td id">d2c807aa</td><td class="td id">price</td><td class="td">{tag("emerald", "done")}</td><td class="td">success · 08-11 14:31 落定</td><td class="td id" style="color:#a1a1aa">maintenance</td></tr>',
        f'<tr class="rz"><td class="td id">6f9c41d2</td><td class="td id">inventory</td><td class="td">{tag("amber", "pending", dashed=True)}</td><td class="td" style="color:#b45309">在途 3 天 —— 提交结局不确定,不重复提交,等自愈链</td><td class="td id" style="color:#a1a1aa">maintenance</td></tr>',
    ])
    feeds = card("该 SKU 的 feed 台账 · ops.feed_log / feed_items",
                 f'<table><tr><th class="th">feedId</th><th class="th">类型</th><th class="th">feed 状态</th><th class="th">SKU 级结果</th><th class="th">提交方</th></tr>{feeds_rows}</table>')

    header = f'''<div class="card" style="padding:16px;display:flex;align-items:center;gap:14px">
      <span style="font-size:12px;color:#0369a1;display:inline-flex;align-items:center;gap:2px">产品 {ic("ChevronRight", 12, "#a1a1aa")}</span>
      <span class="id" style="font-size:15px;font-weight:600;color:#18181b;display:inline-flex;align-items:center;gap:6px">B0CHXNPXVX {ic("Copy", 13, "#a1a1aa")}</span>
      <span style="font-size:13px;color:#52525b;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Anker 65W USB C Charger, 3-Port Foldable Compact…</span>
      {tag("emerald", "approved")}{pill("emerald", "在架 · A085")}
      <span style="flex:1"></span>
      <span class="btn sm bs">{ic("ExternalLink", 13)}亚马逊</span><span class="btn sm bs">{ic("ExternalLink", 13)}沃尔玛后台</span>
    </div>'''
    note = ('<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#71717a;background:#fffbeb;'
            'border:1px solid #fde68a;border-radius:6px;padding:8px 12px">'
            + ic("AlertTriangle", 13, "#b45309") +
            '这里没有「编辑价格/标题」—— 那样的写入通道不存在。价格由工作流按规则算出;要改,去改规则(飞书配置表),或对单店跑维护工作流。</div>')
    content = header + f'<div style="display:flex;gap:16px">{amazon}{walmart}</div>' + timeline + feeds + note
    return shell(page("products", ["产品", "B0CHXNPXVX"], content, 1400), 1440, 1400)

# ════════════════════════ 7. 订单待人工队列 ════════════════════════
def orderqueue_board():
    rows = [
        ("08-18 09:12", "108934567890123", "PHUMWMT202606110034", "A102", "$52.49", "商品一致性 · 相似度 43%", True),
        ("08-18 08:05", "108934561177452", "PHUMWMT202605280081", "A085", "$31.98", "限价 · 超 $3.20", False),
        ("08-17 22:47", "108934522903318", "PHUMWMT202607050012", "A117", "$47.99", "采集完整性 · 快照缺运费", False),
        ("08-17 20:31", "108934519984401", "PHUMWMT202603140007", "A093", "$68.00", "配送时长 · 9 天", False),
        ("08-17 14:12", "108934488251170", "PHUMWMT20260801056", "A128", "$24.99", "采购方匹配 · 无可用采购方", False),
        ("08-16 19:44", "108934417765093", "PHUMWMT202512190022", "A140", "$89.99", "商品一致性 · 相似度 51%", False),
        ("08-16 11:03", "108934402118864", "PHUMWMT202604220019", "A076", "$19.99", "限价 · 超 $0.85", False),
    ]
    trs = "".join(
        f'<tr class="rz" style="{"background:#fafafa;outline:1px solid #18181b;outline-offset:-1px" if on else ""}">'
        f'<td class="td id" style="color:#71717a">{t}</td><td class="td id" style="font-weight:500">{po}</td>'
        f'<td class="td id">{sku}</td><td class="td id">{store}</td><td class="td num">{amt}</td>'
        f'<td class="td">{tag("amber", gate, dashed=True)}</td><td class="td">{tag("amber", "待人工", dashed=True)}</td></tr>'
        for t, po, sku, store, amt, gate, on in rows)
    left = card("待人工 · 23 单(六道闸任一给不出确定答案,一律进这里 —— 绝不当通过)",
        f'''<div style="padding:12px 16px;display:flex;gap:8px;border-bottom:1px solid #f4f4f5">
          <span class="btn sm bs">店铺:全部</span><span class="btn sm bs">判闸:全部</span><span class="btn sm bs">滞留 ≥ 1 天</span>
          <span style="flex:1"></span><span class="btn sm bghost">导出</span></div>
        <table><tr><th class="th">下单时间</th><th class="th">订单号</th><th class="th">SKU</th><th class="th">店铺</th><th class="th" style="text-align:right">金额</th><th class="th">判成待人工的闸</th><th class="th">状态</th></tr>{trs}''' +
        '<div style="padding:10px 16px;font-size:11px;color:#a1a1aa">六道闸依次判:钓鱼(邮编)→ 采集完整性 → 商品一致性 → 配送时长 → 采购方匹配 → 限价</div>',
        style="flex:1;min-width:0")

    def gate(name, ok, detail):
        icon = ic("Check", 13, "#047857") if ok else ic("X", 13, "#b45309")
        col = "#27272a" if ok else "#b45309"
        return (f'<div style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:12.5px;color:{col}">'
                f'{icon}<span style="width:88px;flex:none">{name}</span><span style="color:#71717a">{detail}</span></div>')
    drawer = f'''<div class="card" style="width:480px;flex:none;display:flex;flex-direction:column">
      <header class="chead"><div style="display:flex;align-items:center;gap:8px"><h2 class="ctitle id">108934567890123</h2>{tag("amber", "待人工", dashed=True)}</div>{ic("X", 15, "#a1a1aa")}</header>
      <div style="padding:14px 16px;display:flex;flex-direction:column;gap:14px;overflow:hidden">
        <div>
          <div style="font-size:12px;color:#71717a;margin-bottom:4px">六道闸</div>
          {gate("钓鱼邮编", True, "97035 不在钓鱼库")}{gate("采集完整性", True, "快照 08-18 07:40 · 全字段")}
          {gate("商品一致性", False, "标题相似度 43% —— 给不出确定答案")}
          {gate("配送时长", True, "5 天")}{gate("采购方匹配", True, "P-201 可用")}{gate("限价", True, "落地 $30.98 ≤ 限 $33.50")}
        </div>
        <div>
          <div style="font-size:12px;color:#71717a;margin-bottom:6px">证据 · 标题对照(相似度 43%)</div>
          <div style="border:1px solid #fde68a;background:#fffbeb;border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.7">
            <div><span style="color:#b45309;font-weight:500">沃尔玛卖的:</span>Anker 65W USB C Charger, 3-Port Foldable…</div>
            <div style="margin-top:4px"><span style="color:#b45309;font-weight:500">亚马逊现在:</span>Anker Nano II 30W GaN Charger, Single Port…</div>
            <div style="margin-top:4px;color:#71717a">疑似链接被换品 —— 人判:是同款迭代还是被偷梁换柱?</div>
          </div>
        </div>
        <div style="display:flex;gap:12px">
          <div style="flex:1;height:110px;border:1px dashed #d4d4d8;border-radius:6px;display:grid;place-items:center;font-size:11px;color:#a1a1aa">亚马逊快照截图</div>
          <div style="width:170px;background:#fafafa;border:1px solid #f4f4f5;border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.9">
            <div>采购 <span class="id">$23.99</span></div><div>运费 <span class="id">$6.99</span></div>
            <div>落地 <span class="id" style="font-weight:600">$30.98</span></div><div>售价 <span class="id">$52.49</span></div>
          </div>
        </div>
        <div style="border-top:1px solid #f4f4f5;padding-top:12px;display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;gap:8px"><span class="btn bs" style="flex:1">{ic("ExternalLink", 14)}沃尔玛后台处理</span><span class="btn bs">{ic("Copy", 14)}复制订单号</span></div>
          <div style="font-size:11px;color:#a1a1aa;line-height:1.6">判定由 order_audit 写回 orders.order_lines;人工处理(发货/取消)在沃尔玛后台完成 —— 这里不提供改判按钮。</div>
        </div>
      </div>
    </div>'''
    content = f'<div style="display:flex;gap:16px;flex:1;min-height:0">{left}{drawer}</div>'
    return shell(page("oq", ["订单待人工"], content, 1000), 1440, 1000)

# ════════════════════════ 8. 上架闸门漏斗 ════════════════════════
def funnel_board():
    stages = [
        ("待上架(飞书上架表过审行)", 1262, None, "#18181b"),
        ("非 ACTIVE 店", 38, "凭证失效/暂停的店整体让位", "#a1a1aa"),
        ("超配额", 310, "配额 = 成功提交口径,按店计", "#a1a1aa"),
        ("风控", 124, "品牌词/制造商双字段", "#a1a1aa"),
        ("黑名单", 86, "brand + asin + seller + 类目 四库", "#a1a1aa"),
        ("全局去重", 41, "同 ASIN 已在任一店在架", "#a1a1aa"),
        ("防呆", 7, "SKU 登记簿不认识的一律不动", "#a1a1aa"),
        ("数据不全", 214, "缺快照/缺运费 → 自动推采集,明天再来", "#a1a1aa"),
    ]
    bars = []
    for i, (name, n, note, _) in enumerate(stages):
        if i == 0:
            w = 100
            bar = f'<div style="height:22px;border-radius:4px;background:#18181b;width:{w}%"></div>'
            num = f'<span class="id" style="font-weight:600">{n:,}</span>'
        else:
            w = max(2.2, n / 1262 * 100)
            bar = f'<div style="height:22px;border-radius:4px;background:#e4e4e7;width:{w:.1f}%"></div>'
            num = f'<span class="id" style="color:#b91c1c">−{n:,}</span>'
        note_s = f'<span style="font-size:11px;color:#a1a1aa">{note}</span>' if note else ""
        bars.append(f'''<div style="display:flex;align-items:center;gap:12px;padding:5px 0">
          <span style="width:190px;flex:none;font-size:13px;color:#27272a">{name}</span>
          <div style="flex:1">{bar}</div>
          <span style="width:64px;text-align:right;flex:none">{num}</span>
          <span style="width:250px;flex:none">{note_s}</span></div>''')
    bars.append(f'''<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-top:1px solid #f4f4f5;margin-top:4px">
      <span style="width:190px;flex:none;font-size:13px;font-weight:600;color:#047857">实际提交</span>
      <div style="flex:1"><div style="height:22px;border-radius:4px;background:#10b981;width:35.0%"></div></div>
      <span style="width:64px;text-align:right;flex:none" class="id"><b>442</b></span>
      <span style="width:250px;flex:none;font-size:11px;color:#a1a1aa">领 UPC 442 · 9 店 · 每档可点开看被拦行</span></div>''')
    funnel = card('闸门漏斗 · 今晚 20:00 那一轮(run #18492)', f'<div style="padding:12px 16px">{"".join(bars)}</div>',
                  action='<span class="btn sm bs">按店拆分</span><span class="btn sm bs">对比昨天</span>')

    outcome = f'''<div style="display:flex;gap:16px">
      <div class="card" style="flex:1;padding:14px 16px;display:flex;align-items:center;gap:10px">{dot("emerald")}<span style="font-size:13px">已提交</span><span class="kpival" style="font-size:20px">438</span></div>
      <div class="card" style="flex:1;padding:14px 16px;display:flex;align-items:center;gap:10px">{dot("red")}<span style="font-size:13px">失败(附错误码)</span><span class="kpival" style="font-size:20px">3</span></div>
      <div class="card" style="flex:1;padding:14px 16px;display:flex;align-items:center;gap:10px;border-color:#fde68a"><span class="dothollow"></span><span style="font-size:13px">结局不确定 —— 不重复提交,等自愈链收尾</span><span class="kpival" style="font-size:20px;color:#b45309">1</span></div>
    </div>'''

    blocked = card("被拦明细 · 黑名单(−86)", f'''<table>
      <tr><th class="th">ASIN</th><th class="th">品牌</th><th class="th">命中</th><th class="th">入库时间 / 来源</th><th class="th">店铺</th></tr>
      <tr class="rz"><td class="td id">B0BSNKKR6T</td><td class="td">SONY</td><td class="td">{tag("red", "brand_blacklist")}</td><td class="td" style="font-size:12px;color:#71717a">2026-05-12 · 飞书人工归拢</td><td class="td id">A102</td></tr>
      <tr class="rz"><td class="td id">B0CJM1WPXR</td><td class="td">LEGO</td><td class="td">{tag("red", "brand_err_hits")}</td><td class="td" style="font-size:12px;color:#71717a">2026-08-02 · 程序自产(后台报错回收)</td><td class="td id">A085</td></tr>
      <tr class="rz"><td class="td id">B09XM4NP2Q</td><td class="td">—</td><td class="td">{tag("red", "asin_blacklist · 知产")}</td><td class="td" style="font-size:12px;color:#71717a">2026-07-19 · problem_scan 归类</td><td class="td id">A117</td></tr>
    </table>''' + '<div style="padding:10px 16px;font-size:11px;color:#a1a1aa">黑名单入库那一刻拦截即生效(上架/审核读库);飞书表格只是投影,晚两小时更新 —— 表格没更新 ≠ 还没生效。</div>')

    tabs = '''<div style="display:flex;gap:2px;border-bottom:1px solid #e4e4e7">
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;font-weight:500;color:#18181b;border-bottom:2px solid #18181b">闸门漏斗</span>
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;color:#52525b">UPC 池 <span class="navbadge amber" style="margin-left:6px">412</span></span>
      <span style="height:36px;display:flex;align-items:center;padding:0 12px;font-size:13px;color:#52525b">变体组</span>
    </div>'''
    content = tabs + funnel + outcome + blocked
    return shell(page("listing", ["上架", "闸门漏斗"], content, 900), 1440, 900)

# ════════════════════════ 输出 ════════════════════════
import json, pathlib

BOARDS = {
    "DesignSystem.dc.html": (ds_board, "设计系统", 1440, 1200),
    "Main.dc.html": (main_board, "总览(首页)", 1440, 1100),
    "RunSelect.dc.html": (wfselect_board, "工作流 · 选择与参数", 1440, 900),
    "RunPreview.dc.html": (wfpreview_board, "工作流 · 预览破坏面", 1440, 900),
    "RunLive.dc.html": (wflive_board, "工作流 · 执行与结局", 1440, 900),
    "ProductDetail.dc.html": (product_board, "产品详情", 1440, 1400),
    "OrderQueue.dc.html": (orderqueue_board, "订单待人工", 1440, 1000),
    "ListingFunnel.dc.html": (funnel_board, "上架闸门漏斗", 1440, 900),
}

POS = {
    "DesignSystem.dc.html": (0, 0),
    "Main.dc.html": (0, 1320),
    "OrderQueue.dc.html": (1560, 1320),
    "RunSelect.dc.html": (0, 2540),
    "RunPreview.dc.html": (1560, 2540),
    "RunLive.dc.html": (3120, 2540),
    "ListingFunnel.dc.html": (0, 3560),
    "ProductDetail.dc.html": (1560, 3560),
}

for fname, (fn, title, w, h) in BOARDS.items():
    pathlib.Path(fname).write_text(fn(), encoding="utf-8")
    print(f"{fname:24s} {len(pathlib.Path(fname).read_bytes())/1024:6.1f} KB")

canvas = {
    "artboards": [
        {"file": f, "title": BOARDS[f][1], "x": POS[f][0], "y": POS[f][1],
         "w": BOARDS[f][2], "h": BOARDS[f][3]}
        for f in BOARDS
    ],
    "annotations": [
        {"id": "round-1-note", "x": 0, "y": -150, "w": 560,
         "text": "第一轮六块(工作流触发拆成三联,共 8 画板)· 配色与组件词汇逐值对齐旧系统 erp-core:黑主按钮 + emerald/amber/red/sky/violet/gray 状态点,Inter + Noto Sans SC + JetBrains Mono,行高 32/40 两档。\n数据全部对上库里真有的表与状态,详见仓库 docs/frontend_brief.md。"},
    ],
    "launch": {"view": "canvas"},
}
pathlib.Path("canvas.json").write_text(json.dumps(canvas, ensure_ascii=False, indent=2), encoding="utf-8")
print("canvas.json OK")
