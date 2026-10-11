"""菜谱缺口计算测试（S5）：充足、不足、零库存、单位不匹配与名称不匹配。"""

from __future__ import annotations

import pytest

from core.models import InventoryItem
from core.recipe_math import (
    MATCH_MATCHED,
    MATCH_UNMATCHED,
    MATCH_UNIT_MISMATCH,
    IngredientRequirement,
    compute_gaps,
    summarize_gaps,
)
from core.validation import ValidationError, normalize_name


def make_item(
    item_id: int,
    *,
    name: str = "番茄",
    unit: str = "个",
    quantity: float = 1.0,
    status: str = "active",
) -> InventoryItem:
    return InventoryItem(
        id=item_id,
        name=name,
        name_normalized=normalize_name(name),
        category="蔬菜",
        quantity=quantity,
        unit=unit,
        purchased_on="2026-10-01",
        purchase_date_defaulted=False,
        storage="冷藏",
        status=status,
    )


def requirement(name: str = "番茄", quantity: float = 1.0, unit: str = "个") -> IngredientRequirement:
    return IngredientRequirement(name=name, quantity=quantity, unit=unit)


def test_sufficient_stock_reports_zero_missing() -> None:
    gaps = compute_gaps(requirements=[requirement(quantity=2)], inventory=[make_item(1, quantity=5)])

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.match_state == MATCH_MATCHED
    assert gap.available_quantity == pytest.approx(5.0)
    assert gap.missing_quantity == pytest.approx(0.0)
    assert gap.is_sufficient is True
    assert gap.matched_batch_ids == (1,)


def test_shortage_reports_missing_quantity() -> None:
    gaps = compute_gaps(requirements=[requirement(quantity=4)], inventory=[make_item(1, quantity=2)])

    assert gaps[0].match_state == MATCH_MATCHED
    assert gaps[0].missing_quantity == pytest.approx(2.0)
    assert gaps[0].is_sufficient is False


def test_multiple_batches_are_summed() -> None:
    inventory = [
        make_item(1, quantity=2),
        make_item(2, quantity=3, status="active"),
        make_item(3, quantity=9, status="consumed"),
    ]

    gaps = compute_gaps(requirements=[requirement(quantity=6)], inventory=inventory)

    assert gaps[0].available_quantity == pytest.approx(5.0)
    assert gaps[0].missing_quantity == pytest.approx(1.0)
    assert gaps[0].matched_batch_ids == (1, 2)


def test_zero_stock_is_reported_as_missing_not_unknown() -> None:
    gaps = compute_gaps(requirements=[requirement(quantity=3)], inventory=[make_item(1, quantity=0)])

    assert gaps[0].match_state == MATCH_MATCHED
    assert gaps[0].available_quantity == pytest.approx(0.0)
    assert gaps[0].missing_quantity == pytest.approx(3.0)


def test_exhausted_batches_count_as_known_zero() -> None:
    """曾经有记录、批次已全部耗尽 → 已知无剩余（不是"未知"，也要给缺口）。"""
    inventory = [make_item(1, quantity=0, status="consumed"), make_item(2, quantity=0, status="discarded")]

    gaps = compute_gaps(requirements=[requirement(quantity=2)], inventory=inventory)

    assert gaps[0].match_state == MATCH_MATCHED
    assert gaps[0].available_quantity == pytest.approx(0.0)
    assert gaps[0].missing_quantity == pytest.approx(2.0)
    assert "已知无剩余" in (gaps[0].note or "")


def test_same_name_with_different_unit_is_not_comparable() -> None:
    inventory = [make_item(1, name="番茄", unit="盒", quantity=2)]

    gaps = compute_gaps(requirements=[requirement(name="番茄", quantity=2, unit="个")], inventory=inventory)

    assert gaps[0].match_state == MATCH_UNIT_MISMATCH
    assert gaps[0].available_quantity is None
    assert gaps[0].missing_quantity is None
    assert "盒" in (gaps[0].note or "")


def test_unknown_name_is_reported_as_unknown_not_zero() -> None:
    gaps = compute_gaps(requirements=[requirement(name="松露", quantity=1)], inventory=[make_item(1)])

    assert gaps[0].match_state == MATCH_UNMATCHED
    assert gaps[0].available_quantity is None
    assert gaps[0].missing_quantity is None
    assert "不代表 0" in (gaps[0].note or "")


def test_name_normalization_matches_whitespace_and_case() -> None:
    inventory = [make_item(1, name="Tomato", quantity=2)]

    gaps = compute_gaps(requirements=[requirement(name="  tomato ", quantity=1)], inventory=inventory)

    assert gaps[0].match_state == MATCH_MATCHED
    assert gaps[0].available_quantity == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc", None])
def test_invalid_requirement_quantity_is_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        compute_gaps(requirements=[requirement(quantity=bad)], inventory=[make_item(1)])


def test_blank_requirement_name_or_unit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        compute_gaps(requirements=[requirement(name="   ")], inventory=[])
    with pytest.raises(ValidationError):
        compute_gaps(requirements=[requirement(unit="")], inventory=[])


def test_computation_does_not_modify_inventory() -> None:
    inventory = [make_item(1, quantity=2), make_item(2, quantity=3, status="consumed")]
    before = [(item.id, item.quantity, item.status) for item in inventory]

    compute_gaps(requirements=[requirement(quantity=10)], inventory=inventory)

    assert [(item.id, item.quantity, item.status) for item in inventory] == before


def test_gap_summary_counts_each_state() -> None:
    inventory = [
        make_item(1, name="番茄", unit="个", quantity=5),
        make_item(2, name="鸡蛋", unit="个", quantity=0),
        make_item(3, name="牛奶", unit="盒", quantity=1),
    ]
    requirements = [
        requirement(name="番茄", quantity=2, unit="个"),   # 充足
        requirement(name="鸡蛋", quantity=3, unit="个"),   # 不足
        requirement(name="牛奶", quantity=1, unit="L"),    # 单位不匹配
        requirement(name="松露", quantity=1, unit="个"),   # 未匹配
    ]

    summary = summarize_gaps(compute_gaps(requirements=requirements, inventory=inventory))

    assert summary.matched_sufficient == 1
    assert summary.matched_missing == 1
    assert summary.unit_mismatch == 1
    assert summary.unmatched == 1


def test_empty_inventory_means_unknown_for_everything() -> None:
    gaps = compute_gaps(requirements=[requirement()], inventory=[])

    assert gaps[0].match_state == MATCH_UNMATCHED
    assert gaps[0].available_quantity is None
