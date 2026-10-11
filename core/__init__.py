"""业务核心层：纯 Python 的规则、校验与计算。

本包不 import `storage`，也不 import `ai`（docs/architecture.md 第 4 节）：
所有函数接收数据、返回结果，由 `services` 负责编排与持久化。
"""
