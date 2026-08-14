"""审核流水线共享数据结构(移植自旧仓 pipelines/models.py,批次 B 瘦身版)。

只保留批次 B(纯规则:Phase0 + L2 + shortcut)真正用到的字段与类型:
  - ProductInfo 只留 Phase0/L2 会读的 8 个字段(旧仓的 country_of_origin /
    image_urls / stock_count / stock_status 是 L3/L4 视觉与库存分支的输入,
    批次 B 无 L3/L4,不迁)。
  - L1Info 对应旧仓 L1Result,去掉 zh_can_do 与三个"向后兼容字段"
    (requires_certificate / zh_seller_forbidden / requirements_text):
    新架构里 R1 的 zh_can_do、R3 的证书要求全部改从 ctx.pt_meta / ctx.pt_spec 取。
  - L3Result / L4Result 不迁(批次 B 零 LLM 零视觉);AuditOutcome 仍保留
    l3 / l4 两个槽位,保证 all_hits 的拼接顺序与旧仓一致,将来接上即可用。

本模块是纯数据结构,不做任何 DB 访问、不读文件、不含业务判断。

public:
  SCORE_THRESHOLD, Verdict, Stage,
  ProductInfo, RuleHit, Phase0Result, L1Info, L2Result, AuditOutcome
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# L2 判定阈值:score_final < 60 → reject。
# 旧仓走 config.get_settings().score_threshold(pipelines/models.py:100-103),
# 默认值 60 来自 config/settings.py:236 `_get_int("SCORE_THRESHOLD", 60)`。
# 新仓审核规则不读环境变量(旧仓生产从未覆盖过它),定为模块常量。
SCORE_THRESHOLD = 60

Verdict = Literal["pass", "reject", "pending"]
Stage = Literal["L0", "L1", "L2", "L3", "L4", "SHORTCUT"]


@dataclass
class ProductInfo:
    """审核输入:产品详情(新仓由 catalog.products + snapshots 组装)。

    所有字符串字段的"空"语义是 `""` 而非 None,但规则代码里仍按旧仓写法
    保留 `(x or "")` 的防御(调用方若直接塞 None 不会炸)。
    """

    asin: str
    title: str = ""
    brand: str = ""
    bullet_points: list[str] = field(default_factory=list)
    long_description: str = ""
    amazon_category_path: str = ""   # 形如 'Clothing, Shoes & Jewelry > Men > Shoes'
    seller_id: str = ""              # Phase0 飞书卖家黑名单用
    seller_name: str = ""            # 仅写进 detail,不参与判定
    known_pt: str | None = None      # 产品行已知 PT(历史实证回填/先前结论;
                                     # 批次 C 新增,resolve_pt ①b 级消费)
    known_pt_source: str | None = None  # 该 PT 的来历:'walmart_confirmed'=沃尔玛
                                     # 真接受过;其余(含 NULL)= 我们自己推断的。
                                     # 分道后 ①b 级只把前者当实证(2026-08-14)
    browse_node_id: str = ""         # 类目 ID 链末段(当前最细类目;名称会漂
                                     # ID 不会,resolve_pt ②a 级直查映射表)

    @property
    def searchable_text(self) -> str:
        """输入:自身 title/bullets/description → 输出:换行拼接的大文本。

        逐字迁自 pipelines/models.py:29-36:bullets **不截断**、falsy 段过滤、
        "\\n" 连接。L2 的黑名单/关键词扫描用它;**Phase0 商标规则不用**
        (那里另拼一份 hay:bullets 只取前 5 条、desc 只取前 1000 字符)。
        """
        parts = [self.title or ""]
        parts.extend(self.bullet_points)
        if self.long_description:
            parts.append(self.long_description)
        return "\n".join(p for p in parts if p)


@dataclass
class RuleHit:
    """一条规则命中记录(对应 audit.audit_hits 表一行)。

    detail 最终以 JSONB 落库,键名与键序均是移植契约的一部分,不得擅改。
    """

    stage: Stage
    rule_code: str
    penalty: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Phase0Result:
    """Phase 0 精准前置过滤结果(飞书黑名单 / 类目禁售 / 商标符号 / 品牌黑名单)。

    blocked=True 时整个流程直接终止,不进 L1/L2。
    四条规则串行短路,所以 hits 最多只有 1 条。
    """

    blocked: bool = False
    matched_brand: str | None = None      # 品牌/商标短语命中
    matched_category: str | None = None   # Amazon 类目命中(飞书表或 8 大类)
    hits: list[RuleHit] = field(default_factory=list)


@dataclass
class L1Info:
    """L1 类目判定结果(批次 B 无 L1 判定逻辑,由调用方按已知 PT 直接构造)。

    Phase0 命中时用桩值 ("(phase0_blocked)", "低", "skipped") 填充,
    与旧仓 orchestrator.py:343 的字面量一致。
    """

    walmart_product_type: str | None = None       # None = 批次 B 解不出 PT
    pt_confidence: str | None = None              # 高/中/低
    pt_source: str | None = None                  # 'walmart_confirmed' / 'map_direct' / 'skipped'
    walmart_category: str | None = None           # 给 L2 R2 做前缀匹配用
    excluded_category_reason: str | None = None   # L1 硬拦(批次 B 恒 None)
    hits: list[RuleHit] = field(default_factory=list)


@dataclass
class L2Result:
    """L2 硬规则打分结果(100 起始,累加 penalty 后得分)。"""

    score_final: int
    hits: list[RuleHit] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        """输入:自身 score_final → 输出:'reject'(低于阈值)或 'pass'。

        逐字迁自 pipelines/models.py:100-103,只把 settings 查询换成模块常量
        SCORE_THRESHOLD(=60)。因软规则 penalty 全为 0,净语义 = 任一硬规则
        (-100)命中即 reject。
        """
        return "reject" if self.score_final < SCORE_THRESHOLD else "pass"


@dataclass
class AuditOutcome:
    """端到端一次审核的最终结果。"""

    asin: str
    verdict: Verdict
    score_final: int | None            # verdict='pending'(PT 解不出)时为 None
    stage_stopped_at: Stage | None     # None 表示所有阶段都过了
    l1: L1Info
    phase0: Phase0Result | None = None
    l2: L2Result | None = None
    l3: Any = None                     # 批次 B 无 L3(留槽位保证 all_hits 顺序)
    l4: Any = None                     # 批次 B 无 L4

    # 对齐 Walmart 37 条政策 category_en 的最终拒绝原因(verdict=reject 时有意义)。
    # 注意:旧仓与新仓 audit.audit_runs 都没有这一列,它只活在内存/返回摘要里。
    final_reason_category: str | None = None

    @property
    def all_hits(self) -> list[RuleHit]:
        """输入:自身各层结果 → 输出:按 phase0→l1→l2→l3→l4 顺序拼平的命中列表。

        顺序逐字迁自 pipelines/models.py:142-151(落库顺序即此顺序)。
        旧仓 l1 是必填字段故无判空;新仓补了一个 None 保护,l1 存在时行为不变。
        """
        bag: list[RuleHit] = []
        if self.phase0 is not None:
            bag.extend(self.phase0.hits)
        if self.l1 is not None:
            bag.extend(self.l1.hits)
        for r in (self.l2, self.l3, self.l4):
            if r is not None:
                bag.extend(r.hits)
        return bag


__all__ = [
    "SCORE_THRESHOLD",
    "Verdict",
    "Stage",
    "ProductInfo",
    "RuleHit",
    "Phase0Result",
    "L1Info",
    "L2Result",
    "AuditOutcome",
]
