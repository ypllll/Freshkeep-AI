"""保质期与新鲜度领域测试（product_spec §6.3；R-01 ~ R-04、R-25、R-26）。

全部使用显式传入的 `today`，不依赖系统当前时间，因此边界稳定。
"""

from __future__ import annotations

from datetime import date

import pytest

from core.dates import (
    AI_ESTIMATE_DISCLAIMER,
    AI_ESTIMATE_LABEL,
    DISCLAIMER,
    REFERENCE_SHELF_LIFE,
    SOURCE_UNKNOWN,
    SOURCE_USER_DATE,
    SOURCE_USER_DAYS,
    SOURCE_USER_DAYS_UNRESOLVED,
    STATUS_DUE_SOON,
    STATUS_EXPIRED,
    STATUS_FRESH,
    STATUS_UNKNOWN,
    ai_estimate,
    build_expiry_view,
    freshness_status,
    reference_range,
    resolve_expiry,
    validate_shelf_life_days,
)
from core.validation import ValidationError

TODAY = date(2026, 10, 11)
DUE_SOON_DAYS = 2


# --- 判定顺序（R-01 ~ R-04、R-25、R-26） ------------------------------------


def test_user_date_wins_and_needs_no_purchase_date() -> None:
    resolved = resolve_expiry(
        purchased_on=None, expiry_date="2026-12-01", shelf_life_days=None
    )

    assert resolved.expiry_date == date(2026, 12, 1)
    assert resolved.expiry_source == SOURCE_USER_DATE
    assert resolved.conflict is False


def test_user_days_never_become_a_formal_expiry_date() -> None:
    resolved = resolve_expiry(
        purchased_on="2026-10-01", expiry_date=None, shelf_life_days=10
    )

    assert resolved.expiry_date is None, "系统不得把天数换算成正式有效期"
    assert resolved.shelf_life_days == 10
    assert resolved.expiry_source == SOURCE_USER_DAYS_UNRESOLVED
    assert resolved.message is not None and "AI 估算" in resolved.message


def test_conflicting_date_and_days_keeps_date_and_reports_conflict() -> None:
    resolved = resolve_expiry(
        purchased_on="2026-10-01", expiry_date="2026-10-05", shelf_life_days=30
    )

    assert resolved.expiry_date == date(2026, 10, 5), "R-04：以具体日期为正式记录"
    assert resolved.shelf_life_days == 30, "保留用户输入的天数原值"
    assert resolved.expiry_source == SOURCE_USER_DATE
    assert resolved.conflict is True
    assert resolved.message is not None
    assert "不一致" in resolved.message
    assert "请确认哪个正确" in resolved.message


def test_matching_date_and_days_is_not_a_conflict() -> None:
    resolved = resolve_expiry(
        purchased_on="2026-10-01", expiry_date="2026-10-11", shelf_life_days=10
    )

    assert resolved.conflict is False
    assert resolved.message is None


def test_days_without_purchase_date_are_not_converted_and_not_assumed_today() -> None:
    resolved = resolve_expiry(purchased_on=None, expiry_date=None, shelf_life_days=10)

    assert resolved.expiry_date is None, "不得假定购买日期为今天"
    assert resolved.shelf_life_days == 10, "保留用户输入的天数"
    assert resolved.expiry_source == SOURCE_USER_DAYS_UNRESOLVED
    assert resolved.message is not None
    assert "不会生成任何估算日期" in resolved.message


def test_no_user_input_means_unknown_formal_expiry() -> None:
    resolved = resolve_expiry(purchased_on="2026-10-01", expiry_date=None, shelf_life_days=None)

    assert resolved.expiry_date is None
    assert resolved.shelf_life_days is None
    assert resolved.expiry_source == SOURCE_UNKNOWN


@pytest.mark.parametrize(
    ("purchased_on", "expiry_date", "days"),
    [
        ("2026-10-10", "2026-10-01", None),
        ("2026-10-10", "2026-10-09", 5),
    ],
)
def test_expiry_before_purchase_is_rejected(purchased_on, expiry_date, days) -> None:
    with pytest.raises(ValidationError):
        resolve_expiry(purchased_on=purchased_on, expiry_date=expiry_date, shelf_life_days=days)


