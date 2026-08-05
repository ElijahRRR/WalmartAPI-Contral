"""services — 跨 workflow 复用的积木层。

依赖规则:services 可 import api 与 registry,严禁 import workflows / cli。
工程规范:新增积木前必须先通读本目录现有函数确认无重复;
每个函数 docstring 第一行写清"输入什么 → 输出什么"。

当前积木:
  stores.py   店铺凭证读取(飞书店铺凭证表 + 本地快照兜底)
其余积木随各 workflow 迁移按需补充(先查重再新增)。
"""
