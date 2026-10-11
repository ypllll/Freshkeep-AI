"""过敏原领域与持久化测试（product_spec §6.4；R-05 ~ R-08、R-14）。

包含三层：

1. 纯函数判定（别名、三态、复合调味料、按批次独立）；
2. 服务与数据库（用户过敏原清单、批次声明与保质期字段的落库、事件不受影响）；
3. 迁移（v2 → v3 保留数据；迁移失败回滚且不破坏数据）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from core import allergens as allergen_rules
from core.models import InventoryItem
from core.validation import ValidationError, normalize_name
from services import inventory_service
from storage import db as db_module
from storage import schema as schema_module

TODAY = date(2026, 10, 11)

# 记录真实的迁移定义，供 monkeypatch 之后恢复（模块导入时即捕获）
REAL_MIGRATIONS = list(schema_module.MIGRATIONS)
REAL_SCHEMA_VERSION = schema_module.SCHEMA_VERSION


def make_item(
    item_id: int = 1,
    *,
    name: str = "土豆",
    declared_allergens: str | None = None,
    allergen_status: str = "unknown",
) -> InventoryItem:
    return InventoryItem(
        id=item_id,
        name=name,
        name_normalized=normalize_name(name),
        category="蔬菜",
        quantity=1.0,
        unit="个",
        purchased_on="2026-10-01",
        purchase_date_defaulted=False,
        storage="冷藏",
        allergen_status=allergen_status,
        declared_allergens=declared_allergens,
    )


@pytest.fixture
def db_conn(tmp_db_path):
    connection = inventory_service.open_connection(tmp_db_path)
    try:
        yield connection
    finally:
        connection.close()


# --- 归一化与别名 -----------------------------------------------------------


def test_normalize_handles_case_whitespace_and_brackets() -> None:
    assert allergen_rules.normalize_allergen("  Peanut  ") == "peanut"
    assert allergen_rules.normalize_allergen("花生（生）") == "花生"
    assert allergen_rules.normalize_allergen("大  豆") == "大 豆"


def test_canonical_maps_explicit_aliases_only() -> None:
    assert allergen_rules.canonical_allergen("花生酱") == "花生"
    assert allergen_rules.canonical_allergen("黄豆") == "大豆"
    assert allergen_rules.canonical_allergen("芝士") == "牛奶"
    assert allergen_rules.canonical_allergen("藜麦") == "藜麦", "未收录的名称不猜测"


def test_single_character_aliases_only_match_exactly() -> None:
    """单字别名（如"蟹"）只做精确匹配，避免把"蟹味菇"误判为甲壳类。"""
    for canonical, aliases in allergen_rules.ALLERGEN_ALIASES.items():
        assert aliases, canonical
        for alias in aliases:
            assert normalize_name(alias), f"{canonical} 存在空别名"

    exact = allergen_rules.assess_ingredient(
        name="蟹", declared_allergens=None, allergen_status="declared", user_allergens=["甲壳类"]
    )
    assert exact.status == allergen_rules.STATUS_CONFLICT

    contained = allergen_rules.assess_ingredient(
        name="蟹味菇", declared_allergens=None, allergen_status="declared", user_allergens=["甲壳类"]
    )
    assert contained.status == allergen_rules.STATUS_NONE_KNOWN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("花生，牛奶", ["花生", "牛奶"]),
        ("花生、花生酱", ["花生"]),
        ("大豆;小麦 芝麻", ["大豆", "小麦", "芝麻"]),
        ("", []),
    ],
)
def test_parse_allergen_input(raw: str, expected: list) -> None:
    assert allergen_rules.parse_allergen_input(raw) == expected


# --- 三态判定 ---------------------------------------------------------------


def test_alias_hit_in_ingredient_name_is_a_conflict() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="花生酱", declared_allergens=None, allergen_status="unknown", user_allergens=["花生"]
    )

    assert assessment.status == allergen_rules.STATUS_CONFLICT
    assert assessment.matched_allergens == ("花生",)
    assert "花生" in assessment.matched_tokens
    assert "冲突" in assessment.message


def test_declared_entry_hit_is_a_conflict() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="混合调味粉",
        declared_allergens=["可能含花生"],
        allergen_status="declared",
        user_allergens=["花生"],
    )

    assert assessment.status == allergen_rules.STATUS_CONFLICT
    assert assessment.declaration_state == allergen_rules.DECLARATION_DECLARED


def test_undefined_declaration_is_unverifiable_when_user_has_allergens() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="土豆", declared_allergens=None, allergen_status="unknown", user_allergens=["花生"]
    )

    assert assessment.status == allergen_rules.STATUS_UNVERIFIABLE
    assert assessment.needs_review is True
    assert "未声明" in assessment.message
    assert "花生" in assessment.message


def test_undefined_declaration_without_user_allergens_is_not_a_conflict() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="土豆", declared_allergens=None, allergen_status="unknown", user_allergens=[]
    )

    assert assessment.status == allergen_rules.STATUS_NONE_KNOWN
    assert assessment.declaration_state == allergen_rules.DECLARATION_UNKNOWN
    assert "没有声明任何过敏原" in assessment.reasons[0]


@pytest.mark.parametrize(
    ("name", "user_allergen", "expected_word"),
    [("酱油", "大豆", "大豆"), ("蚝油", "贝类", "贝类"), ("沙茶酱", "花生", "花生")],
)
def test_compound_seasonings_are_unverifiable(name: str, user_allergen: str, expected_word: str) -> None:
    assessment = allergen_rules.assess_ingredient(
        name=name, declared_allergens=None, allergen_status="unknown", user_allergens=[user_allergen]
    )

    assert assessment.status == allergen_rules.STATUS_UNVERIFIABLE
    assert "复合调味料" in assessment.message
    assert expected_word in assessment.message
    assert "请自行核实" in assessment.message


def test_compound_seasoning_without_overlap_is_still_unverifiable() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="酱油", declared_allergens=None, allergen_status="declared", user_allergens=["芒果"]
    )

    assert assessment.status == allergen_rules.STATUS_UNVERIFIABLE
    assert "可能含" in assessment.message


def test_no_known_conflict_message_never_claims_safety() -> None:
    assessment = allergen_rules.assess_ingredient(
        name="土豆", declared_allergens=["无"], allergen_status="declared", user_allergens=["花生"]
    )

    assert assessment.status == allergen_rules.STATUS_NONE_KNOWN
    assert assessment.message == allergen_rules.NONE_KNOWN_MESSAGE
    assert "不代表安全" in assessment.message
    for forbidden in allergen_rules.FORBIDDEN_WORDINGS:
        assert forbidden not in assessment.message


def test_similar_but_unlisted_names_do_not_trigger_conflict() -> None:
    """匹配只依据显式别名表：形近/同音词不会被当作同一种过敏原。"""
    assessment = allergen_rules.assess_ingredient(
        name="鱼腥草", declared_allergens=None, allergen_status="declared", user_allergens=["鱼类"]
    )
    assert assessment.status == allergen_rules.STATUS_NONE_KNOWN

    cake = allergen_rules.assess_ingredient(
        name="蛋糕", declared_allergens=None, allergen_status="declared", user_allergens=["鸡蛋"]
    )
    assert cake.status == allergen_rules.STATUS_NONE_KNOWN


@pytest.mark.parametrize(
    ("name", "user_allergen"),
    [
        ("酸奶", "牛奶"),
        ("挂面", "小麦"),
        ("虾米", "甲壳类"),
        ("蛤蜊", "贝类"),
        ("味噌", "大豆"),
    ],
)
def test_common_derived_foods_are_recognised_by_explicit_aliases(
    name: str, user_allergen: str
) -> None:
    assessment = allergen_rules.assess_ingredient(
        name=name, declared_allergens=None, allergen_status="declared", user_allergens=[user_allergen]
    )

    assert assessment.status == allergen_rules.STATUS_CONFLICT
    assert user_allergen in assessment.matched_allergens


def test_declaration_is_per_batch_and_not_inherited() -> None:
    declared_batch = make_item(1, name="挂面", declared_allergens='["牛奶"]', allergen_status="declared")
    undeclared_batch = make_item(2, name="挂面", declared_allergens=None, allergen_status="unknown")

    first = allergen_rules.assess_item(declared_batch, ["牛奶"])
    second = allergen_rules.assess_item(undeclared_batch, ["牛奶"])

    assert first.status == allergen_rules.STATUS_CONFLICT
    assert second.status == allergen_rules.STATUS_UNVERIFIABLE
    assert second.declaration_state == allergen_rules.DECLARATION_UNKNOWN


def test_encode_and_decode_declared_allergens_round_trip() -> None:
    assert allergen_rules.encode_declared_allergens(None) is None
    encoded = allergen_rules.encode_declared_allergens(["花生", "牛奶"])
    assert encoded is not None and encoded.startswith("[")
    assert allergen_rules.decode_declared_allergens(encoded) == ["花生", "牛奶"]
    assert allergen_rules.decode_declared_allergens(None) == []
    assert allergen_rules.decode_declared_allergens("不是 JSON") == ["不是", "json"]


# --- 服务与数据库 -----------------------------------------------------------


def test_user_allergen_list_is_persisted(db_conn) -> None:
    allergen_id = inventory_service.add_user_allergen(db_conn, "花生")
    inventory_service.add_user_allergen(db_conn, "牛奶")

    entries = inventory_service.list_user_allergens(db_conn)
    assert [(entry.id, entry.display_name) for entry in entries] == [
        (allergen_id, "花生"),
        (entries[1].id, "牛奶"),
    ]
    assert entries[0].name_normalized == "花生"


def test_user_allergen_alias_and_duplicate_are_rejected(db_conn) -> None:
    inventory_service.add_user_allergen(db_conn, "花生")

    with pytest.raises(inventory_service.AllergenError):
        inventory_service.add_user_allergen(db_conn, "花生酱")

    assert len(inventory_service.list_user_allergens(db_conn)) == 1


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", 123])
def test_invalid_user_allergen_input_is_rejected(db_conn, bad) -> None:
    with pytest.raises(ValidationError):
        inventory_service.add_user_allergen(db_conn, bad)

    assert inventory_service.list_user_allergens(db_conn) == []


def test_removing_unknown_allergen_is_rejected(db_conn) -> None:
    with pytest.raises(inventory_service.AllergenError):
        inventory_service.remove_user_allergen(db_conn, 999)


def test_remove_allergen_deletes_the_row(db_conn) -> None:
    allergen_id = inventory_service.add_user_allergen(db_conn, "花生")

    inventory_service.remove_user_allergen(db_conn, allergen_id)

    assert inventory_service.list_user_allergens(db_conn) == []


def test_batch_stores_expiry_and_declaration(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="挂面",
        category="主食",
        quantity=2,
        unit="包",
        storage="常温",
        purchased_on="2026-10-01",
        expiry_date="2027-04-01",
        shelf_life_days=180,
        declared_allergens=["小麦"],
        allergen_declared=True,
        today=TODAY,
    )

    item = inventory_service.get_batch(db_conn, item_id)
    assert item.expiry_date == "2027-04-01"
    assert item.shelf_life_days == 180
    assert item.expiry_source == "user_date"
    assert item.allergen_status == "declared"
    assert allergen_rules.decode_declared_allergens(item.declared_allergens) == ["小麦"]


def test_batch_without_expiry_or_declaration_keeps_unknown_and_null(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="土豆",
        category="蔬菜",
        quantity=3,
        unit="个",
        storage="冷藏",
        purchased_on="2026-10-01",
        today=TODAY,
    )

    item = inventory_service.get_batch(db_conn, item_id)
    assert item.expiry_date is None
    assert item.shelf_life_days is None
    assert item.expiry_source == "unknown"
    assert item.allergen_status == "unknown"
    assert item.declared_allergens is None, "未声明必须是 NULL 而不是空数组"


def test_declared_allergens_accept_form_text_without_splitting_characters(db_conn) -> None:
    """页面传的是文本，必须按分隔符解析，不能把"小麦"拆成"小"和"麦"。"""
    item_id = inventory_service.add_batch(
        db_conn,
        name="挂面",
        category="主食",
        quantity=1,
        unit="包",
        storage="常温",
        purchased_on="2026-10-01",
        declared_allergens="小麦，牛奶",
        allergen_declared=True,
        today=TODAY,
    )

    item = inventory_service.get_batch(db_conn, item_id)
    assert allergen_rules.decode_declared_allergens(item.declared_allergens) == ["小麦", "牛奶"]


def test_declared_allergens_accept_a_list(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="饼干",
        category="主食",
        quantity=1,
        unit="包",
        storage="常温",
        purchased_on="2026-10-01",
        declared_allergens=["小麦", "牛奶"],
        allergen_declared=True,
        today=TODAY,
    )

    item = inventory_service.get_batch(db_conn, item_id)
    assert allergen_rules.decode_declared_allergens(item.declared_allergens) == ["小麦", "牛奶"]


def test_declared_but_empty_list_is_stored_as_declared(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="土豆",
        category="蔬菜",
        quantity=1,
        unit="个",
        storage="冷藏",
        purchased_on="2026-10-01",
        declared_allergens=[],
        allergen_declared=True,
        today=TODAY,
    )

    item = inventory_service.get_batch(db_conn, item_id)
    assert item.allergen_status == "declared"
    assert allergen_rules.decode_declared_allergens(item.declared_allergens) == []
    assert item.declared_allergens is not None, "已声明应保存为空数组而不是 NULL"


def test_batch_view_reports_unresolved_days_and_allergen_state(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="燕麦片",
        category="蛋奶",
        quantity=1,
        unit="盒",
        storage="冷藏",
        purchased_on=None,
        shelf_life_days=7,
        initial=True,
        today=TODAY,
    )
    inventory_service.add_user_allergen(db_conn, "牛奶")

    view = inventory_service.get_batch_view(db_conn, item_id, today=TODAY)

    assert view.item.expiry_source == "user_days_unresolved"
    assert view.expiry.status == "unknown"
    assert view.expiry.warning is not None
    assert view.allergen.status == allergen_rules.STATUS_UNVERIFIABLE
    assert view.allergen.declaration_state == "unknown"


def test_expiry_and_allergen_features_do_not_change_stock_semantics(db_conn) -> None:
    item_id = inventory_service.add_batch(
        db_conn,
        name="挂面",
        category="主食",
        quantity=5,
        unit="包",
        storage="常温",
        purchased_on="2026-10-01",
        expiry_date="2027-04-01",
        declared_allergens="小麦",
        allergen_declared=True,
        today=TODAY,
    )
    events = inventory_service.list_events(db_conn, item_id=item_id)
    assert len(events) == 1
    assert events[0].action == "purchase_add"
    assert events[0].qty_delta == pytest.approx(5.0)

    inventory_service.consume(db_conn, quantity=2, item_id=item_id)

    item = inventory_service.get_batch(db_conn, item_id)
    assert item.quantity == pytest.approx(3.0)
    summaries = inventory_service.list_summary(db_conn)
    assert summaries[0].total_quantity == pytest.approx(3.0)
    assert sum(event.qty_delta for event in inventory_service.list_events(db_conn, item_id=item_id)) == pytest.approx(3.0)


def test_data_survives_reconnect_with_expiry_and_allergens(tmp_db_path) -> None:
    first = inventory_service.open_connection(tmp_db_path)
    try:
        inventory_service.add_user_allergen(first, "花生")
        item_id = inventory_service.add_batch(
            first,
            name="花生酱",
            category="调味",
            quantity=1,
            unit="瓶",
            storage="常温",
            purchased_on="2026-10-01",
            shelf_life_days=90,
            declared_allergens=["花生"],
            allergen_declared=True,
            today=TODAY,
        )
    finally:
        first.close()

    second = inventory_service.open_connection(tmp_db_path)
    try:
        view = inventory_service.get_batch_view(second, item_id, today=TODAY)
        assert view.item.expiry_date is None, "只填天数不得生成正式有效期"
        assert view.item.shelf_life_days == 90
        assert view.item.expiry_source == "user_days_unresolved"
        assert view.expiry.ai_estimate_text is not None
        assert view.allergen.status == allergen_rules.STATUS_CONFLICT
        assert [entry.display_name for entry in inventory_service.list_user_allergens(second)] == ["花生"]
    finally:
        second.close()


# --- 迁移 -------------------------------------------------------------------


def patch_migrations(monkeypatch, migrations, version: int) -> None:
    monkeypatch.setattr(schema_module, "MIGRATIONS", migrations)
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", version)


def test_real_schema_upgrade_from_v2_keeps_inventory_and_events(
    tmp_db_path, monkeypatch
) -> None:
    assert REAL_SCHEMA_VERSION >= 3, "本测试要求存在 v3 迁移"
    legacy = REAL_MIGRATIONS[:2]
    patch_migrations(monkeypatch, legacy, 2)
    assert db_module.initialize_database(tmp_db_path) == [1, 2]

    conn = db_module.connect(tmp_db_path)
    try:
        conn.execute(
            """
            INSERT INTO inventory_item (
                name, name_normalized, category, quantity, unit, purchased_on,
                purchase_date_defaulted, storage, expiry_source, allergen_status,
                status, created_at, updated_at
            ) VALUES ('番茄', '番茄', '蔬菜', 3, '个', '2026-10-01', 0, '冷藏',
                      'unknown', 'unknown', 'active', 't', 't')
            """
        )
        conn.execute(
            """
            INSERT INTO inventory_event (item_id, action, qty_delta, qty_after, source, at)
            VALUES (1, 'purchase_add', 3, 3, 'manual', 't')
            """
        )
    finally:
        conn.close()

    patch_migrations(monkeypatch, REAL_MIGRATIONS, REAL_SCHEMA_VERSION)
    upgraded = db_module.initialize_database(tmp_db_path)

    assert upgraded == [3], "只应执行尚未应用的版本"
    check = db_module.connect(tmp_db_path)
    try:
        assert db_module.get_user_version(check) == REAL_SCHEMA_VERSION
        rows = list(check.execute("SELECT name, quantity FROM inventory_item"))
        assert [(row["name"], row["quantity"]) for row in rows] == [("番茄", 3.0)]
        assert len(list(check.execute("SELECT id FROM inventory_event"))) == 1
        tables = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "user_allergen" in tables
    finally:
        check.close()


def test_failed_v3_migration_rolls_back_and_keeps_data(tmp_db_path, monkeypatch) -> None:
    legacy = REAL_MIGRATIONS[:2]
    patch_migrations(monkeypatch, legacy, 2)
    assert db_module.initialize_database(tmp_db_path) == [1, 2]

    conn = db_module.connect(tmp_db_path)
    try:
        conn.execute(
            """
            INSERT INTO inventory_item (
                name, name_normalized, category, quantity, unit, storage,
                expiry_source, allergen_status, status, created_at, updated_at
            ) VALUES ('牛奶', '牛奶', '蛋奶', 1, '盒', '冷藏', 'unknown', 'unknown',
                      'active', 't', 't')
            """
        )
    finally:
        conn.close()

    def failing_step(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟 v3 迁移失败")

    patch_migrations(
        monkeypatch,
        legacy + [(REAL_SCHEMA_VERSION, [failing_step])],
        REAL_SCHEMA_VERSION,
    )

    with pytest.raises(db_module.MigrationError):
        db_module.initialize_database(tmp_db_path)

    assert tmp_db_path.exists(), "迁移失败不得删除数据库"
    check = db_module.connect(tmp_db_path)
    try:
        assert db_module.get_user_version(check) == 2, "版本号不得推进"
        names = [row["name"] for row in check.execute("SELECT name FROM inventory_item")]
        assert names == ["牛奶"], "原有数据必须保留"
        tables = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "user_allergen" not in tables
    finally:
        check.close()
