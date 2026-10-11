"""数据结构与取值白名单（docs/architecture.md 第 6 节）。

`InventoryItem` 的一行即一条**购买批次**：同一名称的多次购买会产生多条记录，
互不覆盖（product_spec R-15）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

# 取值白名单（product_spec §6.2）
UNITS: Tuple[str, ...] = ("g", "kg", "ml", "L", "个", "瓶", "盒", "包", "袋", "把", "份")
CATEGORIES: Tuple[str, ...] = (
    "蔬菜",
    "水果",
    "肉类",
    "海鲜",
    "蛋奶",
    "主食",
    "调味",
    "熟食",
    "其他",
)
STORAGE_KINDS: Tuple[str, ...] = ("冷藏", "冷冻", "常温", "其他")

# 保质期来源：阶段 3 才会写入 user_date / user_days / user_days_unresolved
EXPIRY_SOURCES: Tuple[str, ...] = ("user_date", "user_days", "user_days_unresolved", "unknown")
ALLERGEN_STATUSES: Tuple[str, ...] = ("declared", "unknown")

ITEM_STATUSES: Tuple[str, ...] = ("active", "consumed", "discarded")
EVENT_ACTIONS: Tuple[str, ...] = ("initial_add", "purchase_add", "edit", "consume", "discard")
EVENT_SOURCES: Tuple[str, ...] = ("manual", "recipe_plan")


@dataclass(frozen=True)
class InventoryItem:
    """一条库存批次。"""

    id: int
    name: str
    name_normalized: str
    category: str
    quantity: float
    unit: str
    purchased_on: Optional[str]
    purchase_date_defaulted: bool
    storage: str
    expiry_date: Optional[str] = None
    shelf_life_days: Optional[int] = None
    expiry_source: str = "unknown"
    allergen_status: str = "unknown"
    declared_allergens: Optional[str] = None
    notes: Optional[str] = None
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "InventoryItem":
        return cls(
            id=int(row["id"]),
            name=row["name"],
            name_normalized=row["name_normalized"],
            category=row["category"],
            quantity=float(row["quantity"]),
            unit=row["unit"],
            purchased_on=row["purchased_on"],
            purchase_date_defaulted=bool(row["purchase_date_defaulted"]),
            storage=row["storage"],
            expiry_date=row["expiry_date"],
            shelf_life_days=row["shelf_life_days"],
            expiry_source=row["expiry_source"],
            allergen_status=row["allergen_status"],
            declared_allergens=row["declared_allergens"],
            notes=row["notes"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class InventoryEvent:
    """一条库存数量变化记录（R-23）。"""

    id: int
    item_id: int
    action: str
    qty_delta: float
    qty_after: float
    source: str
    plan_id: Optional[int]
    at: str
    note: Optional[str] = None
    item_name: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "InventoryEvent":
        keys = row.keys()
        return cls(
            id=int(row["id"]),
            item_id=int(row["item_id"]),
            action=row["action"],
            qty_delta=float(row["qty_delta"]),
            qty_after=float(row["qty_after"]),
            source=row["source"],
            plan_id=row["plan_id"],
            at=row["at"],
            note=row["note"],
            item_name=row["item_name"] if "item_name" in keys else None,
        )


@dataclass(frozen=True)
class BatchAllocation:
    """一次扣减在某个批次上的分摊结果（R-16、R-29）。"""

    item_id: int
    name: str
    unit: str
    take: float
    quantity_before: float
    expiry_date: Optional[str] = None
    purchased_on: Optional[str] = None

    @property
    def quantity_after(self) -> float:
        return round(self.quantity_before - self.take, 6)


@dataclass(frozen=True)
class StockSummaryRow:
    """按"同名（归一化）+ 同单位"聚合的汇总行（R-15）。"""

    name: str
    name_normalized: str
    unit: str
    total_quantity: float
    batch_count: int


def group_key(item: InventoryItem) -> Tuple[str, str]:
    """汇总分组键：归一化名称 + 单位。"""
    return (item.name_normalized, item.unit)


def active_batches(items: Sequence[InventoryItem]) -> Tuple[InventoryItem, ...]:
    """筛选仍在库（`active`）且数量大于 0 的批次。"""
    return tuple(item for item in items if item.status == "active" and item.quantity > 0)
