"""飞书唯一读写通道 + 限额登记表(所有者 2026-08-27 定稿)。

钉三件事:
  ① 限额登记表:每条常量都带得出官方出处(「官方」字样 + URL + 核对日期),
     工程值必须自报「工程值,非官方」——查不到就说查不到,不许编。
  ② 唯一写通道 sheet_write_ranges:行/字节双预算切批、**当轮写完不留下一轮**、
     结构硬闸(列/单元格)直接抛、90227 对半重切一层兜底并记日志计数。
  ③ 唯一读通道 sheet_values_rows:块粒度取登记表;裸读降为私有 _values_raw,
     对外只留 sheet_values_small(小范围)——F1 留的旧名 sheet_values 转发
     已于 2026-08-27 F2 清完调用点后删除,不许复活。

分批与限流的行为回归在 tests/test_feishu.py(token/退避/多维表格切块)那边,
这里只管「限额从哪来」和「读写各只有一条路」。
"""

import json
import logging
import re
import time
from pathlib import Path

import httpx
import pytest

from api import feishu
from registry.resources import Spreadsheet

SHEET = Spreadsheet(name="X", token="TOK", sheet_id="SID", columns=("a", "b"))
REGISTRY_TSV = Path(__file__).resolve().parents[1] / "refdata" / "feishu_limits.tsv"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    feishu._token_cache.clear()
    yield
    if feishu._client is not None:
        feishu._client.close()
    feishu._client = None
    feishu._token_cache.clear()


@pytest.fixture
def slept(monkeypatch):
    """把 time.sleep 换成记账本:退避不真等,但等了多久要看得见。"""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: waits.append(s))
    return waits


def _sink(monkeypatch):
    """把 _call 换成收件箱,返回收到的 json_body 列表。"""
    sent: list[dict] = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    return sent


def _rows_of(body: dict) -> int:
    return sum(len(vr["values"]) for vr in body["valueRanges"])


# ══════════════════════════════════════════════════════════════════════════════
#  ① 限额登记表
# ══════════════════════════════════════════════════════════════════════════════

_SRC = Path(feishu.__file__).read_text(encoding="utf-8")


def _registry_block() -> list[str]:
    """限额登记表那一段的源码行(从表头横幅到 _TRANSIENT_CODES 之前)。"""
    lines = _SRC.splitlines()
    start = next(i for i, ln in enumerate(lines) if "限额登记表(所有者" in ln)
    end = next(i for i, ln in enumerate(lines) if ln.startswith("_TRANSIENT_CODES"))
    assert start < end
    return lines[start:end]


def test_every_limit_constant_cites_an_official_source():
    """登记表里每条常量都必须带「官方」出处 + URL + 核对日期。

    这条是防漂移的锁:下一个人加限额时要么写得出官方原值与链接,要么在注释里
    自报「工程值,非官方」。两样都没有 = 编了个数字,直接红。
    """
    consts = [ln for ln in _registry_block()
              if re.match(r"^_[A-Z][A-Z_0-9]* = ", ln)]
    assert len(consts) >= 16, f"登记表只剩 {len(consts)} 条,是不是被搬走了?"
    for ln in consts:
        name = ln.split(" = ")[0]
        assert "#" in ln, f"{name} 没有行内注释"
        note = ln.split("#", 1)[1]
        assert "官方" in note, f"{name} 的注释没写官方出处"
        assert "https://" in note, f"{name} 的注释没有官方 URL"
        assert "核对 2026-08-27" in note, f"{name} 的注释没有核对日期"


def test_engineering_values_declare_themselves_as_unofficial():
    """官方没给数字的四条必须明写「工程值,非官方」,不许混进官方值里冒充。"""
    block = "\n".join(_registry_block())
    for name in ("_SHEET_WRITE_BYTE_BUDGET", "_SHEET_RANGES_PER_REQUEST",
                 "_SHEET_READ_BLOCK_ROWS", "_RATELIMIT_RESET_CAP_SECS"):
        line = next(ln for ln in _registry_block() if ln.startswith(name + " = "))
        assert "工程值,非官方" in line, name
    assert "官方限制 × 95%" in block                    # 折算规矩写在表头
    assert "refdata/feishu_limits.tsv" in block          # 对照全表的指针


