"""飞书通知的排版规范(所有者定稿 2026-08-17)。**纯函数,零 I/O。**

参照物是旧系统那份日报:标题 + 分节 + 每行一个指标 + 数字带千分位,
一眼能找到重要信息。所有者原话:「这种样式分隔清楚,不臃肿。
且我能很明确的看到重要信息」。

## 五条规矩(排版只是表面,前三条才是"不臃肿"的实质)

1. **第一行 = 结论 + 最重要的那一个数。** 飞书的消息列表、手机推送、
   `ops.runs` 的表格视图都只显示第一行 —— 第一行说不清,后面写多少都白写。

2. **主指标恒打印,例外计数为 0 则省略。** 这是唯一安全的零抑制口径:
   主指标是"这一轮干了多少"(入库 0 行**是重要信息**,必须看见);
   例外计数是"有没有出岔子"(`风控拦截 0` 不是信息,是噪声)。
   现状的臃肿几乎全来自后者 —— `闸门:非ACTIVE店 0,超配额 0,PT无spec 0,
   风控拦截 0,去重 1,黑名单 0,待数据源 0,数据过滤 1,配送超时 0` 里,
   真正发生的只有两项。
   ⚠ 别把这条推广成"小数字也省":省的判据是**恰好 0**,不是"看着不重要"。
   本仓的教训是静默常态化(主路径坏了没人知道),所以 1 也要报。

3. **每条 ⚠ 自带处置。** 只说"有 3 行卡住"等于把排查甩给读的人;
   说"3 行卡在 executing,等 catalog_sync 观测(店铺 A085 未被扫)"才可行动。

4. **数字千分位,金额带 $ 与两位小数。** `29860` 与 `29,860` 的差别是
   看一眼和数一遍。

5. **一条链一条通知**:成功的步骤压成一行(它自己的第一行),失败/跳过的
   给全文。七步链发七段全文 = 谁都不会读第八次。这条在 `cli.py` 实现。

## 怎么用

    from services import notify_fmt as nf

    return nf.summary(
        nf.head("上架", f"提交 {nf.num(ok)} 件 / {stores} 店"),
        metrics=[("待上架", nf.num(len(pending))),
                 ("实际提交", nf.num(ok)),
                 ("本日剩余配额", nf.num(quota_left))],
        notes=[("全局去重", dedup), ("产品占用", claimed),
               ("库存不足", low_stock)],          # 0 的自动消失
        warns=[f"⚠ UPC 池余量 {left} 个,不够明天(补池:cli.py upc_sync)"
               if left < 500 else ""],           # 空串自动消失
    )
"""


def num(v, unit: str = "") -> str:
    """输入:数字 → 输出:带千分位的串(None/空 → '—')。"""
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    body = f"{int(n):,}" if float(n).is_integer() else f"{n:,.2f}"
    return f"{body}{unit}"


def money(v, symbol: str = "$") -> str:
    """输入:金额 → 输出:$1,853.33(None → '—')。"""
    if v is None or v == "":
        return "—"
    try:
        return f"{symbol}{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def pct(part, whole) -> str:
    """输入:部分/整体 → 输出:'12.3%';整体为 0 时给 '—'(不是 0%)。

    分母为 0 报 0% 是在撒谎:"一个都没成"与"压根没有"是两件事。
    """
    try:
        w = float(whole)
        if not w:
            return "—"
        return f"{float(part) / w * 100:.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def head(name: str, gist: str = "", when: str = "") -> str:
    """输入:名字 + 一句话结论(+日期)→ 输出:第一行。

    形态:`名字 · 日期 | 结论`。名字在最前 —— 飞书列表里同一时间有好几条
    通知,先看见是谁,再看见成没成。
    """
    left = f"{name} · {when}" if when else name
    return f"{left} | {gist}" if gist else left


def _rows(pairs, drop_zero: bool) -> list[str]:
    out = []
    for item in pairs or ():
        if isinstance(item, str):           # 直接给整行(warns 用)
            if item.strip():
                out.append(item.strip())
            continue
        label, value = item
        if drop_zero and (value in (0, "0", None, "", [], {})):
            continue
        out.append(f"· {label} {value if isinstance(value, str) else num(value)}")
    return out


def summary(first_line: str, *, metrics=(), notes=(), warns=(),
            sections=(), tail: str = "") -> str:
    """输入:第一行 + 主指标/例外/告警/自定义节 → 输出:整条通知正文。

    · `metrics` **恒打印**(哪怕 0);
    · `notes` 为 0/空的自动消失(规矩 2);
    · `warns` 收 ⚠ 整行,空串自动消失(方便写成条件表达式);
    · `sections` = [(小标题, [行, …])],空节自动消失;
    · `tail` 一行收尾(下一步命令之类),空则省。
    """
    lines = [first_line]
    m = _rows(metrics, drop_zero=False)
    if m:
        lines += m
    n = _rows(notes, drop_zero=True)
    if n:
        lines.append("—— 其中 ——")
        lines += n
    for title, rows in sections or ():
        rows = [r for r in (rows or ()) if str(r).strip()]
        if rows:
            lines.append(f"—— {title} ——")
            lines += [f"· {r}" if not str(r).startswith(("·", "⚠", " "))
                      else str(r) for r in rows]
    w = _rows(warns, drop_zero=True)
    if w:
        lines += w
    if tail.strip():
        lines.append(tail.strip())
    return "\n".join(lines)


def first_line_of(text: str) -> str:
    """输入:一条摘要全文 → 输出:第一行(链通知折叠成功步骤时用)。"""
    for ln in str(text or "").splitlines():
        if ln.strip():
            return ln.strip()
    return ""
