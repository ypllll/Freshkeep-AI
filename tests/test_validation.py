"""输入校验测试：关键校验必须由数据层负责（R-10、R-24；product_spec §6.2）。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from core.validation import (
    ValidationError,
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


# --- 名称 -------------------------------------------------------------------


def test_validate_name_keeps_user_wording_but_trims_spaces() -> None:
    assert validate_name("  番茄 ") == "番茄"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\n", "番茄\n鸡蛋", "a" * 81, "（）"],
)
def test_validate_name_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValidationError):
        validate_name(raw)


@pytest.mark.parametrize("raw", [123, None, ["番茄"]])
def test_validate_name_rejects_non_text(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_name(raw)


def test_normalize_name_removes_brackets_and_case() -> None:
    assert normalize_name(" 番茄（大） ") == "番茄"
    assert normalize_name("Tomato") == "tomato"
    assert normalize_name("  Tomato  ") == normalize_name("tomato")


def test_normalize_name_collapses_inner_whitespace() -> None:
    assert normalize_name("green   onion") == "green onion"


# --- 数量 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, 1.0), ("2.5", 2.5), (0.5, 0.5), ("0.25", 0.25)],
)
def test_validate_quantity_accepts_positive_numbers(raw: object, expected: float) -> None:
    assert validate_quantity(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [0, 0.0, -1, "-0.5", "", "abc", None, float("nan"), float("inf"), float("-inf"), "nan", "inf", True, False],
)
def test_validate_quantity_rejects_zero_negative_and_non_finite(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_quantity(raw)


# --- 白名单字段 -------------------------------------------------------------


def test_validate_unit_accepts_whitelist_and_strips() -> None:
    assert validate_unit("  个 ") == "个"
    assert validate_unit("kg") == "kg"


@pytest.mark.parametrize("raw", ["斤", "克", "", None, "G", 1])
def test_validate_unit_rejects_outside_whitelist(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_unit(raw)


def test_validate_category_and_storage_accept_whitelist_values() -> None:
    assert validate_category("蔬菜") == "蔬菜"
    assert validate_storage("冷冻") == "冷冻"


@pytest.mark.parametrize("raw", ["冰鲜", "", None, "cold"])
def test_validate_category_rejects_unknown_value(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_category(raw)


@pytest.mark.parametrize("raw", ["室温", "", None])
def test_validate_storage_rejects_unknown_value(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_storage(raw)


# --- 购买日期 ---------------------------------------------------------------


TODAY = date(2026, 10, 11)


def test_validate_purchase_date_accepts_date_and_iso_text() -> None:
    assert validate_purchase_date(date(2026, 10, 1), today=TODAY, allow_unknown=False) == date(2026, 10, 1)
    assert validate_purchase_date("2026-10-01", today=TODAY, allow_unknown=False) == date(2026, 10, 1)
    assert validate_purchase_date(datetime(2026, 10, 1, 8, 30), today=TODAY, allow_unknown=False) == date(2026, 10, 1)
    assert validate_purchase_date(TODAY, today=TODAY, allow_unknown=False) == TODAY


def test_validate_purchase_date_rejects_future_date() -> None:
    with pytest.raises(ValidationError):
        validate_purchase_date(date(2026, 10, 12), today=TODAY, allow_unknown=False)


def test_validate_purchase_date_allows_unknown_only_when_explicit() -> None:
    assert validate_purchase_date(None, today=TODAY, allow_unknown=True) is None
    assert validate_purchase_date("  ", today=TODAY, allow_unknown=True) is None

    with pytest.raises(ValidationError):
        validate_purchase_date(None, today=TODAY, allow_unknown=False)


@pytest.mark.parametrize("raw", ["2026/10/01", "not-a-date", 20261001])
def test_validate_purchase_date_rejects_bad_format(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_purchase_date(raw, today=TODAY, allow_unknown=False)


# --- 批次 ID 与备注 ---------------------------------------------------------


def test_validate_item_id_accepts_positive_int() -> None:
    assert validate_item_id(7) == 7


@pytest.mark.parametrize("raw", [0, -1, "7", 1.5, True, None])
def test_validate_item_id_rejects_non_positive_or_non_int(raw: object) -> None:
    with pytest.raises(ValidationError):
        validate_item_id(raw)


def test_validate_notes_normalizes_empty_to_none() -> None:
    assert validate_notes(None) is None
    assert validate_notes("   ") is None
    assert validate_notes("  送给邻居 ") == "送给邻居"


def test_validate_notes_rejects_non_text() -> None:
    with pytest.raises(ValidationError):
        validate_notes(42)
