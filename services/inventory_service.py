"""数字冰箱的业务编排（阶段 2）。

职责：

- 调用 `core.validation` 做关键校验（数据层负责，而不是界面表单）；
- 调用 `core.allocation` 决定消耗/丢弃如何分摊到多个批次；
- 在**同一个连接与事务**内完成库存更新与事件写入，任一步失败整体回滚；
- 对界面返回结构化结果（例如每个批次的实际扣减量）。

本阶段不涉及保质期判定、过敏原判定与菜谱规划。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Union

import config
from core import allocation as allocation_rules
from core.models import InventoryEvent, InventoryItem, StockSummaryRow
from core.validation import (
    normalize_name,
    validate_category,
    validate_item_id,
    validate_name,
    validate_notes,
    validate_purchase_date,
    validate_quantity,
    validate_storage,
    validate_unit,
)
from storage import db as db_module
from storage import inventory_repo

EPSILON = 1e-9

# 事件动作（R-23）
ACTION_INITIAL_ADD = "initial_add"
ACTION_PURCHASE_ADD = "purchase_add"
ACTION_EDIT = "edit"
ACTION_CONSUME = "consume"
ACTION_DISCARD = "discard"


class InventoryError(Exception):
    """面向用户的库存操作错误。"""


class BatchNotFoundError(InventoryError):
    """指定的批次不存在或已不在库。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def open_connection(db_path: Union[str, Path, None] = None) -> sqlite3.Connection:
    """打开数据库并把结构升级到最新版本（界面通过本函数访问存储层）。"""
    path = Path(db_path) if db_path is not None else config.get_database_path()
    conn = db_module.connect(path)
    db_module.ensure_schema(conn)
    return conn


# --- 入库 -------------------------------------------------------------------


def add_batch(
    conn: sqlite3.Connection,
    *,
    name: str,
    category: str,
    quantity: float,
    unit: str,
    storage: str,
    purchased_on: Union[str, date, None] = None,
    purchase_date_defaulted: bool = False,
    notes: Optional[str] = None,
    initial: bool = False,
    today: Optional[date] = None,
) -> int:
    """新增一条批次记录并写入事件，返回批次 id。

    `initial=True` 表示"首次录入冰箱已有食材"（事件动作 `initial_add`），
    否则按"新购买入库"记录（`purchase_add`）；两者都会新增独立批次（R-15）。
    """
    today = today or date.today()
    clean_name = validate_name(name)
    clean_category = validate_category(category)
    clean_unit = validate_unit(unit)
    clean_storage = validate_storage(storage)
    clean_quantity = validate_quantity(quantity)
    clean_notes = validate_notes(notes)
    # 首次录入已有食材时允许"购买日期未知"；新购买必须给出日期（R-25）
    purchase_date = validate_purchase_date(
        purchased_on, today=today, allow_unknown=initial
    )
    if purchase_date is None:
        defaulted = False
    else:
        defaulted = bool(purchase_date_defaulted)

    timestamp = _now()
    with db_module.transaction(conn):
        item_id = inventory_repo.insert_batch(
            conn,
            name=clean_name,
            name_normalized=normalize_name(clean_name),
            category=clean_category,
            quantity=clean_quantity,
            unit=clean_unit,
            purchased_on=purchase_date.isoformat() if purchase_date else None,
            purchase_date_defaulted=defaulted,
            storage=clean_storage,
            notes=clean_notes,
            created_at=timestamp,
            updated_at=timestamp,
        )
        inventory_repo.insert_event(
            conn,
            item_id=item_id,
            action=ACTION_INITIAL_ADD if initial else ACTION_PURCHASE_ADD,
            qty_delta=clean_quantity,
            qty_after=clean_quantity,
            at=timestamp,
            source="manual",
        )
    return item_id


# --- 查询 -------------------------------------------------------------------


def list_summary(conn: sqlite3.Connection) -> List[StockSummaryRow]:
    """按"同名 + 同单位"汇总活动批次（汇总值恒等于批次之和）。"""
    return inventory_repo.summarize_active(conn)


def list_batches(
    conn: sqlite3.Connection,
    *,
    name_normalized: Optional[str] = None,
    unit: Optional[str] = None,
    active_only: bool = False,
) -> List[InventoryItem]:
    batches = inventory_repo.list_batches(conn, name_normalized=name_normalized, unit=unit)
    if active_only:
        batches = [item for item in batches if item.status == "active"]
    return batches


def get_batch(conn: sqlite3.Connection, item_id: int) -> InventoryItem:
    batch = inventory_repo.get_batch(conn, validate_item_id(item_id))
    if batch is None:
        raise BatchNotFoundError(f"批次 {item_id} 不存在")
    return batch


def list_events(
    conn: sqlite3.Connection, *, item_id: Optional[int] = None, limit: int = 100
) -> List[InventoryEvent]:
    return inventory_repo.list_events(conn, item_id=item_id, limit=limit)


# --- 数量变更 ---------------------------------------------------------------


