"""policy_sync 回归:转录件解析 / 对行归一化 / upsert 口径(全程不触网、不触真库)。

这条工作流写的是 **L3 的判定输入**。它出错的三种形态都不会自己报红:

  · 解析歪了 → `full_policy` 写成半截或空,L3 的政策段悄悄少一块;
  · 对行猜错 → A 政策的正文写进 B 行,判定从此指着错的政策名;
  · 手一滑写了人工列 → 运营维护的中文列被英文覆盖,而飞书那边看不出来;
  · 改名改歪 → `category_en` 是全链唯一键(§十.7),改错一行 = L3 的
    reason_category 白名单、S2 候选块、报错文本 join 三处一起指错。

所以断言分两路:**解析**打在 42 份真实转录件上(夹具骗不了自己),
**写库口径**用假连接钉死(SQL 列清单、id 分配、dry-run 零写)。
"""

import json

import pytest

from registry import paths, resources
from workflows import policy_sync as ps

_EN = paths.policy_pages_dir("en")
# 列 → information_schema.data_type(机器四列的类型是**报告判据**,写错了不炸:
# policy_updated_at 建成 text 照样写得进去,只是日期比较悄悄按字符串走)
_COL_TYPES = {
    "id": "integer", "category_en": "text", "category_zh": "text",
    "overall_status": "text", "preapproval": "text", "zh_seller_risk": "text",
    "prohibited_items": "text", "conditional_items": "text",
    "preapproval_items": "text", "legal_refs": "text", "zh_seller_notes": "text",
    "full_policy": "text", "official_url": "text",
    "policy_updated_at": "date", "raw": "jsonb",
    "synced_at": "timestamp with time zone",
}
_ALL_COLS = tuple(_COL_TYPES)

# 表内存量样本:六组词形差各占一行(§〇 实证)+ 一行官方已不含的幽灵。
# ⚠ 六组词形差在 2026-09-02 之后**都要改名**(表内名 := 官方拼写);
#   旧缩写名(POLICY_LEGACY_NAMES)另有 _LEGACY_TABLE 一组专测。
_TABLE = [
    (1, "Alcohol", "旧正文"),
    (2, "Cosmetics Products", "旧正文"),                       # 官方 Cosmetic
    (3, "Plants & Seeds", ""),                                 # 官方 and
    (4, "Stamps & Tickets", None),                             # 官方 and
    (5, "Tobacco, E-Cigarettes, and Vaping Products", "旧"),   # 官方无牛津逗号
    (6, "Jewelry, Watches, Precious Gemstones, Currency, Coins and "
        "Precious Metals", ""),                                # 官方带 (Covered Goods)
    (7, "Knives and other Melee Weapons", ""),                 # 官方 Other 大写
    (8, "Ghost Policy", "官方已不含"),
]


# ══════════════════════════════════════════════════════════════════════════
#  假连接
# ══════════════════════════════════════════════════════════════════════════

class _Cur:
    def __init__(self, store):
        self.store = store
        self._all: list = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store["ops"].append((sql, params))
        flat = " ".join(sql.split())
        if "information_schema.columns" in flat:
            self._all = [(c, self.store["types"].get(c, "text"))
                         for c in self.store["columns"]]
        elif "max(id)" in flat:
            self._one = (self.store["max_id"],)
        elif "FROM audit.walmart_prohibited_policy ORDER BY id" in flat:
            self._all = list(self.store["rows"])

    def fetchall(self):
        return self._all

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, rows=None, columns=None, max_id=None, types=None):
        rows = _TABLE if rows is None else rows
        self.store = {
            "rows": rows, "ops": [],
            "columns": list(_ALL_COLS) if columns is None else list(columns),
            "types": dict(_COL_TYPES, **(types or {})),
            "max_id": max(([r[0] for r in rows] or [0])) if max_id is None
            else max_id,
        }

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.store)


def _wire(monkeypatch, conn):
    monkeypatch.setattr(ps.db, "pg_conn", lambda *a, **k: conn)
    return conn


def _verbs(conn) -> list[str]:
    return [sql.split()[0].upper() for sql, _ in conn.store["ops"]]


def _writes(conn) -> list[tuple]:
    return [(sql, p) for sql, p in conn.store["ops"]
            if sql.split()[0].upper() in ("UPDATE", "INSERT")]


def _renames(conn) -> list[tuple]:
    """改名语句(_RENAME_SQL);与刷新同为 UPDATE,靠列清单区分。"""
    return [(sql, p) for sql, p in _writes(conn)
            if sql.split()[0].upper() == "UPDATE" and "category_en" in sql]


def _refreshes(conn) -> list[tuple]:
    return [(sql, p) for sql, p in _writes(conn)
            if sql.split()[0].upper() == "UPDATE" and "full_policy" in sql]


# ══════════════════════════════════════════════════════════════════════════
#  一、解析(打在 42 份真实转录件上)
# ══════════════════════════════════════════════════════════════════════════

