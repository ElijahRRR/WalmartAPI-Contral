"""飞书标准通道的三道守门(F2 收尾,2026-08-27)。

F1/F2 把飞书读写收成了唯一通道(写 `sheet_write_ranges` / 读 `sheet_values_rows`,
小范围薄壳 `sheet_values_small`),把限额收成了 api/feishu.py 顶部一张登记表。
收口只值钱一次 —— **守不守得住**才决定它三个月后还在不在。本文件不测行为,
只守边界,三条:

  ① **端点路径字面量只准长在 api/feishu.py 里。** services/workflows 一旦自己
     拼出 `/open-apis/sheets/...` 去发请求,分块、90221 对半、同表串行锁、批间
     节流全绕过了,而它长得跟正路一模一样(2026-08-19 上架表 21 列一把读撞
     90221 就是这个形状)。
  ② **小范围薄壳只准接一眼看得出上界的范围。** `sheet_values_small` 不分块、
     不兜底;范围上界一旦随表长增长,它就是一个伪装成小范围的大范围读。
     裸读 `_values_raw` 同理:私有就该只有通道内部两个人用。
  ③ **限额常量只准在登记表里出生,且每条说得出出处。** 散落出第二个出处必漂
     (F1 并掉的 `_SHEET_WRITE_BLOCK_ROWS=4000` 就是这么来的:官方 5000 的
     95% 是 4750,那个 4000 谁也说不清出处)。

与 tests/test_feishu_channels.py 的分工:那边守**行为**(预算切批、90227 对半、
频控 reset 头、99991403 不重试)与登记表注释的完整三件套(官方原值 + URL +
核对日期);本文件守**边界**(谁在通道外面、常量在不在登记表里出生),两边
角度不同,不是一件事做两遍。

限额口径与四个错误码的分工见 docs/conventions.md §八;官方原句对照全表在
refdata/feishu_limits.tsv。
"""

import ast
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 「小范围」的阈值:行差 ≤ 10 行。取 10 不取 1 是因为现实里的小范围除了表头
#: 单行,还有「表头 + 前几行样例」这类固定几行的读法;取 10 不取 100 是因为
#: 再往上就该由标准读通道分块了 —— 薄壳没有 90221 兜底,赌的是"这几行不可能
#: 撑爆 10MB 响应",十行以内这个赌注才成立。
_SMALL_RANGE_MAX_ROWS = 10

# ══════════════════════════════════════════════════════════════════════════════
#  白名单(**要改守门,先改这里,别删断言**)
#
#  守门要能被维护:例外必须显式登记、写得出理由,而不是被红了就把断言注释掉。
#  空表 = 当前零例外,不是"还没启用"。登记项失效(文件/函数/常量没了)会被
#  下面的"白名单不许烂掉"用例点名,免得白名单越攒越像筛子。
# ══════════════════════════════════════════════════════════════════════════════

#: ① 允许出现飞书表格端点路径字面量的**通道外**文件 → 为什么。
#: 粒度是整文件(不给逐行豁免):要么这个文件真有正当理由自己发请求,要么
#: 它就该改回走通道 —— 逐行豁免会让白名单变成一张"哪行红了就加一行"的清单。
_ENDPOINT_LITERAL_OK: dict[str, str] = {}

#: ② 允许把范围上界拼出来的 `sheet_values_small` 调用点。
#: 键 =(仓内相对路径, 所在函数名);值 =(封顶常量名, 为什么)。
#: 封顶常量必须真的存在于该模块 —— 它才是"这个范围不会随表长增长"的凭据。
_DYNAMIC_RANGE_OK: dict[tuple[str, str], tuple[str, str]] = {
    ("services/blacklist_sheet.py", "next_empty"): (
        "_SCAN_BLOCK",
        "逐段短路扫列 A 的首个空行:每段行数由 blacklist_sheet._SCAN_BLOCK 固定"
        "封顶(不随表长增长),而且要的就是扫到空行立刻返回 —— 走分块通道会把"
        "整表读完,正好把这里省下的时间又赔回去(理由原文见该调用点上方注释)"),
}

