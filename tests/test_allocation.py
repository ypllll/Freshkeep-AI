"""批次分配规则测试：顺序固定、可复现、数量不足即拒绝（product_spec §6.5、R-16）。"""

from __future__ import annotations

import pytest

from core.allocation import (
    InsufficientStockError,
    allocate,
    assert_summary_matches_batches,
    sort_batches,
    summarize,
    total_quantity,
)
from core.models import InventoryItem, StockSummaryRow
from core.validation import ValidationError, normalize_name


def make_item(
    item_id: int,
    *,
    name: str = "番茄",
    unit: str = "个",
    quantity: float = 1.0,
    purchased_on: str | None = "2026-10-01",
    expiry_date: str | None = None,
    status: str = "active",
) -> InventoryItem:
    return InventoryItem(
        id=item_id,
        name=name,
        name_normalized=normalize_name(name),
        category="蔬菜",
        quantity=quantity,
        unit=unit,
        purchased_on=purchased_on,
        purchase_date_defaulted=False,
        storage="冷藏",
        expiry_date=expiry_date,
        status=status,
    )


# --- 排序规则 ---------------------------------------------------------------


def test_batches_with_known_expiry_come_first() -> None:
    without_expiry = make_item(1, quantity=10, purchased_on="2020-01-01")
    with_expiry = make_item(2, quantity=5, purchased_on="2026-10-10", expiry_date="2026-12-01")

    ordered = sort_batches([without_expiry, with_expiry])

    assert [item.id for item in ordered] == [2, 1]


def test_known_expiry_sorted_earliest_first() -> None:
    later = make_item(1, expiry_date="2026-12-01")
    earlier = make_item(2, expiry_date="2026-10-20")

    assert [item.id for item in sort_batches([later, earlier])] == [2, 1]


def test_earliest_purchase_date_first_when_expiry_unknown() -> None:
    newer = make_item(1, purchased_on="2026-10-05")
    older = make_item(2, purchased_on="2026-10-01")

    assert [item.id for item in sort_batches([newer, older])] == [2, 1]


def test_unknown_purchase_date_goes_after_known_date() -> None:
    unknown = make_item(1, purchased_on=None)
    known = make_item(2, purchased_on="2026-10-09")

    assert [item.id for item in sort_batches([unknown, known])] == [2, 1]


def test_same_priority_falls_back_to_id_order() -> None:
    third = make_item(3)
    first = make_item(1)
    second = make_item(2)

    assert [item.id for item in sort_batches([third, first, second])] == [1, 2, 3]


def test_inactive_and_empty_batches_are_not_allocatable() -> None:
    consumed = make_item(1, status="consumed")
    empty = make_item(2, quantity=0)
    active = make_item(3, quantity=2)

    assert [item.id for item in sort_batches([consumed, empty, active])] == [3]


# --- 分配结果 ---------------------------------------------------------------


def test_allocate_uses_earliest_batch_first_and_reports_each_deduction() -> None:
    newer = make_item(1, quantity=5, purchased_on="2026-10-05")
    older = make_item(2, quantity=3, purchased_on="2026-10-01")

    allocations = allocate([newer, older], 4)

    assert [(a.item_id, a.take) for a in allocations] == [(2, 3.0), (1, 1.0)]
    assert allocations[0].quantity_before == 3.0
    assert allocations[0].quantity_after == 0.0
    assert allocations[1].quantity_after == 4.0


def test_allocate_exact_total_uses_every_batch() -> None:
    batches = [make_item(1, quantity=2, purchased_on="2026-10-01"), make_item(2, quantity=3, purchased_on="2026-10-02")]

    allocations = allocate(batches, 5)

    assert [(a.item_id, a.take) for a in allocations] == [(1, 2.0), (2, 3.0)]
    assert sum(a.take for a in allocations) == pytest.approx(5.0)


def test_allocate_is_reproducible() -> None:
    batches = [
        make_item(1, quantity=5, purchased_on="2026-10-05"),
        make_item(2, quantity=3, purchased_on="2026-10-01"),
        make_item(3, quantity=3, purchased_on=None),
    ]

    first = allocate(batches, 9)
    second = allocate(batches, 9)

    assert first == second
    assert [(a.item_id, a.take) for a in first] == [(2, 3.0), (1, 5.0), (3, 1.0)]


def test_allocate_rejects_when_total_is_not_enough() -> None:
    batches = [make_item(1, quantity=2), make_item(2, quantity=1)]

    with pytest.raises(InsufficientStockError) as excinfo:
        allocate(batches, 4)

    assert excinfo.value.requested == 4
    assert excinfo.value.available == pytest.approx(3.0)


def test_allocate_rejects_invalid_requested_quantity() -> None:
    batches = [make_item(1, quantity=5)]

    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            allocate(batches, bad)


def test_allocate_on_empty_inventory_is_rejected() -> None:
    with pytest.raises(InsufficientStockError):
        allocate([], 1)


def test_total_quantity_ignores_inactive_batches() -> None:
    batches = [make_item(1, quantity=2), make_item(2, quantity=3, status="discarded")]

    assert total_quantity(batches) == pytest.approx(2.0)


# --- 汇总一致性 -------------------------------------------------------------


def test_summarize_groups_same_name_and_unit_only() -> None:
    batches = [
        make_item(1, name="番茄", unit="个", quantity=2),
        make_item(2, name="番茄 ", unit="个", quantity=3),
        make_item(3, name="番茄", unit="盒", quantity=1),
        make_item(4, name="牛奶", unit="L", quantity=1.5),
    ]

    rows = {f"{row.name}|{row.unit}": row for row in summarize(batches)}

    assert rows["番茄|个"].total_quantity == pytest.approx(5.0)
    assert rows["番茄|个"].batch_count == 2
    assert rows["番茄|盒"].total_quantity == pytest.approx(1.0)
    assert rows["牛奶|L"].total_quantity == pytest.approx(1.5)
    assert len(rows) == 3


def test_summary_matches_sum_of_batches() -> None:
    batches = [
        make_item(1, name="番茄", quantity=2),
        make_item(2, name="番茄", quantity=3.5),
        make_item(3, name="鸡蛋", unit="个", quantity=6),
    ]

    rows = summarize(batches)

    assert_summary_matches_batches(batches, rows)
    assert sum(row.total_quantity for row in rows) == pytest.approx(11.5)


def test_summary_check_detects_tampered_value() -> None:
    batches = [make_item(1, quantity=2)]
    tampered = [
        StockSummaryRow(
            name="番茄",
            name_normalized="番茄",
            unit="个",
            total_quantity=99.0,
            batch_count=1,
        )
    ]

    with pytest.raises(ValidationError):
        assert_summary_matches_batches(batches, tampered)
