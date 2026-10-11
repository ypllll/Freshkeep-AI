"""数字冰箱业务规则测试（阶段 2）。

覆盖：批次独立、汇总一致、事件语义、非法输入拒绝、固定分配顺序、
多批次原子性、跨连接持久化。测试使用 `tmp_path` 下的一次性数据库。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from core.allocation import InsufficientStockError
from core.validation import ValidationError, normalize_name
from services import inventory_service
from storage import inventory_repo

TODAY = date(2026, 10, 11)


@pytest.fixture
def db_conn(tmp_db_path):
    connection = inventory_service.open_connection(tmp_db_path)
    try:
        yield connection
    finally:
        connection.close()


def add(
    conn,
    *,
    name: str = "番茄",
    quantity: float = 1.0,
    unit: str = "个",
    category: str = "蔬菜",
    storage: str = "冷藏",
    purchased_on="2026-10-01",
    initial: bool = False,
    notes=None,
    purchase_date_defaulted: bool = False,
) -> int:
    return inventory_service.add_batch(
        conn,
        name=name,
        category=category,
        quantity=quantity,
        unit=unit,
        storage=storage,
        purchased_on=purchased_on,
        purchase_date_defaulted=purchase_date_defaulted,
        notes=notes,
        initial=initial,
        today=TODAY,
    )


def events_of(conn, item_id: int):
    return inventory_service.list_events(conn, item_id=item_id)


def event_count(conn) -> int:
    return len(inventory_service.list_events(conn, limit=1000))


# --- 批次独立与汇总 ---------------------------------------------------------


def test_new_batch_does_not_overwrite_existing_same_name(db_conn) -> None:
    first_id = add(db_conn, quantity=3)
    second_id = add(db_conn, quantity=2, purchased_on="2026-10-08")

    assert first_id != second_id
    batches = inventory_service.list_batches(db_conn, name_normalized=normalize_name("番茄"))

    assert [item.quantity for item in batches] == [3.0, 2.0]
    assert inventory_service.get_batch(db_conn, first_id).quantity == pytest.approx(3.0)


def test_summary_equals_sum_of_active_batches(db_conn) -> None:
    add(db_conn, name="番茄", unit="个", quantity=2)
    add(db_conn, name="番茄", unit="个", quantity=3, purchased_on="2026-10-05")
    add(db_conn, name="牛奶", unit="L", quantity=1.5)

    summaries = inventory_service.list_summary(db_conn)
    by_key = {(row.name_normalized, row.unit): row for row in summaries}

    assert by_key[("番茄", "个")].total_quantity == pytest.approx(5.0)
    assert by_key[("番茄", "个")].batch_count == 2
    assert by_key[("牛奶", "L")].total_quantity == pytest.approx(1.5)

    active = inventory_service.list_batches(db_conn, active_only=True)
    for row in summaries:
        expected = sum(
            item.quantity
            for item in active
            if item.name_normalized == row.name_normalized and item.unit == row.unit
        )
        assert row.total_quantity == pytest.approx(expected)


def test_same_name_with_different_units_is_not_merged(db_conn) -> None:
    add(db_conn, name="番茄", unit="个", quantity=2)
    add(db_conn, name="番茄", unit="盒", quantity=1)

    keys = {(row.name_normalized, row.unit) for row in inventory_service.list_summary(db_conn)}

    assert keys == {("番茄", "个"), ("番茄", "盒")}


def test_summary_excludes_batches_consumed_to_zero(db_conn) -> None:
    add(db_conn, quantity=2)
    add(db_conn, quantity=3, purchased_on="2026-10-05")

    inventory_service.consume(
        db_conn, quantity=5, name_normalized=normalize_name("番茄"), unit="个"
    )

    assert inventory_service.list_summary(db_conn) == []


# --- 入库事件 ---------------------------------------------------------------


def test_add_batch_writes_purchase_add_event(db_conn) -> None:
    item_id = add(db_conn, quantity=2.5)

    events = events_of(db_conn, item_id)
    assert len(events) == 1
    assert events[0].action == "purchase_add"
    assert events[0].qty_delta == pytest.approx(2.5)
    assert events[0].qty_after == pytest.approx(2.5)
    assert events[0].source == "manual"


def test_initial_entry_uses_initial_add_and_allows_unknown_purchase_date(db_conn) -> None:
    item_id = add(db_conn, quantity=1, initial=True, purchased_on=None)
    batch = inventory_service.get_batch(db_conn, item_id)

    assert batch.purchased_on is None
    assert batch.purchase_date_defaulted is False
    assert events_of(db_conn, item_id)[0].action == "initial_add"


def test_new_purchase_requires_purchase_date(db_conn) -> None:
    with pytest.raises(ValidationError):
        inventory_service.add_batch(
            db_conn,
            name="番茄",
            category="蔬菜",
            quantity=1,
            unit="个",
            storage="冷藏",
            purchased_on=None,
            initial=False,
            today=TODAY,
        )

    assert inventory_service.list_summary(db_conn) == []


def test_defaulted_purchase_date_is_visible_in_record(db_conn) -> None:
    item_id = add(db_conn, purchased_on=TODAY, purchase_date_defaulted=True)
    batch = inventory_service.get_batch(db_conn, item_id)

    assert batch.purchased_on == "2026-10-11"
    assert batch.purchase_date_defaulted is True


# --- 修改数量 ---------------------------------------------------------------


def test_update_quantity_writes_edit_event_with_signed_delta(db_conn) -> None:
    item_id = add(db_conn, quantity=3)

    assert inventory_service.update_quantity(db_conn, item_id, 5) == (3.0, 5.0)
    assert inventory_service.update_quantity(db_conn, item_id, 2) == (5.0, 2.0)

    deltas = [event.qty_delta for event in events_of(db_conn, item_id) if event.action == "edit"]
    assert deltas == [-3.0, 2.0]


def test_update_quantity_without_change_writes_no_event(db_conn) -> None:
    item_id = add(db_conn, quantity=3)

    inventory_service.update_quantity(db_conn, item_id, 3)

    assert [event.action for event in events_of(db_conn, item_id)] == ["purchase_add"]


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc"])
def test_update_quantity_rejects_invalid_values(db_conn, bad) -> None:
    item_id = add(db_conn, quantity=3)

    with pytest.raises(ValidationError):
        inventory_service.update_quantity(db_conn, item_id, bad)

    assert inventory_service.get_batch(db_conn, item_id).quantity == pytest.approx(3.0)
    assert len(events_of(db_conn, item_id)) == 1


# --- 消耗与丢弃：自动分配 ---------------------------------------------------


def test_consume_across_batches_follows_fixed_order(db_conn) -> None:
    newer_id = add(db_conn, quantity=5, purchased_on="2026-10-05")
    older_id = add(db_conn, quantity=3, purchased_on="2026-10-01")

    allocations = inventory_service.consume(
        db_conn, quantity=4, name_normalized=normalize_name("番茄"), unit="个"
    )

    assert [(a.item_id, a.take) for a in allocations] == [(older_id, 3.0), (newer_id, 1.0)]

    older = inventory_service.get_batch(db_conn, older_id)
    newer = inventory_service.get_batch(db_conn, newer_id)
    assert older.quantity == pytest.approx(0.0)
    assert older.status == "consumed"
    assert newer.quantity == pytest.approx(4.0)
    assert newer.status == "active"

    assert [(e.action, e.qty_delta, e.qty_after) for e in events_of(db_conn, older_id)] == [
        ("consume", -3.0, 0.0),
        ("purchase_add", 3.0, 3.0),
    ]
    assert [(e.action, e.qty_delta, e.qty_after) for e in events_of(db_conn, newer_id)] == [
        ("consume", -1.0, 4.0),
        ("purchase_add", 5.0, 5.0),
    ]


def test_consume_allocates_by_purchase_date_when_dates_differ(db_conn) -> None:
    unknown_date_id = add(db_conn, quantity=5, initial=True, purchased_on=None)
    dated_id = add(db_conn, quantity=5, purchased_on="2026-10-09")

    allocations = inventory_service.consume(
        db_conn, quantity=2, name_normalized=normalize_name("番茄"), unit="个"
    )

    assert [a.item_id for a in allocations] == [dated_id]
    assert inventory_service.get_batch(db_conn, unknown_date_id).quantity == pytest.approx(5.0)


def test_discard_marks_batch_discarded_at_zero(db_conn) -> None:
    item_id = add(db_conn, quantity=2)

    inventory_service.discard(db_conn, quantity=2, item_id=item_id)

    batch = inventory_service.get_batch(db_conn, item_id)
    assert batch.quantity == pytest.approx(0.0)
    assert batch.status == "discarded"
    assert events_of(db_conn, item_id)[0].action == "discard"


def test_event_deltas_sum_equals_stock_change(db_conn) -> None:
    item_id = add(db_conn, quantity=2)
    inventory_service.update_quantity(db_conn, item_id, 5)
    inventory_service.consume(db_conn, quantity=1, item_id=item_id)

    events = events_of(db_conn, item_id)
    final_quantity = inventory_service.get_batch(db_conn, item_id).quantity

    assert sum(event.qty_delta for event in events) == pytest.approx(final_quantity)
    assert final_quantity == pytest.approx(4.0)


# --- 消耗与丢弃：拒绝路径 ---------------------------------------------------


def test_consume_rejects_oversized_request_without_any_change(db_conn) -> None:
    item_id = add(db_conn, quantity=5)

    with pytest.raises(InsufficientStockError):
        inventory_service.consume(
            db_conn, quantity=6, name_normalized=normalize_name("番茄"), unit="个"
        )

    assert inventory_service.get_batch(db_conn, item_id).quantity == pytest.approx(5.0)
    assert len(events_of(db_conn, item_id)) == 1


def test_discard_rejects_oversized_request(db_conn) -> None:
    item_id = add(db_conn, quantity=1)

    with pytest.raises(InsufficientStockError):
        inventory_service.discard(db_conn, quantity=2, item_id=item_id)

    assert inventory_service.get_batch(db_conn, item_id).quantity == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "abc"])
def test_consume_rejects_invalid_quantity(db_conn, bad) -> None:
    item_id = add(db_conn, quantity=5)

    with pytest.raises(ValidationError):
        inventory_service.consume(
            db_conn, quantity=bad, name_normalized=normalize_name("番茄"), unit="个"
        )

    assert inventory_service.get_batch(db_conn, item_id).quantity == pytest.approx(5.0)
    assert event_count(db_conn) == 1


def test_specified_batch_consume_touches_only_that_batch(db_conn) -> None:
    older_id = add(db_conn, quantity=5, purchased_on="2026-10-01")
    newer_id = add(db_conn, quantity=5, purchased_on="2026-10-09")

    allocations = inventory_service.consume(db_conn, quantity=2, item_id=newer_id)

    assert [a.item_id for a in allocations] == [newer_id]
    assert inventory_service.get_batch(db_conn, newer_id).quantity == pytest.approx(3.0)
    assert inventory_service.get_batch(db_conn, older_id).quantity == pytest.approx(5.0)
    assert len(events_of(db_conn, older_id)) == 1


def test_specified_batch_shortage_is_rejected_without_cross_batch_topping_up(db_conn) -> None:
    small_id = add(db_conn, quantity=1, purchased_on="2026-10-01")
    big_id = add(db_conn, quantity=5, purchased_on="2026-10-09")

    with pytest.raises(InsufficientStockError):
        inventory_service.consume(db_conn, quantity=3, item_id=small_id)

    assert inventory_service.get_batch(db_conn, small_id).quantity == pytest.approx(1.0)
    assert inventory_service.get_batch(db_conn, big_id).quantity == pytest.approx(5.0)
    assert event_count(db_conn) == 2, "失败的操作不得写入事件"


def test_operations_on_non_active_batch_are_rejected(db_conn) -> None:
    item_id = add(db_conn, quantity=1)
    inventory_service.consume(db_conn, quantity=1, item_id=item_id)

    with pytest.raises(inventory_service.BatchNotFoundError):
        inventory_service.consume(db_conn, quantity=1, item_id=item_id)

    with pytest.raises(inventory_service.BatchNotFoundError):
        inventory_service.update_quantity(db_conn, item_id, 5)

    with pytest.raises(inventory_service.BatchNotFoundError):
        inventory_service.get_batch(db_conn, 99999)


# --- 原子性 -----------------------------------------------------------------


def test_rollback_when_a_batch_update_conflicts(db_conn, monkeypatch) -> None:
    first_id = add(db_conn, quantity=3, purchased_on="2026-10-01")
    second_id = add(db_conn, quantity=5, purchased_on="2026-10-05")
    original = inventory_repo.decrease_quantity
    calls = {"count": 0}

    def flaky(conn, item_id, amount, updated_at, *, new_status=None):
        calls["count"] += 1
        if calls["count"] == 2:
            return 0
        return original(conn, item_id, amount, updated_at, new_status=new_status)

    monkeypatch.setattr(inventory_repo, "decrease_quantity", flaky)

    with pytest.raises(inventory_service.InventoryError):
        inventory_service.consume(
            db_conn, quantity=6, name_normalized=normalize_name("番茄"), unit="个"
        )

    assert inventory_service.get_batch(db_conn, first_id).quantity == pytest.approx(3.0)
    assert inventory_service.get_batch(db_conn, second_id).quantity == pytest.approx(5.0)
    assert inventory_service.get_batch(db_conn, first_id).status == "active"
    assert event_count(db_conn) == 2, "回滚后不得留下任何消耗事件"


def test_rollback_when_event_write_fails(db_conn, monkeypatch) -> None:
    first_id = add(db_conn, quantity=3, purchased_on="2026-10-01")
    second_id = add(db_conn, quantity=5, purchased_on="2026-10-05")
    original = inventory_repo.insert_event
    calls = {"count": 0}

    def flaky(conn, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("模拟事件写入失败")
        return original(conn, **kwargs)

    monkeypatch.setattr(inventory_repo, "insert_event", flaky)

    with pytest.raises(sqlite3.OperationalError):
        inventory_service.consume(
            db_conn, quantity=6, name_normalized=normalize_name("番茄"), unit="个"
        )

    assert inventory_service.get_batch(db_conn, first_id).quantity == pytest.approx(3.0)
    assert inventory_service.get_batch(db_conn, second_id).quantity == pytest.approx(5.0)
    assert event_count(db_conn) == 2


# --- 数据层校验与持久化 -----------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"unit": "斤"},
        {"category": "冰鲜"},
        {"storage": "室温"},
        {"name": "   "},
        {"quantity": 0},
        {"quantity": -2},
        {"quantity": float("nan")},
        {"purchased_on": "2099-01-01"},
    ],
)
def test_invalid_input_is_rejected_in_the_data_layer(db_conn, override) -> None:
    parameters = {
        "name": "番茄",
        "quantity": 1.0,
        "unit": "个",
        "category": "蔬菜",
        "storage": "冷藏",
        "purchased_on": "2026-10-01",
    }
    parameters.update(override)

    with pytest.raises(ValidationError):
        add(db_conn, **parameters)

    assert inventory_service.list_summary(db_conn) == []
    assert event_count(db_conn) == 0


def test_data_survives_reconnect(tmp_db_path) -> None:
    first = inventory_service.open_connection(tmp_db_path)
    try:
        item_id = add(first, quantity=2.5)
        inventory_service.consume(first, quantity=0.5, item_id=item_id)
    finally:
        first.close()

    second = inventory_service.open_connection(tmp_db_path)
    try:
        batch = inventory_service.get_batch(second, item_id)
        assert batch.quantity == pytest.approx(2.0)
        summaries = inventory_service.list_summary(second)
        assert summaries[0].total_quantity == pytest.approx(2.0)
        assert len(events_of(second, item_id)) == 2
    finally:
        second.close()
