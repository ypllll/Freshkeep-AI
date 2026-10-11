"""运行参数与固定业务常量。

本模块只负责读取运行参数，不含业务逻辑。按 docs/architecture.md 第 10 节：

- 数据库路径通过环境变量 `DATABASE_PATH` 配置，默认 `data/freshkeep.db`；
- `DUE_SOON_DAYS` 是**固定业务常量**（2 天），不提供用户配置功能，因此不读取环境变量；
- 参考期限数据属于日期领域逻辑，保存在 `core/dates.py`，此处不保存副本；
- AI 相关配置在阶段 4 加入。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 固定业务常量：只有基于用户提供的日期或天数计算出的期限才可用于确定性分级。
DUE_SOON_DAYS = 2

DEFAULT_DATABASE_PATH = "data/freshkeep.db"


def get_database_path() -> Path:
    """返回数据库文件路径（绝对路径）。相对路径以项目根目录为基准。"""
    raw = os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip()
    if not raw:
        raw = DEFAULT_DATABASE_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path