#: ③ 允许长在登记表**之外**的限额型常量 → 为什么不属于登记表。
_LIMIT_CONST_OUTSIDE_REGISTRY_OK: dict[str, str] = {
    "_SHEET_CELL_MAX_CHARS":
        "业务脏数据闸(20000 超了截断 + 告警、轮次照走),不是通道限额;与通道"
        "硬闸 _SHEET_CELL_HARD_MAX_CHARS(40000,超了直接抛)两层分工,"
        "见 api/feishu.py 该常量上方的 #: 注释与 docs/conventions.md §八",
    "_MAX_ATTEMPTS":
        "重试次数,不是飞书限额:退避参数照抄旧系统实测值(见模块头注与 "
        "docs/legacy_survey.md),官方对'重试几次'没有也不会有规定",
}


# ══════════════════════════════════════════════════════════════════════════════
#  取材
# ══════════════════════════════════════════════════════════════════════════════

#: 唯一读写通道本体。三道守门的射程都是"**它**之外",不是"api/ 之外"——
#: 规矩原话是「只准出现在 api/feishu.py」,漏掉 api/ 的兄弟模块就等于给
#: api/scraper.py 自己拼一条 sheets 路留了后门。
_CHANNEL = "api/feishu.py"


def _prod_files() -> list[tuple[str, Path]]:
    """输入:无 → 输出:[(仓内相对路径, 绝对路径)] —— 唯一通道之外的全部生产代码。

    services/workflows 是守门要求的最小射程;registry、cli.py 与 **api/ 里除
    api/feishu.py 以外的模块**一并纳入:它们同样在通道之外,同样没有理由自己
    去碰飞书端点。tests/ 不在射程内(桩 `_values_raw`、桩 `sheet_values_small`
    正是测试该干的事)。
    """
    files = [ROOT / "cli.py"]
    for d in ("services", "workflows", "registry", "api"):
        files += sorted((ROOT / d).rglob("*.py"))
    return [(rel, p) for rel, p in ((str(p.relative_to(ROOT)), p) for p in files)
            if "__pycache__" not in p.parts and rel != _CHANNEL]


def _module_of(rel: str):
    """输入:仓内相对路径 → 输出:导入好的模块对象。"""
    return importlib.import_module(rel[:-3].replace("/", "."))


# ══════════════════════════════════════════════════════════════════════════════
#  ① 端点路径字面量只准长在 api/feishu.py 里
# ══════════════════════════════════════════════════════════════════════════════

_ENDPOINT_RE = re.compile(r"open-apis/(?:sheets|bitable)")

#: 文本轨多容忍一种写法:`"/open-apis/" + "sheets/…"` 里,`/` 与 `sheets` 之间
#: 只隔着引号/加号/空格(全是非词字符),`\W*` 正好跨得过去;中文说明文字跨不
#: 过去(`\w` 认中文),所以不会把「open-apis/ 下的 sheets 接口」这种散文判红。
_ENDPOINT_TEXT_RE = re.compile(r"open-apis/\W*(?:sheets|bitable)")


