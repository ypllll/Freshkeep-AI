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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

import config
from core import allocation as allocation_rules
from core import allergens as allergen_rules
from core import dates as date_rules
from core.allergens import ItemAllergenView, UserAllergen
from core.dates import ExpiryView
from core.models import EVENT_SOURCES, InventoryEvent, InventoryItem, StockSummaryRow
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
from storage import allergen_repo
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


class AllergenError(InventoryError):
    """用户过敏原清单操作错误（例如重复添加）。"""


@dataclass(frozen=True)
class BatchView:
    """一条批次的展示视图：库存字段 + 保质期视图 + 过敏原判定。

    页面只负责渲染这三个部分，不做任何计算或判定（architecture §4）。
    """

    item: InventoryItem
    expiry: ExpiryView
    allergen: ItemAllergenView


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
    expiry_date: Union[str, date, None] = None,
    shelf_life_days: Optional[int] = None,
    declared_allergens: Optional[Union[str, Sequence[str]]] = None,
    allergen_declared: bool = False,
    initial: bool = False,
    today: Optional[date] = None,
) -> int:
    """新增一条批次记录并写入事件，返回批次 id。

    `initial=True` 表示"首次录入冰箱已有食材"（事件动作 `initial_add`），
    否则按"新购买入库"记录（`purchase_add`）；两者都会新增独立批次（R-15）。

    保质期：只接受用户提供的日期或天数，按 `core.dates.resolve_expiry` 解析
    （R-01 ~ R-04、R-26）；系统不会生成正式到期日期。

    过敏原：`allergen_declared=False` 表示该批次成分未声明（R-14），
    此时 `declared_allergens` 存 NULL 而不是空数组；声明按批次独立保存，
    同名食材的其他批次不受影响。
    """
    today = today or date.today()
    clean_name = validate_name(name)
    clean_category = validate_category(category)
    clean_unit = validate_unit(unit)
    clean_storage = validate_storage(storage)
    clean_quantity = validate_quantity(quantity)
    clean_notes = validate_notes(notes)
    # 购买日期由用户主动填写，允许留空（留空 = 未知，不默认今天；product_spec R-25）
    purchase_date = validate_purchase_date(purchased_on, today=today, allow_unknown=True)
    if purchase_date is None:
        defaulted = False
    else:
        defaulted = bool(purchase_date_defaulted)

    resolved = date_rules.resolve_expiry(
        purchased_on=purchase_date,
        expiry_date=expiry_date,
        shelf_life_days=shelf_life_days,
    )

    if allergen_declared:
        clean_allergen_status = "declared"
        if isinstance(declared_allergens, str):
            # 页面直接传用户输入的文本：按分隔符解析，绝不要把字符串拆成单字
            declared_values = allergen_rules.parse_allergen_input(declared_allergens)
        else:
            declared_values = [str(value) for value in (declared_allergens or [])]
        encoded_allergens = allergen_rules.encode_declared_allergens(declared_values)
    else:
        clean_allergen_status = "unknown"
        encoded_allergens = None

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
            expiry_date=resolved.expiry_date.isoformat() if resolved.expiry_date else None,
            shelf_life_days=resolved.shelf_life_days,
            expiry_source=resolved.expiry_source,
            allergen_status=clean_allergen_status,
            declared_allergens=encoded_allergens,
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
    source: str = "manual",
    plan_id: Optional[int] = None,
) -> List[allocation_rules.BatchAllocation]:
    """消耗食材。未指定批次时按 §6.5 的固定顺序自动分配。

    `source` / `plan_id` 供菜谱确认使用（`source="recipe_plan"` + 本次规划的编号），
    手动消耗保持默认值不变。
    """
    return _change_stock(
        conn,
        action=ACTION_CONSUME,
        quantity=quantity,
        item_id=item_id,
        name_normalized=name_normalized,
        unit=unit,
        note=note,
        source=source,
        plan_id=plan_id,
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
        source="manual",
        plan_id=None,
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
    source: str = "manual",
    plan_id: Optional[int] = None,
) -> List[allocation_rules.BatchAllocation]:
    quantity = validate_quantity(quantity)
    clean_note = validate_notes(note)
    end_status = "consumed" if action == ACTION_CONSUME else "discarded"
    if source not in EVENT_SOURCES:
        raise InventoryError(f"未知的事件来源：{source}")
    if plan_id is not None and (isinstance(plan_id, bool) or not isinstance(plan_id, int)):
        raise InventoryError("规划编号必须是整数或为空")

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
                    source=source,
                    plan_id=plan_id,
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


