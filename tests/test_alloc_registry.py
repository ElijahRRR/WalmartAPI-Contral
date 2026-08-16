"""分配引擎依赖的 registry 登记项回归(飞书改表头 = 这里先红)。"""

from registry import resources


def test_store_target_and_channel_fields_registered():
    """限额表四列:目标三列(日目标/容量上限)+ 配送限制(一店一渠道权威)。"""
    f = resources.RETIRE_LIMITS.fields
    assert f.target_gmv_daily == "目标销售额"
    assert f.target_orders_daily == "目标订单"
    assert f.max_online == "单店最大在线数"
    assert f.channel_limit == "配送限制"
