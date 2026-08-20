"""类目黑名单录入工作流(所有者定稿 2026-08-20「类目拿到数据库里来」)。

这张表有**两个写入口**,分家靠 `source` 列:飞书镜像归 risk_sync 全量重灌,
清洗名单与种子归本工作流。用例钉死的就是"两边互不冲刷"这条命根子——
一旦重录把 source='feishu' 的行也删了,每天同步一次就把清洗成果洗没了。
"""

import contextlib

import pytest

from services import category_blacklist as cb
from workflows import category_blacklist_import as wf


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.executed.append((sql, args))
        self.rowcount = 7 if "DELETE" in sql else 0
        self._rows = [] if "SELECT" in sql else []

    def executemany(self, sql, rows):
        self.conn.written.extend(rows)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self):
        self.executed, self.written, self.commits = [], [], 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def conn(monkeypatch):
    c = _Conn()
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([c])))
    return c


def _csv(tmp_path, body):
    p = tmp_path / "clean.csv"
    p.write_text("类目,browse_node_id,中文翻译,建议,原因,官方完整路径\n" + body,
                 encoding="utf-8")
    return str(p)


def _csv4(tmp_path, body):
    """v4 清洗表:带「匹配方式」列 —— 子树与否由表说了算,不由 ID 推断。"""
    p = tmp_path / "v4.csv"
    p.write_text("类目,browse_node_id,中文翻译,建议,原因,匹配方式,官方完整路径\n" + body,
                 encoding="utf-8")
    return str(p)


# ── 录入口径:只收「留/增」,建议为删的一行都不许进库 ────────────────────

def test_only_keep_rows_are_imported(tmp_path, conn):
    path = _csv(tmp_path, "\n".join([
        "Toys & Games > Puzzles,166057011,玩具游戏 > 拼图,留-真禁售,IP 风险,T > P",
        "Home & Kitchen > Storage,,家居厨房 > 收纳,删-误伤,置物架不是衣服,H > S",
        "Books > Textbooks,,图书 > 教材,增-补漏,版权,B > T",
        "Beauty > Makeup,,美妆,,没给建议就不动,B > M",
    ]) + "\n")
    out = wf.run({"csv": path, "execute": True})
    vals = {r["mv"] for r in conn.written}
    assert {r["nid"] for r in conn.written} == {"166057011", None}   # 有 ID ⇒ 按子树录
    assert "Books > Textbooks" in vals               # 增-* 也收
    assert not any("Storage" in v for v in vals)     # 删-* 不收
    assert not any("Makeup" in v for v in vals)      # 无建议不收
    assert "录入 2" in out and "跳过:建议为删 1" in out and "无建议 1" in out


def test_id_decides_subtree_or_path_exact(tmp_path, conn):
    """有 browse_node_id 就按子树拦(首选),没有才退回路径等值。"""
    path = _csv(tmp_path, "\n".join([
        "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P",
        "Toys & Games > Oddball,,冷门,留-真禁售,IP,T > O",
    ]) + "\n")
    wf.run({"csv": path, "execute": True})
    by = {r["norm"]: r for r in conn.written}
    node = by["Toys&Games->Puzzles"]
    assert node["mt"] == cb.MATCH_NODE and node["nid"] == "166057011"
    assert node["mv"] == "Toys & Games > Puzzles"    # 存人看的路径,判据是 nid
    path_row = [r for r in conn.written if r["mt"] == cb.MATCH_PATH][0]
    assert path_row["nid"] is None
    assert all(r["source"] == "cleanup" for r in conn.written)


# ── 重录只清自己家的行:飞书镜像不许被误删 ──────────────────────────────

def test_replace_never_touches_feishu_rows(tmp_path, conn):
    path = _csv(tmp_path, "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P\n")
    out = wf.run({"csv": path, "execute": True, "replace": "1"})
    dels = [(s, a) for s, a in conn.executed if "DELETE" in s]
    assert len(dels) == 1
    sql, args = dels[0]
    assert "source = ANY" in sql
    assert sorted(args[0]) == ["cleanup", "seed"]    # 'feishu' 不在删除集里
    assert "feishu" in out and "不动" in out


def test_no_replace_means_no_delete(tmp_path, conn):
    path = _csv(tmp_path, "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P\n")
    wf.run({"csv": path, "execute": True})
    assert not [s for s, _ in conn.executed if "DELETE" in s]


# ── 种子:代码里的常量只在这里被读一次,判定路径永远不读 ────────────────