def test_every_transcript_parses():
    """42 份逐个解析成功,类别名 / URL / 正文都非空 —— 一份坏了就点名。"""
    files = sorted(_EN.glob("*.md"))
    assert len(files) == 42, f"转录件份数变了({len(files)}),先核对 refdata"
    bad = []
    for p in files:
        try:
            rec = ps.parse_policy_file(p)
        except Exception as e:                              # noqa: BLE001
            bad.append(f"{p.name}: {e}")
            continue
        if not (rec["category_en"] and rec["official_url"].startswith("http")
                and rec["full_policy"].strip()):
            bad.append(f"{p.name}: 字段空")
    assert bad == [], bad


def test_every_transcript_yields_a_last_updated_date():
    """官方 Last Updated 三种写法(裸日期 / `Last updated on …` / 带尾注)都要抽得出。"""
    undated = [p.name for p in sorted(_EN.glob("*.md"))
               if ps.parse_policy_file(p)["policy_updated_at"] is None]
    assert undated == []


def test_general_use_products_header_variant():
    """⚠ 16-general-use 是登录门禁页:头注第 2 行带尾注、第 3 行是「转录来源」。

    只认「抓取(UTC)」那一行的解析器会在这一份上炸——而它恰好是所有者手工
    补录的那一份,炸了等于官方新政策进不了库。
    """
    rec = ps.parse_policy_file(_EN / "16-general-use-products.md")
    assert rec["category_en"] == "General-Use Products"
    assert "页面原文" in rec["last_updated_raw"]                 # 尾注确实在
    assert rec["policy_updated_at"].isoformat() == "2026-05-20"
    assert rec["header_fetched_at"] == "2026-09-01"              # 转录来源那行
    assert rec["full_policy"].startswith("**In this guide:**")


def test_title_may_be_followed_by_a_blank_line():
    """08 / 39 两份的 `# 标题` 与 `> 头注` 之间隔着空行(官方页转录的自然差异)。"""
    for name in ("08-children-s-products.md", "39-firearms.md"):
        rec = ps.parse_policy_file(_EN / name)
        assert rec["official_url"].startswith("https://")
        assert rec["policy_updated_at"] is not None


def test_body_is_kept_verbatim_including_chrome():
    """正文入库**原样**(chrome 行也留):清洗只在渲染层做一次(单一清洗路径)。"""
    rec = ps.parse_policy_file(_EN / "05-auto-and-motor-vehicles.md")
    assert rec["full_policy"].startswith("Guide")
    assert "Reading time: 3 min" in rec["full_policy"]
    assert "> 来源:" not in rec["full_policy"]        # 头注不进正文
    assert rec["chars"] == len(rec["full_policy"])


def test_undated_header_keeps_the_raw_text(tmp_path):
    """抽不到日期 → policy_updated_at 置 NULL,原文留在 raw.last_updated_raw。

    绝不用抓取日顶替:那会让「官方没改过」和「我们没读懂日期」长得一模一样。
    """
    f = tmp_path / "99-x.md"
    f.write_text("# X Policy\n> 来源: https://example.com/x\n"
                 "> 官方 Last Updated: 见页脚\n> 抓取(UTC): 2026-09-01\n\n"
                 "## Overview\n\nbody\n", encoding="utf-8")
    rec = ps.parse_policy_file(f)
    assert rec["policy_updated_at"] is None
    assert rec["last_updated_raw"] == "见页脚"
    assert json.loads(ps._raw_json(rec))["last_updated_raw"] == "见页脚"


@pytest.mark.parametrize("text,why", [
    ("没有标题行\n> 来源: https://x\n> 官方 Last Updated: Dec 1, 2025\n\nbody\n",
     "首行不是"),
    ("# X\n\n## Overview\n\nbody\n", "头注块"),
    ("# X\n> 官方 Last Updated: Dec 1, 2025\n\nbody\n", "来源 URL"),
    ("# X\n> 来源: https://x\n> 抓取(UTC): 2026-09-01\n\nbody\n", "Last Updated"),
    ("# X\n> 来源: https://x\n> 官方 Last Updated: Dec 1, 2025\n\n", "正文为空"),
])
def test_structural_failures_raise(tmp_path, text, why):
    f = tmp_path / "broken.md"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=why):
        ps.parse_policy_file(f)


# ══════════════════════════════════════════════════════════════════════════
#  二、对行归一化(§〇 六组词形差)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("official,in_table", [
    ("Cosmetic Products", "Cosmetics Products"),
    ("Plants and Seeds", "Plants & Seeds"),
    ("Tobacco, E-Cigarettes and Vaping Products",
     "Tobacco, E-Cigarettes, and Vaping Products"),
    ("Knives and Other Melee Weapons", "Knives and other Melee Weapons"),
    ("Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious "
     "Metals (Covered Goods)",
     "Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious Metals"),
    ("Stamps and Tickets", "Stamps & Tickets"),
])
def test_the_six_known_word_form_gaps_all_match(official, in_table):
    """§〇 逐条实证的词形差,六组必须全部对上 —— 对不上就是六行白白新增。"""
    assert ps.norm_category(official) == ps.norm_category(in_table)


