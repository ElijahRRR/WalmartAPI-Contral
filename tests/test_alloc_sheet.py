"""产品分配表(registry.ALLOC_SHEET + services/alloc_sheet)回归。

所有者 2026-09-17 建表并定稿:只填 ASIN 列,其余 16 列脚本填;「店铺」已填的
行重跑跳过;流别三值;末尾「是否在线」。这里钉的是**表的契约**(表头原文、
列权责、按表头名定位)—— 写错列是静默的,而表上的「店铺」列是所有者照着去
上架的东西。
"""

import pytest

from registry import resources
from services import alloc_sheet, sheet_layout

#: 所有者 2026-09-17 从飞书复制过来的表头原文,**逐字**(顺序也是今天的顺序)。
OWNER_HEADER = ["店铺", "ASIN", "品牌", "产品分", "罚分", "罚分原因", "流别",
                "未分配原因", "商品品类(五大类)", "商品大类(26类)", "评分", "评论数",
                "配送方式", "配送天数", "落地价", "窗口销售额(毛额)", "是否在线"]


def _wire(monkeypatch, header_row=OWNER_HEADER):
    """输入:表头行 → 输出:无。让 alloc_sheet 认这一行表头(清缓存后重读)。"""
    monkeypatch.setattr(resources, "ALLOC_SHEET", resources.Spreadsheet(
        name="产品分配表", token="TOK", sheet_id="SID",
        columns=resources.ALLOC_SHEET.columns,
        headers=dict(resources.ALLOC_SHEET.headers)))
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_values_small",
                        lambda sheet, rng: [list(header_row)])
    monkeypatch.setattr(alloc_sheet, "_LAYOUT", None)


# ── registry 契约 ─────────────────────────────────────────────────────

def test_registry_headers_are_the_owners_17_columns_verbatim():
    """登记的表头 = 所有者贴的 17 列原文,一个字不差;列序 = 今天的顺序。"""
    s = resources.ALLOC_SHEET
    assert [s.headers[c] for c in s.columns] == OWNER_HEADER
    assert len(s.columns) == 17 and len(set(s.columns)) == 17


def test_registry_reuses_the_online_sheet_token_and_only_needs_a_sheet_id():
    """「产品中心」与「在线产品总表」是同一个飞书文件(所有者确认):token 复用
    FEISHU_ONLINE_SHEET_TOKEN,.env 只要新增 FEISHU_ALLOC_SHEET_ID。"""
    import inspect
    src = inspect.getsource(resources)
    block = src[src.index("ALLOC_SHEET = Spreadsheet("):]
    block = block[:block.index("\n)\n")]
    assert 'os.environ.get("FEISHU_ONLINE_SHEET_TOKEN"' in block
    assert 'os.environ.get("FEISHU_ALLOC_SHEET_ID"' in block
    assert "wiki=True" not in block
    # .env 模板里要有这个变量名(照 README 部署的新机器才知道要填它)
    from workflows import init_data_root
    assert "FEISHU_ALLOC_SHEET_ID=" in inspect.getsource(init_data_root)


def test_script_fields_are_everything_but_the_owners_asin_column():
    """机器域 = 登记的全部列去掉 ASIN;从 registry 派生,不另抄一份。"""
    fields = alloc_sheet.script_fields()
    assert "asin" not in fields and len(fields) == 16
    assert set(fields) | {"asin"} == set(resources.ALLOC_SHEET.columns)


# ── 读:窄读、归一、店铺已填的行原样带回 ─────────────────────────────

