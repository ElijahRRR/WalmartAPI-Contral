"""类目黑名单(L0 类目闸的唯一判据来源)—— **规则住在库里,不写在代码里**。

所有者定稿 2026-08-20 两条:

1. **按 browse_node_id 拦整棵子树**,不再按路径字符串精确等值。
   旧行为的两个致命面(2026-08-20 实测):
     · 父级不覆盖子级 —— 名单里写了 `Toys & Games > Puzzles`,产品是
       `… > Puzzles > 3-D Puzzles` **照样放行**。1.18 万条名单里 56% 的条目
       是为了补这个洞逐层枚举出来的,而亚马逊每加一个叶子类目就多一个漏洞,
       且**不报错**;
     · 名字会漂 —— 名单写 `Wall Art` / `Games & Accessories`,官方树里实际叫
       `Wall Décor` / `Games`,按名字**一条都拦不住**。
   ID 链(`catalog.products.browse_node_chain`,根→叶)只要出现禁售子树根的
   ID 就拦,改名与新增叶子都自动覆盖。

2. **代码里的类目常量搬进数据库**(减轻代码臃肿):原
   `audit_phase0.FORBIDDEN_AMAZON_TOPS` 四个硬编码顶级已迁成表内
   `match_type='top_name'` 的行,增删类目改表不改代码。
   `SEED_RULES` 只是**建库种子**,判定路径一个字都不读它。

三种匹配方式(同一张表,`match_type` 列区分):
  node_subtree  产品 ID 链里出现 `browse_node_id` ⇒ 拦整棵子树(**首选**)
  top_name      按顶级类目名(亚马逊顶级 browse node 无 ID,只能按名字)
  path_exact    归一化完整路径等值(飞书镜像的历史行,兼容保留)

api/services 分层:本模块**零 DB 访问**——`load()` 收一个连接把表读成
frozenset/dict,`check()` 是纯函数。审核判定件(audit_phase0)只认 check()。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

_TOP_STRIP_RE = re.compile(r"[\s,]+")

logger = logging.getLogger("services.category_blacklist")

MATCH_NODE = "node_subtree"
MATCH_TOP = "top_name"
MATCH_PATH = "path_exact"

# 建库种子(**只用于 db_init / 导入工作流,判定不读**)。
# 顶级四条来自原 audit_phase0.FORBIDDEN_AMAZON_TOPS;电子游戏与五棵子树是
# 所有者 2026-08-19/20 的批复。服饰鞋靴珠宝**已从顶级整拦撤下**——新口径是
# 「成人与儿童的衣服/裤/鞋不做,配饰箱包帽子珠宝可做,婴儿全部不做」,
# 按分支判,不能再整顶级一刀切。
SEED_RULES: list[dict] = [
    {"match_type": MATCH_TOP, "match_value": "Books", "category_zh": "图书",
     "reason": "Books 禁售: 版权/内容合规风险, 搬运模式不适合",
     "walmart_policy": "Intellectual Property"},
    {"match_type": MATCH_TOP, "match_value": "Kindle Store", "category_zh": "Kindle 电子书",
     "reason": "Kindle Store 禁售: 电子书版权", "walmart_policy": "Digital Goods"},
    {"match_type": MATCH_TOP, "match_value": "Automotive", "category_zh": "汽车用品",
     "reason": "汽配禁售: DOT/SAE 认证 + 安全件责任 + CARB/delete kit",
     "walmart_policy": "Auto & Motor Vehicles"},
    {"match_type": MATCH_TOP, "match_value": "Grocery & Gourmet Food", "category_zh": "食品杂货",
     "reason": "食品禁售: FDA 食品设施注册 + 标签合规(所有者 2026-08-19 定稿整顶级拦)",
     "walmart_policy": "Food Products"},
    {"match_type": MATCH_TOP, "match_value": "Video Games", "category_zh": "电子游戏",
     "reason": "电子游戏禁售(所有者 2026-08-20 定稿整顶级拦)",
     "walmart_policy": "Intellectual Property"},
    # ── 五棵子树整块不做(所有者 2026-08-20;两条遥控合用同一个父节点)──
    {"match_type": MATCH_NODE, "browse_node_id": "3736081",
     "match_value": "Home & Kitchen > Wall Décor", "category_zh": "家居厨房 > 墙面装饰画",
     "reason": "墙面装饰画整棵不做(名单原文写 Wall Art,官方树叫 Wall Décor)",
     "walmart_policy": "Intellectual Property"},
    {"match_type": MATCH_NODE, "browse_node_id": "3013597011",
     "match_value": "Industrial & Scientific > Lab & Scientific Products > Lab Chemicals",
     "category_zh": "工业与科学 > 实验室化学品",
     "reason": "实验室化学品整棵不做(危化品合规)", "walmart_policy": "Hazardous Materials"},
    {"match_type": MATCH_NODE, "browse_node_id": "166220011",
     "match_value": "Toys & Games > Games", "category_zh": "玩具游戏 > 桌游与配件",
     "reason": "桌游与配件整棵不做(名单原文写 Games & Accessories,官方树叫 Games)",
     "walmart_policy": "Intellectual Property"},
    {"match_type": MATCH_NODE, "browse_node_id": "6925830011",
     "match_value": "Toys & Games > Remote- & App-Controlled Toys",
     "category_zh": "遥控/App 控制车模与配件",
     "reason": "遥控车模与配件整棵不做(覆盖「遥控车模配件」与「遥控车模」两枝)",
     "walmart_policy": "Electronics & RF"},
]


@dataclass(frozen=True)
class CatRules:
    """装配好的类目黑名单(判定件只认这个,不碰 DB)。"""

    nodes: dict = field(default_factory=dict)   # node_id -> 行 dict
    tops: dict = field(default_factory=dict)    # 顶级名(小写压空格) -> 行 dict
    paths: dict = field(default_factory=dict)   # 归一化完整路径 -> 行 dict

    def __len__(self) -> int:
        return len(self.nodes) + len(self.tops) + len(self.paths)


@dataclass(frozen=True)
class CatHit:
    """一次命中:matched_by 决定 detail 怎么写,rule_code 决定理由映射走哪条。"""

    matched_by: str
    matched_value: str
    category_path: str = ""
    category_zh: str = ""
    reason: str = ""
    walmart_policy: str = ""


_SQL = """
SELECT match_type, match_value, browse_node_id, category_norm, category_raw,
       category_zh, reason, walmart_policy
