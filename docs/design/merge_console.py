# -*- coding: utf-8 -*-
"""把所有者提供的交互原型合入本画布,并施加 erp-core 皮肤。幂等:每次从
ref 原文(console.ref.dc.html 的源)重建。

⚠ 换肤方法论(2026-08-18 第二次返工的教训):**不做变量硬映射**。
原型把一个蓝色 accent 用在 72 处、承担了七种角色(链接/导航高亮/区块标
/强调数字/中间态签/选中态/主按钮)。第一版把 accent 直接映成 zinc-900 黑,
等于把七种角色全砸成黑灰 —— 层级和对比全毁。这版按角色拆:

  · 信息/链接/强调  → sky(erp-core 的「在途/信息」蓝,#0369A1 系)
  · 主动作/激活导航 → zinc-900 黑(**独立组件规则**,不走 accent)
  · 危险动作       → red-600 实心(替换原型的斜纹描边语言)
  · 语义三组       → red / amber / emerald 各 50 底 · 700 字 · 200 线
  · 中间态         → sky 虚线(与「忙」的灰虚线必须分得开)
"""
import pathlib

SRC = pathlib.Path("../ref-design/console.dc.html")
s = SRC.read_text(encoding="utf-8")

def sub1(old, new, n=1):
    global s
    assert s.count(old) == n, f"锚点计数 {s.count(old)} ≠ {n}: " + old[:60]
    s = s.replace(old, new)

# ══ 1. 内联设计系统资源(画布 CSP 下相对路径加载不了)══
CSS = pathlib.Path("industry-styles.css").read_text(encoding="utf-8")
for old, new in [
    ("@import url('https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;700&family=Barlow+Condensed:wght@400;600&display=swap');",
     "/* 字体统一在页面 <link> 引入:Inter + Noto Sans SC + JetBrains Mono */"),
    ("--color-bg: #f2f2f3;", "--color-bg: #fafafa;"),
    ("--color-surface: #e9e9ea;", "--color-surface: #ffffff;"),
    ("--color-text: #1d1f20;", "--color-text: #18181b;"),
    ("--color-accent: #5980a6;", "--color-accent: #0369a1;"),
    ("--color-accent-2: #728fab;", "--color-accent-2: #52525b;"),
    ("--color-divider: color-mix(in srgb, #1d1f20 16%, transparent);", "--color-divider: #e4e4e7;"),
    ('--font-heading: "Barlow Condensed", system-ui, sans-serif;', "--font-heading: 'Inter', 'Noto Sans SC', system-ui, sans-serif;"),
    ('--font-body: "Barlow", system-ui, sans-serif;', "--font-body: 'Inter', 'Noto Sans SC', system-ui, sans-serif;"),
    ("--color-neutral-100: #f5f5f8;", "--color-neutral-100: #f4f4f5;"),
    ("--color-neutral-200: #e7e7ea;", "--color-neutral-200: #e4e4e7;"),
    ("--color-neutral-300: #d4d4d7;", "--color-neutral-300: #d4d4d8;"),
    ("--color-neutral-400: #b7b7ba;", "--color-neutral-400: #a1a1aa;"),
    ("--color-neutral-500: #98989b;", "--color-neutral-500: #71717a;"),
    ("--color-neutral-600: #7a7a7d;", "--color-neutral-600: #52525b;"),
    ("--color-neutral-700: #5d5d60;", "--color-neutral-700: #3f3f46;"),
    ("--color-neutral-800: #424244;", "--color-neutral-800: #27272a;"),
    ("--color-neutral-900: #2b2b2d;", "--color-neutral-900: #18181b;"),
    ("--color-accent-100: #eef6ff;", "--color-accent-100: #f0f9ff;"),
    ("--color-accent-200: #d6ebff;", "--color-accent-200: #e0f2fe;"),
    ("--color-accent-300: #b5d9fd;", "--color-accent-300: #bae6fd;"),
    ("--color-accent-400: #94bce3;", "--color-accent-400: #38bdf8;"),
    ("--color-accent-500: #749dc4;", "--color-accent-500: #0284c7;"),
    ("--color-accent-600: #597ea3;", "--color-accent-600: #0369a1;"),
    ("--color-accent-700: #416180;", "--color-accent-700: #075985;"),
    ("--color-accent-800: #2c455d;", "--color-accent-800: #0c4a6e;"),
    ("--color-accent-900: #1d2d3d;", "--color-accent-900: #082f49;"),
    ("--color-accent-2-100: #eef6ff;", "--color-accent-2-100: #f4f4f5;"),
    ("--color-accent-2-200: #d6ebff;", "--color-accent-2-200: #e4e4e7;"),
    ("--color-accent-2-300: #bdd8f2;", "--color-accent-2-300: #d4d4d8;"),
    ("--color-accent-2-400: #9ebbd8;", "--color-accent-2-400: #a1a1aa;"),
    ("--color-accent-2-500: #7e9cb8;", "--color-accent-2-500: #71717a;"),
    ("--color-accent-2-600: #627d98;", "--color-accent-2-600: #52525b;"),
    ("--color-accent-2-700: #486077;", "--color-accent-2-700: #3f3f46;"),
    ("--color-accent-2-800: #314457;", "--color-accent-2-800: #27272a;"),
    ("--color-accent-2-900: #1f2d3a;", "--color-accent-2-900: #18181b;"),
]:
    assert old in CSS, "CSS 锚点缺失: " + old[:50]
    CSS = CSS.replace(old, new)
