"""policy_sync 回归:转录件解析 / 对行归一化 / upsert 口径(全程不触网、不触真库)。

这条工作流写的是 **L3 的判定输入**。它出错的三种形态都不会自己报红:

  · 解析歪了 → `full_policy` 写成半截或空,L3 的政策段悄悄少一块;
  · 对行猜错 → A 政策的正文写进 B 行,判定从此指着错的政策名;
  · 手一滑写了人工列 → 运营维护的中文列被英文覆盖,而飞书那边看不出来。

所以断言分两路:**解析**打在 42 份真实转录件上(夹具骗不了自己),
**写库口径**用假连接钉死(SQL 列清单、id 分配、dry-run 零写)。
"""

import json

import pytest

from registry import paths
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

# 表内存量样本:六组词形差各占一行(§〇 实证)+ 一行官方已不含的幽灵
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


def test_abbreviated_table_names_stay_unmatched_on_purpose():
    """缩写差**故意**对不上:那是所有者裁决改名还是新增,不许在这儿猜。"""
    assert ps.norm_category("Drugs and Drug Paraphernalia") != \
        ps.norm_category("Drugs & Paraphernalia")
    assert ps.norm_category("Electronics and Radio Frequency Devices") != \
        ps.norm_category("Electronics & RF")


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
    assert verbs.count("UPDATE") == 7          # 六组词形差 + Alcohol
    assert verbs.count("INSERT") == 42 - 7
    assert out.splitlines()[0].startswith("新增 35 / 刷新 7 / 未对上 35 / "
                                          "官方缺席 1")


def test_human_columns_never_appear_in_a_write(monkeypatch):
    """⚠ 人工列一律不读不写(§二):中文列被英文覆盖,飞书那边看不出来。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    for sql, _ in _writes(conn):
        for col in ps._HUMAN_COLS:
            assert col not in sql, f"{col} 出现在写语句里:{sql}"


def test_update_never_renames_an_existing_row(monkeypatch):
    """存量 category_en 不改名:旧结论与 L3 的 reason_category 挂在现值上。"""
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    for sql, params in _writes(conn):
        if sql.split()[0].upper() == "UPDATE":
            assert "category_en" not in sql
            assert "category_en" not in params


def test_update_writes_exactly_the_six_machine_columns(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True})
    sql = next(s for s, _ in _writes(conn) if s.split()[0].upper() == "UPDATE")
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
    # 歧义行不算「官方已不含」——那是"点到了但不敢动",不是官方删了这个类别
    assert out.splitlines()[0].startswith("新增 41 / 刷新 0 / 未对上 42 / "
                                          "官方缺席 0")


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
    assert out.splitlines()[0].startswith("新增 0 / 刷新 1 / 未对上 0 / "
                                          "官方缺席 7")


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
    assert out.splitlines()[0].startswith("新增 0 / 刷新 1 / 未对上 0 / "
                                          "官方缺席 6")   # 8 行 - Alcohol - 该行
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
    """
    rows = [(1, "Drugs & Paraphernalia", "旧正文"),
            (2, "Electronics & RF", "旧正文")]
    conn = _wire(monkeypatch, _Conn(rows=rows))
    out = ps.run({"execute": True, "dry_run": True})
    text = (paths.reports_dir() / ps._REPORT_FILE).read_text(encoding="utf-8")

    assert "疑似改名对" in out and "同概念双行" in out
    assert out.splitlines()[1].startswith("⚠ 疑似改名对")     # 首行之后就点名
    block = text.split("▍疑似改名对")[1].split("▍官方已不含")[0]
    assert "Drugs and Drug Paraphernalia" in block and "id 1" in block
    assert "Electronics and Radio Frequency Devices" in block and "id 2" in block
    # 判定本身**不变**:两行都没被认领,照旧进新增 + 官方已不含
    assert _verbs(conn) == ["SELECT"] * 3
    assert out.splitlines()[0].startswith("新增 42 / 刷新 0 / 未对上 42 / "
                                          "官方缺席 2")


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


def test_real_run_names_all_three_knock_on_consequences(monkeypatch):
    """⚠ 真跑的连带后果三条**都不会自己发生、也都不会报错** —— 摘要必须逐条点名。

      ① AUDIT_RULES_VERSION 不提版 ⇒ rerule / mode=stale 对这次变更完全无感;
      ② 新增行人工中文列全 NULL ⇒ S4 渲染出空壳标题(有类别名、没判据);
      ③ audit_l3.py 的 S1/S3 提示词硬写「37 条」⇒ 与实际行数不符(改不改所有者定)。
    """
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True})
    assert "AUDIT_RULES_VERSION" in out and "L3 输入已变更" in out
    assert "registry/resources.py" in out
    assert "空壳标题" in out and "人工中文列全是 NULL" in out
    assert "services/audit_l3.py" in out and "「37 条」" in out
    assert "43 行" in out          # 存量 8 + 新增 35:与硬写的 37 条对不上
    # 本工作流**不动** audit_l3.py:只提醒,由所有者随 L3 批决定
    assert "本工作流不动 audit_l3.py" in out


def test_dry_run_tells_the_operator_what_to_eyeball(monkeypatch):
    """⚠ 报告里打了标记,没人被告知要看 = 白打。dry-run 摘要必须点名两处。"""
    _wire(monkeypatch, _Conn())
    out = ps.run({"execute": True, "dry_run": True})
    assert "人眼核对" in out
    assert "未对上" in out and "←官方名" in out


def test_report_lands_in_reports_dir_on_both_paths(monkeypatch):
    conn = _wire(monkeypatch, _Conn())
    ps.run({"execute": True, "dry_run": True})
    report = paths.reports_dir() / ps._REPORT_FILE
    text = report.read_text(encoding="utf-8")
    for head in ("▍将补列", "▍类型不符", "▍新增", "▍对上", "▍未对上",
                 "▍疑似改名对", "▍官方已不含", "▍解析失败(本轮不刷新)",
                 "▍解析失败的转录件"):
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
