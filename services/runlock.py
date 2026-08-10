"""工作流单实例锁(flock)。cli.py 与「借别人的锁」的工作流共用这一份实现。

为什么要能"借锁":有些工作流会**就地做另一条工作流的活**。今天只有一处——
`order_audit -p wait=1` 推完采集后要就地跑增量摄取,而增量摄取的游标
(`ops.cursors` 里 name='product_ingest')是**独占推进**的:两个进程同时拉
`/api/export/incremental` 并各自落 next_cursor,后写的会把先写的盖掉,
中间那一段记录**永远不会再被拉一次**(游标只前进不回头)——两侧都不报错,
只是产品中心少了一批数据。

所以借活也要借锁:谁动 product_ingest 的游标,谁就得先拿 product_ingest 的锁。
拿不到就跳过这一步(不是失败:说明真的 product_ingest 正在跑,数据照样会进来)。

    with runlock.hold("product_ingest") as got:
        if not got:
            ...            # 别人正在跑,这轮跳过
"""

import contextlib
import fcntl
import logging
import os
from datetime import datetime, timezone

from registry import paths

logger = logging.getLogger("services.runlock")


def acquire(name: str):
    """输入:锁名(=工作流名)→ 输出:文件句柄(拿到)或 None(已被占用)。

    ⚠ **返回值必须留着**:句柄被 GC 掉即释放锁。cli.py 靠把它绑在局部变量上
    活到进程结束,别改成 `_acquire_lock(...)` 丢弃返回值那种写法。
    """
    d = paths.locks_dir()
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / f"{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n")
    fh.flush()
    return fh


@contextlib.contextmanager
def hold(name: str):
    """输入:锁名 → 输出:上下文里 True=拿到锁 / False=已被占用(不抛错)。

    退出时释放。**拿不到不是错误**——调用方按"这轮跳过"处理,
    因为占着锁的那个进程正在干同一件事。
    """
    fh = acquire(name)
    try:
        yield fh is not None
    finally:
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