sub1('<link rel="stylesheet" href="_ds/industry-71bd4807-93ea-4481-ab4b-859c6b1d7a71/styles.css">',
     "<style>\n" + CSS + "\n</style>")
sub1('<script src="_ds/industry-71bd4807-93ea-4481-ab4b-859c6b1d7a71/_ds_bundle.js"></script>',
     '<script>window.Industry_indust = window.Industry_indust || {__errors: []};</script>')

# ══ 2. 字体 ══
sub1('<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600;700&family=Barlow+Semi+Condensed:wght@400;500&display=swap" rel="stylesheet">',
     '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">')

# ══ 3. 主题变量:语义三组 + sky 信息蓝 ══
sub1("""[data-theme]{
  --sev-bad:#a32b1c; --sev-bad-ink:#7d2015; --sev-bad-bg:#fbeae7; --sev-bad-line:#d99d92;
  --sev-warn-ink:#7a5205; --sev-warn-bg:#fdf2de; --sev-warn-line:#ddb972;
  --sev-ok-ink:#1b5e40; --sev-ok-bg:#e4f1ea; --sev-ok-line:#9dc5b1;
}""",
"""[data-theme]{
  --sev-bad:#dc2626; --sev-bad-ink:#b91c1c; --sev-bad-bg:#fef2f2; --sev-bad-line:#fecaca;
  --sev-warn-ink:#b45309; --sev-warn-bg:#fffbeb; --sev-warn-line:#fde68a;
  --sev-ok-ink:#047857; --sev-ok-bg:#ecfdf5; --sev-ok-line:#a7f3d0; --sev-ok-dot:#10b981;
  --sev-mid-ink:#0369a1; --sev-mid-line:#7dd3fc;
  --font-heading:'Inter','Noto Sans SC',system-ui,sans-serif;
  --font-body:'Inter','Noto Sans SC',system-ui,sans-serif;
}""")
sub1("""[data-theme="light"]{
  --color-bg:#ffffff; --color-text:#0e1114;
  --color-divider:#c6ccd2;
  --color-accent:#2b5c8c;
  --color-accent-100:#e6edf5; --color-accent-200:#c9dbeb; --color-accent-300:#9bbcd8;
  --color-accent-400:#6a93b8; --color-accent-500:#2b5c8c; --color-accent-600:#234d76;
  --color-accent-700:#1d4062; --color-accent-800:#16324d; --color-accent-900:#0c2035;
  --color-neutral-400:#aeb5bc;
}""",
"""[data-theme="light"]{
  --color-bg:#fafafa; --color-surface:#ffffff; --color-text:#18181b;
  --color-divider:#e4e4e7;
  --color-accent:#0369a1;
  --color-accent-100:#f0f9ff; --color-accent-200:#e0f2fe; --color-accent-300:#bae6fd;
  --color-accent-400:#38bdf8; --color-accent-500:#0284c7; --color-accent-600:#0369a1;
  --color-accent-700:#075985; --color-accent-800:#0c4a6e; --color-accent-900:#082f49;
  --color-neutral-400:#a1a1aa;
}""")
sub1('[data-theme="light"] > aside{background:#eceff2}',
     '[data-theme="light"] > aside{background:#ffffff}')
