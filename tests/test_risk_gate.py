"""listing L2b 回归:wiki 节点解析、风控同步、否决闸条件。"""

import contextlib

from api import feishu
from registry import resources
from registry.resources import Spreadsheet
from services import risk_gate
from workflows import risk_sync


class _Conn:
    def __init__(self, fetch_seq=None):
        self.sqls = []
        self.fetch_seq = list(fetch_seq or [])

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return self.fetch_seq.pop(0) if self.fetch_seq else []

    def fetchone(self):
        # walmart_pt_meta 重灌前的行数(0 = 空表首灌,骤缩护栏不挡)
        return (0,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_wiki_token_resolution_cached(monkeypatch):
    calls = []
    feishu._wiki_token_cache.clear()

    def fake_call(method, path, *, json_body=None, params=None, timeout=60):
        calls.append(path)
        if "wiki/v2/spaces/get_node" in path:
            return {"node": {"obj_token": "REAL_SHEET_TOK"}}
        assert "REAL_SHEET_TOK" in path      # 解析后的 token 进 sheets 接口
        return {"sheets": [{"sheet_id": "SID",
                            "grid_properties": {"row_count": 5}}]}

    monkeypatch.setattr(feishu, "_call", fake_call)
    s = Spreadsheet(name="W", token="WIKI_TOK", sheet_id="SID",
                    columns=("a",), wiki=True)
    assert feishu.sheet_row_count(s) == 5
    assert feishu.sheet_row_count(s) == 5
    assert sum("get_node" in p for p in calls) == 1      # 解析结果缓存
    # 非 wiki 表不解析
    plain = Spreadsheet(name="P", token="REAL_SHEET_TOK", sheet_id="SID",
                        columns=("a",))
    assert feishu.sheet_row_count(plain) == 5
    feishu._wiki_token_cache.clear()


def test_sync_upsert_only_add_and_update():
    conn = _Conn()
    n = risk_gate.sync_product_types(conn, [
        {"product_type": "Cups", "admit_status": "禁售", "cn_seller": ""},
        {"product_type": "", "admit_status": "x"},        # 无 PT 丢弃
    ])
    assert n == 1
    sql, rows = conn.sqls[0]
    assert "ON CONFLICT (product_type) DO UPDATE" in sql  # 只增改不删
    assert rows[0][0] == "Cups"

    n2 = risk_gate.sync_brands(conn, [{"brand": " Nike ", "source": "商标库",
                                       "added_date": "2026-08-07"}])
    assert n2 == 1
    sql2, rows2 = conn.sqls[1]
    assert "ON CONFLICT (brand_key) DO UPDATE" in sql2
    assert rows2[0][0] == "nike" and rows2[0][1] == "Nike"   # casefold 键+原文


def test_gate_conditions_verbatim():
    conn = _Conn(fetch_seq=[
        [("Cups", "禁售", ""), ("Toys", "", "否-限制"), ("Mugs", "正常", "是")],
        [("nike",)],
    ])
    gate = risk_gate.load_gate(conn)
    assert gate["banned_pts"] == {"Cups", "Toys"}         # 禁售 或 否 开头
    assert risk_gate.check(gate, "Cups", None) == "禁售类目:Cups"
    assert risk_gate.check(gate, "Mugs", "NIKE") == "黑名单品牌:NIKE"  # casefold
    assert risk_gate.check(gate, "Mugs", "Adidas") is None
    assert risk_gate.check(gate, None, None) is None


def test_risk_sync_workflow(monkeypatch):
    monkeypatch.setattr(resources, "RISK_PT_SHEET",
                        Spreadsheet(name="沃尔玛类目表", token="W1",
                                    sheet_id="S1",
                                    columns=resources.RISK_PT_SHEET.columns,
                                    wiki=True))
    monkeypatch.setattr(resources, "BRAND_BAN_SHEET",
                        Spreadsheet(name="禁止品牌收集", token="W2",
                                    sheet_id="S2",
                                    columns=resources.BRAND_BAN_SHEET.columns,
                                    wiki=True))
    sheets_data = {
        "S1": [["Home", "PTG1", "Cups", "禁售", "", "", "", "", "", ""]],
        "S2": [["Nike", "商标库", "2026-08-07"]],
    }
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: 2)
    monkeypatch.setattr(feishu, "sheet_values",
                        lambda s, rng: sheets_data[s.sheet_id])
    conn = _Conn(fetch_seq=[[("Cups", "禁售", "")], [("nike",)]])
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))

    out = risk_sync.run({})
    assert "类目表:读 1 行,入库 1" in out
    # 同一份数据的第二个消费方:审核两道闸只查 walmart_pt_meta,
    # 它此前是死快照没人同步(所有者 2026-08-17 实遇飞书删了库里还在)
    assert "walmart_pt_meta:全量重灌 1 行" in out
    assert "品牌表:读 1 行,入库 1" in out
    # 摘要必须说清是**沃尔玛 PT**、查的哪张表(2026-08-21:原文「禁售类目 N 个」
    # 紧跟在「黑名单亚马逊类目 223 条」后面,所有者当场把两个数当成一件事)
    assert "上架禁售沃尔玛 PT 1 个" in out
    assert "catalog.risk_product_types" in out and "亚马逊" in out
    assert "黑名单品牌 1 个" in out


def test_check_brand_and_manufacturer(monkeypatch):
    """黑名单双字段查(所有者批复 2026-08-12):brand=Generic 真品牌在
    manufacturer 是亚马逊常态,只查 brand 必漏。"""
    gate = {"banned_pts": set(), "brands": {"acme"}}
    assert risk_gate.check(gate, None, "Acme", None) == "黑名单品牌:Acme"
    assert risk_gate.check(gate, None, "Generic", "ACME") \
        == "黑名单制造商:ACME"
    assert risk_gate.check(gate, None, "Generic", "Other") is None
    assert risk_gate.check(gate, None, None, None) is None