def test_registry_values_are_95_percent_of_official():
    """取值 = 官方 × 95% 向下取整;官方自荐更严时取更严者。"""
    assert feishu._SHEET_WRITE_MAX_ROWS == 5000 * 95 // 100 == 4750
    assert feishu._SHEET_WRITE_MAX_COLS == 100 * 95 // 100 == 95
    assert feishu._SHEET_DIMENSION_MAX == 5000 * 95 // 100 == 4750
    assert feishu._BITABLE_BATCH_CREATE_MAX == 1000 * 95 // 100 == 950
    assert feishu._BITABLE_BATCH_UPDATE_MAX == 1000 * 95 // 100 == 950
    assert feishu._BITABLE_BATCH_DELETE_MAX == 500 * 95 // 100 == 475  # 删只有一半
    assert feishu._BITABLE_PAGE_SIZE == 500 * 95 // 100 == 475
    # 单元格:官方硬上限 50000×95%=47500,但官方自荐 40000 → 取更严的 40000
    assert feishu._SHEET_CELL_HARD_MAX_CHARS == 40000 < 50000 * 95 // 100
    # 业务脏数据闸是另一层职责,原值不动
    assert feishu._SHEET_CELL_MAX_CHARS == 20000


def test_scattered_limit_constants_were_merged_into_the_registry():
    """散落的旧常量必须已并入登记表 —— 留着就是第二个出处,必漂。"""
    assert not hasattr(feishu, "_SHEET_WRITE_BLOCK_ROWS")
    assert not hasattr(feishu, "_BATCH_LIMIT")


def test_official_quote_archive_covers_the_registry():
    """refdata/feishu_limits.tsv:官方原句对照全表,含「官方未说明」项。

    与 walmart_rate_limits.tsv 同款纪律:查到的写原句 + URL,查不到的如实写
    「官方未说明」,不许推断——留白比编数字安全。
    """
    lines = REGISTRY_TSV.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    assert head[:3] == ["分组", "限制项", "官方值"]
    assert head[-3:] == ["官方URL", "官方原句", "核对日期"]
    rows = [ln.split("\t") for ln in lines[1:] if ln.strip()]
    assert all(len(r) == len(head) for r in rows)
    assert len(rows) > 100
    assert all(r[-1] == "2026-08-27" for r in rows)
    # 官方未说明的项也必须在册(否则下一个人会以为「没查过」而重查/瞎猜)
    assert sum(1 for r in rows if r[2] == "官方未说明") >= 10
    blob = "\n".join(lines)
    for quote in ("单次写入数据不得超过 5000 行、100列。",
                  "在多维表格数据表中新增多条记录，单次调用最多新增 1,000 条记录。",
                  "- 单次调用中最多删除 500 条记录。",
                  "该接口返回数据的最大限制为 10 MB。",
                  "单个文档只能串行调用"):
        assert quote in blob, quote


# ══════════════════════════════════════════════════════════════════════════════
#  ② 唯一写通道:预算切批
# ══════════════════════════════════════════════════════════════════════════════

def test_4751_rows_split_into_two_batches_finished_in_one_call(monkeypatch, slept):
    """4751 行 = 行预算 + 1:切成 2 批,且**两批都在这一次调用里发完**。

    所有者原话「当轮写完不留下一轮」:通道不许把余量攒到下一轮——攒着就等于
    悄悄少写,调用方拿到的行数还是全量,对不上账。
    """
    sent = _sink(monkeypatch)
    rows = [[f"v{i}", "b"] for i in range(4751)]
    n = feishu.sheet_write_ranges(SHEET, [("A2:B4752", rows)])
    assert n == 4751                                  # 一行不留
    assert [_rows_of(b) for b in sent] == [4750, 1]   # 满了就封批,不是一锅端
    ranges = [vr["range"] for b in sent for vr in b["valueRanges"]]
    assert ranges == ["SID!A2:B4751", "SID!A4752:B4752"]
    assert len(slept) == 1                            # 批间节流一次,批数-1