sub1("""[data-theme="dark"]{
  --color-bg:#17191b; --color-surface:#1f2225; --color-text:#e8e9ea;
  --color-divider:color-mix(in srgb,#e8e9ea 30%,transparent);
  --sev-bad:#c2452f; --sev-bad-ink:#f2b6ab; --sev-bad-bg:#3a1a14; --sev-bad-line:#7d3325;
  --sev-warn-ink:#e8c07a; --sev-warn-bg:#332715; --sev-warn-line:#6b5426;
  --sev-ok-ink:#8fd0ae; --sev-ok-bg:#16301f; --sev-ok-line:#2f6144;
  --color-accent:#7ea5c9;
  --color-accent-100:#22303d; --color-accent-200:#2b425a; --color-accent-300:#3a5a78; --color-accent-400:#5c81a8; --color-accent-700:#a9c6e0;
  --color-accent-800:#cfe1f2; --color-accent-900:#e8f1fa;
  --color-neutral-100:#23262a; --color-neutral-800:#d3d5d7; --color-neutral-900:#eceded;
  --shadow-lg:0 12px 32px rgba(0,0,0,.55);
}""",
"""[data-theme="dark"]{
  --color-bg:#18181b; --color-surface:#232327; --color-text:#f4f4f5;
  --color-divider:color-mix(in srgb,#f4f4f5 20%,transparent);
  --sev-bad:#dc2626; --sev-bad-ink:#fca5a5; --sev-bad-bg:#3f1d1d; --sev-bad-line:#7f1d1d;
  --sev-warn-ink:#fcd34d; --sev-warn-bg:#3a2e12; --sev-warn-line:#78591b;
  --sev-ok-ink:#6ee7b7; --sev-ok-bg:#132e21; --sev-ok-line:#065f46; --sev-ok-dot:#34d399;
  --sev-mid-ink:#7dd3fc; --sev-mid-line:#0369a1;
  --color-accent:#7dd3fc;
  --color-accent-100:#12283a; --color-accent-200:#143a54; --color-accent-300:#1e5372; --color-accent-400:#2f6f96; --color-accent-700:#a5ddfc;
  --color-accent-800:#cfeefe; --color-accent-900:#e8f7ff;
  --color-neutral-100:#27272a; --color-neutral-400:#71717a; --color-neutral-800:#d4d4d8; --color-neutral-900:#f4f4f5;
  --shadow-lg:0 12px 32px rgba(0,0,0,.55);
}""")
sub1('[data-theme="dark"] .tag-accent{background:#22303d;color:#cfe1f2}',
     '[data-theme="dark"] .tag-accent{background:#12283a;color:#cfeefe}')
sub1('[data-theme="dark"] .tag-neutral{background:#23262a;color:#d3d5d7}',
     '[data-theme="dark"] .tag-neutral{background:#27272a;color:#d4d4d8}')

# ══ 4. 等宽与 erp-core 组件皮肤层(按组件重写;追加在原型样式之后,级联取胜)══
sub1('.mono{font-family:"Barlow Semi Condensed",ui-monospace,monospace;font-variant-numeric:tabular-nums}',
     ".mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums;font-size:.94em;letter-spacing:-0.01em}")
sub1(".hz{background-image:repeating-linear-gradient(135deg,transparent 0 5px,color-mix(in srgb,var(--color-text) 22%,transparent) 5px 6px)}",
     """.hz{background-image:repeating-linear-gradient(135deg,transparent 0 5px,color-mix(in srgb,var(--color-text) 22%,transparent) 5px 6px)}

/* ═══ erp-core 组件皮肤(逐组件重写,非变量映射)═══ */
body{font-size:14px}
.blueprint > .corner{display:none}
.blueprint{border-radius:8px}
.card{background:var(--color-surface);border:1px solid var(--color-divider);border-radius:8px}
.card.blueprint{background:var(--color-surface)}
.dialog{background:var(--color-surface);border:1px solid var(--color-divider);border-radius:8px}
.btn{border-radius:6px}
.input{border-radius:6px;background:var(--color-surface);border-color:#d4d4d8}
.seg{border-radius:6px;border-color:#d4d4d8}
.seg-opt:has(input:checked){background:#18181b;color:#fff}
.tag{border-radius:4px}
.btn-primary{background:#18181b;border-color:#18181b;color:#fff}
.btn-primary:hover{background:#27272a}
.btn-primary:active{background:#3f3f46}
.btn-secondary{border-color:#d4d4d8;background:var(--color-surface);color:var(--color-text)}
.btn-secondary:hover{background:#f4f4f5}
.btn-secondary:active{background:#e4e4e7}
.btn-ghost{color:#3f3f46}
.btn-ghost:hover{background:#f4f4f5}
.btn.hz{background:#dc2626;border-color:#dc2626;color:#fff;background-image:none}
.btn.hz:hover{background:#b91c1c}
.table thead th{background:#fafafa;color:#71717a;border-bottom:1px solid #e4e4e7}
.table td{border-bottom:1px solid #f4f4f5}
.table tbody tr:hover{background:#fafafa}
:focus-visible{outline-color:#18181b}
/* —— 深色等价 —— */
[data-theme="dark"] > aside{background:#1c1c20}
[data-theme="dark"] .input{background:#27272a;border-color:#3f3f46}
[data-theme="dark"] .seg{border-color:#3f3f46}
[data-theme="dark"] .seg-opt:has(input:checked){background:#f4f4f5;color:#18181b}
[data-theme="dark"] .btn-primary{background:#f4f4f5;border-color:#f4f4f5;color:#18181b}
[data-theme="dark"] .btn-primary:hover{background:#e4e4e7}
[data-theme="dark"] .btn-primary:active{background:#d4d4d8}
[data-theme="dark"] .btn-secondary{border-color:#3f3f46}
[data-theme="dark"] .btn-secondary:hover{background:#3f3f46}
[data-theme="dark"] .btn-secondary:active{background:#52525b}
[data-theme="dark"] .btn-ghost{color:#d4d4d8}
[data-theme="dark"] .btn-ghost:hover{background:#27272a}
[data-theme="dark"] .table thead th{background:#1f1f23;color:#a1a1aa;border-bottom-color:#3f3f46}
[data-theme="dark"] .table td{border-bottom-color:#2b2b30}
[data-theme="dark"] .table tbody tr:hover{background:#1f1f23}
[data-theme="dark"] :focus-visible{outline-color:#f4f4f5}""")

