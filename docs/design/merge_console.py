# -*- coding: utf-8 -*-
"""把所有者提供的交互原型(设计项目 465557a5 的 沃尔玛运营控制台.dc.html)
合入本画布:内联 _ds 资源(画布 CSP 下相对路径加载不了)、修两处事实、
补「审核中心」与「类目映射」两个原型里缺的视图。幂等:每次从 ref 原文重建。"""
import pathlib

SRC = pathlib.Path("../ref-design/console.dc.html")
CSS = pathlib.Path("industry-styles.css").read_text(encoding="utf-8")
s = SRC.read_text(encoding="utf-8")

def sub1(old, new):
    global s
    assert s.count(old) == 1, "锚点不唯一或缺失: " + old[:60]
    s = s.replace(old, new)

# ── 1. 内联设计系统资源 ──
sub1('<link rel="stylesheet" href="_ds/industry-71bd4807-93ea-4481-ab4b-859c6b1d7a71/styles.css">',
     "<style>\n" + CSS + "\n</style>")
sub1('<script src="_ds/industry-71bd4807-93ea-4481-ab4b-859c6b1d7a71/_ds_bundle.js"></script>',
     '<script>window.Industry_indust = window.Industry_indust || {__errors: []};</script>')

# ── 2. 两处事实修正 ──
sub1("没抢到锁：上一轮 17:30 那次还在跑（27 个在途轮询慢）。不是失败，不要重试",
     "没抢到锁：上一轮 17:30 那次还在跑。不是失败，不要重试。⚠ 这一轮不写 ops.runs（拿不到锁在记录之前就退出）——此行是时间轴合成显示")
# 该字符串出现两次(时间轴 + 运行记录),两处一起改
old_wh = "日报推送失败：飞书群 webhook 403"
assert s.count(old_wh) == 2
s = s.replace(old_wh, "日报发送失败：飞书应用凭证过期（401）——通知走应用直发，没有 webhook")

# ── 3. PAGES / NAV 注册两个新视图 ──
sub1("  products:  ['产品中心', '产品列表 · 亚马逊侧 / 沃尔玛侧对照'],",
     "  products:  ['产品中心', '产品列表 · 亚马逊侧 / 沃尔玛侧对照'],\n"
     "  audit:     ['产品中心', '审核中心 · L0-L4 分层 · pending 两来源 · 重审三通道'],\n"
     "  catmap:    ['产品中心', '类目映射 · node_id 锚 · 四桶缺口 · 置信度生命周期'],")
sub1("  ['产品', [['products','产品列表','128k',''],['listing','上架漏斗 · UPC 池','40',''],['blacklist','黑名单中心','4','']]],",
     "  ['产品', [['products','产品列表','128k',''],['audit','审核中心','1.2k',''],['listing','上架漏斗 · UPC 池','40',''],['blacklist','黑名单中心','4',''],['catmap','类目映射','37','']]],")

# ── 4. renderVals 视图开关与数据接线 ──
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

# ── 5. 两个新视图的数据(与其余视图同一批语义色常量) ──
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

# ── 6. 两个新视图的标记(沿用 cellgrid / card blueprint / 脚注条语言) ──
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
                  <div style="border:1px solid var(--color-divider);padding:10px 12px;display:flex;flex-direction:column;gap:5px">
                    <span class="mono" style="font-size:12px;font-weight:600;color:var(--color-accent-800)">{{ r.cmd }}</span>
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