def update_quantity(
    conn: sqlite3.Connection, item_id: int, new_quantity: float
) -> tuple:
    """修改指定批次的数量，返回 (变更前, 变更后)。数量未变化时不写事件（R-23）。"""
    item_id = validate_item_id(item_id)
    quantity = validate_quantity(new_quantity)

    with db_module.transaction(conn):
        batch = _require_active_batch(conn, item_id)
        timestamp = _now()
        rowcount = inventory_repo.set_quantity_if_active(conn, item_id, quantity, timestamp)
        if rowcount != 1:
            raise InventoryError("该批次已不在库或已被修改，请刷新后重试")
        current = _require_active_batch(conn, item_id)
        delta = round(current.quantity - batch.quantity, 6)
        if abs(delta) > EPSILON:
            inventory_repo.insert_event(
                conn,
                item_id=item_id,
                action=ACTION_EDIT,
                qty_delta=delta,
                qty_after=current.quantity,
                at=timestamp,
                source="manual",
            )
        return batch.quantity, current.quantity


def consume(
    conn: sqlite3.Connection,
    *,
    quantity: float,
    item_id: Optional[int] = None,
    name_normalized: Optional[str] = None,
    unit: Optional[str] = None,
    note: Optional[str] = None,
) -> List[allocation_rules.BatchAllocation]:
    """消耗食材。未指定批次时按 §6.5 的固定顺序自动分配。"""
    return _change_stock(
        conn,
        action=ACTION_CONSUME,
        quantity=quantity,
        item_id=item_id,
        name_normalized=name_normalized,
        unit=unit,
        note=note,
    )


def discard(
    conn: sqlite3.Connection,
    *,
    quantity: float,
    item_id: Optional[int] = None,
    name_normalized: Optional[str] = None,
    unit: Optional[str] = None,
    note: Optional[str] = None,
) -> List[allocation_rules.BatchAllocation]:
    """丢弃食材。分配规则与消耗一致。"""
    return _change_stock(
        conn,
        action=ACTION_DISCARD,
        quantity=quantity,
        item_id=item_id,
        name_normalized=name_normalized,
        unit=unit,
        note=note,
    )


def _change_stock(
    conn: sqlite3.Connection,
    *,
    action: str,
    quantity: float,
    item_id: Optional[int],
    name_normalized: Optional[str],
    unit: Optional[str],
    note: Optional[str],
) -> List[allocation_rules.BatchAllocation]:
    quantity = validate_quantity(quantity)
    clean_note = validate_notes(note)
    end_status = "consumed" if action == ACTION_CONSUME else "discarded"

    with db_module.transaction(conn):
        if item_id is not None:
            item_id = validate_item_id(item_id)
            batch = _require_active_batch(conn, item_id)
            if batch.quantity + EPSILON < quantity:
                raise allocation_rules.InsufficientStockError(
                    quantity, batch.quantity, label=f"批次 {item_id}（{batch.name}）"
                )
            allocations = [
                allocation_rules.BatchAllocation(
                    item_id=batch.id,
                    name=batch.name,
                    unit=batch.unit,
                    take=round(quantity, 6),
                    quantity_before=batch.quantity,
                    expiry_date=batch.expiry_date,
                    purchased_on=batch.purchased_on,
                )
            ]
        else:
            if not name_normalized:
                raise InventoryError("请选择要操作的食材或指定批次")
            batches = inventory_repo.list_batches(
                conn, name_normalized=name_normalized, unit=unit
            )
            allocations = allocation_rules.allocate(batches, quantity)

        for allocation in allocations:
            after = round(allocation.quantity_before - allocation.take, 6)
            new_status = end_status if after <= EPSILON else None
            timestamp = _now()
            rowcount = inventory_repo.decrease_quantity(
                conn,
                allocation.item_id,
                allocation.take,
                timestamp,
                new_status=new_status,
            )
            if rowcount != 1:
                raise InventoryError(
                    f"批次 {allocation.item_id} 的库存已变化，本次操作已取消"
                )
            current = inventory_repo.get_batch(conn, allocation.item_id)
            if current is None:
                raise InventoryError(f"批次 {allocation.item_id} 不存在，本次操作已取消")
            delta = round(current.quantity - allocation.quantity_before, 6)
            if abs(delta) > EPSILON:
                inventory_repo.insert_event(
                    conn,
                    item_id=allocation.item_id,
                    action=action,
                    qty_delta=delta,
                    qty_after=current.quantity,
                    at=timestamp,
                    source="manual",
                    note=clean_note,
                )
        return allocations


def _require_active_batch(conn: sqlite3.Connection, item_id: int) -> InventoryItem:
    batch = inventory_repo.get_batch(conn, item_id)
    if batch is None:
        raise BatchNotFoundError(f"批次 {item_id} 不存在")
    if batch.status != "active":
        raise BatchNotFoundError(f"批次 {item_id} 已{'消耗' if batch.status == 'consumed' else '丢弃'}，无法继续操作")
    return batch
