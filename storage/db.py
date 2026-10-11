"""SQLite 连接、事务与版本化结构初始化。

职责边界（docs/architecture.md 第 5 节）：

- 提供短生命周期连接与显式事务上下文，供 `services` 使用；
- 依据 `PRAGMA user_version` 按序执行 `storage.schema.MIGRATIONS`；
- 迁移失败时报错停止、回滚当前步骤，**不删除、不重建、不清空任何数据**。

本模块不含业务规则，也不感知 Streamlit 与 AI。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Sequence, Union

from storage import schema as schema_module


class MigrationError(RuntimeError):
    """结构迁移失败（版本不一致、步骤非法或执行出错）。"""


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。

    使用 `isolation_level=None` 关闭 sqlite3 的隐式事务管理，
    由 `transaction()` 显式控制 BEGIN / COMMIT / ROLLBACK。
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_user_version(conn: sqlite3.Connection) -> int:
    """读取当前结构版本。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：异常时整体回滚。

    若连接已处于事务中，则复用外层事务，由外层决定提交或回滚
    （一次业务操作必须共用同一个连接与事务）。
    """
    if conn.in_transaction:
        yield conn
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _validated_migrations() -> Sequence[schema_module.Migration]:
    """读取并校验迁移步骤：版本号唯一、从 1 起连续、每步非空。"""
    migrations = list(schema_module.MIGRATIONS)
    versions = [version for version, _ in migrations]

    if not migrations:
        raise MigrationError("迁移列表为空，无法初始化数据库结构")
    if len(set(versions)) != len(versions):
        raise MigrationError(f"迁移版本号重复：{versions}")
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"迁移版本号必须从 1 开始连续递增，当前为：{versions}")
    if schema_module.SCHEMA_VERSION != versions[-1]:
        raise MigrationError(
            "SCHEMA_VERSION 与迁移列表不一致："
            f"SCHEMA_VERSION={schema_module.SCHEMA_VERSION}，迁移最大版本={versions[-1]}"
        )
    for version, steps in migrations:
        if not steps:
            raise MigrationError(f"迁移版本 {version} 没有任何步骤")

    return migrations


def ensure_schema(conn: sqlite3.Connection) -> List[int]:
    """把数据库结构升级到最新版本，返回本次实际应用的版本号列表。

    - 重复调用是幂等的：已经是最新版本时返回空列表，不重复建表；
    - 数据库版本高于代码版本时拒绝继续（避免用旧代码写入新结构）；
    - 任一版本步骤失败：回滚该步骤、抛 `MigrationError`、停止后续版本，
      `user_version` 保持迁移前的值，已有数据不受影响。
    """
    migrations = _validated_migrations()
    target_version = schema_module.SCHEMA_VERSION
    current_version = get_user_version(conn)

    if current_version > target_version:
        raise MigrationError(
            f"数据库结构版本（{current_version}）高于代码支持的版本（{target_version}），"
            "请使用匹配版本的代码，或由人工处理；不会自动删除或重建数据库。"
        )

    applied: List[int] = []
    for version, steps in migrations:
        if version <= current_version:
            continue
        try:
            with transaction(conn):
                for step in steps:
                    step(conn)
                conn.execute(f"PRAGMA user_version = {int(version)}")
        except Exception as exc:
            raise MigrationError(
                f"迁移到结构版本 {version} 失败，已回滚并停止；"
                f"数据库未被删除或重建：{exc}"
            ) from exc
        applied.append(version)

    return applied


def initialize_database(db_path: Union[str, Path]) -> List[int]:
    """打开数据库并升级到最新结构版本，返回本次应用的版本号列表。"""
    conn = connect(db_path)
    try:
        return ensure_schema(conn)
    finally:
        conn.close()
