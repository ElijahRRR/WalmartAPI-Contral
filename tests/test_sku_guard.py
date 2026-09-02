"""SKU 身份改造的守门(**整套改造唯一的一份**,批次 0a 建,2026-09-02)。

三件事先说清楚:

① **本文件是 SKU 改造唯一的一份守门文件。** 批次 0b/1/2/3 与横切包只准增删
   这里的白名单条目,**不许再建第二份**。四份并存正是 conventions §六要禁的
   形态,而且已经被实测出后果:同一张 extract_asin 白名单在三个包里各写一份、
   abandoned_at 白名单在四个包里给出三种数目、字母表一致性断言两条互斥 ——
   守门测试自己犯了它要守的规矩。
② **白名单每一条都要写清理由与预期收口批次。** 永久豁免(product_audit 的
   第一条腿、两个旧库导入工作流、规则自身之家)显式标 `permanent`;其余写它
   该在哪个批次消失。要改守门,先改白名单,**别删断言**。
③ 与 tests/test_feishu_guard.py 的分工:同款纪律(白名单 dict + 末尾一条
   "白名单不许烂掉")、不同域 —— 那边守飞书通道边界,这边守 SKU 身份口径。

守什么(口径全文见 docs/conventions.md §九):

  · SQL 里 `x.asin = y.sku` 这种硬等号会随切码**静默失效**(不报错,只是再也
    匹配不上任何一行);身份表达式的唯一写法是 `coalesce(ls.source_key, w.sku)`。
  · Python 侧同理:直接调 `extract_asin` 的地方,切码后一律返 None。
  · `abandoned_at IS NULL` 是个**危险谓词**:写进 resolve / 维护链 / 订单反查
    就会让旧码带回来的订单查不到产品;它只允许出现在三处。
  · 登记簿的写入出口只有两个,弃码三列只有一个写者。
  · 12 位不透明码的字母表只准在 services/sku_codec.py 出生;schema.sql 的部分
    索引条件与它逐字对齐(不对齐 = 索引和代码对"什么是新码"的判断不一致)。
"""

import ast
import re
import socket
from pathlib import Path

import pytest

from services import sku_codec

ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = (ROOT / "refdata" / "schema.sql").read_text(encoding="utf-8")

# ══════════════════════════════════════════════════════════════════════════════
#  白名单(**要改守门,先改这里,别删断言**)
#
#  值 = (预期收口批次, 理由)。`permanent` = 这不是待办,是有理由的永久豁免。
#  批次号(如 `0b`)= 那个批次合并时这一条必须消失。
#  ⚠ PR-0a-2(15 处读侧收口)已合:它带进来的六条临时条目全部删除,
#  维护链 / audit_rules / alloc_survey / alloc_push / alloc_plan / alloc_products
#  从此**出现即红**。
# ══════════════════════════════════════════════════════════════════════════════

#: ① 允许出现 `x.asin = y.sku` 硬等号的文件。
_HARD_EQUALITY_OK: dict[str, tuple[str, str]] = {
    "workflows/product_audit.py": (
        "permanent",
        "_pick_where('online') 的第一条腿故意保留 w.sku = p.asin:那是对 products "
        "每行做的相关子查询,写成 coalesce 表达式就用不上 walmart_items_sku_idx,"
        "几十万行候选退化成逐行全表扫(2026-08-14 视图挂死同一类事故)。新码由"
        "第二条腿(走 listing_sources_key_idx)覆盖,两条腿 OR 起来各走各的索引"),
}

#: ② 允许直接调 `extract_asin` 的文件(收口后应改成 pick_asin / SQL 侧 coalesce)。
#: ⚠ **不扫 is_standard_asin**:workflows/brand_scrape.py 与
#: workflows/product_refresh.py 用它做「合法 ASIN 形态闸」,与 SKU→ASIN 是两个
#: 能力(推一个非标准码去采集只会永远采不到 → 永远缺品牌 → 永远再推)。
#: ⚠ PR-0b-1 已合:order_lines / blacklist / order_audit(工作流)/
#: order_asin_normalize 四条**已删** —— 它们从此出现即红。
_EXTRACT_ASIN_OK: dict[str, tuple[str, str]] = {
    "services/sku_asin.py": (
        "permanent", "规则自身之家:extract_asin 与 pick_asin 都长在这里"),
    "workflows/order_history_import.py": (
        "permanent", "只导旧库历史数据,那批行的 SKU 永远是存量形态"),
    "workflows/pt_backfill.py": (
        "permanent", "只读旧 walmart_cleanup 库,同上"),
    "services/product_events.py": (
        "permanent",
        "record_many **仅 store 为空的平台级事件分支**:登记簿主键是 (store, sku),"
        "没有店就没得查(product_ingest 那一刻根本没有店铺)。四个平台级来源 —— "
        "product_ingest / audit_store.event_row / product_audit 补采,外加 "
        "audit_history_fold 的直插 SQL(绕过本函数,asin 列直填)。带 store 的行"
        "(含 cleanup_history_import)走登记簿腿"),
    "services/order_audit.py": (
        "permanent",
        "line_asin 的**兜底腿**:订单链以 order_lines.asin 列为准,登记簿那一跳在"
        "写入侧;asin 为 NULL 的存量行才回落形态提取。订单链取 ASIN 只此一处"),
}

#: ⑥ 允许在 workflows/ 里出现 `catalog.listing_sources` 的文件。
#: ⚠ 判据是**怎么用**:全表级取数按 services/sku_asin 模块头的纪律走 SQL 侧
#: `LEFT JOIN` 取 `coalesce(ls.source_key, w.sku)`(PR-0a-2 的 15 处读侧收口),
#: 那是身份表达式的唯一写法,不是第二条反查路径;Python 逐对反查(resolve_many)
#: 则一律只准住在 services —— 后者由 test_registry_hop_lives_in_services_only 拦。
#: 射程不含模块 docstring:文档里写清"这一跳查的是登记簿"正是我们要的东西。
_REGISTRY_SQL_OK: dict[str, tuple[str, str]] = {
    "workflows/sources_backfill.py": (
        "permanent", "_SQL_GAP 就是登记簿的补给线本身(在架却未登记的那批)"),
    "workflows/list_new.py": (
        "permanent", "PR-0a-2:去重闸 / 代际口径 / 家族在架三条全表级 LEFT JOIN"),
    "workflows/product_audit.py": (
        "permanent", "PR-0a-2:mode=online 的第二条腿(走 listing_sources_key_idx)"),
    "workflows/product_refresh.py": (
        "permanent", "PR-0a-2:推采集目标取身份键"),
    "workflows/alloc_plan.py": ("permanent", "PR-0a-2:「已在架」集合"),
    "workflows/alloc_products.py": ("permanent", "PR-0a-2:「已在架」集合"),
    "workflows/alloc_push.py": ("permanent", "PR-0a-2:「已在架」集合"),
}

