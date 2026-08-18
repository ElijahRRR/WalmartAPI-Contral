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
import errno
import fcntl
import logging
import os
import stat as _stat
from datetime import datetime, timezone

from registry import paths

logger = logging.getLogger("services.runlock")


class LockUnavailable(RuntimeError):
    """锁**建不出来**(权限/磁盘),不是"被别人占着"。

    两者必须分开:被占着是常态(返回 None,调用方按"这轮跳过"处理),
    建不出来是配置坏了,必须炸出来让人修。
    """


def _owner_of(path) -> str:
    """输入:路径 → 输出:'属主=xxx 权限=-rw-r--r--',拿不到就说拿不到。"""
    try:
        st = path.stat()
    except FileNotFoundError:
        return "不存在"
    except OSError as e:                                        # noqa: BLE001
        return f"读不到属性({e})"
    try:
        import pwd
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (ImportError, KeyError):
        owner = str(st.st_uid)
    return f"属主={owner} 权限={_stat.filemode(st.st_mode)}"


def _me() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return str(os.getuid())


def _looks_writable(target) -> bool:
    """输入:锁文件路径 → 输出:按**普通文件权限**看,当前用户本该写得动吗。

    ⚠ `os.access` 只看 DAC(属主/组/mode),**看不见沙箱与 MAC**。这正是它在这里
    有用的原因:DAC 说能写、实际却被拒 ⇒ 拦你的是 DAC 之外的东西。
    """
    if target.exists():
        return os.access(target, os.W_OK)
    return target.parent.is_dir() and os.access(target.parent, os.W_OK)


def _permission_hint(target, err: OSError | None = None) -> str:
    """输入:锁文件路径(+原始 OSError)→ 输出:一句能直接照着修的诊断。

    ⚠ 原先这里只让 OSError 裸奔上去,报出来就是「Permission denied: …lock」——
    人(和替我们看告警的智能体)只能猜。所以把属主/权限/当前用户一次说清。

    ⚠⚠ **必须先分清 EACCES 与 EPERM**(2026-08-17 生产两次踩反方向):
      · `EACCES`(errno 13)= 普通文件权限不够。最常见是某次用 sudo 跑过、
        锁文件归了 root(此时**目录**是好的,改目录没用)。
      · `EPERM`(errno 1)= 属主与权限都正常**却仍被拒** ⇒ 拦你的不是文件权限,
        是**沙箱 / MAC**(macOS 上典型是 Codex 之类的智能体沙箱只放行仓库目录,
        而 DATA_ROOT 是仓库的**同级**目录;也可能是 TCC 或 uchg 标志)。
        这一档去 chmod/chown 是白费力气 —— 实测那一次属主是对的,而首版这段
        提示照样在劝人 chown。

    ⚠ 需要改属主时建议 `chown` 而**不是** `rm`:删掉锁文件不会让此刻正持有 flock
    的进程释放锁,只会让新进程创建一个**新的 inode** 去锁 —— 于是两个实例同时跑
    同一条危险链,而互斥"看起来还在"。chown 保留 inode,语义不变。
    """
    facts = (f"锁文件 {target}({_owner_of(target)});"
             f"锁目录 {target.parent}({_owner_of(target.parent)});"
             f"DATA_ROOT {target.parent.parent}({_owner_of(target.parent.parent)});"
             f"当前用户={_me()}")
    if err is not None:
        facts += f";errno={err.errno}({errno.errorcode.get(err.errno, '?')})"

    if _looks_writable(target):
        return (facts + "。⚠ **属主与权限都正常,却仍被拒** —— 那就不是文件权限"
                "问题,别去 chmod/chown。这一档几乎总是**沙箱**:跑它的那个进程"
                "只被放行了仓库目录,而 DATA_ROOT 是仓库的**同级**目录"
                f"({target.parent.parent})。修法是给那个 runner 的沙箱配置加上"
                "对 DATA_ROOT 的写权限(Codex 走项目级 .codex/config.toml 的 "
                "`sandbox_workspace_write.writable_roots`),**不要** chmod 777、"
                "不要改 WALMART_DATA_ROOT、不要建软链接 —— 那三样都是把状态搬到"
                "别处,只会让两个身份各写各的一份")
    return (facts + "。⚠ 最常见成因:某次用 sudo 跑过,锁文件归了 root"
            "(此时目录权限是好的,改目录没用)。修法 —— 先确认没有 cli.py 在跑,"
            f"再把属主改回来:`sudo chown \"$(id -un)\" {target}`(整体归位:"
            f"`sudo chown -R \"$(id -un)\" {target.parent.parent}`)。"
            "**别用 rm**:删锁文件不会让正持锁的进程放手,只会让新进程锁到另一个 "
            "inode 上 —— 两个实例同时跑同一条链,而互斥看起来还在")


def acquire(name: str):
    """输入:锁名(=工作流名)→ 输出:文件句柄(拿到)或 None(已被占用)。

    ⚠ **返回值必须留着**:句柄被 GC 掉即释放锁。cli.py 靠把它绑在局部变量上
    活到进程结束,别改成 `_acquire_lock(...)` 丢弃返回值那种写法。

    拿不到锁(别人在跑)返回 None;**建不了锁**(权限/磁盘)抛
    `LockUnavailable`,消息里带属主诊断 —— 见 `_permission_hint`。
    """
    d = paths.locks_dir()
    target = d / f"{name}.lock"
    try:
        d.mkdir(parents=True, exist_ok=True)
        fh = open(target, "w")
    except OSError as e:
        raise LockUnavailable(
            f"建不了运行锁:{e}。{_permission_hint(target, e)}") from e
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
