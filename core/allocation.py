"""批次消耗分配规则（product_spec §6.5，R-16 / R-29）。

当用户没有指定批次时，按**固定、可解释、可复现**的顺序决定扣减哪些批次：

1. 先消耗有用户提供期限的活动批次，按期限从早到晚（先到期先消耗）；
2. 期限相同的，按购买日期从早到晚；购买日期未知的批次排在已知日期之后；
3. 仍相同的，按批次 id 升序；
4. **期限未知的批次一律排在已知期限批次之后**，
   不参与"先到期"排序，也不为其推断或伪造到期日期。

阶段 2 尚未录入用户期限（`expiry_date` 恒为 NULL），因此排序实际由规则 2、3 决定；
规则 1 在阶段 3 补齐期限数据后自动生效。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from core.models import BatchAllocation, InventoryItem, StockSummaryRow, active_batches
from core.validation import ValidationError, validate_quantity

EPSILON = 1e-9


class InsufficientStockError(ValueError):
    """可用数量不足，操作被拒绝（R-24：库存不得变成负数）。"""

    def __init__(self, requested: float, available: float, *, label: str = "该食材") -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"{label}可用数量为 {available:g}，不足以完成 {requested:g}，请核实库存后重试"
        )


def sort_batches(batches: Iterable[InventoryItem]) -> List[InventoryItem]:
    """按 §6.5 的固定顺序返回可扣减的批次列表。"""
    return sorted(active_batches(tuple(batches)), key=_sort_key)


def _sort_key(item: InventoryItem):
    has_expiry = item.expiry_date is not None
    has_purchase = item.purchased_on is not None
    return (
        0 if has_expiry else 1,
        item.expiry_date or "",
        0 if has_purchase else 1,
        item.purchased_on or "",
        item.id,
    )


def total_quantity(batches: Iterable[InventoryItem]) -> float:
    """活动批次数量之和。"""
    return round(sum(item.quantity for item in active_batches(tuple(batches))), 6)


def allocate(batches: Sequence[InventoryItem], requested: float) -> List[BatchAllocation]:
    """把一次扣减量分摊到多个批次。

    - 数量不足时抛出 `InsufficientStockError`，由调用方整体拒绝；
    - 结果包含每个批次的扣减量与扣减前数量，便于界面展示与测试复现。
    """
    quantity = validate_quantity(requested)
    ordered = sort_batches(batches)
    available = total_quantity(ordered)
    if available + EPSILON < quantity:
        raise InsufficientStockError(quantity, available)

    remaining = quantity
    allocations: List[BatchAllocation] = []
    for item in ordered:
        if remaining <= EPSILON:
            break
        take = round(min(item.quantity, remaining), 6)
        if take <= 0:
            continue
        allocations.append(
            BatchAllocation(
                item_id=item.id,
                name=item.name,
                unit=item.unit,
                take=take,
                quantity_before=item.quantity,
                expiry_date=item.expiry_date,
                purchased_on=item.purchased_on,
            )
        )
        remaining = round(remaining - take, 6)
    return allocations


def summarize(batches: Iterable[InventoryItem]) -> List[StockSummaryRow]:
    """按"同名（归一化）+ 同单位"汇总活动批次，供界面展示与一致性校验。"""
    groups: dict = {}
    for item in active_batches(tuple(batches)):
        key = (item.name_normalized, item.unit)
        entry = groups.setdefault(
            key,
            {"name": item.name, "total": 0.0, "count": 0},
        )
        entry["total"] += item.quantity
        entry["count"] += 1

    return [
        StockSummaryRow(
            name=entry["name"],
            name_normalized=key[0],
            unit=key[1],
            total_quantity=round(entry["total"], 6),
            batch_count=entry["count"],
        )
        for key, entry in sorted(groups.items())
    ]


def assert_summary_matches_batches(
    batches: Sequence[InventoryItem], summaries: Sequence[StockSummaryRow]
) -> None:
    """校验汇总值等于各活动批次之和（不一致即抛错，供测试与界面自检使用）。"""
    computed = {(row.name_normalized, row.unit): row.total_quantity for row in summarize(batches)}
    for row in summaries:
        key = (row.name_normalized, row.unit)
        if key not in computed:
            raise ValidationError(f"汇总行 {key} 在批次中不存在")
        if abs(computed[key] - row.total_quantity) > EPSILON:
            raise ValidationError(
                f"汇总数量（{row.total_quantity:g}）不等于批次之和（{computed[key]:g}）：{key}"
            )