# ══ 5. 语义常量与点阵 ══
sub1("const MID  = {chipBg:'transparent',chipFg:'var(--color-accent-700)',chipBorder:'var(--color-accent-400)',chipBorderStyle:'dashed'};",
     "const MID  = {chipBg:'transparent',chipFg:'var(--sev-mid-ink)',chipBorder:'var(--sev-mid-line)',chipBorderStyle:'dashed'};")
sub1("out.push({title: '店铺 ' + (i + 1) + ' · 凭证有效', bg: 'var(--color-accent-700)', borderStyle: 'solid'});",
     "out.push({title: '店铺 ' + (i + 1) + ' · 凭证有效', bg: 'var(--sev-ok-dot)', borderStyle: 'solid'});")

# ══ 6. 激活导航 = 黑底白字(erp-core navActive;深色反白)══
sub1("""  navGroups() {
    return NAV.map(([title, items]) => ({
      title,
      items: items.map(([id, label, badge, tone]) => ({
        label, badge, href: '#' + id, go: this.go(id),
        rule: this.state.page === id ? 'var(--color-accent)' : 'transparent',
        bg: this.state.page === id ? 'color-mix(in srgb,var(--color-accent) 12%,transparent)' : 'transparent',
        badgeColor: tone === 'accent' ? 'var(--color-accent)' : 'color-mix(in srgb,var(--color-text) 42%,transparent)',
      })),
    }));
  }""",
"""  navGroups() {
    const dark = this.theme() === 'dark';
    return NAV.map(([title, items]) => ({
      title,
      items: items.map(([id, label, badge, tone]) => {
        const on = this.state.page === id;
        return {
          label, badge, href: '#' + id, go: this.go(id),
          rule: 'transparent',
          bg: on ? (dark ? '#f4f4f5' : '#18181b') : 'transparent',
          fg: on ? (dark ? '#18181b' : '#ffffff') : 'inherit',
          badgeColor: on ? (dark ? 'rgba(24,24,27,.6)' : 'rgba(255,255,255,.72)')
                         : tone === 'accent' ? 'var(--color-accent)'
                         : 'color-mix(in srgb,var(--color-text) 42%,transparent)',
        };
      }),
    }));
  }""")
sub1('style="display:flex;align-items:center;gap:8px;padding:6px 8px;text-decoration:none;font-size:13.5px;color:inherit;border-left:2px solid {{ it.rule }};background:{{ it.bg }}"',
     'style="display:flex;align-items:center;gap:8px;padding:6px 8px;text-decoration:none;font-size:13.5px;color:{{ it.fg }};border-radius:6px;border-left:2px solid {{ it.rule }};background:{{ it.bg }}"')

# ══ 7. 危险语言换装:斜纹描边 → 红实心(与皮肤层的 .btn.hz 一致)+ 危数改 20 ══
sub1("{chip:'危险 · 预览→确认', ...FAIL, hatch:'hz', desc:'21 条会真写沃尔玛且不可逆：上架、删除、改价。斜纹描边，两步走。'},",
     "{chip:'危险 · 预览→确认', ...FAIL, hatch:'', desc:'20 条会真写沃尔玛且不可逆：上架、删除、改价。红实心按钮，预览 → 确认两步走。'},")
sub1('危险工作流的按钮是<span class="hz" style="padding:1px 6px;border:1px solid var(--color-text)">斜纹描边</span>，只读的是普通线框 —— 一眼看出这个按钮会不会写沃尔玛。',
     '危险工作流的按钮是<span style="padding:1px 8px;background:#dc2626;color:#fff;border-radius:4px">红实心</span>，只读的是普通线框 —— 一眼看出这个按钮会不会写沃尔玛。')
