"""SQLite 结构定义与有序迁移步骤。

设计依据 docs/architecture.md 第 7 节：

- 用 `PRAGMA user_version` 作为结构版本游标；
- 迁移步骤按版本号升序保存，每步是一组可执行的函数；
- 新增表用 `CREATE TABLE IF NOT EXISTS`，新增字段用 `add_column_if_missing()`；
- SQLite 不支持删列、改列名、改约束，这类破坏性变更本阶段不做；
- 迁移失败时由 `storage.db.ensure_schema()` 报错停止，不删除、不重建数据库。

每个阶段只追加新的版本号与步骤，不修改已发布的步骤。
"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Tuple

MigrationStep = Callable[[sqlite3.Connection], None]
Migration = Tuple[int, List[MigrationStep]]

# 结构版本 1：库存批次表。一行 = 一条购买批次（docs/architecture.md 第 6.1 节）。
INVENTORY_ITEM_DDL = """
CREATE TABLE IF NOT EXISTS inventory_item (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT    NOT NULL,
    name_normalized         TEXT    NOT NULL,
    category                TEXT    NOT NULL,
    quantity                REAL    NOT NULL,
    unit                    TEXT    NOT NULL,
    purchased_on            TEXT    NULL,
    purchase_date_defaulted INTEGER NOT NULL DEFAULT 0,
    storage                 TEXT    NOT NULL,
    expiry_date             TEXT    NULL,
    shelf_life_days         INTEGER NULL,
    expiry_source           TEXT    NOT NULL DEFAULT 'unknown',
    allergen_status         TEXT    NOT NULL DEFAULT 'unknown',
    declared_allergens      TEXT    NULL,
    notes                   TEXT    NULL,
    status                  TEXT    NOT NULL DEFAULT 'active',
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL
)
"""


def create_table_if_not_exists(ddl: str) -> MigrationStep:
    """返回一个"建表（若不存在）"的迁移步骤。"""

    def _apply(conn: sqlite3.Connection) -> None:
        conn.execute(ddl)

    return _apply


def add_column_if_missing(table: str, column: str, declaration: str) -> MigrationStep:
    """返回一个"缺列才新增列"的迁移步骤（SQLite 无法安全地删列或改列）。"""

    def _apply(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            raise RuntimeError(f"表 {table} 不存在，无法新增列 {column}")
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    return _apply


# 结构版本 2：库存事件表。所有库存数量变化都必须在此留痕
# （docs/architecture.md 第 6.2 节、product_spec R-12 / R-23 / R-29）。
INVENTORY_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS inventory_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL REFERENCES inventory_item(id),
    action    TEXT    NOT NULL,
    qty_delta REAL    NOT NULL,
    qty_after REAL    NOT NULL,
    source    TEXT    NOT NULL DEFAULT 'manual',
    plan_id   INTEGER NULL,
    at        TEXT    NOT NULL,
    note      TEXT    NULL
)
"""


# 结构版本 3：用户过敏原清单（docs/architecture.md 第 6.4 节、product_spec R-05）。
USER_ALLERGEN_DDL = """
CREATE TABLE IF NOT EXISTS user_allergen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT    NOT NULL,
    name_normalized TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL
)
"""


MIGRATIONS: List[Migration] = [
    (1, [create_table_if_not_exists(INVENTORY_ITEM_DDL)]),
    (2, [create_table_if_not_exists(INVENTORY_EVENT_DDL)]),
    (3, [create_table_if_not_exists(USER_ALLERGEN_DDL)]),
]

SCHEMA_VERSION = max(version for version, _ in MIGRATIONS)
