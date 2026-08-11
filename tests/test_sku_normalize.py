"""sku_normalize 工作流:预览零写入、apply 只补 NULL、item id 两跳倒查。"""

from workflows import sku_normalize as wf


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._rows = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, args=None):
        self.conn.executed.append((sql, args))
        if "DISTINCT sku" in sql:
            self._rows = [(s,) for s in self.conn.skus]
        elif "item_id = ANY" in sql:
            wanted = set(args[0])
            self._rows = [(k, v) for k, v in self.conn.itemmap.items()
                          if k in wanted]
        elif sql.strip().startswith("UPDATE"):
            self.conn.filled = args
            self.rowcount = self.conn.fill_count

    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, skus, itemmap=None, fill_count=0):
        self.skus = skus
        self.itemmap = itemmap or {}
        self.fill_count = fill_count
        self.filled = None
        self.executed = []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _Cur(self)


_SKUS = ["B0AAAAAAA1", "XKJ-B0GXX75JN5-39.98", "102460018738", "WEIRD?!"]


def test_preview_profiles_without_writing(monkeypatch):
    """预览:形态分布 + 可解析数 + 其他桶样本,一行 UPDATE 都不发。"""
    conn = _Conn(_SKUS, itemmap={"102460018738": "YP-B09TDMGVRW-188.88"})
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    out = wf.run({})
    assert "待洗 4 个" in out and "可解析 3 个" in out    # 裸+三段式+倒查命中
    assert "WEIRD?!" in out                              # 其他桶必须报样本
    assert not any(sql.strip().startswith("UPDATE")
                   for sql, _ in conn.executed)


def test_apply_fills_only_resolved(monkeypatch):
    """apply:解析成功的批量补填;解析不了的保持 NULL(原文兜底),
    item id 经 walmart_items 两跳倒查后同样入映射。"""
    conn = _Conn(_SKUS, itemmap={"102460018738": "YP-B09TDMGVRW-188.88"},
                 fill_count=10)
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    out = wf.run({"apply": "1"})
    assert "3/4 个 sku 解析成功" in out and "补填事件 10 行" in out
    skus, asins = conn.filled
    got = dict(zip(skus, asins))
    assert got["XKJ-B0GXX75JN5-39.98"] == "B0GXX75JN5"
    assert got["102460018738"] == "B09TDMGVRW"           # 倒查两跳
    assert "WEIRD?!" not in got


def test_no_pending_rows(monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    assert "无待洗行" in wf.run({})
