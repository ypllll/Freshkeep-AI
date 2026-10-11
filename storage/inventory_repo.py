"""库存批次与库存事件的 SQL。

本模块只负责读写数据，不做业务规则判断；关键条件（例如"数量足够"）
通过 SQL 的条件更新表达，配合受影响行数校验保证并发安全
（docs/architecture.md 第 9 节）。
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from core.models import InventoryEvent, InventoryItem, StockSummaryRow

_ITEM_COLUMNS = """
    id, name, name_normalized, category, quantity, unit,
    purchased_on, purchase_date_defaulted, storage,
    expiry_date, shelf_life_days, expiry_source,
    allergen_status, declared_allergens, notes, status,
    created_at, updated_at
"""


def insert_batch(
    conn: sqlite3.Connection,
    *,
    name: str,
    name_normalized: str,
    category: str,
    quantity: float,
    unit: str,
    purchased_on: Optional[str],
    purchase_date_defaulted: bool,
    storage: str,
    notes: Optional[str],
    created_at: str,
    updated_at: str,
    expiry_source: str = "unknown",
    allergen_status: str = "unknown",
) -> int:
    """新增一条批次记录（R-15：永远 INSERT，不覆盖旧记录）。"""
    cursor = conn.execute(
        """
        INSERT INTO inventory_item (
            name, name_normalized, category, quantity, unit,
            purchased_on, purchase_date_defaulted, storage,
            expiry_source, allergen_status, notes, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            name,
            name_normalized,
            category,
            quantity,
            unit,
            purchased_on,
            1 if purchase_date_defaulted else 0,
            storage,
            expiry_source,
            allergen_status,
            notes,
            created_at,
            updated_at,
        ),
    )
    return int(cursor.lastrowid or 0)


def get_batch(conn: sqlite3.Connection, item_id: int) -> Optional[InventoryItem]:
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM inventory_item WHERE id = ?", (item_id,)
    ).fetchone()
    return InventoryItem.from_row(row) if row is not None else None


def list_batches(
    conn: sqlite3.Connection,
    *,
    name_normalized: Optional[str] = None,
    unit: Optional[str] = None,
) -> List[InventoryItem]:
    sql = f"SELECT {_ITEM_COLUMNS} FROM inventory_item WHERE 1 = 1"
    params: list = []
    if name_normalized is not None:
        sql += " AND name_normalized = ?"
        params.append(name_normalized)
    if unit is not None:
        sql += " AND unit = ?"
        params.append(unit)
    sql += " ORDER BY id"
    return [InventoryItem.from_row(row) for row in conn.execute(sql, params)]


def list_active_batches(conn: sqlite3.Connection) -> List[InventoryItem]:
    rows = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM inventory_item WHERE status = 'active' ORDER BY id"
    )
    return [InventoryItem.from_row(row) for row in rows]


def summarize_active(conn: sqlite3.Connection) -> List[StockSummaryRow]:
    """按"归一化名称 + 单位"汇总活动批次数量。"""
    rows = conn.execute(
        """
        SELECT MIN(name) AS name, name_normalized, unit,
               ROUND(SUM(quantity), 6) AS total_quantity,
               COUNT(*) AS batch_count
        FROM inventory_item
        WHERE status = 'active'
        GROUP BY name_normalized, unit
        ORDER BY name_normalized, unit
        """
    )
    return [
        StockSummaryRow(
            name=row["name"],
            name_normalized=row["name_normalized"],
            unit=row["unit"],
            total_quantity=float(row["total_quantity"]),
            batch_count=int(row["batch_count"]),
        )
        for row in rows
    ]


def decrease_quantity(
    conn: sqlite3.Connection,
    item_id: int,
    amount: float,
    updated_at: str,
    *,
    new_status: Optional[str] = None,
) -> int:
    """条件扣减：仅当批次仍为活动状态且数量足够时更新，返回受影响行数。

    数量不足或状态已变化时返回 0，由服务层整体拒绝并回滚（R-22、R-24）。
    """
    sql = """
        UPDATE inventory_item
        SET quantity = ROUND(quantity - ?, 6), updated_at = ?
    """
    params: list = [amount, updated_at]
    if new_status is not None:
        sql += ", status = ?"
        params.append(new_status)
    sql += " WHERE id = ? AND status = 'active' AND quantity >= ?"
    params.extend([item_id, amount])
    return conn.execute(sql, params).rowcount


def set_quantity_if_active(
    conn: sqlite3.Connection, item_id: int, new_quantity: float, updated_at: str
) -> int:
    """修改批次数量（仅限活动批次），返回受影响行数。"""
    return conn.execute(
        """
        UPDATE inventory_item
        SET quantity = ?, updated_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (new_quantity, updated_at, item_id),
    ).rowcount


def insert_event(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    action: str,
    qty_delta: float,
    qty_after: float,
    at: str,
    source: str = "manual",
    plan_id: Optional[int] = None,
    note: Optional[str] = None,
) -> int:
    """写入一条库存事件（R-12、R-23）。"""
    cursor = conn.execute(
        """
        INSERT INTO inventory_event (item_id, action, qty_delta, qty_after, source, plan_id, at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, action, qty_delta, qty_after, source, plan_id, at, note),
    )
    return int(cursor.lastrowid or 0)


def list_events(
    conn: sqlite3.Connection, *, item_id: Optional[int] = None, limit: int = 100
) -> List[InventoryEvent]:
    """按时间倒序读取事件；附带批次名称便于界面展示。"""
    sql = """
        SELECT e.id, e.item_id, e.action, e.qty_delta, e.qty_after,
               e.source, e.plan_id, e.at, e.note,
               i.name AS item_name
        FROM inventory_event AS e
        LEFT JOIN inventory_item AS i ON i.id = e.item_id
        WHERE 1 = 1
    """
    params: list = []
    if item_id is not None:
        sql += " AND e.item_id = ?"
        params.append(item_id)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(int(limit))
    return [InventoryEvent.from_row(row) for row in conn.execute(sql, params)]