@pytest.mark.parametrize("bad", [0, -1, 1.5, "abc", "1.5"])
def test_invalid_shelf_life_days_are_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        validate_shelf_life_days(bad)


@pytest.mark.parametrize(("raw", "expected"), [("7", 7), (7, 7), (7.0, 7), ("", None), (None, None)])
def test_shelf_life_days_accept_forms_from_forms_and_code(raw, expected) -> None:
    assert validate_shelf_life_days(raw) == expected


# --- 新鲜度分级（R-02） -----------------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "expected"),
    [
        ("2026-10-10", STATUS_EXPIRED),   # 昨天
        ("2026-10-11", STATUS_DUE_SOON),  # 今天到期
        ("2026-10-12", STATUS_DUE_SOON),  # 阈值内
        ("2026-10-13", STATUS_DUE_SOON),  # 正好等于阈值
        ("2026-10-14", STATUS_FRESH),     # 超过阈值
    ],
)
def test_freshness_boundaries(expiry: str, expected: str) -> None:
    assert (
        freshness_status(
            expiry_date=expiry,
            expiry_source=SOURCE_USER_DATE,
            today=TODAY,
            due_soon_days=DUE_SOON_DAYS,
        )
        == expected
    )


@pytest.mark.parametrize(
    "source", [SOURCE_UNKNOWN, SOURCE_USER_DAYS_UNRESOLVED]
)
def test_only_formal_user_dates_can_be_graded(source: str) -> None:
    assert (
        freshness_status(
            expiry_date=None,
            expiry_source=source,
            today=TODAY,
            due_soon_days=DUE_SOON_DAYS,
        )
        == STATUS_UNKNOWN
    )


def test_user_days_source_is_graded_normally() -> None:
    assert (
        freshness_status(
            expiry_date="2026-10-12",
            expiry_source=SOURCE_USER_DAYS,
            today=TODAY,
            due_soon_days=DUE_SOON_DAYS,
        )
        == STATUS_DUE_SOON
    )


# --- 参考区间（R-03） -------------------------------------------------------


def test_reference_range_hit_returns_days_and_basis() -> None:
    reference = reference_range("蔬菜", "冷藏")

    assert reference is not None
    assert reference.days_min < reference.days_max
    assert reference.basis


@pytest.mark.parametrize(
    ("category", "storage"),
    [("其他", "冷藏"), ("蔬菜", "其他"), (None, "冷藏"), ("蔬菜", None), ("海鲜", "常温")],
)
def test_reference_range_miss_returns_none(category, storage) -> None:
    assert reference_range(category, storage) is None


def test_reference_table_stays_limited_and_has_basis() -> None:
    assert len(REFERENCE_SHELF_LIFE) <= 20, "V1 只维护有限参考表"
    for (category, storage), reference in REFERENCE_SHELF_LIFE.items():
        assert category != "其他" and storage != "其他"
        assert 0 < reference.days_min <= reference.days_max
        assert reference.basis


def test_reference_range_text_has_no_dates_and_keeps_disclaimer() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date=None,
        shelf_life_days=None,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.formal_text == "未知", "参考区间不得写入正式期限"
    assert view.reference_text is not None
    assert "约 3–7 天" in view.reference_text
    assert "叶菜类" in view.reference_text
    assert DISCLAIMER in view.reference_text
    assert "2026-" not in view.reference_text, "通用参考区间不换算成日期"
    assert view.status == STATUS_UNKNOWN, "参考区间不得把状态提升为新鲜"


def test_ai_estimate_is_labelled_and_never_written_as_formal_expiry() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date=None,
        shelf_life_days=None,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.ai_estimate_text is not None
    assert view.ai_estimate_text.startswith(AI_ESTIMATE_LABEL)
    assert "2026-10-04" in view.ai_estimate_text
    assert "2026-10-08" in view.ai_estimate_text
    assert AI_ESTIMATE_DISCLAIMER in view.ai_estimate_text
    assert view.formal_text == "未知", "AI 估算不得写成正式有效期"
    assert view.source == SOURCE_UNKNOWN
    assert view.status == STATUS_UNKNOWN, "AI 估算不参与新鲜度分级"