sub1("<!-- ═══ 预览 → 确认 → 执行：右侧抽屉（21 条危险工作流共用） ═══ -->",
     "<!-- ═══ 预览 → 确认 → 执行：右侧抽屉（20 条危险工作流共用） ═══ -->")
sub1("note:'21 条危险工作流共用。", "note:'20 条危险工作流共用。")
sub1('token 对齐本设计系统（方角、hairline、四角定位十字用一个 Frame 组件包）',
     'token 对齐 erp-core（zinc 中性系、黑主按钮、sky 信息蓝、圆角 8 / 6 / 4、hairline 分隔）')

# ══ 8. 两处事实修正 ══
sub1("没抢到锁：上一轮 17:30 那次还在跑（27 个在途轮询慢）。不是失败，不要重试",
     "没抢到锁：上一轮 17:30 那次还在跑。不是失败，不要重试。⚠ 这一轮不写 ops.runs（拿不到锁在记录之前就退出）——此行是时间轴合成显示")
old_wh = "日报推送失败：飞书群 webhook 403"
assert s.count(old_wh) == 2
s = s.replace(old_wh, "日报发送失败：飞书应用凭证过期（401）——通知走应用直发，没有 webhook")


# ══ 8.5 三处数据修正(原型自带)══
# ① 订单表审核列越界:audit 值在 r[10],r[11] 是 undefined → 整列落到兜底「建议拒绝」
sub1("      const c = chip(r[11]);", "      const c = chip(r[10]);")
# ② 危险工作流实数 20(grep -c '^DANGEROUS = True' workflows/*.py)
sub1("""              <div style="font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:color-mix(in srgb,var(--color-text) 55%,transparent)">危险（会写沃尔玛 / 不可逆）</div>
              <div class="num" style="font-family:var(--font-heading);font-weight:600;font-size:28px">21</div>""",
"""              <div style="font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:color-mix(in srgb,var(--color-text) 55%,transparent)">危险（会写沃尔玛 / 不可逆）</div>
              <div class="num" style="font-family:var(--font-heading);font-weight:600;font-size:28px">20</div>""")
# ③ 「在调度里 11」数的是任务不是工作流:11 条任务共跑 21 条工作流,手动 45 条
sub1('<div style="font-size:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">其余 55 条人手动触发</div>',
     '<div style="font-size:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">11 条任务共跑 21 条工作流;其余 45 条人手动触发</div>')

# ══ 9. 注册并补齐两个原型缺的视图(审核中心 / 类目映射)══
sub1("  products:  ['产品中心', '产品列表 · 亚马逊侧 / 沃尔玛侧对照'],",
     "  products:  ['产品中心', '产品列表 · 亚马逊侧 / 沃尔玛侧对照'],\n"
     "  audit:     ['产品中心', '审核中心 · L0-L4 分层 · pending 两来源 · 重审三通道'],\n"
     "  catmap:    ['产品中心', '类目映射 · node_id 锚 · 四桶缺口 · 置信度生命周期'],")
sub1("  ['产品', [['products','产品列表','128k',''],['listing','上架漏斗 · UPC 池','40',''],['blacklist','黑名单中心','4','']]],",
     "  ['产品', [['products','产品列表','128k',''],['audit','审核中心','1.2k',''],['listing','上架漏斗 · UPC 池','40',''],['blacklist','黑名单中心','4',''],['catmap','类目映射','37','']]],")
sub1("      isAlloc: page === 'alloc', isNotes: page === 'notes',",
     "      isAlloc: page === 'alloc', isNotes: page === 'notes',\n"
     "      isAudit: page === 'audit', isCatmap: page === 'catmap',")
sub1("      stores: this.stores(),",
     "      stores: this.stores(),\n"
     "      auditTiles: this.auditTiles(),\n"
     "      auditLayers: this.auditLayers(),\n"
     "      auditRedo: this.auditRedo(),\n"
     "      catmapTiles: this.catmapTiles(),\n"
     "      catmapBuckets: this.catmapBuckets(),\n"
     "      catmapLife: this.catmapLife(),")

