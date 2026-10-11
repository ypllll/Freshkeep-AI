"""storage 层基础行为测试：连接、事务、结构初始化与配置常量。"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

import config
from storage import db as db_module
from storage import schema as schema_module


def insert_item(conn: sqlite3.Connection, name: str = "番茄", quantity: float = 3.0) -> None:
    """写入一条最小可用的库存批次记录（仅供本文件测试使用）。"""
    conn.execute(
        """
        INSERT INTO inventory_item (
            name, name_normalized, category, quantity, unit,
            purchased_on, storage, expiry_source, allergen_status,
            status, created_at, updated_at
        ) VALUES (?, ?, '蔬菜', ?, '个', '2026-10-11', '冷藏', 'unknown', 'unknown',
                  'active', '2026-10-11T10:00:00', '2026-10-11T10:00:00')
        """,
        (name, name, quantity),
    )


def count_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM inventory_item").fetchone()[0])


# --- 连接 -------------------------------------------------------------------


def test_connect_creates_file_and_enables_foreign_keys(tmp_db_path: Path) -> None:
    connection = db_module.connect(tmp_db_path)
    try:
        assert tmp_db_path.exists()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.row_factory is sqlite3.Row
    finally:
        connection.close()


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "freshkeep.db"
    connection = db_module.connect(nested)
    try:
        assert nested.exists()
    finally:
        connection.close()


# --- 结构初始化 -------------------------------------------------------------


def test_ensure_schema_creates_tables_and_sets_version(conn: sqlite3.Connection) -> None:
    applied = db_module.ensure_schema(conn)

    assert applied == [1]
    assert db_module.get_user_version(conn) == schema_module.SCHEMA_VERSION
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "inventory_item" in tables


def test_ensure_schema_is_idempotent_and_keeps_data(conn: sqlite3.Connection) -> None:
    db_module.ensure_schema(conn)
    insert_item(conn)

    applied_again = db_module.ensure_schema(conn)

    assert applied_again == [], "重复初始化不应重复执行迁移"
    assert db_module.get_user_version(conn) == schema_module.SCHEMA_VERSION
    assert count_items(conn) == 1, "重复初始化不得破坏已有数据"


def test_ensure_schema_rejects_database_newer_than_code(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version = {schema_module.SCHEMA_VERSION + 1}")

    with pytest.raises(db_module.MigrationError):
        db_module.ensure_schema(conn)

    assert db_module.get_user_version(conn) == schema_module.SCHEMA_VERSION + 1


def test_initialize_database_creates_and_migrates(tmp_db_path: Path) -> None:
    applied = db_module.initialize_database(tmp_db_path)

    assert applied == [1]
    assert tmp_db_path.exists()

    connection = db_module.connect(tmp_db_path)
    try:
        assert db_module.get_user_version(connection) == schema_module.SCHEMA_VERSION
    finally:
        connection.close()


def test_migration_versions_are_contiguous() -> None:
    versions = [version for version, _ in schema_module.MIGRATIONS]

    assert versions == list(range(1, len(versions) + 1))
    assert schema_module.SCHEMA_VERSION == versions[-1]
    assert all(steps for _, steps in schema_module.MIGRATIONS)


def test_ensure_schema_rejects_non_contiguous_migrations(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schema_module,
        "MIGRATIONS",
        [(1, [schema_module.create_table_if_not_exists("CREATE TABLE t1 (id INTEGER)")]),
         (3, [schema_module.create_table_if_not_exists("CREATE TABLE t3 (id INTEGER)")])],
    )
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", 3)

    with pytest.raises(db_module.MigrationError):
        db_module.ensure_schema(conn)

    assert db_module.get_user_version(conn) == 0


# --- 事务 -------------------------------------------------------------------


def test_transaction_commits(conn: sqlite3.Connection) -> None:
    db_module.ensure_schema(conn)

    with db_module.transaction(conn):
        insert_item(conn)

    assert count_items(conn) == 1


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    db_module.ensure_schema(conn)

    with pytest.raises(RuntimeError):
        with db_module.transaction(conn):
            insert_item(conn)
            raise RuntimeError("模拟业务失败")

    assert count_items(conn) == 0


def test_nested_transaction_reuses_outer_transaction(conn: sqlite3.Connection) -> None:
    db_module.ensure_schema(conn)

    with pytest.raises(RuntimeError):
        with db_module.transaction(conn):
            insert_item(conn, "番茄")
            with db_module.transaction(conn):
                insert_item(conn, "鸡蛋")
            raise RuntimeError("外层失败")

    assert count_items(conn) == 0, "内层不得单独提交，必须随外层一起回滚"


# --- 运行时（只读） ---------------------------------------------------------


def test_config_due_soon_days_is_fixed_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DUE_SOON_DAYS", "99")

    reloaded = importlib.reload(config)
    try:
        assert reloaded.DUE_SOON_DAYS == 2, "DUE_SOON_DAYS 是固定常量，不提供配置"
    finally:
        importlib.reload(config)


def test_config_database_path_default_and_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert config.get_database_path() == config.PROJECT_ROOT / config.DEFAULT_DATABASE_PATH

    override = tmp_path / "custom.db"
    monkeypatch.setenv("DATABASE_PATH", str(override))
    assert config.get_database_path() == override


def test_test_database_is_isolated_from_demo_database(tmp_db_path: Path) -> None:
    assert tmp_db_path.parent != config.PROJECT_ROOT / "data"
    assert tmp_db_path != config.get_database_path()


# --- 目录骨架 ---------------------------------------------------------------


def test_project_structure_files_exist(project_root: Path) -> None:
    expected = [
        "app.py",
        "config.py",
        "requirements.txt",
        ".env.example",
        ".gitignore",
        "storage/__init__.py",
        "storage/db.py",
        "storage/schema.py",
        "pages/1_数字冰箱.py",
        "pages/2_食谱规划.py",
        "tests/conftest.py",
        "tests/test_db.py",
        "tests/test_schema_migration.py",
        "README.md",
        "docs/product_spec.md",
        "docs/architecture.md",
    ]
    missing = [relative for relative in expected if not (project_root / relative).exists()]

    assert missing == []


def test_storage_layer_does_not_depend_on_ui(project_root: Path) -> None:
    for relative in ("storage/db.py", "storage/schema.py", "storage/__init__.py"):
        source = (project_root / relative).read_text(encoding="utf-8")
        assert "streamlit" not in source, f"{relative} 不得依赖 UI 框架"