#: ③ 允许出现 `abandoned_at` 的**消费方** .py。
#: ⚠ refdata/schema.sql 显式排除在扫描面之外:那几条是**部分索引的局部条件**
#: (DDL),不是消费方过滤,不计入这张白名单。
_ABANDONED_AT_OK: dict[str, tuple[str, str]] = {
    "services/sku_codec.py": (
        "permanent", "mint 的复用查询要的就是活码;abandon 自己写这三列"),
    "workflows/list_new.py": (
        "permanent", "_SQL_LISTED_ASINS 本店去重闸:码已弃 = 沃尔玛侧无物可撞,"
                     "该放行(_FAMILY_LISTED_SQL 有意不带这个谓词,见那处头注)"),
    "workflows/alloc_push.py": (
        "permanent", "_SQL_ONLINE:派工的「已在架」按活码算"),
}

#: ⑦ **四个弃码点**:允许出现 `sku_codec.abandon(...)` 调用的文件(批次 2)。
#: 多一个点 = 沃尔玛侧还活着的记录被我们当成死的,下一轮新码新 UPC 去上同一个
#: item(同店重复 listing,沃尔玛不会替你拦);少一个点 = 僵尸行永远挡着新码。
#: 两种都是**静默**的 —— 没有任何回执会告诉你"你不该弃这个码"。
#: 批次 3 会加第五行 workflows/sku_migrate.py(改码 SkuUpdate,弃码点 4)。
_ABANDON_CALLERS_OK: dict[str, tuple[str, str]] = {
    "services/sku_codec.py": (
        "permanent", "abandon 的定义之家(弃码唯一实现;烧号分派表也在这里)"),
    "workflows/catalog_sync.py": (
        "permanent",
        "弃码点 1:DELETE 经**观测核验**(delete_verified)后弃码,与事件同一"
        "事务。**不是按回执弃**:「回执成功但后台没删」是所有者实证过的故障"
        "模式(delete_not_effective),按回执弃码 = 下一轮拿新码新 UPC 去上一个"
        "还活着的 item"),
    "workflows/sku_locked_heal.py": (
        "permanent",
        "弃码点 2:SKU_LOCKED 自愈链 RETIRE 回执成功 + 冷却期满。四个点里唯一"
        "绑回执的一个 —— 锁死的 SKU 可能从未进过 walmart_items,没有观测可等"),
    "services/listing_sheet.py": (
        "permanent",
        "弃码点 3:ERR_EXT_DATA_0101119 撞库,码与 UPC 一起换(决策 B)。"
        "拆的是「撞库 → 同 SKU 换 UPC → 0101211 → 自愈链」这个死循环"),
}

#: ⑧ **反向名单**:这些文件里 abandon 必须零出现(每条写清为什么绝不许弃码)。
#: 它们全是"下架/清理/失明"类动作 —— 沃尔玛侧记录仍在、仍绑着我们的 UPC。
_ABANDON_FORBIDDEN: dict[str, str] = {
    "workflows/product_clear.py":
        "停用(RETIRE)不弃码(决策 A 默认):可恢复,码与 UPC 都还活着;"
        "真删了也要等 catalog_sync 的观测核验(弃码点 1)才算数",
    "workflows/problem_product_cleanup.py":
        "破坏动作的唯一出口,但它提交的 DELETE 同样按观测核验收尾 —— "
        "在这里按回执弃码就是把弃码点 1 的判据搬到了回执上",
    "workflows/maintenance.py":
        "改价/改库存/清库存/改标题都不改变「这条记录还在不在」,与码的寿命无关",
    "services/walmart_catalog.py":
        "mark_missing 记的是**本轮没扫到**(缺席),不是删除:缺席行随时会"
        "reappear(item_reappeared 是账本里的常规事件),弃了码它回来时就成了"
        "同店两条同内容记录",
    "services/feed_track.py":
        "回执入账只记事实。提交失败/被拒/Unknown/PROHIBITED 一律不弃码:"
        "沃尔玛侧那条记录可能已经建好了(Unknown 的定义就是不知道),"
        "换码重上 = 白烧一个 UPC + 可能的重复 listing",
}

#: ④ 允许 UPDATE 登记簿的文件(弃码三列只有一个写者)。
_LISTING_SOURCES_UPDATE_OK: dict[str, tuple[str, str]] = {
    "services/sku_codec.py": (
        "permanent", "abandon 与批次 3 的改码替换是三列唯一的写者"),
}

