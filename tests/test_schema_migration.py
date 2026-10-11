"""版本化迁移测试：幂等、旧数据保留、失败回滚与停止。

用与真实结构同名的最小"历史版本"表模拟升级场景，验证
`storage.db.ensure_schema()` 的迁移引擎本身（docs/architecture.md 第 7 节）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from storage import db as db_module
from storage import schema as schema_module

# 模拟"历史版本"：只含 id 与 name 的旧表结构。
LEGACY_ITEM_DDL = """
CREATE TABLE IF NOT EXISTS inventory_item (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
)
"""


def legacy_migrations():
    return [(1, [schema_module.create_table_if_not_exists(LEGACY_ITEM_DDL)])]


def upgraded_migrations():
    return legacy_migrations() + [
        (
            2,
            [
                schema_module.add_column_if_missing(
                    "inventory_item", "quantity", "REAL NOT NULL DEFAULT 0"
                )
            ],
        )
    ]


def patch_migrations(monkeypatch: pytest.MonkeyPatch, migrations, version: int) -> None:
    monkeypatch.setattr(schema_module, "MIGRATIONS", migrations)
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", version)


def insert_legacy_row(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT INTO inventory_item (name) VALUES (?)", (name,))


def table_names(conn: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def column_names(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_reaches_latest_version(conn: sqlite3.Connection) -> None:
    applied = db_module.ensure_schema(conn)

    assert applied == list(range(1, schema_module.SCHEMA_VERSION + 1))
    assert db_module.get_user_version(conn) == schema_module.SCHEMA_VERSION
    assert "inventory_item" in table_names(conn)


def test_upgrade_preserves_rows_and_adds_new_column(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_migrations(monkeypatch, legacy_migrations(), 1)
    assert db_module.ensure_schema(conn) == [1]
    insert_legacy_row(conn, "番茄")
    insert_legacy_row(conn, "鸡蛋")

    patch_migrations(monkeypatch, upgraded_migrations(), 2)
    applied = db_module.ensure_schema(conn)

    assert applied == [2], "只应执行尚未应用的版本"
    assert db_module.get_user_version(conn) == 2
    assert "quantity" in column_names(conn, "inventory_item"), "新列应已存在"

    rows = list(conn.execute("SELECT name, quantity FROM inventory_item ORDER BY id"))
    assert [row["name"] for row in rows] == ["番茄", "鸡蛋"], "旧数据必须保留"
    assert [row["quantity"] for row in rows] == [0.0, 0.0], "存量行取新列默认值"


def test_upgrade_is_idempotent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_migrations(monkeypatch, upgraded_migrations(), 2)
    assert db_module.ensure_schema(conn) == [1, 2]

    assert db_module.ensure_schema(conn) == [], "已是最新版本时不应重复执行"
    assert db_module.get_user_version(conn) == 2


def test_failed_migration_rolls_back_step_and_stops(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_migrations(monkeypatch, upgraded_migrations(), 2)
    assert db_module.ensure_schema(conn) == [1, 2]
    insert_legacy_row(conn, "番茄")

    def failing_step(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟迁移失败")

    broken = upgraded_migrations() + [
        (
            3,
            [
                schema_module.create_table_if_not_exists(
                    "CREATE TABLE IF NOT EXISTS scratch (id INTEGER)"
                ),
                failing_step,
            ],
        )
    ]
    patch_migrations(monkeypatch, broken, 3)

    with pytest.raises(db_module.MigrationError) as excinfo:
        db_module.ensure_schema(conn)

    assert "模拟迁移失败" in str(excinfo.value)
    assert db_module.get_user_version(conn) == 2, "失败的版本不得推进 user_version"
    assert "scratch" not in table_names(conn), "同一步骤内已执行的语句也必须回滚"
    remaining = [row["name"] for row in conn.execute("SELECT name FROM inventory_item")]
    assert remaining == ["番茄"]


def test_failed_migration_keeps_database_file_and_data(
    tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_migrations(monkeypatch, legacy_migrations(), 1)
    assert db_module.initialize_database(tmp_db_path) == [1]

    seed = db_module.connect(tmp_db_path)
    try:
        insert_legacy_row(seed, "牛奶")
    finally:
        seed.close()

    def failing_step(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟迁移失败")

    patch_migrations(monkeypatch, legacy_migrations() + [(2, [failing_step])], 2)

    with pytest.raises(db_module.MigrationError):
        db_module.initialize_database(tmp_db_path)

    assert tmp_db_path.exists(), "迁移失败不得删除数据库文件"

    check = db_module.connect(tmp_db_path)
    try:
        assert db_module.get_user_version(check) == 1
        names = [row["name"] for row in check.execute("SELECT name FROM inventory_item")]
        assert names == ["牛奶"], "迁移失败不得清空已有数据"
    finally:
        check.close()


def test_add_column_if_missing_rejects_unknown_table(conn: sqlite3.Connection) -> None:
    step = schema_module.add_column_if_missing("not_a_table", "col", "TEXT NULL")

    with pytest.raises(RuntimeError):
        step(conn)
