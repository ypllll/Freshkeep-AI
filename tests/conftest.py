"""pytest 公共夹具。

测试一律使用 `tmp_path` 下的一次性数据库，绝不读写演示库 `data/freshkeep.db`
（docs/architecture.md 第 7 节）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage import db as db_module  # noqa: E402  （需先修正 sys.path）


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """一次性测试数据库路径（位于 pytest 的临时目录内）。"""
    return tmp_path / "freshkeep_test.db"


@pytest.fixture
def conn(tmp_db_path: Path):
    """已建立连接但尚未建表的数据库连接。"""
    connection = db_module.connect(tmp_db_path)
    try:
        yield connection
    finally:
        connection.close()