#: ⑤ 允许 INSERT 登记簿的文件(登记只有两个出口)。
_LISTING_SOURCES_INSERT_OK: dict[str, tuple[str, str]] = {
    "services/listing_sources.py": (
        "permanent", "register:存量 backfill 与跟卖 B 列人工号的首次登记"),
    "services/sku_codec.py": (
        "permanent", "mint:抽码与登记同一函数同一事务"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  取材
# ══════════════════════════════════════════════════════════════════════════════

def _prod_files() -> list[tuple[str, Path]]:
    """输入:无 → 输出:[(仓内相对路径, 绝对路径)] —— 全部生产 Python 代码。

    tests/ 不在射程内:守门自己要写白名单、假数据里要出现被禁的字面量。
    """
    files = [ROOT / "cli.py"]
    for d in ("services", "workflows", "registry", "api"):
        files += sorted((ROOT / d).rglob("*.py"))
    return [(str(p.relative_to(ROOT)), p) for p in files
            if "__pycache__" not in p.parts]


def _offenders(pattern: re.Pattern, allow: dict) -> list[str]:
    """输入:正则 + 白名单 → 输出:命中且不在白名单里的 `路径:行号 行文`。"""
    out: list[str] = []
    for rel, path in _prod_files():
        if rel in allow:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                out.append(f"{rel}:{n} {line.strip()[:80]}")
    return out


def _fmt(offenders: list[str], head: str) -> str:
    return head + "\n  " + "\n  ".join(offenders)


# ══════════════════════════════════════════════════════════════════════════════
#  ① SQL 硬等号(sku 与 asin 直接比)
# ══════════════════════════════════════════════════════════════════════════════

_HARD_EQ_RE = re.compile(
    r"\b[a-z_]+\.asin\s*=\s*[a-z_]+\.sku\b|\b[a-z_]+\.sku\s*=\s*[a-z_]+\.asin\b")


def test_sku_and_asin_hard_equality_is_extinct():
    """`x.asin = y.sku` 切码后**静默失效** —— 不报错,只是再也匹配不上任何一行。

    后果按处不同:维护链失明(不改价、不清零)、删除意图产出面凭空变化、
    在架复审候选恒空。身份表达式的唯一写法是 `coalesce(ls.source_key, w.sku)`,
    ls 限 source_type='amz'(口径全文见 conventions §九)。
    """
    offenders = _offenders(_HARD_EQ_RE, _HARD_EQUALITY_OK)
    for n, line in enumerate(_SCHEMA.splitlines(), 1):
        if _HARD_EQ_RE.search(line):
            offenders.append(f"refdata/schema.sql:{n} {line.strip()[:80]}")
    assert not offenders, _fmt(
        offenders, "SKU 与 ASIN 的硬等号切码后必然失效,改成 "
                   "coalesce(ls.source_key, w.sku)(或两条腿 OR):")


# ══════════════════════════════════════════════════════════════════════════════
#  ② extract_asin 的调用点
# ══════════════════════════════════════════════════════════════════════════════

def _ast_uses_extract_asin(src: str) -> bool:
    """输入:源码 → 输出:AST 里是否引用了 extract_asin 这个名字。"""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name) and node.id == "extract_asin":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "extract_asin":
            return True
        if isinstance(node, ast.ImportFrom) and any(
                a.name == "extract_asin" for a in node.names):
            return True
    return False


def test_extract_asin_callers_are_whitelisted():
    """AST 轨 + 文本轨双做(conventions §五:按名字 grep 单向找一定会出错)。

    AST 轨认真的调用与导入,文本轨兜住注释/docstring 里"先写好再抄进代码"的
    前一步。切码后 extract_asin 对不透明码必返 None —— 调用点不改,就是一次
    没有任何报错的全量失明。
    """
    offenders: list[str] = []
    for rel, path in _prod_files():
        if rel in _EXTRACT_ASIN_OK:
            continue
        src = path.read_text(encoding="utf-8")
        if _ast_uses_extract_asin(src):
            offenders.append(f"{rel}(AST 引用)")
        if "extract_asin" in src:
            offenders.append(f"{rel}(文本出现)")
    assert not offenders, _fmt(
        sorted(set(offenders)),
        "extract_asin 只兜存量形态;新码要走 services.sku_asin.pick_asin "
        "或 SQL 侧的 coalesce(ls.source_key, w.sku):")


# ══════════════════════════════════════════════════════════════════════════════
#  ③④⑤ 登记簿:危险谓词与两个写入出口
# ══════════════════════════════════════════════════════════════════════════════

def test_abandoned_at_predicate_only_where_the_whitelist_says():
    """`abandoned_at IS NULL` 写错地方 = 旧码带回来的订单/售后**查不到产品**。

    消费方契约:resolve / 维护链 JOIN / 事件归并 / 订单反查一律不按它过滤。
    它只允许出现在 mint 的复用查询、list_new 去重闸、alloc_push._SQL_ONLINE
    三处(批次 3 起增 sku_migrate 的候选选取为第四处)。

    射程是**这一列被当作条件用或被赋值**(`abandoned_at IS NULL` / `= now()` /
    任何比较),不是这个词本身:模块 docstring 里写"本模块不按 abandoned_at
    过滤"的那种消费方契约,正是我们要的文档,扫它没有意义。
    """
    offenders = _offenders(re.compile(r"abandoned_at\s*(?:=|<|>|\bis\b)", re.I),
                           _ABANDONED_AT_OK)
    assert not offenders, _fmt(
        offenders, "abandoned_at 只准出现在白名单登记的消费方里(conventions §九):")


def test_only_sku_codec_writes_the_abandon_columns():
    """两个模块往同一张表写,不写死分工就会长出第二条弃码路径 ——
    而弃码是不可逆的(码弃了就再也不给这个品用)。"""
    offenders = _offenders(re.compile(r"UPDATE\s+catalog\.listing_sources"),
                           _LISTING_SOURCES_UPDATE_OK)
    assert not offenders, _fmt(
        offenders, "登记簿的 UPDATE(尤其弃码三列)只准 services/sku_codec 写:")


def test_the_registry_table_has_exactly_two_insert_sites():
    """登记只有两个出口:register(首次登记)与 mint(抽码即登记)。

    第三个出口 = 第三条抽码路径,而且不报错:它照样写得进去,只是不过 mint 的
    复用查询与全局查重(conventions §六:一个能力一条实现路径)。
    """
    offenders = _offenders(re.compile(r"INSERT\s+INTO\s+catalog\.listing_sources"),
                           _LISTING_SOURCES_INSERT_OK)
    assert not offenders, _fmt(
        offenders, "登记簿的 INSERT 只有 listing_sources.register 与 sku_codec.mint:")


# ══════════════════════════════════════════════════════════════════════════════
#  ⑦⑧ 四个弃码点(批次 2 接线):正向白名单 + 反向名单
# ══════════════════════════════════════════════════════════════════════════════

def _calls_abandon(path: Path) -> bool:
    """输入:.py 路径 → 输出:**代码里**是否调用/导入了 abandon(不扫注释文档)。

    射程只能是 AST:四个弃码点的邻居文件里到处都在**解释**为什么自己不弃码
    (那正是我们要的文档),文本扫会把这些解释判成违规,于是白名单只能越写
    越长,最后没人看得懂它守的是什么。
    """
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "abandon":
            return True                      # 定义之家(sku_codec)也算"在场"
        if isinstance(node, ast.Attribute) and node.attr == "abandon":
            return True
        if isinstance(node, ast.Name) and node.id == "abandon":
            return True
        if isinstance(node, ast.ImportFrom) and any(
                a.name == "abandon" for a in node.names):
            return True
    return False


def test_abandon_callers_are_the_four_points_only():
    """弃码点**只有四个**,而且都在白名单里写清了判据(conventions §九)。

    第五个弃码点出现时应该是有人在讨论后加进这张表,而不是某条链顺手调了一次
    —— 弃码不可逆(登记簿没有撤销弃码的路径),而且错了不报错。
    """
    offenders = [rel for rel, path in _prod_files()
                 if rel not in _ABANDON_CALLERS_OK and _calls_abandon(path)]
    assert not offenders, _fmt(
        offenders, "sku_codec.abandon 只允许在四个弃码点调用(conventions §九):")
    # 反过来:白名单里登记的四个点必须真的还在调,否则那一条是该删的历史
    stale = [rel for rel in _ABANDON_CALLERS_OK
             if not _calls_abandon(ROOT / rel)]
    assert not stale, _fmt(stale, "白名单登记了弃码点,但那个文件已经不调 abandon:")


def test_destructive_workflows_never_abandon():
    """反向钉死:停用/清理/维护/缺席/回执五条链**永不**弃码。

    它们全是"下架"类动作 —— 沃尔玛侧记录仍在、仍绑着我们的 UPC,抽新码 =
    同店两条同内容记录 + 白烧一个 UPC(sku_codec 模块头注 ②)。
    """
    offenders = [f"{rel}({why})" for rel, why in _ABANDON_FORBIDDEN.items()
                 if _calls_abandon(ROOT / rel)]
    assert not offenders, _fmt(offenders, "这些文件绝不许调 abandon:")


def test_sku_update_reason_has_no_caller_yet():
    """`REASON_SKU_UPDATE`(改码)在本批**全仓零调用**:唯一调用方是批次 3 的
    workflows/sku_migrate.py。

    常量与"不烧号"分支现在就存在且被测试覆盖,是为了批次 3 不另开第二条弃码
    实现(双轨禁止)。批次 3 启用时必须**显式改掉这条断言** —— 改不掉就说明
    有人提前接了线,而那会在没有迁码闭环的情况下把旧行标死。
    """
    offenders = [f"{rel}:{n}" for rel, path in _prod_files()
                 if rel != "services/sku_codec.py"
                 for n, line in enumerate(
                     path.read_text(encoding="utf-8").splitlines(), 1)
                 if "ABANDON_SKU_UPDATE" in line]
    assert not offenders, _fmt(
        offenders, "sku_update 是批次 3 的弃码原因,本批全仓零调用:")


def test_sku_update_never_burns_a_upc():
    """改码时 item 还在、UPC 还绑着 —— 烧号等于白烧一个号,还得再领一个。"""
    assert sku_codec.ABANDON_SKU_UPDATE not in sku_codec._BURN_STATUS
    assert set(sku_codec._BURN_STATUS) == {
        sku_codec.ABANDON_DELETE_VERIFIED, sku_codec.ABANDON_SKU_LOCKED,
        sku_codec.ABANDON_UPC_CONFLICT}


def test_cooldown_and_generation_constants_have_one_home():
    """退役冷却小时数与代际上限**各只有一个出生地**(services/sku_codec.py)。

    两个消费方各写一个 24,一漂就没人说得清冷却到底几小时(而闸门看起来
    照常工作)。此前 24 长在 sku_locked_heal 的 params 默认值里,list_new 的
    退役冷却闸是第二个消费方 —— 泛化的那一刻必须收口。
    """
    home = "services/sku_codec.py"
    assert sku_codec.RETIRE_COOLDOWN_HOURS == 24
    assert sku_codec.MAX_SKU_GENERATIONS == 3
    born = re.compile(r"^\s*(RETIRE_COOLDOWN_HOURS|MAX_SKU_GENERATIONS)\s*=")
    offenders = [f"{rel}:{n} {line.strip()}" for rel, path in _prod_files()
                 if rel != home
                 for n, line in enumerate(
                     path.read_text(encoding="utf-8").splitlines(), 1)
                 if born.search(line)]
    assert not offenders, _fmt(offenders, f"这两个常量只准在 {home} 出生:")
    heal = (ROOT / "workflows" / "sku_locked_heal.py").read_text(encoding="utf-8")
    assert 'params.get("cooldown_hours",\n' in heal or \
        'params.get("cooldown_hours", sku_codec.RETIRE_COOLDOWN_HOURS)' in heal
    assert '"cooldown_hours", 24' not in heal        # 旧的第二份真相


# ══════════════════════════════════════════════════════════════════════════════
#  ⑨ 跟卖侧的第二条发码路径已死(批次 2 删)
# ══════════════════════════════════════════════════════════════════════════════

_DEAD_GENERATORS = ("make_sku", "next_serial_start", "SKU_PREFIX")


def _second_generator_hits(path: Path) -> list[str]:
    """输入:.py 路径 → 输出:命中旧发码器的证据(模块 docstring 不计)。

    模块 docstring 里写"旧生成器已删、为什么删"正是要留的文档;命中判据是
    **代码里用到了那些名字**,或字符串字面量里还留着 PHUMWMT 前缀。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                       # 掐掉模块 docstring
    hits: list[str] = []
    for top in body:
        for node in ast.walk(top):
            if isinstance(node, ast.Name) and node.id in _DEAD_GENERATORS:
                hits.append(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in _DEAD_GENERATORS:
                hits.append(node.attr)
            elif isinstance(node, ast.ImportFrom):
                hits += [a.name for a in node.names if a.name in _DEAD_GENERATORS]
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and "PHUMWMT" in node.value):
                hits.append("PHUMWMT 字面量")
    return hits


def test_no_second_sku_generator_survives():
    """跟卖 SKU 的生成从此**只有 mint 一条路**(conventions §六)。

    旧的 PHUMWMT + 提交日期 + 当日 4 位序号生成器已在批次 2 删除:① 把上架
    日期写进 SKU,与货源隐匿目标直接冲突;② 每轮重发取新序号 ⇒ 载荷漂 ⇒
    api/feeds 的 payload_key 在途防重失效;③ 留着就是一条随时会被误用的第二
    路径,而误用不报错。存量 PHUMWMT 行不受影响(读路径全格式通吃)。
    """
    from services import match_feed
    for gone in _DEAD_GENERATORS:
        assert not hasattr(match_feed, gone), gone
    offenders = [f"{rel}:{sorted(set(hits))}" for rel, path in _prod_files()
                 if (hits := _second_generator_hits(path))]
    assert not offenders, _fmt(offenders, "第二条发码路径复活了(只准 sku_codec.mint):")


# ══════════════════════════════════════════════════════════════════════════════
#  ⑩ 回执码不写字面量(B2-32)
# ══════════════════════════════════════════════════════════════════════════════

#: 回执事件码 `{kind}_feed_{status}` 的具名常量之家。
_RECEIPT_CODE_HOME = "services/product_events.py"


def test_no_receipt_code_literals_in_business_sql():
    """回执码只准由 product_events 的常量拼进 SQL,**不写字面量**。

    _FEED_KIND 一改取值,写字面量的那条 SQL 会**静默返回空集** —— 闸门形同
    虚设而且不报错(list_new 的退役冷却闸、product_events 的删除核验起点都
    读它)。回执码是推导出来的,所以纪律在这里天然破功,补两个具名常量是
    最小修法。
    """
    pat = re.compile(r"_feed_(success|failed)")
    offenders = [f"{rel}:{n} {line.strip()[:80]}"
                 for rel, path in _prod_files() if rel != _RECEIPT_CODE_HOME
                 for n, line in enumerate(
                     path.read_text(encoding="utf-8").splitlines(), 1)
                 if pat.search(line)]
    assert not offenders, _fmt(
        offenders, f"回执码字面量:改引用 {_RECEIPT_CODE_HOME} 的具名常量:")


# ══════════════════════════════════════════════════════════════════════════════
#  ⑥ 编码规则:一份字母表,一条活码索引,一条回填口径
# ══════════════════════════════════════════════════════════════════════════════

_OPAQUE_CLASS_RE = re.compile(r"sku ~ '\^\[([^\]]+)\]\{(\d+)\}\$'")


def test_schema_opaque_predicate_matches_the_codec_alphabet():
    """schema.sql 的部分索引条件与 sku_codec 的常量必须**逐字一致**。

    不一致 = 索引和代码对"什么是新码"的判断不同:代码认为是新码的行落不进
    唯一索引(并发双 mint 就拦不住),或者反过来把存量行拦在索引里让 db_init
    整份回滚(一次 execute,一条失败全份回滚)。
    """
    hits = _OPAQUE_CLASS_RE.findall(_SCHEMA)
    assert len(hits) == 2, f"不透明码形态条件应恰好两条(两条唯一索引),实得 {hits}"
    for chars, length in hits:
        assert chars == sku_codec._ALPHABET, (chars, sku_codec._ALPHABET)
        assert int(length) == sku_codec._LEN
    # 「至少一个字母」那半条不能漏:漏了,12 位纯数字的沃尔玛 item id 会落进
    # 「新码」唯一索引,与 sku_codec.is_opaque 的判据不一致。
    for name in ("listing_sources_opaque_sku_uidx", "listing_sources_live_uidx"):
        stmt = _SCHEMA[_SCHEMA.index(name):]
        stmt = stmt[:stmt.index(";")]
        assert "AND sku ~ '[A-Z]'" in stmt, name


def test_the_opaque_alphabet_is_born_only_in_sku_codec():
    """字母表只准在 services/sku_codec.py 出生(决策 E)。

    registry / sku_asin / 横切包各放一份的方案被实测判死:三处并存会配出
    两条互斥的守门断言(schema 字符类 == registry 常量 vs == sku_codec 常量),
    不可能同时绿。registry 只登记 SKU_SOURCE_LETTERS(所有者要拍的取值)。
    """
    home = "services/sku_codec.py"
    assert sku_codec._ALPHABET in (ROOT / home).read_text(encoding="utf-8")
    offenders = [rel for rel, path in _prod_files()
                 if rel != home
                 and sku_codec._ALPHABET in path.read_text(encoding="utf-8")]
    assert not offenders, _fmt(
        offenders, f"12 位码的字母表只准长在 {home}(schema.sql 的索引条件除外,"
                   "由上一条用例与它对齐):")


def test_the_live_unique_index_is_named_once_and_carries_replaced_by():
    """活码唯一索引的名字与条件由批次 0a **一次建到位**,后续批次不许重建。

    防的是这个:某个批次写 `DROP INDEX IF EXISTS <另一个名字>` —— 打空、静默
    no-op,然后裸建一条无条件唯一索引,而 db_init 一次 execute 整份 schema.sql,
    那条索引在存量上必然建失败 ⇒ 整份回滚 ⇒ 生产建库直接停摆。
    """
    name = "listing_sources_live_uidx"
    assert _SCHEMA.count(name) == 1, "活码唯一索引名在 schema.sql 里出现了不止一次"
    stmt = _SCHEMA[_SCHEMA.index(name):]
    stmt = stmt[:stmt.index(";")]
    assert "replaced_by IS NULL" in stmt        # 批次 3 因此不必重建它
    assert "abandoned_at IS NULL" in stmt
    assert "source_key IS NOT NULL" in stmt
    assert "DROP INDEX" not in _SCHEMA
    # mint 的复用查询与 live_key_idx 的局部条件必须逐字对齐,否则用不上索引
    key_idx = _SCHEMA[_SCHEMA.index("listing_sources_live_key_idx"):]
    key_idx = key_idx[:key_idx.index(";")]
    assert "WHERE abandoned_at IS NULL AND replaced_by IS NULL" in key_idx
    assert "abandoned_at IS NULL AND replaced_by IS NULL" in sku_codec._SQL_LIVE


def test_generation_index_exists_in_schema():
    """代际上限闸的索引必须在 schema.sql 里(批次 2 唯一新增的一条)。

    list_new 每轮按 (店, 来源, 源头键) 数已弃码行数;没有它就是每轮全表扫
    listing_sources。局部条件取 IS NOT NULL 而不是全表索引:活码行是绝大多数,
    把它们装进这个索引没有任何查询会用到。
    """
    name = "listing_sources_abandoned_idx"
    assert _SCHEMA.count(name) == 1, "新索引名在 schema.sql 里出现了不止一次"
    stmt = _SCHEMA[_SCHEMA.index(f"CREATE INDEX IF NOT EXISTS {name}"):]
    stmt = stmt[:stmt.index(";")]
    assert "(store, source_type, source_key)" in stmt   # 与 GROUP BY 同键
    assert "WHERE abandoned_at IS NOT NULL" in stmt
    # 与守门③不冲突:DDL 的局部条件不计入 `abandoned_at IS NULL` 那张白名单
    assert "abandoned_at IS NULL" not in stmt


def test_backfill_regex_agrees_with_sources_backfill():
    """db_init 的存量回填与生产在跑的 sources_backfill 是**同一条口径**。

    缺右锚会把 B0XXXXXXXX-2 这类「重上后缀」SKU 判成 amz 并把 source_key 截成
    前 10 位,身份键与 SKU 从此不等 —— 而那批行会因此第一次进入维护链的删除
    意图产出面。这不是理论缺口:0a 的验收本身就要跑 db_init。
    """
    from workflows import sources_backfill
    block = _SCHEMA[_SCHEMA.index("INSERT INTO catalog.listing_sources"):]
    block = block[:block.index(";")]
    shapes = re.findall(r"sku ~ '([^']+)'", block)
    assert shapes and len(set(shapes)) == 1, f"回填的两处判型必须同一条正则:{shapes}"
    assert shapes[0].startswith("^") and shapes[0].endswith("$"), shapes[0]
    assert "left(sku" not in block, "amz 分支必须整串入 source_key,不许截断"
    assert "THEN sku END" in block
    pat = sources_backfill._ASIN_RE.pattern
    assert pat.startswith("^") and pat.endswith("$"), pat


# ══════════════════════════════════════════════════════════════════════════════
#  ⑦ 登记簿那一跳的归属与订单链的唯一取数口(批次 0b)
# ══════════════════════════════════════════════════════════════════════════════

def _body(path: Path) -> str:
    """输入:.py 路径 → 输出:去掉**模块 docstring** 之后的源码。

    射程是代码不是文档:清洗工作流的模块头必须写清"这一跳查的是登记簿"
    (那正是我们要的文档),扫那个词本身没有意义 —— 与 abandoned_at 那条
    "判它被当条件用还是被写文档"同一条纪律。
    """
    src = path.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src))
    return src.replace(doc, "", 1) if doc else src


def test_registry_hop_lives_in_services_only():
    """钉的是:**Python 逐对反查(resolve_many)只准住在 services**。

    违反了会这样静默出事:清洗/审核工作流各写一份 JOIN 或各调一次 resolve_many,
    规则扩充时漏改没人调的那份不报错 —— 摘要正常、功能悄悄没了。
    全表级取数走 SQL 侧 LEFT JOIN 是**另一件事**(见 _REGISTRY_SQL_OK 的说明)。
    """
    offenders: list[str] = []
    for rel, path in _prod_files():
        if not rel.startswith("workflows/"):
            continue
        body = _body(path)
        if "resolve_many" in body:
            offenders.append(f"{rel}(resolve_many:工作流只准用 resolve_pairs)")
        if "catalog.listing_sources" in body and rel not in _REGISTRY_SQL_OK:
            offenders.append(f"{rel}(catalog.listing_sources)")
    assert not offenders, _fmt(
        offenders, "登记簿那一跳只准住在 services(工作流走 sku_asin.resolve_pairs):")


def test_only_cleaner_workflows_call_resolve_pairs():
    """钉的是:resolve_pairs 是**清洗类**工作流的入口,不是通用便利函数。

    违反了会这样静默出事:它比 resolve_many 多一跳查 walmart_items 的倒查,
    在实时链路上逐行调等于每行两条 SQL —— 功能对、账单和延迟悄悄翻倍。
    """
    allowed = {"workflows/sku_normalize.py", "workflows/order_asin_normalize.py"}
    offenders = [rel for rel, path in _prod_files()
                 if rel.startswith("workflows/") and rel not in allowed
                 and "resolve_pairs" in _body(path)]
    assert not offenders, _fmt(
        offenders, f"resolve_pairs 只给这两条清洗工作流:{sorted(allowed)}:")


def test_both_cleaners_fill_sql_carry_store():
    """钉的是:两条清洗器的 _FILL_SQL 都按 (店, sku) 定位行。

    违反了会这样静默出事:同一串 sku 在两家店切码后指向不同产品,不带 store
    的 UPDATE 会把 A 店的 asin 写到 B 店的行上;而 `=` 写法会漏掉
    product_events 的 store=NULL 行(平台级事件),两种都不报错。
    """
    from workflows import order_asin_normalize as oan
    from workflows import sku_normalize as sn
    for wf in (sn, oan):
        assert "DISTINCT store, sku" in wf._DISTINCT_SQL, wf.__name__
        assert "unnest(%s::text[]) AS store" in wf._FILL_SQL, wf.__name__
        assert "IS NOT DISTINCT FROM" in wf._FILL_SQL, wf.__name__


def test_order_chain_derives_the_asin_in_exactly_one_place():
    """钉的是:订单链上"这一行的 ASIN 是什么"只有一份实现(rules.line_asin)。

    违反了会这样静默出事:_snapshots / _scrape_fails / _judge_all / _phish_record
    四处各算各的,want 清单里的键与 judge 算出的键对不上 —— 快照取回来了但
    判定说没有,行永远挂"待采集"等一个不会来的快照,每轮还烧一次配额。
    """
    from services import order_audit as rules
    order_chain = [(rel, path) for rel, path in _prod_files()
                   if "order_audit" in rel or "order_lines" in rel]
    offenders = [rel for rel, path in order_chain
                 if rel != "services/order_audit.py"
                 and re.search(r"\^B\[0-9A-Z\]\{9\}\$", path.read_text(encoding="utf-8"))]
    assert not offenders, _fmt(offenders, "ASIN 形态闸只准长在 services/order_audit:")
    wf_src = (ROOT / "workflows" / "order_audit.py").read_text(encoding="utf-8")
    assert "extract_asin" not in wf_src and "sku_asin" not in wf_src
    assert callable(rules.line_asin)


def test_feed_track_does_not_resolve_asin_itself():
    """钉的是:违禁回执只把 store + sku 原样递给 blacklist.record_asins。

    违反了会这样静默出事:在 feed_track 再解一次 ASIN 就是第二份规则,
    两处口径一旦分叉,黑名单键与拦截闸查的键对不上 —— 违禁品照样上架。
    """
    src = (ROOT / "services" / "feed_track.py").read_text(encoding="utf-8")
    for banned in ("resolve_many", "extract_asin", "listing_sources"):
        assert banned not in src, banned


# ══════════════════════════════════════════════════════════════════════════════
#  ⑦ 上架表:列字母不许写死,"这一行的 SKU 是什么"只许有一个出处
#
#  所有者 2026-09-02 第二次重排上架表表头(SKU 插进 C 列、理由拆两列、尾部
#  换两列人工列),要求「以后再调整列顺序也能准确写入」。做法是
#  services/listing_sheet.layout():读一次表头行,按 registry 登记的中文表头
#  认列,所有 range 由它算。守门要钉死的就是这条路径不许被绕过。
# ══════════════════════════════════════════════════════════════════════════════

#: 上架表读写积木本体 —— 列字母只准在它内部由 layout 算出来。
_LISTING = "services/listing_sheet.py"
#: 会问"这一行的 SKU 是什么"的三个文件。
_ROW_SKU_CONSUMERS = (_LISTING, "workflows/list_new.py",
                      "workflows/sku_locked_heal.py")

#: f-string 里紧挨着插值的列字母(`f"K{r}:Q{r}"` 这种)。前面必须是非字母,
#: 免得把 "Unknown {n}" 里的 n 当成列字母。
_HARDCODED_COL_RE = re.compile(r"(?<![A-Za-z])[A-Z]{1,2}\{\}")
#: 整串就是一个 A1 范围的字面量(`"K2:Q2"` / `"C7"`)。
_LITERAL_RANGE_RE = re.compile(r"^[A-Z]{1,2}\d+(:[A-Z]{1,2}\d+)?$")


def _string_literals(path: Path) -> list[tuple[int, str]]:
    """输入:.py 路径 → 输出:[(行号, 字面量文本)];f-string 的插值段记成 `{}`。"""
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.JoinedStr):
            out.append((node.lineno, "".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str) else "{}"
                for part in node.values)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def test_listing_sheet_has_no_hardcoded_column_letters():
    """上架表的列字母只准由 `layout()` 算,源码里一个写死的都不许有。

    这是 2026-09-02 重排的直接教训:此前写入侧全是 `f"K{行}:Q{行}"` 这类
    硬编码段,所有者在中间插一列,**全体静默错位**(标题盖掉 SKU 列),
    一行报错都不会有。改成按表头名定位之后,唯一还允许出现的字母是读取
    起点 "A"(表格左边界)与表头行的 `A1:…1`,两者都不随列序变。
    """
    offenders = []
    for lineno, text in _string_literals(ROOT / _LISTING):
        if _HARDCODED_COL_RE.search(text) or _LITERAL_RANGE_RE.match(text):
            offenders.append(f"{_LISTING}:{lineno} {text[:60]!r}")
    assert not offenders, _fmt(
        offenders, "上架表写死了列字母(改用 layout()/_ranges 算):")


def test_the_row_sku_fallback_lives_in_exactly_one_place():
    """「SKU 列为空就回落 ASIN」这句话只准写一次(`listing_sheet.row_sku`)。

    回执找行、Unknown 自愈、退役载荷、冷却键、mark_used 问的是同一个问题。
    散着写五份 `r["sku"] or r["asin"]`,批次 2 切码时漏改任何一份都是静默
    失效:回执永不回填、退役发错码退不到,摘要还一切正常。
    """
    offenders = []
    for rel in _ROW_SKU_CONSUMERS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        skip = range(0)                       # row_sku 自身(连同它的头注)不算
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef) and node.name == "row_sku":
                skip = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        for n, line in enumerate(text.splitlines(), 1):
            if n in skip:
                continue
            if re.search(r'\["sku"\]\s*or|\.get\("sku"\)\s*or', line):
                offenders.append(f"{rel}:{n} {line.strip()[:80]}")
    assert not offenders, _fmt(
        offenders, "第二份「SKU 空则回落 ASIN」表达式(改调 listing_sheet.row_sku):")


# ── 列定位的行为面:fail-closed、顺序打乱、永不碰所有者的三列 ──────────────

_HEADERS = ["店铺", "ASIN", "SKU", "walmart上架标题", "walmart_product_type",
            "审核结果", "类别", "具体内容", "审核日期", "amz价格", "库存",
            "walmart价格", "是否上架", "上架feedid", "上架日期", "未上架理由",
            "上架结果", "报错", "feed查询日期", "登记日期", "查询编码"]

#: 程序永不写的三列:「类别」归另一条 PR,「登记日期」「查询编码」人工填。
_NEVER_WRITTEN = ("audit_category", "registered_date", "query_code")


def _wire_header(monkeypatch, header_row):
    """输入:表头行 → 输出:无。让 listing_sheet 认这一行表头(清缓存后重读)。"""
    from registry.resources import Spreadsheet
    from registry import resources
    from services import listing_sheet
    monkeypatch.setattr(resources, "LISTING_SHEET", Spreadsheet(
        name="上架表", token="TOK", sheet_id="SID",
        columns=resources.LISTING_SHEET.columns,
        headers=dict(resources.LISTING_SHEET.headers)))
    monkeypatch.setattr(listing_sheet.feishu, "sheet_values_small",
                        lambda sheet, rng: [list(header_row)])
    monkeypatch.setattr(listing_sheet, "_LAYOUT", None)


def _col_num(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n


def _all_writes(monkeypatch) -> list[tuple[str, list]]:
    """输入:无 → 输出:上架表全部写函数各跑一次产出的 [(range, 值矩阵)]。"""
    from services import listing_sheet as ls
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(ls.feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    monkeypatch.setattr(ls.feishu, "sheet_ensure_rows", lambda s, n: 0)
    monkeypatch.setattr(ls, "read_rows", lambda upto=None: [])
    ls.append_assignments([("T1", "B0AAAA0001")])
    ls.write_submit_cols([(2, ["标题", "9.9", 3, "19.9", "Yes", "F1", "d", ""])])
    ls.write_submit_cols([(3, ["标题", "9.9", 3, "19.9", "Yes", "F1", "d", "",
                               "A0X1Y2Z3W4V5"])])
    ls.write_sku_col([(4, "A0X1Y2Z3W4V5")])
    ls.write_data_cols([(5, ["标题", "9.9", 3, "19.9"])])
    ls.write_audit_cols([(6, ["标题", "Cups", "pass", "", "2026-09-02"])])
    ls.write_audit_notes([(7, "未采集")])
    ls.write_reasons([(8, "价格不合适")])
    ls.clear_for_relist([9])
    return sent


def test_listing_writes_never_touch_the_owner_only_columns(monkeypatch):
    """全部写函数的 range 一律避开 类别 / 登记日期 / 查询编码 三列。

    「类别」由另一条 PR 写(两处都写 = 互相覆盖);「登记日期」「查询编码」
    是所有者手填的列,程序碰一下就是把人工数据擦掉,而且没人会发现。
    """
    from services import listing_sheet as ls
    _wire_header(monkeypatch, _HEADERS)
    banned = {ls._index_map()[f] for f in _NEVER_WRITTEN}
    hit = []
    for rng, _vals in _all_writes(monkeypatch):
        a, _, b = rng.partition(":")
        lo = _col_num(re.match(r"[A-Z]+", a).group())
        hi = _col_num(re.match(r"[A-Z]+", b or a).group())
        hit += [f"{rng} 覆盖第 {c} 列" for c in banned if lo <= c <= hi]
    assert not hit, _fmt(hit, "写入范围碰了不许碰的列:")


def test_listing_writes_follow_a_shuffled_header(monkeypatch):
    """所有者把列顺序打乱,写入照样落到对的列 —— 代码一行都不用改。

    这就是「按表头名定位」的全部目的。这里把 SKU 挪到最后一列、把
    「未上架理由」挪到最前面,断言每个值都还在自己那一格。
    """
    from services import listing_sheet as ls
    shuffled = (["未上架理由"] + [h for h in _HEADERS if h != "未上架理由"
                                  and h != "SKU"] + ["SKU"])
    _wire_header(monkeypatch, shuffled)
    lay = ls.layout()
    assert lay["not_listed_reason"] == "A" and lay["sku"] == "U"
    sent: list = []
    monkeypatch.setattr(ls.feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    ls.write_reasons([(4, "价格不合适")])
    ls.write_sku_col([(4, "A0X1Y2Z3W4V5")])
    assert sent == [("A4:A4", [["价格不合适"]]),
                    ("U4:U4", [["A0X1Y2Z3W4V5"]])]


@pytest.mark.parametrize("bad, why", [
    ([h for h in _HEADERS if h != "SKU"], "缺列"),
    (_HEADERS + ["ASIN"], "重复"),
])
def test_listing_layout_is_fail_closed_on_a_broken_header(monkeypatch, bad, why):
    """表头缺一列或有重名 → 抛错拒绝读写,**一格都不写**。

    宁可这一轮不跑:列认不准就会把值写进别人的列,而且不报错(2026-09-02
    重排前那套硬编码字母正是这么错位的)。
    """
    from services import listing_sheet as ls
    _wire_header(monkeypatch, bad)
    monkeypatch.setattr(ls.feishu, "sheet_write_ranges",
                        lambda s, ups: pytest.fail(f"{why} 的表头还敢写"))
    with pytest.raises(LookupError):
        ls.layout()
    with pytest.raises(LookupError):
        ls.write_reasons([(2, "理由")])


def test_listing_layout_only_warns_about_columns_it_does_not_know(caplog):
    """所有者自己加的列不算错:只告警,照常工作(多出的列程序看不见)。"""
    from services import listing_sheet as ls
    import logging
    mp = pytest.MonkeyPatch()
    try:
        _wire_header(mp, _HEADERS + ["所有者自己加的列"])
        with caplog.at_level(logging.WARNING, logger="services.listing_sheet"):
            lay = ls.layout()
        assert lay["query_code"] == "U"          # 登记的列一个没错位
        assert "所有者自己加的列" in caplog.text
    finally:
        mp.undo()


class _EmptyReg:
    """登记簿一行都查不到(= 今天库里未登记 / 非 amz 的存量行)。"""

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None): self.sql = sql

    def fetchall(self): return []

    def cursor(self): return self


#: 仓内已知的全部存量形态(裸 ASIN / 三段式 / 前缀含数字的三段式 / 纯数字
#: item id / PHUMWMT 人工号 / 认不出的怪东西 / 空)。
_LEGACY_SHAPES = ("B0GXX75JN5", "XKJ-B0GXX75JN5-39.98", "A109-B08QF9XLMH-02",
                  "102460018738", "PHUMWMT20240815001", "怪东西", "")


def test_legacy_shapes_resolve_identically():
    """钉的是:**本批次「零行为变化」的机器证明** —— 未登记的存量行经
    resolve_many 与 extract_asin 输出逐字相同,两个方向都钉。

    违反了会这样静默出事:若 resolve_many 对已登记的非 amz 行直接返 None
    (今天 sources_backfill 把三段式 sku 登记成 unknown、source_key=NULL),
    那些行的 asin 会在事件账本 / 黑名单 / 订单三处同时变 NULL —— 三条链一起
    失明,而且全部不报错。
    """
    from services import sku_asin
    got = sku_asin.resolve_many(_EmptyReg(), [("T1", s) for s in _LEGACY_SHAPES])
    for s in _LEGACY_SHAPES:                       # 正向:每一个形态同值
        assert got.get(("T1", s)) == sku_asin.extract_asin(s), s
    # 反向:extract_asin 提得出的,resolve_many 一个不少(也一个不多)
    assert {k for _st, k in got} == {s for s in _LEGACY_SHAPES
                                     if sku_asin.extract_asin(s)}


# ══════════════════════════════════════════════════════════════════════════════
#  白名单不许烂掉
# ══════════════════════════════════════════════════════════════════════════════

_ALL_WHITELISTS = {
    "_HARD_EQUALITY_OK": _HARD_EQUALITY_OK,
    "_EXTRACT_ASIN_OK": _EXTRACT_ASIN_OK,
    "_REGISTRY_SQL_OK": _REGISTRY_SQL_OK,
    "_ABANDONED_AT_OK": _ABANDONED_AT_OK,
    "_LISTING_SOURCES_UPDATE_OK": _LISTING_SOURCES_UPDATE_OK,
    "_LISTING_SOURCES_INSERT_OK": _LISTING_SOURCES_INSERT_OK,
    "_ABANDON_CALLERS_OK": _ABANDON_CALLERS_OK,
}


def test_the_whitelists_do_not_rot():
    """每一条都要还指得着东西,还要写得出理由与预期收口批次。

    指空了就是该删的历史,不是豁免;理由为空的条目下一个人不敢删,于是白名单
    只增不减,越攒越像筛子。
    """
    stale: list[str] = []
    for wl_name, wl in _ALL_WHITELISTS.items():
        for rel, entry in wl.items():
            assert isinstance(entry, tuple) and len(entry) == 2, f"{wl_name}[{rel}]"
            batch, reason = entry
            assert reason.strip(), f"{wl_name}[{rel}] 没写理由"
            assert batch.strip(), f"{wl_name}[{rel}] 没写预期收口批次"
            if not (ROOT / rel).exists():
                stale.append(f"{wl_name}: {rel} 文件已不在")
    # 反向名单是 {路径: 理由} 的扁平形状,单独核一遍(指空了同样是该删的历史)
    for rel, why in _ABANDON_FORBIDDEN.items():
        assert why.strip(), f"_ABANDON_FORBIDDEN[{rel}] 没写理由"
        if not (ROOT / rel).exists():
            stale.append(f"_ABANDON_FORBIDDEN: {rel} 文件已不在")
    assert not stale, "白名单有失效条目,删掉它们:\n  " + "\n  ".join(stale)


# ══════════════════════════════════════════════════════════════════════════════
#  沙箱 PG 集成:两条部分唯一索引的方向(照抄 tests/test_risk_trace.py 的写法)
#
#  ⚠ 这里的地址是**测试夹具**,不是生产资源(生产走 registry/db.pg_dsn())。
#  固定在非标准端口 55432 上正是为了不可能连到生产库;造的数据全在一个最后
#  回滚的事务里,不留残渣。
# ══════════════════════════════════════════════════════════════════════════════

_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data"


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")

_LEGACY, _OPAQUE = "B0GUARD0001", "AGUARD234567"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。"""
    import os
    monkeypatch.setenv("WALMART_PG_DSN", os.environ.get("WALMART_TEST_PG_DSN", _DSN))
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _insert(conn, store, sku, source_key="B0KEYGUARD1", source_type="amz"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.listing_sources "
            "(store, sku, source_type, source_key, workflow) "
            "VALUES (%s, %s, %s, %s, 'test_sku_guard')",
            (store, sku, source_type, source_key))


@needs_pg
def test_pg_two_stores_may_share_a_legacy_sku_but_never_a_new_code(pg):
    """全局 sku 唯一**只对新码生效**:存量 sku=asin 跨店重复是既成事实,
    无条件唯一在存量上一定建不起来(而 db_init 一条失败整份回滚)。
    跨店永不复用新码 —— 两家店同一个码串在沃尔玛合法,但那正是"两家店有关联"
    的信号,而关联就是封号线。"""
    import psycopg
    _insert(pg, "GUARD_A", _LEGACY)
    with pg.transaction():                      # 存量形态:两店同 SKU 允许
        _insert(pg, "GUARD_B", _LEGACY, source_key="B0KEYGUARD2")
    _insert(pg, "GUARD_A", _OPAQUE, source_key="B0KEYGUARD3")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg.transaction():
            _insert(pg, "GUARD_B", _OPAQUE, source_key="B0KEYGUARD4")


@needs_pg
def test_pg_two_live_rows_may_share_a_legacy_key_but_never_a_minted_one(pg):
    """活码键唯一拦并发双 mint,同样只对新码生效:存量 match 行同一 GTIN 可能
    挂过多个人工号,限死形态后对 mint 的保护仍然是完整的。"""
    import psycopg
    _insert(pg, "GUARD_C", "B0GUARD0002", source_key="B0SHAREKEY")
    with pg.transaction():                      # 存量形态:同键两活行允许
        _insert(pg, "GUARD_C", "B0GUARD0003", source_key="B0SHAREKEY")
    _insert(pg, "GUARD_C", "AGUARD234568", source_key="B0MINTEDKEY")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with pg.transaction():
            _insert(pg, "GUARD_C", "AGUARD234569", source_key="B0MINTEDKEY")