def test_curly_apostrophe_and_plural_forms_fold_together():
    assert ps.norm_category("Children’s Products") == \
        ps.norm_category("Children's Products") == \
        ps.norm_category("Childrens Products")


def test_normalization_never_merges_two_different_official_names():
    """⚠ 归一化放得越宽,越可能把两个真不同的类别合成一行(写坏就是覆盖)。

    42 个官方名两两不撞是这条规则能用的前提。
    """
    keys = [ps.norm_category(ps.parse_policy_file(p)["category_en"])
            for p in sorted(_EN.glob("*.md"))]
    assert len(set(keys)) == len(keys)


def test_normalization_still_refuses_to_merge_abbreviations_by_itself():
    """⚠ 缩写差是**语义合并**,归一化一个字都不许沾 —— 认领它们的是
    `registry.resources.POLICY_LEGACY_NAMES`(所有者逐条裁决过的落纸),不是词形规则。

    这条守门是为了让"顺手把缩写也归一化了"的改法当场撞墙:归一化一旦放宽到
    语义,`Electronics & RF` 与 `Electronics and Radio Frequency Devices` 之外的
    别的缩写也会被无声吞掉,而那正是"把 A 政策正文写进 B 行"的入口。
    """
    assert ps.norm_category("Drugs and Drug Paraphernalia") != \
        ps.norm_category("Drugs & Paraphernalia")
    assert ps.norm_category("Electronics and Radio Frequency Devices") != \
        ps.norm_category("Electronics & RF")


def test_the_legacy_map_targets_are_verbatim_official_names():
    """⚠ 映射表的值必须与 refdata 头注 H1 **逐字一致** —— 差一个字符,改名就把
    表内名改成了一个官方并不存在的拼写,而全链拿它当唯一键。"""
    official = {ps.parse_policy_file(f)["category_en"]
                for f in sorted(_EN.glob("*.md"))}
    missing = sorted(v for v in resources.POLICY_LEGACY_NAMES.values()
                     if v not in official)
    assert missing == [], f"这些目标值不在 42 份转录件的 H1 里:{missing}"
    # 旧名本身不许与官方名撞车(撞了就说明它已经不是"旧"名了)
    assert not (set(resources.POLICY_LEGACY_NAMES) & official)


# ══════════════════════════════════════════════════════════════════════════
#  三、写库口径(假连接钉死)
# ══════════════════════════════════════════════════════════════════════════

def test_dry_run_touches_the_database_only_with_selects(monkeypatch):
    """dry-run **零写库**:假连接上只许出现 SELECT(ALTER/UPDATE/INSERT 都算写)。

    ⚠ 这里**不断言 commit**:提交发生在 `db.pg_conn` 的上下文管理器里,而这条
    测试把 `pg_conn` 整个换掉了 —— 假连接上的 commit 永远不会被调用,断言
    "没提交"恒真、挡不住任何回归。真正的防线是上面那句"只许出现 SELECT"。
    """
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    assert set(_verbs(conn)) == {"SELECT"}, conn.store["ops"]
    assert "一行未写库" in out


def test_real_run_updates_the_matched_rows_and_inserts_the_rest(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    verbs = _verbs(conn)
    # 七行对上 → 七条刷新;其中六行拼写与官方不同 → 六条改名(Alcohol 已是官方名)
    assert len(_refreshes(conn)) == 7
    assert len(_renames(conn)) == 6
    assert verbs.count("UPDATE") == 7 + 6
    assert verbs.count("INSERT") == 42 - 7
    assert out.splitlines()[0].startswith("新增 35 / 刷新 7 / 改名 6 / "
                                          "未对上 35 / 官方缺席 1")


def test_rename_runs_before_the_machine_column_upsert(monkeypatch):
    """改名排在机器列 upsert **之前**(同一事务):先把"这行叫什么"定下来。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    kinds = ["RENAME" if (s.split()[0].upper() == "UPDATE"
                          and "category_en" in s)
             else s.split()[0].upper() for s, _ in conn.store["ops"]]
    assert max(i for i, k in enumerate(kinds) if k == "RENAME") < \
        min(i for i, k in enumerate(kinds) if k in ("UPDATE", "INSERT"))


def test_rename_sql_only_ever_sets_category_en(monkeypatch):
    """⚠ 改名语句的列清单就是它的安全边界:只许 SET category_en,id 只在 WHERE。"""
    flat = " ".join(ps._RENAME_SQL.split())
    assert flat.startswith("UPDATE audit.walmart_prohibited_policy "
                           "SET category_en = %(category_en)s WHERE id = %(id)s")
    body = flat.split("SET", 1)[1].split("WHERE", 1)[0]
    for col in _ALL_COLS:
        if col != "category_en":
            assert col not in body, f"{col} 出现在改名语句的 SET 里"
    for col in ps._HUMAN_COLS:
        assert col not in flat

    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    for sql, params in _renames(conn):
        assert set(params) == {"id", "category_en"}


def test_renaming_keeps_the_id_and_matches_the_official_spelling(monkeypatch):
    """id 不变(旧结论、飞书投影、外部引用全挂在 id 上),名字换成官方拼写。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    got = {p["id"]: p["category_en"] for _s, p in _renames(conn)}
    assert got == {
        2: "Cosmetic Products",
        3: "Plants and Seeds",
        4: "Stamps and Tickets",
        5: "Tobacco, E-Cigarettes and Vaping Products",
        6: "Jewelry, Watches, Precious Gemstones, Currency, Coins and "
           "Precious Metals (Covered Goods)",
        7: "Knives and Other Melee Weapons",
    }
    # 表内名与官方拼写完全相同的那一行**不进改名清单**(不写无谓的 UPDATE)
    assert 1 not in got
    # 官方已不含的幽灵行照旧不动
    assert 8 not in got


def test_legacy_abbreviations_are_claimed_and_renamed(monkeypatch):
    """⚠ 存量缩写名(§十.6 那 7 行)经 POLICY_LEGACY_NAMES 认领 → 改名,
    **不再**走"新增一行 + 官方已不含一行"那条会写出同概念双行的路。"""
    rows = [(i + 1, legacy, "旧正文") for i, legacy
            in enumerate(sorted(resources.POLICY_LEGACY_NAMES))]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True})
    got = {p["id"]: p["category_en"] for _s, p in _renames(conn)}
    assert got == {i + 1: resources.POLICY_LEGACY_NAMES[legacy]
                   for i, legacy in enumerate(sorted(resources.POLICY_LEGACY_NAMES))}
    assert len(_refreshes(conn)) == 7               # 认领了就照常刷新正文
    assert out.splitlines()[0].startswith("新增 35 / 刷新 7 / 改名 7 / "
                                          "未对上 35 / 官方缺席 0")
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    block = text.split("▍将改名")[1].split("▍改名冲突")[0]
    assert "「Drugs & Paraphernalia」 → 「Drugs and Drug Paraphernalia」" in block
    assert "(经旧名认领)" in text


