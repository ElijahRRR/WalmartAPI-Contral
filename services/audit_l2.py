"""L2 规则引擎:**只有 R1 类目准入白名单**一条规则,纯函数,自己不碰数据库。

移植自旧仓 `pipelines/l2_rules.py`。外部数据(pt_meta)由调用方经 `ctx` 注入
(见 services/audit_rules.AuditContext);本模块不连库、不读环境变量、不读文件。

执行模型:起始 100 分 → 先叠加 l1.hits 的 penalty(**只加分**,不把 L1 hit 复制进
L2 hits)→ 跑 R1 → 下界保护 -1000。判定在 L2Result.verdict:
score_final < 60 → reject;分数够但 R1 报了"判不了" → pending;否则 pass。

R1 共用一道闸:上游已判死(任一 l1.hit penalty<0,如出版物硬禁)→ 直接放行,
既不重复扣分也不会把确定的拒降级成 pending。

**2026-09-03 C 批(所有者定稿 `docs/audit_pipeline.md` §10):L2 = R1。**
换喂之后(L3 面前是 44 篇沃尔玛官方英文政策全文 + 本 PT 的准入要求 + 上游
确定性证据),"硬代码代 LLM 判语义"的规则全部失去存在理由,同批删净:

  · **R3 类目需证书**(硬/软两支)—— 硬闸只留白名单;本 PT 的 `requirements`
    一行随产品进 L3(B1 已落地),由 LLM 判"这个**具体产品**要不要这张证"
    (2026-08-21 实木咖啡桌被判"整机电器必须 NRTL"那条教训的彻底版);
  · **R4 品牌黑名单扫文案** → 迁 L0 当软证据(`phase0_brand_mention`);
  · **R5 USPTO 在效商标** —— 默认关、覆盖率 2.6 万/1400 万,连同 `_R5_SQL` /
    `load_nice_mapping` / `refdata/audit/pt_nice_class.yaml` 整条删;
  · **R7 促销宣称 / R8 敏感合规** —— 它们的判据本就属沃尔玛 Content Standards
    与 Offensive Content,官方全文 2026-09-02 已整段进 L3 前缀(先补后删,
    无真空期),代码里那两份手工词表是第二份判据;
  · **R10 Made in USA** → 迁 L0 硬拒(`phase0_made_in_usa`);
  · **`_infer_walmart_policy` 四张字面量表**(类目/PT 名/cert 关键词推政策名)
    —— 政策名是**全链唯一键**,不许由关键词推断出来(规格 §二 零推断)。

**2026-08-20 的两件事(所有者定稿,不要往回改):**
  1. **删 R0 与 R2。** R0 是代码里 8 个 walmart_category 硬禁,R2 是 yaml 里 18 条
     禁售大类,它们和 R1 的类目白名单讲的是同一件事 —— 一个类目能不能做。
     三份清单各自维护,改一处漏两处而且**不报错**。前置条件已完成:白名单先按
     官方 spec 重建补齐(pt_spec_sync),再删这两份黑名单,中间无真空期。
     现在类目层面只有 R1 一处判据。
  2. **R1 的两条静默放行改判 pending。** PT 未知 / PT 不在 walmart_pt_meta 时
     原先 `return []`,等于"查不到 = 没问题"直接 100 分放行。删掉 R0/R2 之后
     这个洞再没有别的清单兜底,必须堵。判不了 ≠ 判过了。

不迁/已删的 rule_code(**存量 audit_hits 里仍有**,理由渲染与 `-p rerule=`
保留兼容,新链不再产生):`cat_requires_cert_hard/soft`、`title_desc_blacklist`、
`trademark_live`、`content_promotional`、`walmart_strict_sensitive`、
`made_in_usa_claim`;更早的 R9(®/™,已归 Phase0)、R6
(`blacklist_compatible_for`,2026-04 删除,误伤率 90%)。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from registry import resources
from services.audit_models import L2Result, RuleHit

if TYPE_CHECKING:  # 仅供阅读/类型工具;运行期不依赖,避免与 audit_models 形成硬耦合
    from services.audit_models import L1Info, ProductInfo

logger = logging.getLogger("services.audit_l2")

# =============================================================
# R1: 类目准入 gate — 双字段白名单 (access_state + zh_can_do)
#
# **2026-08-20 起,类目层面能不能做,只有这一条规则说了算。**
# 同期删掉的 R0(代码里 8 个 walmart_category 硬禁)与 R2(yaml 18 条禁售大类)
# 讲的是同一件事的另外两种写法:三份清单各自维护,改一处漏两处,而且漏了
# **不报错**。所有者定稿:「以后我只要维护这个沃尔玛类目白名单即可」。
# 前置条件已完成 —— 白名单先补齐(pt_spec_sync 按官方 spec 重建准入明细),
# 再删黑名单,中间没有真空期。
#
# 业务规则 (用户 2026-04-27):
#   一个 PT 必须**同时满足**两条才允许进入后续审核:
#     1. access_state ∈ {'普通商品', '附条件允许'}      ← 白名单
#     2. zh_can_do = '是' OR zh_can_do 以 '需评估' 开头  ← 白名单
#   任一不满足 → -100 reject
#
# 旧仓实测基线 (上线前用同口径 SQL 对齐, 对不上是数据搬迁问题不是规则问题):
#   通过 gate 5785/7008 PT (82.5%), 被拒 1223;
#   拒的细分: access_state='需Walmart审批' 839 / '禁售' 292 / 'v5已废弃' 66;
#   zh_can_do 含 '否' 的 37 种自由文本变体.
# =============================================================


_ACCESS_WHITELIST = {"普通商品", "附条件允许"}

# R1 判不了时写进 L2Result.pending_reason 的两个 rule_code(evaluate 认这个集合)
PENDING_RULE_CODES = frozenset({"cat_gate_pt_unknown", "cat_gate_pt_not_in_meta"})


def _blocked_upstream(l1: L1Info) -> bool:
    """输入:L1Info → 输出:上游是否已判死(任一 hit 扣了分)。

    取代原先的 `l1.excluded_category_reason` 闸(该字段 2026-08-20 已删)。
    口径与 audit_rules._blocked 一致:**按 penalty<0 判,不按"有没有 hit"** ——
    0 分留痕的 hit(如 unmapped_amazon_path 哨兵)不算判死。
    """
    return any(h.penalty < 0 for h in l1.hits)


def _rule_category_gate(l1: L1Info, ctx: Any) -> list[RuleHit]:
    """输入:L1 结果 + ctx(用 ctx.pt_meta) → 输出:R1 命中的 hit(0 或 1 条,-100)。

    四种出口:
      cat_access_blocked    -100  access_state 不在白名单(需审批/禁售/已废弃/空)
      cat_zh_blocked        -100  zh_can_do 既不等于 '是' 也不以 '需评估' 开头(含空)
      cat_gate_pt_unknown      0  PT 空 / 'unknown' / '(unknown)' → **待人工**
      cat_gate_pt_not_in_meta  0  PT 不在 walmart_pt_meta        → **待人工**
    前两个**闸一先 return**,所以两个都拒时只报更上游的 cat_access_blocked。
    后两个 penalty=0 但会让 evaluate 把整条结论置成 pending(见 PENDING_RULE_CODES)。

    ⚠ 2026-08-20 修掉两条静默放行(所有者定 P0)。此前这两种情况一律
    `return []` —— 白名单是唯一的类目判据,而"查不到这个 PT"被当成了
    "这个 PT 没问题",分数原封不动 100 分直接 pass。
    **判不了不是判过了**:改判 pending 交人工,与"L1 解不出 PT → pending"
    同一条纪律(审核宁缺勿滥)。不扣分是有意的 —— 扣分等于"证据确凿地拒",
    而事实是**没有证据**。

    ⚠ 说实话:**当前接线下这两条走不到**。`audit_rules.resolve_pt` 末尾有一道
    同款 pt_meta 闸(解出来的 PT 不在表里就丢弃),PT 解不出会先在 L1 转 pending,
    所以 evaluate 收到的 PT 必定在 pt_meta 里。这两个分支是**防御网**,不是
    在补一个正在漏货的洞。留着的理由只有一条:白名单成了唯一判据之后,
    "查不到就放行"这个默认值本身不能存在 —— 上游任何一道闸被放松、或者有人
    直接调 evaluate,后果就是**静默满分放行且不报错**。默认值要站在安全那一侧。

    上游已判死(任一 hit penalty<0,如出版物硬禁)时整条规则不参与:
    既不重复扣分,也**不会把一条已经拒掉的结论降级成 pending**。
    两条 detail 的 access_state 处理**不对称**(前者带 `or "(空)"` 后者不带),照迁。
    """
    if _blocked_upstream(l1):
        return []

    pt = (l1.walmart_product_type or "").strip()
    if not pt or pt in {"unknown", "(unknown)"}:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_gate_pt_unknown",
                penalty=0,
                detail={
                    "walmart_pt": l1.walmart_product_type,
                    "pt_source": l1.pt_source,
                    "rule": "PT 未知 ⇒ 类目白名单查不了 ⇒ 待人工",
                    "source": "L2 R1",
                },
            )
        ]

    row = ctx.pt_meta.get(pt)
    if not row:
        logger.warning("R1: PT %r 不在 walmart_pt_meta,转 pending 待人工", pt)
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_gate_pt_not_in_meta",
                penalty=0,
                detail={
                    "walmart_pt": pt,
                    "pt_source": l1.pt_source,
                    "rule": "PT 不在 walmart_pt_meta ⇒ 类目白名单查不了 ⇒ 待人工",
                    "source": "walmart_pt_meta",
                },
            )
        ]

    access = (row.get("access_state") or "").strip()
    zh = (row.get("zh_can_do") or "").strip()

    # 1. access_state 白名单检查 (优先报)
    if access not in _ACCESS_WHITELIST:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_access_blocked",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "walmart_category": row.get("walmart_category"),
                    # 规则自报类别(§二):这是"类目没开/做不了",不是禁售政策。
                    # ⚠ 原先写死的 `walmart_policy="Restricted/Illegal"` 是**猜的**
                    #   ——白名单拦下与那条政策没有关系,2026-09-02 B1 删
                    "category": resources.AUDIT_CAT_ACCESS,
                    "access_state": access or "(空)",
                    "zh_can_do": zh or "(空)",
                    "rule": "access_state 不在白名单 {普通商品, 附条件允许}",
                    "source": "walmart_pt_meta.access_state",
                },
            )
        ]

    # 2. zh_can_do 白名单检查
    zh_ok = (zh == "是") or zh.startswith("需评估")
    if not zh_ok:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_zh_blocked",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "walmart_category": row.get("walmart_category"),
                    "category": resources.AUDIT_CAT_ACCESS,   # 同上,§二
                    "access_state": access,   # 注意: 这里没有 or "(空)", 照迁
                    "zh_can_do": zh or "(空)",
                    "rule": "zh_can_do 不在白名单 {是, 需评估*}",
                    "source": "walmart_pt_meta.zh_can_do",
                },
            )
        ]

    return []


# =============================================================
# public entry
# =============================================================


def evaluate(product: ProductInfo, l1: L1Info, ctx: Any) -> L2Result:
    """输入:产品 + L1 结果 + 数据上下文 ctx → 输出:L2Result(score + hits + pending)。

    顺序:
      1. 起始 100 分
      2. 叠加 l1.hits 的 penalty(**只累加分数,不把 L1 hit 复制进 L2 hits**,
         避免与 AuditOutcome.all_hits 重复入库)
      3. 跑 R1(2026-09-03 C 批起 L2 只剩这一条;此前是六条"全跑不短路")
      4. 下界保护 -1000
      5. R1 报了"判不了"(PENDING_RULE_CODES)⇒ 置 pending_reason;
         有 -100 的确定拒时 reject 优先(见 L2Result.verdict)

    ⚠ `product` 现在**没有消费者**:R1 只看 L1 解出来的 PT 与 pt_meta 那一行,
    与产品正文无关。形参保留是因为调用方(`audit_rules.audit_one`)与两条
    工作流的接线按这个签名写着,改签名是纯噪声;真要接第二条规则时它就在这儿。

    注(存量 rule_code 仍在库里,渲染与 `-p rerule=` 兼容,新链不再产生):
      - R0 / R2 已删除(2026-08-20 所有者定稿):与 R1 类目白名单三份清单讲同
        一件事,只留 R1 一处维护;
      - R3(类目需证书)/ R5(USPTO)/ R7(促销宣称)/ R8(敏感合规)2026-09-03
        随 C 批删除,R4 与 R10 同批迁入 L0(见模块头注);
      - R6 (blacklist_compatible_for) 已删除, 改由 L3 LLM 判 (规则硬拦误伤率 90%).
      - R9 (trademark_symbol_in_title) 已迁 Phase0 (phase0_trademark), 这里不调用.
    """
    score = 100
    all_hits: list[RuleHit] = []

    # 1. 继承 L1 扣分
    for h in l1.hits:
        score += h.penalty

    # 2. 跑 L2 规则(R1 PT 准入双白名单 —— 类目能不能做的唯一判据)
    pending_reason = None
    for h in _rule_category_gate(l1, ctx):
        all_hits.append(h)
        score += h.penalty
        if pending_reason is None and h.rule_code in PENDING_RULE_CODES:
            pending_reason = h.detail.get("rule") or h.rule_code

    # 3. 下界保护
    if score < -1000:
        score = -1000

    return L2Result(score_final=score, hits=all_hits, pending_reason=pending_reason)


__all__ = [
    "PENDING_RULE_CODES",
    "evaluate",
]