def test_byte_budget_seals_a_batch_before_the_row_budget(monkeypatch, slept):
    """字节预算触顶也要封批 —— 行数远没满,但长文本行已经把请求体撑爆。

    2026-08-18 audit_sheet 就是这么被 90227 拒的:只按行数算,一段 4000 行的
    长文本照样超。行与字节两条预算,先撞哪条哪条生效。
    """
    monkeypatch.setattr(feishu, "_SHEET_WRITE_BYTE_BUDGET", 4000)
    sent = _sink(monkeypatch)
    rows = [["x" * 900, "y" * 900] for _ in range(10)]      # 每行约 1.8KB
    n = feishu.sheet_write_ranges(SHEET, [("A2:B11", rows)])
    assert n == 10
    assert len(sent) > 1, "字节预算没起作用(全塞进一个请求了)"
    assert max(_rows_of(b) for b in sent) < feishu._SHEET_WRITE_MAX_ROWS
    for body in sent:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        assert size <= feishu._SHEET_WRITE_BYTE_BUDGET * 1.1, size
    # 一行不丢、顺序不乱
    flat = [r for b in sent for vr in b["valueRanges"] for r in vr["values"]]
    assert len(flat) == 10 and flat[0][0].startswith("x")


def test_more_than_95_columns_is_a_structural_error(monkeypatch):
    """列超官方 95% 红线 → ValueError。分批只切得出行,切不出列,救不了。"""
    _sink(monkeypatch)
    wide = [["c"] * 96]
    with pytest.raises(ValueError, match="列"):
        feishu.sheet_write_ranges(SHEET, [("A1:CR1", wide)])
    # 95 列正好在红线上,放行
    feishu.sheet_write_ranges(SHEET, [("A1:CQ1", [["c"] * 95])])


def test_hard_gate_and_dirty_gate_each_guard_their_own_path(monkeypatch):
    """两层闸各守各的路(总控裁决 2026-08-27)。

    · 清洗路径(sheet_write_ranges):脏数据截断+告警、**轮次照走**是既有能力
      ——一条 4 万字符的脏报错不许炸掉整轮回写;硬闸在此路对字符长度天然
      不触发,只剩列数闸;
    · 不清洗路径(sheet_overwrite):40000 硬闸直判——那里超长本会被飞书
      90222/90227 整批拒,本地先抛是净收益。
    """
    sent = _sink(monkeypatch)
    feishu.sheet_write_ranges(SHEET, [("A1:A1", [["x" * 40001]])])
    written = sent[0]["valueRanges"][0]["values"][0][0]
    assert len(written) == feishu._SHEET_CELL_MAX_CHARS == 20000   # 截断照走,不抛

    sent.clear()
    feishu.sheet_write_ranges(SHEET, [("A1:A1", [["y" * 30000]])])
    assert len(sent[0]["valueRanges"][0]["values"][0][0]) == 20000

    with pytest.raises(ValueError, match="40000"):                 # 硬闸的岗位在这条路
        feishu.sheet_overwrite(SHEET, [["表头"], ["z" * 40001]])
    assert len(sent) == 1, "抛之前不该已经发出去半批"


def test_90227_halves_the_batch_once_and_counts_it(monkeypatch, caplog, slept):
    """预算失算兜底:90227 → 对半重切一次 + 记日志计数(§六 三要件)。

    三要件逐条:藏在同一个函数内(_sheet_put)、触发必记日志**并计数**
    (兜底静默常态化 = 主路径已坏没人知道)、条件明确只认 90227(不是 catch-all)。
    """
    seen: list[int] = []

    def flaky(method, path, **kw):
        body = kw.get("json_body") or {}
        rows = _rows_of(body)
        seen.append(rows)
        if len(seen) == 1:                       # 第一发:整批被拒
            raise feishu.FeishuError(90227, "request too large")
        return {}

    monkeypatch.setattr(feishu, "_call", flaky)
    caplog.set_level(logging.WARNING, logger="api.feishu")
    before = feishu._oversize_retries()

    ups = [(f"C{r}:G{r}", [[f"v{r}"] * 5]) for r in range(2, 12)]   # 10 行连号
    n = feishu.sheet_write_ranges(SHEET, ups)

    assert n == 10
    assert seen == [10, 5, 5], seen              # 整批 → 对半两发
    assert feishu._oversize_retries() == before + 1
    warned = [r.getMessage() for r in caplog.records]
    assert any("对半重切" in m and "90227" in m for m in warned), warned
    assert any(f"预算失算第 {before + 1} 次" in m for m in warned), warned  # 带计数