def test_an_unmatched_official_page_is_neither_renamed_nor_double_inserted(
        monkeypatch):
    """未对上的官方页只新增一行:不借道改名去动别的行,也不重复插。"""
    conn = _wire(monkeypatch, _Conn(rows=[(1, "Ghost Policy", "x")]))
    ps.run({"execute": True})
    assert _renames(conn) == []
    assert _refreshes(conn) == []
    ins = [p["category_en"] for s, p in _writes(conn)
           if s.split()[0].upper() == "INSERT"]
    assert len(ins) == 42 and len(set(ins)) == 42


def test_dry_run_never_leaks_a_rename(monkeypatch):
    """⚠ 改名也是写库:dry-run 的动词集合仍必须是 {SELECT}。"""
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    assert set(_verbs(conn)) == {"SELECT"}, conn.store["ops"]
    assert _renames(conn) == []
    assert "改名 6" in out.splitlines()[0]
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    assert "▍将改名" in text and "「Cosmetics Products」 → 「Cosmetic Products」" in text


def test_a_rename_conflict_is_held_and_writes_nothing(monkeypatch):
    """⚠ 目标官方名已被表内另一行占用 → 该行**不改名也不刷新**,进「改名冲突」。

    这是一道**后置断言**:同名行在对行那一步就已经因为归一化撞键进了 `held`,
    所以正常输入下这张清单恒空(下一条测试钉死)。它存在是因为
    `POLICY_LEGACY_NAMES` 可由所有者追加 —— 追错一条(两个旧名指到同一个官方名、
    或指到表里已有的另一类)时,必须有人在**写库之前**撞上这堵墙,而不是事后
    在表里发现一对同名行。这里直接钉守门本身:命中即扣留,一个字都不写。
    """
    refresh = [{"id": 3, "table_name": "Plants & Seeds", "via": "词形",
                "page": {"category_en": "Plants and Seeds", "file": "28.md"},
                "old_sha": "x", "old_chars": 1}]
    rows = [(3, "Plants & Seeds", ""), (9, "Plants and Seeds", "")]
    rename, conflict = ps.plan_renames(refresh, rows)
    assert rename == []
    assert conflict == [(3, "Plants & Seeds", "Plants and Seeds", [9])]

    # 端到端:守门一旦命中,那一行既不改名也不刷新,摘要与报告都点名
    monkeypatch.setattr(ps, "plan_renames",
                        lambda refresh, rows: ([], [(3, "Plants & Seeds",
                                                     "Plants and Seeds", [9])]))
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    assert _renames(conn) == []
    assert 3 not in [p["id"] for _s, p in _refreshes(conn)]
    assert "改名冲突 1 条" in out and "不改名也不刷新" in out
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    block = text.split("▍改名冲突")[1].split("▍未对上")[0]
    assert "id 9 占用" in block


