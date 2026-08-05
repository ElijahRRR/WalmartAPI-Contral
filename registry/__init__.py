"""registry — 接线盒:全项目唯一允许出现路径、token、表 ID、服务器地址的地方。

子模块:
  paths.py      DATA_ROOT 与全部本地路径
  resources.py  飞书表格与字段名清单、服务器地址、环境变量读取
  db.py         唯一数据库连接入口

依赖规则:registry 不 import 项目内任何其他层。
"""