def test_90227_after_halving_is_raised_not_halved_again(monkeypatch, slept):
    """兜底只兜一层:对半之后再被拒就抛。

    「预算失算」是保险丝,不是主防线;无限对半会把一次错误的批量拆成几十个
    请求慢慢烧配额,还把真正的故障藏起来。
    """
    def always_too_large(method, path, **kw):
        raise feishu.FeishuError(90227, "request too large")

    monkeypatch.setattr(feishu, "_call", always_too_large)
    ups = [(f"C{r}:G{r}", [[f"v{r}"] * 5]) for r in range(2, 12)]
    with pytest.raises(feishu.FeishuError) as ei:
        feishu.sheet_write_ranges(SHEET, ups)
    assert ei.value.code == 90227
    assert "范围" in ei.value.msg    # 抛出去也要说是哪一块,飞书自己不说


def test_other_write_errors_are_not_halved(monkeypatch, slept):
    """只兜 90227,不当 catch-all:别的错照抛,且报错带上范围。"""
    calls: list[int] = []

    def rejects(method, path, **kw):
        calls.append(1)
        raise feishu.FeishuError(90202, "validate RangeVal fail")

    monkeypatch.setattr(feishu, "_call", rejects)
    with pytest.raises(feishu.FeishuError) as ei:
        feishu.sheet_write_ranges(SHEET, [("A2:B3", [["a", "b"], ["c", "d"]])])
    assert len(calls) == 1                      # 没重切、没重试
    assert "范围" in ei.value.msg               # 飞书不说哪一块,本层补上


def test_overwrite_goes_through_the_same_budget_channel(monkeypatch, slept):
    """整表重写与定点回写是**同一条**写通道(唯一写通道,不是两套切块)。"""
    monkeypatch.setattr(feishu, "_SHEET_WRITE_MAX_ROWS", 3)
    calls: list[tuple] = []

    def fake_call(method, path, **kw):
        calls.append((method, path, kw.get("json_body")))
        if path.endswith("/sheets/query"):
            return {"sheets": [{"sheet_id": "SID",
                                "grid_properties": {"row_count": 7}}]}
        return {}

    monkeypatch.setattr(feishu, "_call", fake_call)
    assert feishu.sheet_overwrite(SHEET, [["h", "h"]] + [[i, i] for i in range(6)]) == 7
    writes = [c for c in calls if "values_batch_update" in c[1]]
    assert [_rows_of(c[2]) for c in writes] == [3, 3, 1]
    assert [c[2]["valueRanges"][0]["range"] for c in writes] == [
        "SID!A1:B3", "SID!A4:B6", "SID!A7:B7"]
    # 整表重写不 scrub:数字保持数字(KPI 看板的日期序列值靠它 + formatter)
    assert writes[1][2]["valueRanges"][0]["values"][0] == [2, 2]


# ══════════════════════════════════════════════════════════════════════════════
#  ③ 唯一读通道
# ══════════════════════════════════════════════════════════════════════════════

def test_read_block_size_comes_from_the_registry(monkeypatch):
    """标准读通道的块粒度取登记表 _SHEET_READ_BLOCK_ROWS,不再是本地魔数。"""
    asked: list[str] = []
    monkeypatch.setattr(feishu, "_values_raw",
                        lambda sheet, rng: asked.append(rng) or [])
    feishu.sheet_values_rows(SHEET, "A", "C", 2, 2 + 4750)
    assert feishu._SHEET_READ_BLOCK_ROWS == 4750
    assert asked == ["A2:C4751", "A4752:C4752"]