def test_sheet_endpoint_paths_live_only_in_the_api_layer():
    """通道外零端点字面量:AST 轨看字符串常量,文本轨连注释一起看。

    两条轨都要:AST 轨认的是**真的会被发出去**的那些字面量(含 f-string 的
    字面段),文本轨兜住紧挨着拼的 `"/open-apis/" + "sheets/..."`,以及
    "先在注释里写好路径再抄进代码"的前一步。
    ⚠ **两轨都拦不住的仍有一种**:路径被拆到别处再拼(`BASE + "/sheets/v2"`,
    BASE 在另一行/另一文件)。守门只挡顺手写下的那一类,别把它当成"绕不过去"——
    真要在通道外自己发请求,靠的是 review,不是这两条正则。
    """
    offenders: list[str] = []
    for rel, path in _prod_files():
        if rel in _ENDPOINT_LITERAL_OK:
            continue
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and _ENDPOINT_RE.search(node.value):
                offenders.append(f"{rel}:{node.lineno} 字符串 {node.value!r}")
        for n, line in enumerate(src.splitlines(), 1):
            if _ENDPOINT_TEXT_RE.search(line):
                offenders.append(f"{rel}:{n} 文本 {line.strip()[:70]}")
    assert not offenders, (
        "飞书表格端点路径只准出现在 api/feishu.py(唯一读写通道):\n  "
        + "\n  ".join(sorted(set(offenders))))


# ══════════════════════════════════════════════════════════════════════════════
#  ② 小范围薄壳只接小范围;裸读不许外泄
# ══════════════════════════════════════════════════════════════════════════════

_DYN = "{}"                                     # f-string 里被拼进来的那一段
_ONE_CELL_RE = re.compile(r"^[A-Z{}]+\d+$")     # 'A1' 这种单格
_RANGE_RE = re.compile(r"^[A-Z{}]+(\d+|\{\}):[A-Z{}]+(\d+|\{\})$")


