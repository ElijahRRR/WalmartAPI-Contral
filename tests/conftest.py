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