def test_a_legacy_row_whose_official_name_is_taken_lands_in_rename_conflict(
        monkeypatch):
    """⚠ 复核场景 A:表里**同时**有旧缩写名与官方名两行(同一个政策)。

    旧写法是"词形没对上才查旧名":官方页先被 `Drugs and Drug Paraphernalia`
    那一行按词形认走,于是登记了旧名的 `Drugs & Paraphernalia` 那一行**谁也没
    点到**,直接掉进「官方已不含」—— 报告等于在说"官方删掉了这个类别",而真相
    是**表里有一对同概念双行等着合并**。判反的方向:人会去动库删行。

    正确形态:它进「改名冲突」(目标官方名已被 id 2 占用),不改名不刷新,
    也**不算官方缺席**;另一行照常刷新。
    """
    rows = [(1, "Drugs & Paraphernalia", "旧正文"),
            (2, "Drugs and Drug Paraphernalia", "旧正文")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True})

    assert _renames(conn) == []                       # 一个名字都没改
    assert [p["id"] for _s, p in _refreshes(conn)] == [2]   # 冲突行不刷新
    assert "改名冲突 1 条" in out and "不改名也不刷新" in out
    assert "官方已不含" not in out                     # ← 旧写法就是错在这里
    assert out.splitlines()[0].startswith("新增 41 / 刷新 1 / 改名 0 / "
                                          "未对上 41 / 官方缺席 0")
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    block = text.split("▍改名冲突")[1].split("▍未对上")[0]
    assert "「Drugs & Paraphernalia」 ↛ 「Drugs and Drug Paraphernalia」" in block
    assert "id 2 占用" in block
    assert "Drugs & Paraphernalia" not in \
        text.split("▍官方已不含")[1].split("▍解析失败(本轮不刷新)")[0]


def test_a_legacy_mapping_pointing_at_an_existing_row_lands_in_rename_conflict(
        monkeypatch):
    """⚠ 复核场景 C:映射表被追错 —— 某个旧名指到了表里**已经存在的另一类**。

    这正是 `POLICY_LEGACY_NAMES` 那句"所有者可追加"的风险面:追错一条不会报错,
    改下去就是把 A 政策的行改名成 B 政策(表里从此两行 Alcohol)。必须在写库
    **之前**撞墙 —— 而撞墙的位置就是对行那一步,不是事后在表里发现。
    """
    monkeypatch.setitem(resources.POLICY_LEGACY_NAMES, "Weird Old Name",
                        "Alcohol")
    rows = [(1, "Weird Old Name", "旧正文"), (2, "Alcohol", "旧正文")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True})

    assert _renames(conn) == []
    assert [p["id"] for _s, p in _refreshes(conn)] == [2]
    assert "改名冲突 1 条" in out
    assert out.splitlines()[0].startswith("新增 41 / 刷新 1 / 改名 0 / "
                                          "未对上 41 / 官方缺席 0")
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    block = text.split("▍改名冲突")[1].split("▍未对上")[0]
    assert "「Weird Old Name」 ↛ 「Alcohol」" in block and "id 2 占用" in block


def test_two_rows_registering_the_same_legacy_name_are_both_held(monkeypatch):
    """⚠ 两行登记同一个旧名(映射表把两个旧名指到同一个官方名,或表里本就有
    两行同名)→ **两行都不动**,进「改名冲突」;那张官方页也**不新增** ——
    在一对待合并的同概念行旁边再添第三行,是把问题变成三倍。"""
    rows = [(1, "Drugs & Paraphernalia", "a"), (5, "Drugs & Paraphernalia", "b")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True, "dry_run": True})

    assert set(_verbs(conn)) == {"SELECT"}
    assert "改名冲突 2 条" in out
    assert out.splitlines()[0].startswith("新增 41 / 刷新 0 / 改名 0 / "
                                          "未对上 41 / 官方缺席 0")
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    block = text.split("▍改名冲突")[1].split("▍未对上")[0]
    assert "id 5 占用" in block and "id 1 占用" in block
    # 官方页没被当成"未对上 → 新增"(41 条新增里没有它)
    assert "Drugs and Drug Paraphernalia" not in \
        text.split("▍新增")[1].split("▍对上")[0]


