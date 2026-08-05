"""api — 外部接口适配层:只负责"把接口调对"(认证/代理/重试/分页/分批/限流)。

依赖规则(CLAUDE.md 铁律 1):api 只准 import registry,严禁 import services / workflows。
业务判断(如"价格低于成本就…")不属于这一层。

文件划分:
  _client.py   沃尔玛认证核心(token 缓存/每店固定代理/401 自愈/429 退避/连接池)
  feishu.py    飞书多维表格 + webhook 通知
  其余按沃尔玛 API 域随工作流迁移逐个补齐:items prices inventory orders
  returns feeds reports insights scraper
"""