GETTERS = r'''
  auditTiles() {
    return [
      {...NEU, key:'表 E 空 · 领取', n:'9,402', note:'上架表驱动。审核权威在库,E 列只是投影 —— 手改 E 不生效'},
      {...OK,  key:'零 LLM 投影', n:'8,140', note:'库里已有结论,直接写回表(E 列写小写 pass / reject)'},
      {...MID, key:'真待审', n:'1,262', note:'未审 + pending。limit 撞满时必须给「总量 / 本轮 / 还剩」三个数'},
      {...UNK, key:'pending · L1 类目', n:'1,144', note:'类目解不出。映射补上自然消化;隔日退避重试'},
      {...UNK, key:'pending · L3 LLM', n:'63', note:'LLM 故障。只有 http_429 才算撞限流,5xx 是对端故障'},
    ];
  }
  auditLayers() {
    const L = [
      ['L0 · Phase0 四硬规则','reject','214', FAIL, 'R0 中国卖家类目硬禁 / R1 准入双白名单 / R2 十八条禁售大类 / R3a 硬认证。串行短路 —— 单条 hit 不代表只违反一条'],
      ['L1 · 类目解析','pending','37', UNK, '解不出 → pending。中间态不是结论;同轮补采闭环先救一批(审不了 118 → 推 audit_gap 批次 → 救回 81 → 仍缺 37 写 F 列)'],
      ['L2 · 硬规则复核','reject','96', FAIL, '结论只由 penalty −100 的硬规则决定;R3b / R3c / R4 黑名单 / R5 商标 penalty=0 是纯证据,不许画成拒因'],
      ['L3 · 语义(LLM)','pending','6', UNK, 'LLM 故障 → pending,隔日退避'],
      ['L4 · 视觉','—','0', NEU, '故障回落 pass 按 rule_code 计数 —— 全故障 = 层未生效,必须亮出来'],
      ['最终','pass','909', OK, '写回 products 五列 + 投影上架表 E 列'],
    ];
    return L.map(([layer, verdict, n, c, note]) => ({layer, verdict, n, note,
      chipBg: c.chipBg, chipFg: c.chipFg, chipBorder: c.chipBorder, chipBorderStyle: c.chipBorderStyle}));
  }
  auditRedo() {
    return [
      {cmd:'-p asins=…', title:'点名强审', note:'无视现有结论。rejected 永不自动重审,这是唯一救活通道。', danger:false},
      {cmd:'-p rerule=R2', title:'定点翻案', note:'改规则后只翻被该规则拒过的;显示 总量 / 本轮 / 还剩。', danger:false},
      {cmd:'-p force_rerun=v3', title:'全量重审', note:'最重 —— LLM 费用警示,必走预览。', danger:true},
    ];
  }
  catmapTiles() {
    return [
      {...OK,  key:'映射覆盖率(产品侧)', n:'64.4%', note:'15,538 node 已映射 10,011;每轮 product_audit 落 PT,下一轮 mine 多挖一批 —— 自增强回路'},
      {...NEU, key:'映射表', n:'13,349', note:'audit.walmart_category_map;键 = browse_node_id,名字会漂 ID 不会'},
      {...MID, key:'三方 JOIN 命中', n:'82.2 / 99.9%', note:'产品侧 / 映射表侧 —— taxonomy_import 预览强制先看这两个数再导入'},
      {...FAIL,key:'map_ambiguous', n:'12', note:'同 node 挂多条高置信不同 PT:②级直出对该 node 失明且无报错 —— 必须高亮;裁剪走 catmap_fix'},
    ];
  }
  catmapBuckets() {
    const B = [
      ['A 桶 · 有实证没映射','1,740 node · 16.6 万件', OK, 'catmap_mine 常态重跑','瓶颈是实证太稀;mine 只数 pt_source=walmart_confirmed 的票(防 LLM 自我印证),票数偏少正常'],
      ['B 桶 · 零实证','3,787 node · 4.5 万件', NEU, '⛔ 不排期(所有者 08-17 裁决)','真实收益仅约 1.37 万件 —— 84% 停在 L0 压根不查类目;解不出走 L1 的 LLM 兜底,慢些贵些但判得了'],
      ['C 桶 · 树里没有','2,759 node', UNK, '采集侧补抓','产品带着 node 但类目树没有 → catmap_gap -p only=not_in_tree 出清单'],
      ['D 桶 · 没货','12,386 node', NEU, '不处理','亚马逊有我们没货 —— 别浪费 LLM'],
    ];
    return B.map(([name, n, c, verdict, note]) => ({name, n, verdict, note,
      chipBg: c.chipBg, chipFg: c.chipFg, chipBorder: c.chipBorder, chipBorderStyle: c.chipBorderStyle}));
  }
  catmapLife() {
    const L = [
      ['mined_trusted','≥5 票且优势 ≥70~80%','高 · 审核 ②级闸直出,不经 LLM', OK],
      ['mined_review','2~4 票、优势达标','中 · 只进候选,LLM 拿它当参考', MID],
      ['map_conflict','实证与旧映射相左(旧行不动)','中', MID],
      ['mined_mixed','票分流,首选是多数派非共识','低 · 弱证据永远绕不过 LLM', UNK],
      ['map_ambiguous','映射表自己挂多条高置信 PT','只报不写;裁剪走 catmap_fix', FAIL],
    ];
    return L.map(([bucket, judge, grade, c]) => ({bucket, judge, grade,
      chipBg: c.chipBg, chipFg: c.chipFg, chipBorder: c.chipBorder, chipBorderStyle: c.chipBorderStyle}));
  }
'''
sub1("  renderVals() {", GETTERS + "\n  renderVals() {")

