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
_EXTRACT_ASIN_OK: dict[str, tuple[str, str]] = {
    "services/sku_asin.py": (
        "permanent", "规则自身之家:extract_asin 与 pick_asin 都长在这里"),
    "workflows/order_history_import.py": (
        "permanent", "只导旧库历史数据,那批行的 SKU 永远是存量形态"),
    "workflows/pt_backfill.py": (
        "permanent", "只读旧 walmart_cleanup 库,同上"),
    "services/order_lines.py": ("0b", "订单行身份收口在批次 0b"),
    "services/product_events.py": ("0b", "record_many 写 asin 列,收口在批次 0b"),
    "services/blacklist.py": ("0b", "ASIN 黑名单键,收口在批次 0b"),
    "workflows/order_audit.py": ("0b", "审核取 ASIN,收口在批次 0b"),
    "workflows/order_asin_normalize.py": ("0b", "只在 docstring 里提及,随 0b 一起改"),
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
#  白名单不许烂掉
# ══════════════════════════════════════════════════════════════════════════════

_ALL_WHITELISTS = {
    "_HARD_EQUALITY_OK": _HARD_EQUALITY_OK,
    "_EXTRACT_ASIN_OK": _EXTRACT_ASIN_OK,
    "_ABANDONED_AT_OK": _ABANDONED_AT_OK,
    "_LISTING_SOURCES_UPDATE_OK": _LISTING_SOURCES_UPDATE_OK,
    "_LISTING_SOURCES_INSERT_OK": _LISTING_SOURCES_INSERT_OK,
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