def test_a_rename_conflict_never_writes_the_held_row_even_on_a_real_run(
        monkeypatch):
    """⚠ 冲突行的安全边界是"零写":它的 id 不许出现在**任何**写语句的参数里。"""
    rows = [(1, "Drugs & Paraphernalia", "旧正文"),
            (2, "Drugs and Drug Paraphernalia", "旧正文")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    ps.run({"execute": True})
    assert 1 not in [p.get("id") for _s, p in _writes(conn)]


def test_the_real_transcripts_produce_no_rename_conflict(monkeypatch):
    """常态:42 份官方页 + 存量表,冲突清单恒空(有值就是映射表被追错了)。"""
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    assert "▍改名冲突" in text and "改名冲突" not in out
    assert text.split("▍改名冲突")[1].split("▍未对上")[0].strip().endswith("(无)")


def test_human_columns_never_appear_in_a_write(monkeypatch):
    """⚠ 人工列一律不读不写(§二):中文列被英文覆盖,飞书那边看不出来。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    for sql, _ in _writes(conn):
        for col in ps._HUMAN_COLS:
            assert col not in sql, f"{col} 出现在写语句里:{sql}"


def test_the_machine_column_update_still_never_touches_category_en(monkeypatch):
    """⚠ 改名只走 `_RENAME_SQL` 一条路(2026-09-02 起),机器列刷新语句照旧
    一个字都不碰 `category_en` —— 两件事混进一条 UPDATE,列清单就不再是
    "一眼见底"的安全边界了(而它正是人工列不被覆盖的那道边界)。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    for sql, params in _refreshes(conn):
        assert "category_en" not in sql
        assert "category_en" not in params


def test_update_writes_exactly_the_five_renewable_machine_columns(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    sql = _refreshes(conn)[0][0]
    for col in ("full_policy", "official_url", "policy_updated_at",
                "synced_at", "raw"):
        assert col in sql


def test_nothing_is_ever_deleted(monkeypatch):
    """表里有、官方没有 → **不删行**,只报告(§二)。"""
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    assert not [v for v in _verbs(conn) if v in ("DELETE", "TRUNCATE", "DROP")]
    src = ps.__file__ and open(ps.__file__, encoding="utf-8").read()
    assert "DELETE FROM" not in src and "TRUNCATE" not in src
    assert "Ghost Policy(id 8)" in out and "不删行" in out


def test_new_rows_take_consecutive_ids_from_max(monkeypatch):
    """id = max(id)+1 起连续分配,且在同一事务里算 —— 不留空洞、不撞主键。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    ids = [p["id"] for s, p in _writes(conn) if s.split()[0].upper() == "INSERT"]
    assert ids == list(range(9, 9 + 35))


def test_inserted_rows_carry_official_spelling_and_null_human_columns(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    ins = {p["category_en"]: p for s, p in _writes(conn)
           if s.split()[0].upper() == "INSERT"}
    assert "Firearms" in ins and "Knives and Other Melee Weapons" not in ins
    row = ins["Firearms"]
    assert set(row) == {"id", "category_en", "full_policy", "official_url",
                        "policy_updated_at", "raw"}
    raw = json.loads(row["raw"])
    assert raw["source"] == "refdata" and raw["file"] == "39-firearms.md"
    assert len(raw["content_sha256"]) == 64 and raw["chars"] == len(row["full_policy"])
    assert raw["header_fetched_at"] == "2026-09-01"


def test_ambiguous_table_names_are_held_not_guessed(monkeypatch):
    """表里两行归一化同名 → 本轮**一行都不动**,进未对上清单等人裁决。"""
    rows = [(1, "Alcohol", "a"), (2, "Alcohols", "b")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True})
    assert not [v for v in _verbs(conn) if v == "UPDATE"]
    assert "表内有 2 行同名" in out
    assert _renames(conn) == []                  # 不敢动 = 连名字也不动
    # 歧义行不算「官方已不含」——那是"点到了但不敢动",不是官方删了这个类别
    assert out.splitlines()[0].startswith("新增 41 / 刷新 0 / 改名 0 / "
                                          "未对上 42 / 官方缺席 0")


def test_parse_failure_is_isolated_and_named(monkeypatch, tmp_path):
    """一份坏了不炸整轮:摘要点名,该类别本轮不刷新(绝不写空值)。"""
    (tmp_path / "01-ok.md").write_text(
        "# Alcohol\n> 来源: https://example.com/a\n"
        "> 官方 Last Updated: Dec 10, 2025\n> 抓取(UTC): 2026-09-01\n\nbody\n",
        encoding="utf-8")
    (tmp_path / "02-broken.md").write_text("完全不是转录件\n", encoding="utf-8")
    monkeypatch.setattr(ps.paths, "policy_pages_dir", lambda lang="en": tmp_path)
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    assert "02-broken.md" in out and "解析失败" in out
    assert _verbs(conn).count("UPDATE") == 1        # 只刷新 Alcohol 那一行
    assert _verbs(conn).count("INSERT") == 0
    assert out.splitlines()[0].startswith("新增 0 / 刷新 1 / 改名 0 / "
                                          "未对上 0 / 官方缺席 7")


def test_a_broken_file_keeps_its_row_out_of_the_absent_list(monkeypatch, tmp_path):
    """⚠ 转录件坏了 ≠ 官方删了这一类。

    标题还读得出就凭它认领表内那一行:进「解析失败」小节,**不进「官方已不含」**。
    混在一起的后果是判反方向 —— 人会按"官方下架了"去动库,而该做的是修文件。
    """
    (tmp_path / "01-alcohol.md").write_text(
        "# Alcohol\n> 来源: https://example.com/a\n"
        "> 官方 Last Updated: Dec 10, 2025\n> 抓取(UTC): 2026-09-01\n\nbody\n",
        encoding="utf-8")
    (tmp_path / "02-cosmetics.md").write_text(          # 标题在,正文没了
        "# Cosmetic Products\n\n乱码乱码\n", encoding="utf-8")
    monkeypatch.setattr(ps.paths, "policy_pages_dir", lambda lang="en": tmp_path)
    conn = _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")

    absent = text.split("▍官方已不含")[1].split("▍解析失败(本轮不刷新)")[0]
    stale = text.split("▍解析失败(本轮不刷新)")[1].split("▍解析失败的转录件")[0]
    assert "Cosmetics Products" in stale                 # 表内名(词形差照样认领)
    assert "Cosmetics Products" not in absent
    assert "Ghost Policy" in absent                      # 真缺席的照旧在
    assert "不算官方已不含" in out
    assert out.splitlines()[0].startswith("新增 0 / 刷新 1 / 改名 0 / "
                                          "未对上 0 / 官方缺席 6")  # 8 - Alcohol - 该行
    assert set(_verbs(conn)) == {"SELECT"}


def test_a_titleless_broken_file_warns_inside_the_absent_section(monkeypatch,
                                                                 tmp_path):
    """标题都读不到时认不了行 —— 那就在「官方已不含」头上挂警示,别让人误判。"""
    (tmp_path / "01-alcohol.md").write_text(
        "# Alcohol\n> 来源: https://example.com/a\n"
        "> 官方 Last Updated: Dec 10, 2025\n> 抓取(UTC): 2026-09-01\n\nbody\n",
        encoding="utf-8")
    (tmp_path / "02-broken.md").write_text("完全不是转录件\n", encoding="utf-8")
    monkeypatch.setattr(ps.paths, "policy_pages_dir", lambda lang="en": tmp_path)
    _wire(monkeypatch, _Conn())
    ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    absent = text.split("▍官方已不含")[1].split("▍解析失败(本轮不刷新)")[0]
    assert "本轮有 1 份解析失败" in absent and "02-broken.md" in absent
    assert "不是官方删了这些类别" in absent


def test_all_files_failing_to_parse_fails_loud(monkeypatch, tmp_path):
    """⚠ 全军覆没不许空转报成功。

    一份都没解析出来还往下走,输出长得和"官方没变化"一模一样(新增 0 / 刷新 0),
    而真相是目录被清空或格式整体改了 —— 那是最坏的失败形态:安静且像成功。
    """
    (tmp_path / "01-broken.md").write_text("完全不是转录件\n", encoding="utf-8")
    (tmp_path / "02-broken.md").write_text("也不是\n", encoding="utf-8")
    monkeypatch.setattr(ps.paths, "policy_pages_dir", lambda lang="en": tmp_path)
    conn = _wire(monkeypatch, _Conn())
    with pytest.raises(RuntimeError, match="全部解析失败"):
        ps.run({"execute": True})
    assert conn.store["ops"] == []                  # 炸在碰库之前


def test_rename_candidates_are_flagged_between_the_two_lists(monkeypatch):
    """⚠「未对上」+「官方已不含」同时点到一个概念 = 多半是官方改了名。

    当成"新增一条 + 删掉一条"处理的后果不报错,是**同概念双行**:S4 会拿到
    两份讲同一件事的政策文本。判定不变(仍然不猜),但必须**点名**给人看。

    ⚠ 2026-09-02 起这张清单提示的是**还没进 `POLICY_LEGACY_NAMES`** 的拼写差
    (已进映射表的缩写名会直接被认领改名,见
    `test_legacy_abbreviations_are_claimed_and_renamed`)—— 它是所有者往映射表
    里追加条目的入口,不是被改名口径取代的旧提示。
    """
    rows = [(1, "Knives & Melee", "旧正文"),
            (2, "Firearms Ammo", "旧正文")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")

    assert "疑似改名对" in out and "同概念双行" in out
    assert out.splitlines()[1].startswith("⚠ 疑似改名对")     # 首行之后就点名
    block = text.split("▍疑似改名对")[1].split("▍官方已不含")[0]
    assert "Knives and Other Melee Weapons" in block and "id 1" in block
    assert "Firearm Ammunition" in block and "id 2" in block
    # 判定本身**不变**:两行都没被认领,照旧进新增 + 官方已不含,一行都不改名
    assert _verbs(conn) == ["SELECT"] * 3
    assert out.splitlines()[0].startswith("新增 42 / 刷新 0 / 改名 0 / "
                                          "未对上 42 / 官方缺席 2")


def test_rename_pairing_stays_quiet_when_nothing_overlaps(monkeypatch):
    """没有词形重合就一对都不报 —— 提示不能变成人人都得读的噪声。"""
    _wire(monkeypatch, _Conn())                    # 缺席的只有 Ghost Policy
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    assert "▍疑似改名对" in text and "(无 —— 两张清单没有词形上重合的候选)" in text
    assert "⚠ 疑似改名对" not in out


def test_column_type_mismatch_is_named_not_fixed(monkeypatch):
    """⚠ 类型歪了不炸也不报错:policy_updated_at 建成 text 照样写得进去,
    只是日期比较悄悄按字符串走。所以只点名 —— ALTER TYPE 可能截断存量,人来决定。"""
    conn = _wire(monkeypatch, _Conn(types={"policy_updated_at": "text",
                                           "raw": "text"}))
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    assert "▍类型不符" in text
    assert "policy_updated_at  实际 text  ≠  预期 date" in text
    assert "raw  实际 text  ≠  预期 jsonb" in text
    assert "policy_updated_at 是 text(预期 date)" in out
    assert set(_verbs(conn)) == {"SELECT"}
    assert "ALTER TYPE" not in "".join(sql for sql, _ in conn.store["ops"])


def test_correct_column_types_report_clean(monkeypatch):
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")
    assert "▍类型不符:(无,机器列类型都对)" in text
    assert "类型与预期不符" not in out


def test_missing_machine_columns_are_added_only_on_a_real_run(monkeypatch):
    """反推表可能缺机器列:dry-run 只报「将补列」,真跑才 ALTER(幂等)。"""
    cols = [c for c in _ALL_COLS if c not in ("raw", "official_url")]
    conn = _wire(monkeypatch, _Conn(columns=cols))
    out = ps.run({"execute": True, "dry_run": True})
    assert set(_verbs(conn)) == {"SELECT"}
    assert "真跑时补" in out and "official_url" in out and "raw" in out

    conn2 = _wire(monkeypatch, _Conn(columns=cols))
    ps.run({"execute": True})
    alters = [sql for sql, _ in conn2.store["ops"]
              if sql.split()[0].upper() == "ALTER"]
    assert len(alters) == 2
    assert all("ADD COLUMN IF NOT EXISTS" in a for a in alters)
    verbs = _verbs(conn2)
    assert verbs.index("ALTER") < verbs.index("UPDATE")      # 先补列再写


def test_missing_table_fails_loud(monkeypatch):
    _wire(monkeypatch, _Conn(columns=[]))
    with pytest.raises(RuntimeError, match="db_init"):
        ps.run({"execute": True})


def test_real_run_names_both_knock_on_consequences(monkeypatch):
    """⚠ 真跑的连带后果**都不会自己发生、也都不会报错** —— 摘要必须逐条点名。

      ① AUDIT_RULES_VERSION:**已由本批递增**,摘要要说清"首跑无需再手动递增",
         否则人会照旧手动再提一版,白白触发第二轮全量重审;
      ② 新增行人工中文列全 NULL ⇒ S4 渲染出空壳标题(有类别名、没判据)。

    原第三条(audit_l3 硬写「37 条」)已随本批动态化 —— 提示词不再有对不上的
    字面量,**这条提醒必须消失**:留着就是叫人去改一个已经不存在的问题。
    """
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    assert "AUDIT_RULES_VERSION" in out and "L3 输入已变更" in out
    assert "registry/resources.py" in out
    assert resources.AUDIT_RULES_VERSION in out and "首跑无需" in out
    assert "空壳标题" in out and "人工中文列全是 NULL" in out
    assert "连带后果两条" in out
    assert "37 条" not in out and "audit_l3.py" not in out
    # ⚠ 成本必须**写在摘要里**:政策表一改,L3 的 system prompt 就变,
    #   catalog.llm_cache 那批全量未命中 —— 与全量重审叠加就是全额重付。
    #   只写在文档里等于没写:跑的人看的是这段输出(所有者 2026-08-21 的
    #   峰谷价差同理,不点名就没人会把大重审排到谷时段)
    assert "全量未命中" in out and "全额重付" in out
    assert "谷时段" in out


def test_dry_run_tells_the_operator_what_to_eyeball(monkeypatch):
    """⚠ 报告里打了标记,没人被告知要看 = 白打。dry-run 摘要必须点名两处。"""
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    assert "人眼核对" in out
    assert "将改名" in out and "←官方名" in out
    assert "未对上" in out and "POLICY_LEGACY_NAMES" in out


def test_report_lands_in_reports_dir_on_both_paths(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True, "dry_run": True})
    report = paths.reports_dir() / ps._REPORT_FILE
    text = report.read_text(encoding="utf-8")
    for head in ("▍将补列", "▍类型不符", "▍新增", "▍对上", "▍将改名",
                 "▍改名冲突", "▍未对上", "▍疑似改名对", "▍官方已不含",
                 "▍解析失败(本轮不刷新)", "▍解析失败的转录件"):
        assert head in text, head
    assert "DRY-RUN,零写库" in text
    assert "sha " in text and "字" in text           # 对上清单带 sha 与字数变化
    assert conn.store["ops"]                          # 确实查过库


# ══════════════════════════════════════════════════════════════════════════
#  四、纪律
# ══════════════════════════════════════════════════════════════════════════

def test_is_marked_dangerous_and_stays_out_of_the_schedule():
    """写 L3 判定输入 = 危险;官方页低频变更 = 手动跑,不进调度(§三)。"""
    from registry import schedule
    assert ps.DANGEROUS is True
    labels = {j["label"] for j in schedule.JOBS}
    assert "policy_sync" not in labels
    for job in schedule.JOBS:
        assert "policy_sync" not in job.get("steps", [])


def test_no_argparse_no_direct_connect_no_hand_built_paths():
    """入口唯一 / 连接唯一 / 路径唯一(铁律 1 与 3)。"""
    src = open(ps.__file__, encoding="utf-8").read()
    assert "argparse" not in src
    assert "psycopg" not in src and "sqlite3" not in src
    assert "paths.policy_pages_dir(" in src         # 目录从 registry 取
    assert "pathlib" not in src and "Path(" not in src
