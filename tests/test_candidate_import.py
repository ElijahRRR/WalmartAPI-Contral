"""候选池导入回归:表头精确匹配 + 值清洗(N/A→NULL 禁 or 0)+ DO NOTHING 幂等。"""

import contextlib

from workflows import candidate_import as wf

_CSV = (
    "ASIN (商品ID),商品标题,品牌,类目路径树,商品评分,评论数,当前价格,"
    "BuyBox 价格,是否 FBA 发货,库存状态,卖家店铺名,商品采集时间\n"
    'B0TEST00A1,Widget,Acme,Home & Kitchen > Storage > Bins,4.5,"1,234",'
    '"$1,299.00",N/A,FBA,In Stock,AcmeStore,2026-08-01 10:00:00\n'
    "B0TEST00A2,Gadget,N/A,,N/A,N/A,7.25,7.25,fbm,,Seller2,\n"
    "bad-asin,X,Y,,,,,,,,\n"
)


class _Conn:
    def __init__(self):
        self.batches: list = []
        self._rc = 0

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        self.batches.append((sql, list(rows)))
        self._rc = len(rows)

    @property
    def rowcount(self):
        return self._rc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_csv(tmp_path, text=_CSV, name="v3_export.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8-sig")
    return str(p)


def test_preview_counts_and_does_not_touch_db(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("预览不许连库")
    monkeypatch.setattr(wf.db, "pg_conn", _boom)
    s = wf.run({"file": _write_csv(tmp_path)})
    assert "读 3 行" in s and "合法 2" in s and "非法跳过 1" in s
    assert "预览完毕" in s


def test_apply_parses_values(tmp_path, monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    s = wf.run({"file": _write_csv(tmp_path), "apply": "1"})
    rows = [r for _, b in conn.batches for r in b]
    assert len(rows) == 2
    a1, a2 = rows
    assert a1["asin"] == "B0TEST00A1"
    assert a1["rating"] == 4.5 and a1["review_count"] == 1234
    assert a1["current_price"] == 1299.0          # "$1,299.00" 清洗
    assert a1["buybox_price"] is None             # N/A → NULL,不是 0
    assert a1["channel"] == "FBA"
    assert a1["category_root"] == "Home & Kitchen"
    assert a2["brand"] is None and a2["channel"] == "FBM"
    assert a2["category_tree"] is None and a2["category_root"] is None
    assert "ON CONFLICT (asin) DO NOTHING" in conn.batches[0][0]
    assert "入库 2 行" in s


def test_missing_asin_column_refuses(tmp_path):
    p = _write_csv(tmp_path, "品牌,商品标题\nAcme,Widget\n", "bad.csv")
    assert wf.run({"file": p, "apply": "1"}).startswith("⛔ 找不到必需列")


def test_non_csv_refused(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_text("x")
    assert "只收 csv" in wf.run({"file": str(p)})