def _calls_with_scope(tree: ast.AST, name: str) -> list[tuple[ast.Call, str]]:
    """输入:模块 AST + 被调函数名 → 输出:[(Call 节点, 所在函数名)]。"""
    out: list[tuple[ast.Call, str]] = []

    def walk(node, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (child.name
                     if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                     else scope)
            if isinstance(child, ast.Call):
                f = child.func
                called = (f.attr if isinstance(f, ast.Attribute)
                          else f.id if isinstance(f, ast.Name) else "")
                if called == name:
                    out.append((child, scope))
            walk(child, inner)

    walk(tree, "<module>")
    return out


def _arg_node(call: ast.Call, index: int, keyword: str):
    """输入:Call + 位置/关键字 → 输出:那个实参的节点(没有则 None)。"""
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return call.args[index] if len(call.args) > index else None


def _range_text(node) -> str | None:
    """输入:范围实参节点 → 输出:范围文本(拼进来的段记成 `{}`),整段动态则 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant)
            and isinstance(part.value, str) else _DYN
            for part in node.values)
    return None


def _row_span(rng: str) -> int | None:
    """输入:范围文本 → 输出:跨几行;行号不是字面量(或范围无界)时 None。

    列可以是拼出来的(`A1:{width}1` 的宽度按登记列数算,与表长无关),
    **行不行** —— 行号一旦是变量,上界就可能随表长走,那是大范围读。
    """
    if _ONE_CELL_RE.match(rng):
        return 1
    m = _RANGE_RE.match(rng)
    if not m or _DYN in (m.group(1), m.group(2)):
        return None
    return int(m.group(2)) - int(m.group(1)) + 1


def test_small_shell_only_takes_ranges_you_can_bound_by_eye():
    """`sheet_values_small` 的通道外调用点:要么行号字面量且够小,要么进白名单。

    薄壳是"省掉一次没必要的分块循环"的捷径,不是"少写几个参数"的捷径:
    它不分块、不兜 90221。范围上界随表长增长的读取一律走 sheet_values_rows。
    """
    offenders: list[str] = []
    for rel, path in _prod_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call, func in _calls_with_scope(tree, "sheet_values_small"):
            where = f"{rel}:{call.lineno}({func})"
            rng = _range_text(_arg_node(call, 1, "a1_range"))
            rows = _row_span(rng) if rng is not None else None
            if rows is not None:
                if rows > _SMALL_RANGE_MAX_ROWS:
                    offenders.append(
                        f"{where} 范围 {rng} 跨 {rows} 行,超小范围阈值 "
                        f"{_SMALL_RANGE_MAX_ROWS}:改走 sheet_values_rows")
                continue
            entry = _DYNAMIC_RANGE_OK.get((rel, func))
            if entry is None:
                offenders.append(
                    f"{where} 范围 {rng or '(整段动态)'} 的行上界不是字面量"
                    "(拼接或无界),未在 _DYNAMIC_RANGE_OK 登记封顶常量")
                continue
            cap = entry[0]
            if not isinstance(getattr(_module_of(rel), cap, None), int):
                offenders.append(
                    f"{where} 白名单声明的封顶常量 {cap} 已不在该模块里,"
                    "范围失去封顶")
    assert not offenders, (
        "小范围薄壳被当大范围读通道用了:\n  " + "\n  ".join(offenders))


def test_the_private_raw_read_never_leaks_out_of_the_api_layer():
    """`_values_raw` 是私有裸读:不分块、不兜底,只准通道内部两个人用。

    它一旦被通道外(services/workflows,以及 api/ 的兄弟模块)拿去用,读通道
    就等于没有 —— 而且从调用点看不出区别,坏起来是 90221(2026-08-19 实见),
    不是语法错。
    """
    offenders = [f"{rel}:{n}"
                 for rel, path in _prod_files()
                 for n, line in enumerate(path.read_text(encoding="utf-8")
                                          .splitlines(), 1)
                 if re.search(r"\b_values_raw\b", line)]
    assert not offenders, f"私有裸读泄到唯一通道({_CHANNEL})之外:{offenders}"


# ══════════════════════════════════════════════════════════════════════════════
#  ③ 限额常量只在登记表里出生,且逐条说得出出处
# ══════════════════════════════════════════════════════════════════════════════

_FEISHU_PY = ROOT / "api" / "feishu.py"

#: 名字里带这些词 + 值是个数 = 限额型常量。按名字判是刻意的:守的是"下一个人
#: 随手在文件中段写 `_XXX_MAX = 4000`"这件事,而那时它还没有出处可查。
_LIMIT_WORDS = ("MAX", "LIMIT", "BUDGET", "BLOCK", "SIZE", "PAGE", "ROWS",
                "COLS", "CHARS", "BATCH", "THROTTLE", "CAP", "PER_REQUEST")


def _registry_span() -> tuple[int, int]:
    """输入:无 → 输出:限额登记表那一段的(起始行, 结束行),1-based 半开区间。"""
    lines = _FEISHU_PY.read_text(encoding="utf-8").splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if "限额登记表(所有者" in ln), 0)
    end = next((i + 1 for i, ln in enumerate(lines)
                if ln.startswith("_TRANSIENT_CODES")), 0)
    assert 0 < start < end, "限额登记表的表头横幅或结束锚点没找到,是不是被搬走了?"
    return start, end


def _module_constants() -> list[tuple[str, int, str, bool]]:
    """输入:无 → 输出:api/feishu.py 的 [(常量名, 行号, 整行源码, 值是不是数)]。

    走 `ast.walk` 而不是只看模块顶层:藏在函数体(或 `if`/`try` 块)里的
    `_XXX_MAX = 4000` 同样是"在登记表外出生",只看 body 会漏掉它 —— 而那正是
    最容易顺手写下的位置。
    """
    src = _FEISHU_PY.read_text(encoding="utf-8")
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        target = (node.targets[0] if isinstance(node, ast.Assign)
                  else node.target if isinstance(node, ast.AnnAssign) else None)
        if not isinstance(target, ast.Name):
            continue
        if not re.fullmatch(r"_?[A-Z][A-Z_0-9]*", target.id):
            continue
        value = node.value
        numeric = (isinstance(value, ast.Constant)
                   and isinstance(value.value, (int, float))
                   and not isinstance(value.value, bool))
        out.append((target.id, node.lineno, lines[node.lineno - 1], numeric))
    return sorted(out, key=lambda t: t[1])      # walk 是广度序,报错按行号读才顺


def test_every_limit_constant_in_the_registry_cites_where_it_came_from():
    """登记表里逐条都要有行内出处注释:官方原值,或自报「工程值」。

    漏注即红 —— 一个没有出处的数字,下一个人既不敢改也不敢信,只会在别处
    再造一个。(完整三件套「官方 + URL + 核对日期」的格式由
    tests/test_feishu_channels.py::test_every_limit_constant_cites_an_official_source
    钉,这里钉的是"新增一条不许漏"。)
    """
    start, end = _registry_span()
    inside = [(name, line) for name, lineno, line, _num in _module_constants()
              if start <= lineno < end]
    assert inside, "限额登记表里一条常量都没有了"
    for name, line in inside:
        note = line.split("#", 1)[1] if "#" in line else ""
        assert "官方" in note or "工程值" in note, (
            f"{name} 的行内注释既没写官方出处也没自报工程值:"
            "查得到就写官方原值 + URL + 核对日期,查不到就明写「工程值,非官方」")


def test_limit_constants_are_not_born_outside_the_registry():
    """限额型常量不许在登记表外出生 —— 第二个出处必漂。

    F1 并掉的 `_SHEET_WRITE_BLOCK_ROWS = 4000` 就是反例:它跟登记表里的
    4750 是同一件事的两个数字,谁也说不清 4000 的出处。真不属于限额的
    (业务闸、重试次数)登记进 _LIMIT_CONST_OUTSIDE_REGISTRY_OK 并写明理由。
    """
    start, end = _registry_span()
    offenders = [
        f"api/feishu.py:{lineno} {name}"
        for name, lineno, _line, numeric in _module_constants()
        if numeric and not (start <= lineno < end)
        and any(w in name for w in _LIMIT_WORDS)
        and name not in _LIMIT_CONST_OUTSIDE_REGISTRY_OK]
    assert not offenders, (
        "限额型常量长在了登记表外(要么搬进登记表并带出处,要么登记进"
        "白名单说明它不是限额):\n  " + "\n  ".join(offenders))


# ══════════════════════════════════════════════════════════════════════════════
#  白名单不许烂掉
# ══════════════════════════════════════════════════════════════════════════════

def test_the_whitelists_do_not_rot():
    """白名单每一条都要还指得着东西:指空了就是该删的历史,不是豁免。"""
    stale: list[str] = []
    for rel in _ENDPOINT_LITERAL_OK:
        if not (ROOT / rel).exists():
            stale.append(f"_ENDPOINT_LITERAL_OK: {rel} 文件已不在")
    for (rel, func), (cap, reason) in _DYNAMIC_RANGE_OK.items():
        assert reason.strip(), f"_DYNAMIC_RANGE_OK[{rel}, {func}] 没写理由"
        path = ROOT / rel
        if not path.exists():
            stale.append(f"_DYNAMIC_RANGE_OK: {rel} 文件已不在")
            continue
        names = {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if func not in names:
            stale.append(f"_DYNAMIC_RANGE_OK: {rel} 里已没有 {func}()")
        elif not isinstance(getattr(_module_of(rel), cap, None), int):
            stale.append(f"_DYNAMIC_RANGE_OK: {rel} 里已没有封顶常量 {cap}")
    known = {name for name, _lineno, _line, _num in _module_constants()}
    for name, reason in _LIMIT_CONST_OUTSIDE_REGISTRY_OK.items():
        assert reason.strip(), f"_LIMIT_CONST_OUTSIDE_REGISTRY_OK[{name}] 没写理由"
        if name not in known:
            stale.append(f"_LIMIT_CONST_OUTSIDE_REGISTRY_OK: {name} 已不在 api/feishu.py")
    assert not stale, "白名单有失效条目,删掉它们:\n  " + "\n  ".join(stale)