VIEWS = r'''      <!-- ═══ 审核中心 ═══ -->
      <sc-if value="{{ isAudit }}">
        <div style="display:flex;flex-direction:column;gap:16px">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1px;background:var(--color-bg);border:1px solid var(--color-divider)">
            <sc-for list="{{ auditTiles }}" as="d" hint-placeholder-count="5">
              <div style="background:var(--color-bg);padding:11px 13px 13px;display:flex;flex-direction:column;gap:5px">
                <span class="tag" style="align-self:flex-start;border:1px {{ d.chipBorderStyle }} {{ d.chipBorder }};background:{{ d.chipBg }};color:{{ d.chipFg }};font-size:10px">{{ d.key }}</span>
                <div class="num" style="font-family:var(--font-heading);font-weight:600;font-size:26px;line-height:1">{{ d.n }}</div>
                <div style="font-size:11px;line-height:1.4;color:color-mix(in srgb,var(--color-text) 58%,transparent);white-space:normal">{{ d.note }}</div>
              </div>
            </sc-for>
          </div>

          <div class="card blueprint" style="padding:0;gap:0">
            <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
            <div style="overflow:auto">
              <table class="table">
                <thead><tr><th>分层</th><th>判定</th><th style="text-align:right">本轮</th><th>说明</th></tr></thead>
                <tbody>
                  <sc-for list="{{ auditLayers }}" as="l" hint-placeholder-count="6">
                    <tr>
                      <td style="font-size:13px;white-space:nowrap">{{ l.layer }}</td>
                      <td><span class="tag" style="border:1px {{ l.chipBorderStyle }} {{ l.chipBorder }};background:{{ l.chipBg }};color:{{ l.chipFg }};font-size:10px">{{ l.verdict }}</span></td>
                      <td class="num mono" style="text-align:right;font-size:13px">{{ l.n }}</td>
                      <td style="font-size:12.5px;white-space:normal;color:color-mix(in srgb,var(--color-text) 78%,transparent)">{{ l.note }}</td>
                    </tr>
                  </sc-for>
                </tbody>
              </table>
            </div>
            <div style="padding:10px 14px;border-top:1px solid var(--color-divider);font-size:11.5px;line-height:1.5;color:color-mix(in srgb,var(--color-text) 68%,transparent);white-space:normal">
              统计一律排除 <span class="mono">stage_stopped_at='SHORTCUT'</span> 影子行(旧系统 reject 粘性产物,204 万存量里有);缺数据行只写 F 列原因、E 列必须留空 —— E 一有值该行就退出审核通道;<span class="mono">audited_at</span> 是审核动作时刻,不能拿它算 pending 龄期。
            </div>
          </div>

          <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
            <div class="card blueprint" style="gap:10px">
              <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
              <div class="card-kicker">重审三通道(危险,各自确认;「清空表 E 列」不是重审入口)</div>
              <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">
                <sc-for list="{{ auditRedo }}" as="r" hint-placeholder-count="3">
                  <div style="border:1px solid var(--color-divider);border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:5px">
                    <span class="mono" style="font-size:12px;font-weight:600;color:var(--color-accent-700)">{{ r.cmd }}</span>
                    <span style="font-size:12.5px;font-weight:500">{{ r.title }}</span>
                    <span style="font-size:11px;line-height:1.5;color:color-mix(in srgb,var(--color-text) 62%,transparent);white-space:normal">{{ r.note }}</span>
                  </div>
                </sc-for>
              </div>
            </div>
            <div class="card blueprint" style="gap:8px">
              <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
              <div class="card-kicker">审核 × 上架冲突(两列分开,不合并)</div>
              <div style="display:flex;align-items:baseline;gap:8px"><span class="num" style="font-family:var(--font-heading);font-weight:600;font-size:24px">4</span><span style="font-size:12px">rejected_still_listed —— 已拒仍在架,该下架</span></div>
              <div style="display:flex;align-items:baseline;gap:8px"><span class="num" style="font-family:var(--font-heading);font-weight:600;font-size:24px">2</span><span style="font-size:12px">rejected_after_listing —— 上架后才被拒,闸漏拦</span></div>
              <div style="font-size:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent);white-space:normal">工具区:audit_why 单品「为什么被拒」/ audit_calibrate 四桶报告 / audit_import 体检(全手动)</div>
            </div>
          </div>
        </div>
      </sc-if>

      <!-- ═══ 类目映射 ═══ -->
      <sc-if value="{{ isCatmap }}">
        <div style="display:flex;flex-direction:column;gap:16px">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1px;background:var(--color-bg);border:1px solid var(--color-divider)">
            <sc-for list="{{ catmapTiles }}" as="d" hint-placeholder-count="4">
              <div style="background:var(--color-bg);padding:11px 13px 13px;display:flex;flex-direction:column;gap:5px">
                <span class="tag" style="align-self:flex-start;border:1px {{ d.chipBorderStyle }} {{ d.chipBorder }};background:{{ d.chipBg }};color:{{ d.chipFg }};font-size:10px">{{ d.key }}</span>
                <div class="num" style="font-family:var(--font-heading);font-weight:600;font-size:26px;line-height:1">{{ d.n }}</div>
                <div style="font-size:11px;line-height:1.4;color:color-mix(in srgb,var(--color-text) 58%,transparent);white-space:normal">{{ d.note }}</div>
              </div>
            </sc-for>
          </div>

          <div class="card blueprint" style="padding:0;gap:0">
            <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
            <div style="overflow:auto">
              <table class="table">
                <thead><tr><th>缺口桶(按 node 分)</th><th style="text-align:right">量</th><th>处置</th><th>为什么</th></tr></thead>
                <tbody>
                  <sc-for list="{{ catmapBuckets }}" as="b" hint-placeholder-count="4">
                    <tr>
                      <td style="font-size:13px;white-space:nowrap">{{ b.name }}</td>
                      <td class="num mono" style="text-align:right;font-size:12.5px;white-space:nowrap">{{ b.n }}</td>
                      <td><span class="tag" style="border:1px {{ b.chipBorderStyle }} {{ b.chipBorder }};background:{{ b.chipBg }};color:{{ b.chipFg }};font-size:10px">{{ b.verdict }}</span></td>
                      <td style="font-size:12.5px;white-space:normal;color:color-mix(in srgb,var(--color-text) 78%,transparent)">{{ b.note }}</td>
                    </tr>
                  </sc-for>
                </tbody>
              </table>
            </div>
            <div style="padding:10px 14px;border-top:1px solid var(--color-divider);font-size:11.5px;line-height:1.5;color:color-mix(in srgb,var(--color-text) 68%,transparent);white-space:normal">
              树是 <span class="mono">DAG</span> 不是树:同一 node 多父多路径,走父链一律查 <span class="mono">amazon_node_paths</span>,「代表路径」只是展示。PG 是权威,飞书「映射明细」只是镜子 —— 在飞书改格子不影响任何判定;<span class="mono">catmap_export</span> 整表重写缩量超 2% 自动⛔停手(08-17 曾误删 1,847 行的教训)。哨兵行「无对应Walmart PT」是信息不是脏数据。
            </div>
          </div>

          <div class="card blueprint" style="padding:0;gap:0">
            <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
            <div style="overflow:auto">
              <table class="table">
                <thead><tr><th>mine 产出桶</th><th>判据</th><th>落档(升档自动 · 降档只走 catmap_fix 人工)</th></tr></thead>
                <tbody>
                  <sc-for list="{{ catmapLife }}" as="l" hint-placeholder-count="5">
                    <tr>
                      <td class="mono" style="font-size:12.5px">{{ l.bucket }}</td>
                      <td style="font-size:12.5px;white-space:normal;color:color-mix(in srgb,var(--color-text) 72%,transparent)">{{ l.judge }}</td>
                      <td><span class="tag" style="border:1px {{ l.chipBorderStyle }} {{ l.chipBorder }};background:{{ l.chipBg }};color:{{ l.chipFg }};font-size:10px">{{ l.grade }}</span></td>
                    </tr>
                  </sc-for>
                </tbody>
              </table>
            </div>
            <div style="padding:10px 14px;border-top:1px solid var(--color-divider);font-size:11.5px;line-height:1.5;color:color-mix(in srgb,var(--color-text) 68%,transparent);white-space:normal">
              九条工作流全手动:<span class="mono">mine</span>(常态重跑)· <span class="mono">promote</span> · <span class="mono">fix</span>(危)· <span class="mono">prune</span>(危)· <span class="mono">gap</span> · <span class="mono">suggest</span>(⛔ 不排期)· <span class="mono">align</span>(无 ID 老行兜底)· <span class="mono">export / import</span>(飞书镜);树侧 <span class="mono">taxonomy_import</span>(预览强制先看三方命中率)与 <span class="mono">taxonomy_derive</span>(零采集补树外 node)。
            </div>
          </div>
        </div>
      </sc-if>

'''
sub1('      <sc-if value="{{ isNotes }}">', VIEWS + '      <sc-if value="{{ isNotes }}">')

pathlib.Path("Console.dc.html").write_text(s, encoding="utf-8")
print(f"Console.dc.html 写出 {len(s)//1024} KB")
