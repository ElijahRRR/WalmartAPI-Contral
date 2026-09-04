"""判定并发的共用件:**首条串行预热**(DeepSeek 前缀缓存;规格 §3.9)。

L3 的 system prompt(S1–S4)是一段静态前缀 —— 44 篇官方英文全文,6 万 token
上下。DeepSeek 按**请求前缀**计缓存:同一段前缀第一次发过去按未命中价,之后
命中价便宜一个数量级。而判定链起跑就是 128 并发,前 ~128 条会**同时**看到
"这段前缀还没被缓存过",一批全按未命中价付(单条未命中约是命中的十几倍)。

所以第一条**同步**判完(把前缀送进供应商侧的缓存),其余再进线程池。省的是
一批 miss 价,代价是一次串行调用(秒级)。

⚠ 与 `catalog.llm_cache` 无关:那是我们自己的整段-messages 缓存(键含 user 段,
每个产品都不同),这里说的是**供应商侧的前缀缓存**。预热不改任何缓存键。

消费方两条(所以这条规则只有这一份实现):`workflows/product_audit` 生产判定、
`workflows/audit_replay` 回放评估 —— 两条链发的是同一段前缀。
"""

from concurrent.futures import Future


def submit_chunk(ex, todo: list, judge, warm: bool) -> tuple[dict, int]:
    """输入:线程池 + 本块候选 [(键, 判定入参)] + 判定函数 + 要不要预热
    → 输出:({future: 键}, 预热条数)。

    预热条数只会是 0 或 1。`warm=False`、或本块只有一条时**不预热** ——
    只有一条产品谈不上"省一批 miss":它自己就是那一次未命中。

    预热那条也装进 `Future` 返回,不单独走一条路径:调用方的 `as_completed`
    循环、落库、计数、单行异常隔离于是一个字都不用改(分两条写 = 预热那条的
    失败处置迟早漂移,而且不会报错)。判定抛的异常原样装进 future,由调用方
    `fut.result()` 时抛出,落进它自己的隔离逻辑。
    """
    futs: dict = {}
    items = list(todo)
    warmed = 0
    if warm and len(items) > 1:
        key, arg = items.pop(0)
        first: Future = Future()
        try:
            first.set_result(judge(arg))
        except Exception as e:              # noqa: BLE001 —— 原样转交调用方
            first.set_exception(e)
        futs[first] = key
        warmed = 1
    futs.update({ex.submit(judge, arg): key for key, arg in items})
    return futs, warmed


__all__ = ["submit_chunk"]