def test_read_targets_reads_only_up_to_the_asin_column_and_normalises(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_row_count", lambda s: 6)
    asked = {}

    def _rows(sheet, c1, c2, rf, rt, **kw):
        asked.update(c1=c1, c2=c2, rf=rf, rt=rt)
        return [(2, ["", " b0aaaa0001 "]), (3, ["A085", "B0BBBB0002"]),
                (4, ["", ""]), (5, [None, "B0CCCC0003"]), (6, ["", "怪东西"])]
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_values_rows", _rows)
    got = alloc_sheet.read_targets()
    # 只读到 店铺/ASIN 两列里靠右的那一列(今天是 B),不拉 17 列全宽
    assert (asked["c1"], asked["c2"], asked["rf"], asked["rt"]) == ("A", "B", 2, 6)
    assert [(r["rownum"], r["asin"], r["store"]) for r in got] == [
        (2, "B0AAAA0001", ""), (3, "B0BBBB0002", "A085"),
        (5, "B0CCCC0003", ""), (6, "怪东西", "")]
    assert got[0]["asin_raw"] == "b0aaaa0001"      # 原文留给报错回显
    # 形态闸不在这里判:怪东西照样带回,由工作流写进「未分配原因」


def test_read_targets_follows_a_shuffled_header(monkeypatch):
    """所有者把 ASIN 挪到最后一列,读取照样对得上、窄读的宽度跟着变。"""
    shuffled = [h for h in OWNER_HEADER if h != "ASIN"] + ["ASIN"]
    _wire(monkeypatch, shuffled)
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_row_count", lambda s: 2)
    asked = {}

    def _rows(sheet, c1, c2, rf, rt, **kw):
        asked["c2"] = c2
        return [(2, ["A085"] + [""] * 15 + ["B0AAAA0001"])]
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_values_rows", _rows)
    got = alloc_sheet.read_targets()
    assert asked["c2"] == "Q"
    assert got == [{"rownum": 2, "asin": "B0AAAA0001", "asin_raw": "B0AAAA0001",
                    "store": "A085"}]


def test_read_targets_refuses_a_drifted_header(monkeypatch):
    """表头少一列 → 读之前就抛错,一行数据都不读(fail-closed)。"""
    _wire(monkeypatch, [h for h in OWNER_HEADER if h != "是否在线"])
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_row_count", lambda s: 9)
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_values_rows",
                        lambda *a, **k: pytest.fail("表头漂移了还敢读数据行"))
    with pytest.raises(sheet_layout.HeaderMismatch, match="是否在线"):
        alloc_sheet.read_targets()


# ── 写:只写机器域,永不碰 ASIN 列 ────────────────────────────────────

def _vals(**over):
    d = {f: "" for f in alloc_sheet.script_fields()}
    d.update(store="A085", brand="acme", score=88.5, flow="自由流", online="否")
    d.update(over)
    return d


def test_write_rows_writes_the_16_machine_columns_in_two_segments_around_asin(monkeypatch):
    """今天的列序是 店铺 | ASIN | 品牌…是否在线:一行 = A 段 + C:Q 段,B 列永不出现。"""
    _wire(monkeypatch)
    sent = []
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    n = alloc_sheet.write_rows([(2, _vals()), (3, _vals(store="B012", score=None))])
    assert n == 2
    assert [r for r, _ in sent] == ["A2:A2", "C2:Q2", "A3:A3", "C3:Q3"]
    assert sent[0][1] == [["A085"]]
    # 值按字段名落位:C=品牌 D=产品分 …;None 写空串,数字转文本
    assert sent[1][1][0][:2] == ["acme", "88.5"]
    assert sent[3][1][0][:2] == ["acme", ""]
    assert all(len(vals[0]) == 15 for r, vals in sent if r.startswith("C"))


def test_write_rows_follows_a_shuffled_header_without_code_changes(monkeypatch):
    """所有者把「是否在线」挪到最前、ASIN 挪到最后:段数与位置自动变,值不串位。"""
    shuffled = ["是否在线"] + [h for h in OWNER_HEADER
                                if h not in ("是否在线", "ASIN")] + ["ASIN"]
    _wire(monkeypatch, shuffled)
    sent = []
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    alloc_sheet.write_rows([(4, _vals(online="是"))])
    # 段按**字段序里相邻的列号**粘:店铺…窗口销售额 连成 B..P 一段,「是否在线」
    # 单独落到 A;ASIN 在 Q,谁都碰不到它
    assert [r for r, _ in sent] == ["B4:P4", "A4:A4"]
    assert sent[0][1][0][0] == "A085" and sent[1][1] == [["是"]]


def test_write_rows_rejects_a_row_with_missing_or_extra_fields(monkeypatch):
    """少一个字段就会整体错位写进别人的列 —— 直接抛,不写。"""
    _wire(monkeypatch)
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_write_ranges",
                        lambda s, ups: pytest.fail("字段对不上还敢写"))
    short = _vals()
    short.pop("online")
    with pytest.raises(ValueError, match="online"):
        alloc_sheet.write_rows([(2, short)])
    with pytest.raises(ValueError, match="asin"):
        alloc_sheet.write_rows([(2, dict(_vals(), asin="B0AAAA0001"))])


def test_write_rows_dry_run_touches_nothing(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(alloc_sheet.feishu, "sheet_write_ranges",
                        lambda s, ups: pytest.fail("dry-run 还敢写"))
    assert alloc_sheet.write_rows([(2, _vals())], execute=False) == 0
    assert alloc_sheet.write_rows([]) == 0
