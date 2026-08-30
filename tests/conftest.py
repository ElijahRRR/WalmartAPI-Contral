import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _rate_buckets_in_memory(monkeypatch):
    """稀缺桶在单测里不落 PG:统一改走进程内窗口。

    生产里 feeds.post.* / prices.put 等稀缺桶的限速状态落 ops.rate_events
    跨进程共享(api/_client._acquire_pg);单测没有 PG,若不改道,任何
    经过这些桶的用例都会因连不上库而炸(fail hard 是生产要的行为,
    不是测试要的)。专测 PG 路径的用例在 test_rate_bucket.py 里
    保存原函数直接调,不受本夹具影响。
    """
    from api import _client
    monkeypatch.setattr(_client, "_acquire_pg", _client._acquire_mem)


@pytest.fixture(autouse=True)
def _reports_never_touch_the_real_data_root(monkeypatch, tmp_path):
    """报告 csv 一律落临时目录 —— **绝不允许写进真实 <DATA_ROOT>/reports/**。

    2026-08-24 实测的事故形态:`tests/test_alloc_audit.py` 里 16 处 `_wire(`
    只有 9 处传了 `reports=tmp_path`,其余 7 处直接用真实 `paths.reports_dir()`。
    于是在**生产机上跑一次 pytest**,所有者正照着做的六份处置清单
    (类目建议 / 渠道不符下架 / 类目不符下架 / 店铺总览 / 同 ASIN 冲突 /
    同品牌冲突)就被覆盖成夹具行 `B0AAAA0001,…,A107,…,下架`——
    文件名、表头、格式全对,**肉眼看不出已经不是真数据了**。

    为什么放这儿而不是逐个补 `reports=`:补 7 处只修了今天这一个文件,
    下一个写 workflow 回归的人照样会漏。`paths.reports_dir()` 是全仓报告类
    workflow 的唯一出口,在这里一次性掐断,以后新增用例默认就是安全的。
    显式传 `reports=` 的用例照常覆盖本夹具(monkeypatch 后设的胜出),不受影响。

    ⚠ **目录必须惰性建**:早建一个空目录会污染那些"断言 tmp_path 里什么都没有"
    的用例(test_backup 两条实测踩到)。没调用过 reports_dir() 的测试,
    磁盘上不该多出任何东西。
    """
    from registry import paths
    d = tmp_path / "_reports_guard"

    def _dir():
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(paths, "reports_dir", _dir)