def test_reference_without_purchase_date_is_not_converted_to_dates() -> None:
    view = build_expiry_view(
        purchased_on=None,
        expiry_date=None,
        shelf_life_days=None,
        category="肉类",
        storage="冷冻",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.reference_text is not None
    assert "2026-" not in view.reference_text, "购买日期未知时不得换算成日期"
    assert view.ai_estimate_text is not None
    assert "未生成" in view.ai_estimate_text, "缺少购买日期时不得生成估算日期"


def test_reference_miss_is_reported_as_unknown_without_guessing() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date=None,
        shelf_life_days=None,
        category="其他",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.reference_text is not None
    assert "未收录" in view.reference_text
    assert "2026-1" not in view.reference_text
    assert view.status == STATUS_UNKNOWN


def test_formal_expiry_never_carries_reference_text() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date="2026-10-20",
        shelf_life_days=None,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.formal_text == "2026-10-20"
    assert view.reference_text is None, "参考区间不得与正式期限混淆"
    assert view.ai_estimate_text is None, "有用户日期时不再生成 AI 估算"
    assert view.status == STATUS_FRESH
    assert view.conflict is False


def test_unresolved_days_view_keeps_unknown_status_and_warning() -> None:
    view = build_expiry_view(
        purchased_on=None,
        expiry_date=None,
        shelf_life_days=15,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.status == STATUS_UNKNOWN
    assert view.status_label == "未知"
    assert view.source == SOURCE_USER_DAYS_UNRESOLVED
    assert "15 天" in view.formal_text
    assert view.warning is not None and "没有填写明确的有效期日期" in view.warning
    assert view.reference_text is None, "用户已提供天数时不再并列通用参考区间"
    assert view.ai_estimate_text is not None and "未生成" in view.ai_estimate_text


def test_conflict_view_exposes_warning_to_the_ui() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date="2026-10-05",
        shelf_life_days=30,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.conflict is True
    assert view.warning is not None and "不一致" in view.warning
    assert view.formal_text == "2026-10-05"


# --- AI 估算（规则 2 / 3 / 5） ----------------------------------------------


def test_ai_estimate_requires_a_purchase_date() -> None:
    assert ai_estimate(category="蔬菜", storage="冷藏", purchased_on=None) is None


@pytest.mark.parametrize(
    ("category", "storage"),
    [("其他", "冷藏"), ("蔬菜", None), (None, "冷藏"), ("海鲜", "常温")],
)
def test_ai_estimate_requires_a_known_category_and_storage(category, storage) -> None:
    assert ai_estimate(category=category, storage=storage, purchased_on="2026-10-01") is None


def test_ai_estimate_computes_a_window_from_the_purchase_date() -> None:
    estimate = ai_estimate(category="蔬菜", storage="冷藏", purchased_on="2026-10-01")

    assert estimate is not None
    assert (estimate.days_min, estimate.days_max) == (3, 7)
    assert estimate.date_min == date(2026, 10, 4)
    assert estimate.date_max == date(2026, 10, 8)
    assert estimate.basis


def test_estimate_is_not_closer_to_the_user_than_the_formal_date() -> None:
    """有用户日期时不生成估算，避免与用户的正式期限竞争。"""
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date="2026-10-31",
        shelf_life_days=None,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
    )

    assert view.ai_estimate_text is None
    assert view.reference_text is None
    assert view.formal_text == "2026-10-31"


# --- 展示以库中来源为准（修复"系统换算被标成用户提供"） ----------------------


def test_stored_source_is_authoritative_for_display() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date="2026-10-31",
        shelf_life_days=30,
        category="蛋奶",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
        stored_source=SOURCE_USER_DAYS,
    )

    assert view.source == SOURCE_USER_DAYS, "不得把历史换算日期重新标成 user_date"
    assert view.source_label.startswith("用户提供（按天数换算")
    assert view.formal_text == "2026-10-31"


def test_unknown_stored_source_falls_back_to_recomputed_source() -> None:
    view = build_expiry_view(
        purchased_on="2026-10-01",
        expiry_date="2026-10-20",
        shelf_life_days=None,
        category="蔬菜",
        storage="冷藏",
        today=TODAY,
        due_soon_days=DUE_SOON_DAYS,
        stored_source="not-a-real-source",
    )

    assert view.source == SOURCE_USER_DATE