def test_row_numbers_re_anchor_at_each_block_head(monkeypatch):
    """块尾空行被裁掉之后,下一块的行号仍从**块首**起算 —— 这是回写的命根子。

    飞书只裁**范围尾部**的空行(中段空行仍占位),所以块尾一空,那一块就少
    返几行。调用方若按返回序号手算(旧的 `i + 2`),后面每一块会整体上移几行,
    而 clear_sheet/match_sheet/upc_pool 拿这个号去拼 `C{行}:F{行}` 定点回写 ——
    错一格就是把结果写到别人那一行上,两边都看不出报错。
    """
    block = feishu._SHEET_READ_BLOCK_ROWS
    holes = {block, block + 1}                  # 第一块的**最后两行**是空的
    grid = {r: [f"v{r}"] for r in range(2, block + 6) if r not in holes}

    def fake_raw(sheet, rng):
        head, tail = rng.split(":")
        got = [grid.get(r, []) for r in range(int(head[1:]), int(tail[1:]) + 1)]
        while got and not got[-1]:              # 只裁范围尾部
            got.pop()
        return got

    monkeypatch.setattr(feishu, "_values_raw", fake_raw)
    pairs = feishu.sheet_values_rows(SHEET, "A", "A", 2, block + 5)
    assert [n for n, _row in pairs] == sorted(grid)          # 空行不占号,其余不漂
    assert all(row == [f"v{n}"] for n, row in pairs)         # 号与内容一一对上
    assert (block + 2, [f"v{block + 2}"]) in pairs           # 第二块首行不上移
    assert [n for n, _ in pairs if n in holes] == []


def test_raw_read_is_private_and_small_shell_is_the_only_public_shortcut(monkeypatch):
    """裸读降为私有 _values_raw;对外只有 sheet_values_small(且写明无界禁用)。"""
    got: list[str] = []
    monkeypatch.setattr(feishu, "_values_raw",
                        lambda sheet, rng: got.append(rng) or [["v"]])
    assert feishu.sheet_values_small(SHEET, "A1:B1") == [["v"]]
    assert got == ["A1:B1"]
    doc = feishu.sheet_values_small.__doc__
    assert "无界范围禁用" in doc and "sheet_values_rows" in doc


