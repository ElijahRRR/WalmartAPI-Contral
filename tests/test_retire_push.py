"""retire_push:已确认的停用/删除清单 → 停用删除表 A~D(2026-11-19 标准选品流程)。"""

import contextlib

import pytest

from services import clear_sheet
from workflows import retire_push as wf


class _Cur:
    def __init__(self, live, disp):
        self.live, self.disp, self._r = live, disp, []
    def execute(self, sql, args=None):
        self._r = list(self.disp) if "ops.dispositions" in sql else list(self.live)
    def fetchall(self): return self._r
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, live, disp): self.live, self.disp = live, disp
    def cursor(self): return _Cur(self.live, self.disp)


def _wire(monkeypatch, tmp_path, text, *, live=(("A", "SKU1"), ("A", "SKU2")),
          disp=(), sheet=(), enabled=("A",)):
    p = tmp_path / "retire.csv"
    p.write_text(text, encoding="utf-8")
    seen = {"writes": []}
    rows = [dict(r) for r in sheet]
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: set(enabled))
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_Conn(live, disp)))
    monkeypatch.setattr(clear_sheet, "read_rows", lambda: [dict(r) for r in rows])

    def _write(sheet_, updates):
        seen["writes"].append(updates)
        for rng, vals in updates:
            r0 = int(rng.split(":")[0][1:])
            for i, v in enumerate(vals):
                rows.append({"rownum": r0 + i, "store": v[0], "sku": v[1], "action": v[2],
                             "reason": v[3], "feed_id": "", "op_date": "",
                             "result": "", "error": ""})
        return len(updates)
    monkeypatch.setattr(clear_sheet.feishu, "sheet_write_ranges", _write)
    monkeypatch.setattr(clear_sheet.feishu, "sheet_ensure_rows", lambda s, n: n)
    monkeypatch.setattr(clear_sheet.resources.RETIRE_SHEET.__class__, "require",
                        lambda self: self)
    return str(p), seen


_OK = "店铺,SKU,动作,原因\nA,SKU1,删除,高退货\nA,SKU2,停用,持续缺货\n"


def _row(n, store, sku, result=""):
    return {"rownum": n, "store": store, "sku": sku, "action": "删除", "reason": "x",
            "feed_id": "", "op_date": "", "result": result, "error": ""}


def test_execute_appends_a_to_d_after_last_row_and_reads_back(monkeypatch, tmp_path):
    p, seen = _wire(monkeypatch, tmp_path, _OK, sheet=[_row(5, "B", "OLD", "成功")])
    out = wf.run({"from_csv": p, "execute": True})
    assert out.startswith("✅ 已追加 2 行") and "第 6 行起" in out and "回读一致" in out
    [[(rng, vals)]] = seen["writes"]
    assert rng == "A6:D7"
    assert vals == [["A", "SKU1", "删除", "高退货"], ["A", "SKU2", "停用", "持续缺货"]]


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    p, seen = _wire(monkeypatch, tmp_path, _OK)
    out = wf.run({"from_csv": p, "execute": False})
    assert out.startswith("🧪") and "停用 1、删除 1" in out and seen["writes"] == []


def test_pending_same_sku_in_sheet_is_skipped_but_settled_history_is_not(monkeypatch, tmp_path):
    sheet = [_row(2, "A", "SKU1", ""), _row(3, "A", "SKU2", "失败")]
    p, seen = _wire(monkeypatch, tmp_path, _OK, sheet=sheet)
    out = wf.run({"from_csv": p, "execute": True})
    assert "跳过" in out
    [[(rng, vals)]] = seen["writes"]
    assert rng == "A4:D4" and vals == [["A", "SKU2", "停用", "持续缺货"]]


def test_whole_batch_rejected_when_any_row_fails_gates(monkeypatch, tmp_path):
    text = _OK + "Z,SKU9,删除,x\n"
    p, seen = _wire(monkeypatch, tmp_path, text, disp=[("A", "SKU2", "delete", "executing")])
    with pytest.raises(ValueError) as e:
        wf.run({"from_csv": p, "execute": True})
    msg = str(e.value)
    assert "整批拒绝" in msg and "Z/SKU9:店铺未启用" in msg
    assert "Z/SKU9:目录里没有这个活 SKU" in msg
    assert "A/SKU2:自动删除链已有未落定的 delete(executing)" in msg
    assert seen["writes"] == []


@pytest.mark.parametrize("text,match", [
    ("店铺,SKU,动作,原因\nA,SKU1,,x\n", "动作无效"),
    ("店铺,SKU,动作,原因\nA,SKU1,下架删除,x\n", "动作无效"),
    ("店铺,SKU,动作,原因\nA,SKU1,删除,\n", "不能为空"),
    ("店铺,SKU,动作,原因\nA,SKU1,删除,x\nA,SKU1,停用,y\n", "重复"),
    ("店铺,SKU,动作\nA,SKU1,删除\n", "四列"),
])
def test_plan_csv_validation(monkeypatch, tmp_path, text, match):
    p, seen = _wire(monkeypatch, tmp_path, text)
    with pytest.raises(ValueError, match=match):
        wf.run({"from_csv": p, "execute": True})
    assert seen["writes"] == []


@pytest.mark.parametrize("params", [{"execute": True}, {"from_csv": " ", "execute": True},
                                    {"from_csv": "x", "store": "A"}])
def test_missing_or_unknown_params_refuse(params):
    with pytest.raises(ValueError):
        wf.run(params)


def test_append_rows_refuses_blank_action():
    with pytest.raises(ValueError, match="动作限"):
        clear_sheet.append_rows([("A", "SKU1", "", "x")], 2, execute=False)