# --- 保质期与过敏原展示视图 -------------------------------------------------


def get_batch_view(
    conn: sqlite3.Connection, item_id: int, *, today: Optional[date] = None
) -> BatchView:
    """单条批次的展示视图（保质期 + 过敏原判定）。"""
    item = get_batch(conn, item_id)
    return _build_view(conn, item, allergens=list_user_allergen_names(conn), today=today)


def list_batch_views(
    conn: sqlite3.Connection,
    *,
    name_normalized: Optional[str] = None,
    unit: Optional[str] = None,
    active_only: bool = False,
    today: Optional[date] = None,
) -> List[BatchView]:
    """批量展示视图。页面只渲染结果，不参与任何期限或过敏原计算。"""
    items = list_batches(
        conn, name_normalized=name_normalized, unit=unit, active_only=active_only
    )
    allergens = list_user_allergen_names(conn)
    return [_build_view(conn, item, allergens=allergens, today=today) for item in items]


def _build_view(
    conn: sqlite3.Connection,
    item: InventoryItem,
    *,
    allergens: Sequence[str],
    today: Optional[date] = None,
) -> BatchView:
    expiry = date_rules.build_expiry_view(
        purchased_on=item.purchased_on,
        expiry_date=item.expiry_date,
        shelf_life_days=item.shelf_life_days,
        category=item.category,
        storage=item.storage,
        today=today or date.today(),
        due_soon_days=config.DUE_SOON_DAYS,
        stored_source=item.expiry_source,
    )
    return BatchView(
        item=item,
        expiry=expiry,
        allergen=allergen_rules.assess_item(item, allergens),
    )


# --- 用户过敏原清单 ---------------------------------------------------------


def list_user_allergens(conn: sqlite3.Connection) -> List[UserAllergen]:
    return allergen_repo.list_allergens(conn)


def list_user_allergen_names(conn: sqlite3.Connection) -> List[str]:
    return [entry.display_name for entry in allergen_repo.list_allergens(conn)]


def add_user_allergen(conn: sqlite3.Connection, raw_name: str) -> int:
    """把用户输入的过敏原加入清单。

    入参会先按名称规则校验（长度、控制字符），再映射到内置别名表的规范名，
    因此"花生酱"会与"花生"视为同一条，重复添加会被拒绝。
    """
    clean = validate_name(raw_name)
    canonical = allergen_rules.canonical_allergen(clean)
    normalized = allergen_rules.normalize_allergen(canonical)
    if not normalized:
        raise ValidationError("请输入过敏原名称")

    with db_module.transaction(conn):
        existing = allergen_repo.find_by_normalized(conn, normalized)
        if existing is not None:
            raise AllergenError(f"「{existing.display_name}」已在过敏原清单中")
        return allergen_repo.insert_allergen(
            conn, display_name=canonical, name_normalized=normalized, created_at=_now()
        )


def remove_user_allergen(conn: sqlite3.Connection, allergen_id: int) -> None:
    allergen_id = validate_item_id(allergen_id)
    with db_module.transaction(conn):
        if allergen_repo.delete_allergen(conn, allergen_id) != 1:
            raise AllergenError(f"过敏原 {allergen_id} 不在清单中")