def test_the_old_sheet_values_name_is_gone_for_good():
    """旧名 sheet_values 已删,全仓零引用 —— 这条钉的是 F2 的完工线。

    F1 曾留一个同名转发(不迁调用点就落地,免得炸调用方),F2 把无界范围换到
    sheet_values_rows、小范围换到 sheet_values_small 之后连同注释一并删除。
    它一旦复活,「读只有一条通道」就又开了个后门:那个名字既不分块也不兜底,
    却长得像正路(2026-08-19 上架表 21 列一把读撞 90221 就是这么来的)。
    """
    assert not hasattr(feishu, "sheet_values")
    root = Path(__file__).resolve().parents[1]
    # cli.py 也要扫:它同样 `from api import feishu`(通知那条路),
    # 只扫四个目录会让"全仓零引用"这句话在唯一的顶层文件上落空
    files = [root / "cli.py"]
    for d in ("api", "services", "workflows", "registry"):
        files += sorted((root / d).rglob("*.py"))
    offenders = []
    for py in files:
        for n, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bsheet_values\s*\(", line):
                offenders.append(f"{py.relative_to(root)}:{n}")
    assert not offenders, f"旧名 sheet_values 复活:{offenders}"
    # 反向对照:正则要抓得住旧名、抓不到两条新通道,否则这条就是永远绿的空断言
    assert re.search(r"\bsheet_values\s*\(", "feishu.sheet_values(s, 'A1:A1')")
    assert not re.search(r"\bsheet_values\s*\(",
                         "feishu.sheet_values_rows(s, 'A', 'C', 2, 9)")
    assert not re.search(r"\bsheet_values\s*\(",
                         "feishu.sheet_values_small(s, 'A1:A1')")


# ══════════════════════════════════════════════════════════════════════════════
#  ④ 频控:reset 头精确等待 / 月度配额不可重试
# ══════════════════════════════════════════════════════════════════════════════

def _serve(responses):
    """按顺序吐 responses 的 MockTransport;token 请求恒成功且不计数。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tt",
                                             "expire": 7200})
        calls.append(1)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    feishu._client = httpx.Client(transport=httpx.MockTransport(handler))
    return calls


def test_ratelimit_reset_header_drives_the_wait(slept):
    """99991400 带 x-ogw-ratelimit-reset 时按它等,不用 1/2/4/8 阶梯。

    官方原句:「使用该响应头延迟请求是解除限频的最好方法。1. 等待
    x-ogw-ratelimit-reset 中指定的秒数。2. 重试请求。」
    ⚠ 旧版 OpenAPI(本仓 sheets/v2 就是)限流时 HTTP 码是 **400** 不是 429,
    所以判据只能认 code=99991400。
    """
    _serve([httpx.Response(400, json={"code": 99991400,
                                      "msg": "request trigger frequency limit"},
                           headers={"x-ogw-ratelimit-reset": "7"}),
            httpx.Response(200, json={"code": 0, "data": {"ok": 1}})])
    assert feishu._call("GET", "/open-apis/x") == {"ok": 1}
    assert slept == [7]                     # 阶梯首档是 1s,这里按头等了 7s


def test_ratelimit_reset_header_is_capped(slept):
    """头给了个离谱的秒数也不能真吊死在这儿:上限 60s(工程值)。"""
    _serve([httpx.Response(400, json={"code": 99991400, "msg": "limit"},
                           headers={"x-ogw-ratelimit-reset": "9999"}),
            httpx.Response(200, json={"code": 0, "data": {}})])
    feishu._call("GET", "/open-apis/x")
    assert slept == [feishu._RATELIMIT_RESET_CAP_SECS] == [60]


def test_without_the_header_the_ladder_still_applies(slept):
    """没有头(官方没承诺一定带)就退回现行阶梯,不新开第二套退避。"""
    _serve([httpx.Response(400, json={"code": 90217, "msg": "TooManyRequest"}),
            httpx.Response(200, json={"code": 0, "data": {}})])
    feishu._call("GET", "/open-apis/x")
    assert slept == [feishu._BACKOFF[0]] == [1]


def test_garbage_header_falls_back_to_the_ladder(slept):
    """头是空的/不是数字 → 阶梯兜底,不能把 None 传给 sleep。"""
    _serve([httpx.Response(400, json={"code": 99991400, "msg": "limit"},
                           headers={"x-ogw-ratelimit-reset": "soon"}),
            httpx.Response(200, json={"code": 0, "data": {}})])
    feishu._call("GET", "/open-apis/x")
    assert slept == [1]


def test_monthly_quota_exhausted_is_never_retried(slept):
    """99991403 = 月度调用量打满,不是频控:直接抛,一次都不重试。

    官方原句「99991403 | This month's API call quota has been exceeded |
    本月 API 调用次数已达上限，请联系企业管理员升级飞书版本。」配额按自然月
    1 号刷新——退避多久都不会好,继续重试只是把剩下的额度也烧掉。
    """
    calls = _serve([httpx.Response(200, json={
        "code": 99991403,
        "msg": "This month's API call quota has been exceeded"})])
    with pytest.raises(feishu.FeishuError) as ei:
        feishu._call("GET", "/open-apis/x")
    assert ei.value.code == 99991403
    assert "月度 API 配额耗尽,不可重试" in ei.value.msg
    assert "升级版本或等下月 1 号" in ei.value.msg
    assert len(calls) == 1, "重试了 —— 配额码不该进可重试集合"
    assert slept == []
    assert 99991403 not in feishu._TRANSIENT_CODES


def test_transient_double_track_survives():
    """瞬时判定的两条轨(int code + 小写子串)都还在 —— 新接口是否暴露 int code
    未经全面验证,子串轨不可删。"""
    assert feishu._is_transient(90235, "")                  # int 轨
    assert feishu._is_transient(424242, "Request Timeout")   # 子串轨
    assert not feishu._is_transient(1254045, "FieldNameNotFound")