def test_seed_carries_tops_and_subtrees(conn):
    out = wf.run({"seed": "1", "execute": True})
    mts = {r["mt"] for r in conn.written}
    assert cb.MATCH_TOP in mts and cb.MATCH_NODE in mts
    assert len(conn.written) == len(cb.SEED_RULES)
    assert all(r["source"] == "seed" for r in conn.written)
    nids = {r["nid"] for r in conn.written if r["mt"] == cb.MATCH_NODE}
    assert {"3736081", "3013597011", "166220011", "6925830011"} <= nids
    assert "写入完成" in out


def test_seed_and_csv_can_be_given_together(tmp_path, conn):
    path = _csv(tmp_path, "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P\n")
    wf.run({"seed": "1", "csv": path, "execute": True})
    assert len(conn.written) == len(cb.SEED_RULES) + 1


# ── dry-run 与空输入:缺省即真跑,但没给数据源要说清而不是静默成功 ────────

def test_dry_run_writes_nothing(tmp_path, conn):
    """⚠ 按 cli 的真实入参:DANGEROUS=False ⇒ execute 恒为 True,`--dry-run`
    只走 dry_run 这一路。只认 execute 的话这条 --dry-run 会直接写库还报成功。"""
    path = _csv(tmp_path, "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P\n")
    out = wf.run({"csv": path, "execute": True, "dry_run": True})
    assert conn.written == [] and conn.executed == []
    assert "dry-run" in out.lower() and "一行未写" in out


def test_no_source_is_reported_not_silently_ok(conn):
    out = wf.run({"execute": True})
    assert "没给数据源" in out
    assert conn.written == []


# ── 「匹配方式」列:子树与否由清洗表说了算,工作流只执行不推断 ──────────────

def test_match_type_column_overrides_id_guess(tmp_path, conn):
    """⚠ 有 ID 也可能**不能**当子树根:名单里 582 条的 ID 是回落匹配来的
    (祖先前缀/存疑/深度对不上)。实见 `…Camera&Photo->…->Chemicals` 的 ID
    回落成了 `Electronics > Camera & Photo` —— 升成子树 = 整个相机摄影大类
    全没了,而且不报错。清洗表判它「路径等值」,工作流就必须只拦本行。
    """
    path = _csv4(tmp_path, "\n".join([
        "E->Camera&Photo->Darkroom->Chemicals,502394,暗房化学品,留-真禁售,ID 是祖先回落,路径等值,E > Camera & Photo",
        "Toys&Games->Puzzles,166057011,拼图,留-真禁售,真子树,子树,T > P",
    ]) + "\n")
    wf.run({"csv": path, "execute": True})
    by = {r["norm"]: r for r in conn.written}
    downgraded = by["E->Camera&Photo->Darkroom->Chemicals"]
    assert downgraded["mt"] == cb.MATCH_PATH
    assert downgraded["nid"] is None          # ID 一并丢掉,免得日后被误当子树
    assert by["Toys&Games->Puzzles"]["mt"] == cb.MATCH_NODE


def test_covered_rows_are_not_imported_again(tmp_path, conn):
    """净增覆盖行的建议也以「增」开头 —— 只看建议列会把它们重复录进去。

    它们是**被某条子树规则连带拦下的官方类目**,本身不是规则;录进去等于把
    子树规则又摊平成一万行路径,改造的意义就没了。
    """
    path = _csv4(tmp_path, "\n".join([
        "Toys&Games->Puzzles,166057011,拼图,留-真禁售,规则本体,子树,T > P",
        "Toys&Games->Puzzles->3-DPuzzles,166058011,3-D拼图,增-子树覆盖,被 166057011 覆盖,(被覆盖,不单独录入),T > P > 3-D",
        "Books->Textbooks,,教材,删-冗余,顶级已拦,不录入,B > T",
    ]) + "\n")
    out = wf.run({"csv": path, "execute": True})
    assert [r["norm"] for r in conn.written] == ["Toys&Games->Puzzles"]
    assert "匹配方式非录入 1" in out           # 删-冗余 走建议列,覆盖行走匹配方式列


def test_missing_column_falls_back_but_says_so(tmp_path, conn):
    """老 CSV 没有「匹配方式」列时退回按 ID 推断,但**必须报数** ——
    静默回落到那个已知会整棵误拦的口径,正是本仓最忌讳的"兜底静默常态化"。"""
    path = _csv(tmp_path, "Toys & Games > Puzzles,166057011,拼图,留-真禁售,IP,T > P\n")
    out = wf.run({"csv": path, "execute": True})
    assert conn.written[0]["mt"] == cb.MATCH_NODE
    assert "没有「匹配方式」列" in out and "1 行" in out