FROM catalog.amazon_cat_blacklist
WHERE enabled
"""


def _norm_top(s: str) -> str:
    """顶级名归一:**空白与逗号全删** + 小写。

    ⚠ 2026-08-20 修:同一个顶级在真实数据里有两种写法,而且**互不匹配**——
      · 官方树 / 产品路径:'Clothing, Shoes & Jewelry'、'Video Games'
      · 飞书类目名单 / 部分采集路径:'Clothing,Shoes&Jewelry'、'VideoGames'
    只压空白的话前者归成 'clothing shoes & jewelry'、后者归成
    'clothing shoes&jewelry',**差一个 & 两侧的空格就整条规则不触发**,
    `VideoGames->XboxOne->Games` 在「电子游戏整顶级不做」下照样放行,
    而且不报错。空白和逗号一起删掉,两种写法归到同一个键。
    (39 个官方顶级 + 已知别名在这个口径下无碰撞,已核。)
    """
    return _TOP_STRIP_RE.sub("", s or "").lower()


def load(conn) -> CatRules:
    """输入:中心库连接 → 输出:CatRules(三种匹配各一张表)。

    表不存在/读失败让异常冒泡 —— 类目闸静默失效等于整条禁售线没了,
    而且**不报错**(本仓口诀:静默常态化 = 主路径已坏没人知道)。
    """
    nodes: dict = {}
    tops: dict = {}
    paths: dict = {}
    with conn.cursor() as cur:
        cur.execute(_SQL)
        for (mt, mv, nid, cnorm, craw, zh, reason, policy) in cur.fetchall():
            row = {"match_value": mv or cnorm or "", "category_path": craw or mv or "",
                   "category_zh": zh or "", "reason": reason or "",
                   "walmart_policy": policy or ""}
            if mt == MATCH_NODE and nid and str(nid).strip():
                nodes[str(nid).strip()] = row
            elif mt == MATCH_TOP and mv:
                tops[_norm_top(mv)] = row
            elif cnorm:
                paths[cnorm] = row
    logger.info("类目黑名单加载:子树 %d / 顶级 %d / 精确路径 %d",
                len(nodes), len(tops), len(paths))
    return CatRules(nodes=nodes, tops=tops, paths=paths)


def chain_ids(browse_node_chain: str) -> list[str]:
    """输入:'123,456,789' 形态的 ID 链 → 输出:去空的 ID 列表(根→叶)。"""
    if not browse_node_chain:
        return []
    return [x.strip() for x in str(browse_node_chain).split(",") if x.strip()]


def check(rules: CatRules, amazon_category_path: str, browse_node_chain: str = "",
          browse_node_id: str = "", norm_path: str = "") -> CatHit | None:
    """输入:规则 + 产品类目路径/ID 链 → 输出:CatHit 或 None(纯函数,零 DB)。

    顺序 = 精确度:子树 ID > 顶级名 > 完整路径等值。
    ⚠ ID 链**从叶往根**查:命中最细的那个子树根,detail 里给的才是"因为它属于
    哪一棵被禁的树",而不是"因为它在某个大类下"。
    """
    if not rules or len(rules) == 0:
        return None

    # 1. 子树:ID 链里任一 ID 是禁售子树根即拦(自叶向根,取最细)
    ids = chain_ids(browse_node_chain)
    if not ids and browse_node_id:
        ids = [str(browse_node_id).strip()]
    for nid in reversed(ids):
        row = rules.nodes.get(nid)
        if row:
            return CatHit(matched_by=MATCH_NODE, matched_value=nid,
                          category_path=row["category_path"],
                          category_zh=row["category_zh"], reason=row["reason"],
                          walmart_policy=row["walmart_policy"])

    # 2. 顶级名(亚马逊顶级 browse node 没有 ID,只能按名字)
    top = (amazon_category_path or "").split(">", 1)[0]
    row = rules.tops.get(_norm_top(top))
    if row:
        return CatHit(matched_by=MATCH_TOP, matched_value=" ".join(top.split()),
                      category_path=row["category_path"],
                      category_zh=row["category_zh"], reason=row["reason"],
                      walmart_policy=row["walmart_policy"])

    # 3. 完整路径等值(飞书镜像的历史行;**父级不覆盖子级**,故排最后)
    if norm_path:
        row = rules.paths.get(norm_path)
        if row:
            return CatHit(matched_by=MATCH_PATH, matched_value=norm_path,
                          category_path=row["category_path"],
                          category_zh=row["category_zh"], reason=row["reason"],
                          walmart_policy=row["walmart_policy"])
    return None


# =============================================================
# 行 → 规则(飞书表与离线 CSV **共用同一个解析件**)
# =============================================================

# 「匹配方式」列的中文取值 → match_type。运营在飞书里填的就是这三个词。
MATCH_BY_ZH = {"子树": MATCH_NODE, "顶级名": MATCH_TOP, "路径等值": MATCH_PATH}


def make_rule(category: str, *, browse_node_id: str = "", category_zh: str = "",
              match_type: str = "", reason: str = "", raw_path: str = "",
              source: str = "manual") -> tuple[dict | None, str]:
    """输入:一条名单行的各字段 → 输出:(规则 dict, "") 或 (None, 跳过原因码)。

    两个写入口(risk_sync 读飞书 / category_blacklist_import 读 CSV)共用本件,
    保证"同一行在两条路径上录出来的规则一模一样"。列名各自在自己那侧取
    (飞书列名只准从 registry 拿,铁律 3),本件只收值。

    ⚠ **「有 ID 就当子树根」是错的**,别再退回那个口径:名单里有一批
    browse_node_id 是回落匹配来的(祖先前缀 / 存疑 / 与官方路径深度对不上),
    当子树根用就整棵误拦(实见暗房化学品的 ID 回落成了整个相机摄影大类)。
    所以子树与否由「匹配方式」列说了算;该列为空才退回按 ID 推断,
    并返回 `fallback` 码让调用方**报数**(静默回落 = 主路径已坏没人知道)。
    """
    from services.audit_phase0 import normalize_amazon_category as _norm  # 循环依赖:audit_phase0 在模块级 import 本模块

    cat = (category or "").strip()
    if not cat:
        return None, "空类目"
    nid = (browse_node_id or "").strip() or None
    how = (match_type or "").strip()
    fallback = ""
    if not how:
        mt = MATCH_NODE if nid else MATCH_PATH
        fallback = "无匹配方式列-按ID推断"
    elif how == "子树" and nid:
        mt = MATCH_NODE
    elif how in ("顶级名", "路径等值"):
        # 顶级 browse node 亚马逊不发 ID;路径等值行的 ID 一并丢掉,
        # 免得日后被谁当成子树根使
        mt, nid = MATCH_BY_ZH[how], None
    else:
        # 「不录入」「(被覆盖,不单独录入)」「子树但没填 ID」都落这里
        return None, "匹配方式非录入"
    norm = _norm(cat)
    if not norm:
        return None, "归一化后为空"
    rule = {"norm": norm, "raw": (raw_path or cat).strip(), "mt": mt,
            "mv": cat, "nid": nid, "zh": (category_zh or "").strip(),
            "reason": (reason or "").strip()[:500], "policy": "", "source": source}
    return rule, fallback


__all__ = ["CatRules", "CatHit", "SEED_RULES", "MATCH_NODE", "MATCH_TOP",
           "MATCH_PATH", "MATCH_BY_ZH", "load", "check", "chain_ids", "make_rule"]
