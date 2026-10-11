"""菜谱确认与库存消费（S6）服务层测试。

覆盖：分离的计划/实际数量、最新库存重算、部分确认、数量校验、单位与名称匹配、
三类过敏原闸门、会话级防重复、逐批次事件与整体回滚。

全部使用一次性数据库与假模型（本文件不调用模型），不发真实网络请求。
"""

from __future__ import annotations

from datetime import date

import pytest

from core import recipe_math
from core.recipe_math import IngredientGap
from services import inventory_service, recipe_service
from storage import inventory_repo

TODAY = date(2026, 10, 11)
PURCHASED_ON = "2026-10-01"


@pytest.fixture
def db_conn(tmp_db_path):
    connection = inventory_service.open_connection(tmp_db_path)
    try:
        yield connection
    finally:
        connection.close()


def add_item(conn, name: str, quantity: float, unit: str = "个", **kwargs) -> int:
    return inventory_service.add_batch(
        conn,
        name=name,
        category=kwargs.pop("category", "蔬菜"),
        quantity=quantity,
        unit=unit,
        storage=kwargs.pop("storage", "冷藏"),
        purchased_on=kwargs.pop("purchased_on", PURCHASED_ON),
        today=kwargs.pop("today", TODAY),
        **kwargs,
    )


def make_recipe(title: str = "番茄炒蛋", ingredients=(("番茄", 3.0, "个"),), **overrides):
    planned = []
    for name, quantity, unit in ingredients:
        planned.append(
            recipe_service.PlannedIngredient(
                name=name,
                quantity=quantity,
                unit=unit,
                gap=IngredientGap(
                    name=name,
                    unit=unit,
                    required_quantity=quantity,
                    match_state=recipe_math.MATCH_MATCHED,
                    available_quantity=0.0,
                    missing_quantity=0.0,
                ),
                allergen_status=overrides.get("allergen_status", "none_known"),
                allergen_message="未发现已知冲突（不代表安全）",
            )
        )
    return recipe_service.PlannedRecipe(
        title=title,
        servings=overrides.get("servings", 2),
        requested_servings=overrides.get("servings", 2),
        servings_match=True,
        rationale="测试用菜谱",
        steps=("切块", "下锅"),
        ingredients=tuple(planned),
        allergen_status=overrides.get("allergen_status", "none_known"),
        allergen_message="未发现已知冲突（不代表安全）",
        allergen_reasons=(),
    )


def make_plan(recipe, *, plan_id: int = 4242, bucket: str = "recommended") -> recipe_service.RecipePlan:
    return recipe_service.RecipePlan(
        plan_id=plan_id,
        user_request="测试需求",
        servings=recipe.servings,
        prompt_version="test",
        inventory_batch_count=1,
        allergen_names=(),
        recommended=(recipe,) if bucket == "recommended" else (),
        needs_review=(recipe,) if bucket == "needs_review" else (),
        rejected=(recipe_service.RejectedRecipe(recipe=recipe, reason="与过敏原冲突"),)
        if bucket == "rejected"
        else (),
        notes=(),
    )


def events_of(conn, item_id: int):
    return inventory_service.list_events(conn, item_id=item_id, limit=100)


def consume_events(conn, item_id: int | None = None):
    """只统计消费事件（入库会写 purchase_add，不能拿整张事件表当"没有扣减"的证据）。"""
    events = inventory_service.list_events(conn, item_id=item_id, limit=1000)
    return [event for event in events if event.action == "consume"]


def quantity_of(conn, item_id: int) -> float:
    return inventory_service.get_batch(conn, item_id).quantity


# --- 基本确认 ---------------------------------------------------------------


def test_confirm_single_ingredient_deducts_and_records_event(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"),))
    plan = make_plan(recipe, plan_id=111)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 3.0}
    )

    assert quantity_of(db_conn, item_id) == pytest.approx(2.0)
    assert result.plan_id == 111
    assert result.total_deducted == pytest.approx(3.0)
    assert result.batch_count == 1
    assert result.event_count == 1

    applied = result.ingredients[0]
    assert applied.planned_quantity == pytest.approx(3.0)
    assert applied.confirmed_quantity == pytest.approx(3.0)
    assert applied.remaining_quantity == pytest.approx(2.0)
    assert applied.deductions[0].batch_id == item_id
    assert applied.deductions[0].quantity == pytest.approx(3.0)
    assert applied.deductions[0].quantity_after == pytest.approx(2.0)


def test_confirm_multiple_ingredients(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"), ("鸡蛋", 2.0, "个")))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 3.0, 1: 2.0}
    )

    assert quantity_of(db_conn, tomato) == pytest.approx(2.0)
    assert quantity_of(db_conn, egg) == pytest.approx(4.0)
    assert result.total_deducted == pytest.approx(5.0)
    assert result.event_count == 2


def test_partial_confirmation_leaves_other_ingredients_untouched(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"), ("鸡蛋", 2.0, "个")))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0}
    )

    assert quantity_of(db_conn, tomato) == pytest.approx(3.0)
    assert quantity_of(db_conn, egg) == pytest.approx(6.0)
    assert consume_events(db_conn, egg) == []
    assert result.skipped_ingredients == ("鸡蛋",)


def test_zero_quantity_is_skipped_without_events(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"), ("鸡蛋", 2.0, "个")))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 0.0, 1: 2.0}
    )

    assert quantity_of(db_conn, tomato) == pytest.approx(5.0)
    assert consume_events(db_conn, tomato) == []
    assert quantity_of(db_conn, egg) == pytest.approx(4.0)
    assert "番茄" in result.skipped_ingredients


def test_empty_confirmation_is_rejected(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    recipe = make_recipe()
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeConfirmationError) as excinfo:
        recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={})

    assert excinfo.value.kind == "confirmation_error"
    assert quantity_of(db_conn, 1) == pytest.approx(5.0)


# --- 数量校验 ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, -0.5, float("nan"), float("inf"), float("-inf"), None, "2", True])
def test_invalid_confirmed_quantity_is_rejected(db_conn, bad) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe()
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeConfirmationError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: bad}
        )

    assert quantity_of(db_conn, item_id) == pytest.approx(5.0)
    assert consume_events(db_conn, item_id) == []


@pytest.mark.parametrize("bad_index", [1, 5, -1, "0", True])
def test_invalid_ingredient_index_is_rejected(db_conn, bad_index) -> None:
    add_item(db_conn, "番茄", 5)
    recipe = make_recipe()
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeConfirmationError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={bad_index: 1.0}
        )


def test_quantity_over_latest_stock_rejects_the_whole_confirmation(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 2)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"), ("鸡蛋", 2.0, "个")))
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeStockShortageError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 3.0, 1: 2.0}
        )

    assert excinfo.value.kind == "insufficient_stock"
    assert "番茄" in str(excinfo.value)
    assert "2" in str(excinfo.value)
    assert quantity_of(db_conn, tomato) == pytest.approx(2.0)
    assert quantity_of(db_conn, egg) == pytest.approx(6.0), "整次确认必须一起失败"
    assert consume_events(db_conn) == []


# --- 最新库存重算 -----------------------------------------------------------


def test_stock_reduced_after_planning_is_rechecked(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 4.0, "个"),))
    plan = make_plan(recipe)

    inventory_service.consume(db_conn, quantity=3, item_id=item_id)  # 规划之后库存变了

    with pytest.raises(recipe_service.RecipeStockShortageError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 4.0}
        )

    assert "2" in str(excinfo.value), "报错应基于最新可用量（2）"
    assert quantity_of(db_conn, item_id) == pytest.approx(2.0)


def test_batches_exhausted_after_planning_count_as_known_zero(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 1.0, "个"),))
    plan = make_plan(recipe)

    inventory_service.consume(db_conn, quantity=5, item_id=item_id)  # 全部用完

    check = recipe_service.inspect_recipe_for_confirmation(db_conn, recipe=recipe)
    assert check.ingredients[0].available_quantity == pytest.approx(0.0)

    with pytest.raises(recipe_service.RecipeStockShortageError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0}
        )


def test_inspect_recipe_is_read_only(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"),))

    check = recipe_service.inspect_recipe_for_confirmation(db_conn, recipe=recipe)

    assert check.ingredients[0].available_quantity == pytest.approx(5.0)
    assert check.ingredients[0].is_deductible is True
    assert check.ingredients[0].max_confirmable == pytest.approx(5.0)
    assert check.blocked is False
    assert quantity_of(db_conn, item_id) == pytest.approx(5.0)
    assert consume_events(db_conn) == []


# --- 匹配与单位 -------------------------------------------------------------


def test_unit_mismatch_is_blocked(db_conn) -> None:
    milk = add_item(db_conn, "牛奶", 1, unit="L", category="蛋奶")
    recipe = make_recipe(ingredients=(("牛奶", 200.0, "ml"),))
    plan = make_plan(recipe)

    check = recipe_service.inspect_recipe_for_confirmation(db_conn, recipe=recipe)
    assert check.ingredients[0].is_deductible is False
    assert check.ingredients[0].max_confirmable is None

    with pytest.raises(recipe_service.RecipeNotDeductibleError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 200.0}
        )

    assert "单位" in str(excinfo.value)
    assert quantity_of(db_conn, milk) == pytest.approx(1.0)


def test_unmatched_ingredient_is_blocked(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("松露", 1.0, "块"),))
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeNotDeductibleError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0}
        )

    assert "松露" in str(excinfo.value)


def test_unmatched_ingredient_can_be_left_unconfirmed(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 2.0, "个"), ("松露", 1.0, "块")))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0, 1: 0.0}
    )

    assert quantity_of(db_conn, tomato) == pytest.approx(3.0)
    assert "松露" in result.skipped_ingredients


# --- 过敏原闸门 -------------------------------------------------------------


def test_conflict_recipe_cannot_be_applied(db_conn) -> None:
    add_item(db_conn, "花生酱", 2, unit="瓶", category="调味", storage="常温")
    recipe = make_recipe(title="花生酱拌面", ingredients=(("花生酱", 1.0, "瓶"),))
    plan = make_plan(recipe, bucket="rejected")

    with pytest.raises(recipe_service.RecipeAllergenConflictError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0}
        )


def test_allergen_changed_after_planning_is_reassessed(db_conn) -> None:
    """规划时还没声明过敏原；确认前声明了花生 → 必须按最新声明拒绝。"""
    item_id = add_item(db_conn, "花生酱", 2, unit="瓶", category="调味", storage="常温")
    recipe = make_recipe(ingredients=(("花生酱", 1.0, "瓶"),))
    plan = make_plan(recipe, bucket="recommended")

    inventory_service.add_user_allergen(db_conn, "花生")

    with pytest.raises(recipe_service.RecipeAllergenConflictError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0}
        )

    assert "花生" in str(excinfo.value)
    assert quantity_of(db_conn, item_id) == pytest.approx(2.0)


def test_unverifiable_requires_explicit_acknowledgement(db_conn) -> None:
    item_id = add_item(db_conn, "土豆", 3)  # 未声明成分的批次
    inventory_service.add_user_allergen(db_conn, "花生")
    recipe = make_recipe(ingredients=(("土豆", 2.0, "个"),))
    plan = make_plan(recipe, bucket="needs_review")

    with pytest.raises(recipe_service.RecipeAllergenAcknowledgementError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0}
        )

    assert excinfo.value.kind == "allergen_unverified"
    assert quantity_of(db_conn, item_id) == pytest.approx(3.0)
    assert consume_events(db_conn) == []


def test_unverifiable_can_proceed_after_acknowledgement(db_conn) -> None:
    item_id = add_item(db_conn, "土豆", 3)
    inventory_service.add_user_allergen(db_conn, "花生")
    recipe = make_recipe(ingredients=(("土豆", 2.0, "个"),))
    plan = make_plan(recipe, bucket="needs_review")

    result = recipe_service.apply_plan(
        db_conn,
        plan=plan,
        recipe=recipe,
        confirmed_quantities={0: 2.0},
        allergen_acknowledged=True,
    )

    assert quantity_of(db_conn, item_id) == pytest.approx(1.0)
    assert result.allergen_status == "unverifiable"
    assert "无法核实" in result.allergen_message
    assert "安全" not in result.allergen_message.replace("不代表安全", "")


def test_none_known_is_not_upgraded_to_safe(db_conn) -> None:
    add_item(
        db_conn,
        "番茄",
        5,
        declared_allergens=["大豆"],
        allergen_declared=True,
    )
    inventory_service.add_user_allergen(db_conn, "花生")
    recipe = make_recipe(ingredients=(("番茄", 2.0, "个"),))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0}
    )

    assert result.allergen_status == "none_known"
    assert "不代表安全" in result.allergen_message
    assert result.allergen_message != "安全"


# --- 规划身份与防重复 -------------------------------------------------------


def test_recipe_not_in_the_plan_is_rejected(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    recipe = make_recipe()
    other_recipe = make_recipe(title="别的菜谱", ingredients=(("番茄", 1.0, "个"),))
    plan = make_plan(recipe)

    with pytest.raises(recipe_service.RecipeConfirmationError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=other_recipe, confirmed_quantities={0: 1.0}
        )


def test_already_applied_plan_is_rejected(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 2.0, "个"),))
    plan = make_plan(recipe, plan_id=777)

    recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0})

    with pytest.raises(recipe_service.RecipePlanAlreadyAppliedError) as excinfo:
        recipe_service.apply_plan(
            db_conn,
            plan=plan,
            recipe=recipe,
            confirmed_quantities={0: 1.0},
            applied_plan_ids=[777],
        )

    assert excinfo.value.kind == "already_applied"
    assert quantity_of(db_conn, item_id) == pytest.approx(3.0), "第二次不得再扣"


def test_duplicate_protection_is_only_as_strong_as_the_session_state(db_conn) -> None:
    """诚实记录限制：不传 applied_plan_ids 时，同一规划可以被再次应用。

    会话状态是唯一防线；没有持久化规划表，因此应用重启/跨会话不具备全局幂等。
    """
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 2.0, "个"),))
    plan = make_plan(recipe, plan_id=888)

    recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0})
    recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0})

    assert quantity_of(db_conn, item_id) == pytest.approx(1.0)


# --- 批次分配与事件 ---------------------------------------------------------


def test_multi_batch_deduction_follows_existing_order(db_conn) -> None:
    newer = add_item(db_conn, "番茄", 3, purchased_on="2026-10-08")
    older = add_item(db_conn, "番茄", 2, purchased_on="2026-10-01")
    recipe = make_recipe(ingredients=(("番茄", 4.0, "个"),))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 4.0}
    )

    applied = result.ingredients[0]
    assert [(item.batch_id, item.quantity) for item in applied.deductions] == [
        (older, 2.0),
        (newer, 2.0),
    ]
    assert quantity_of(db_conn, older) == pytest.approx(0.0)
    assert quantity_of(db_conn, newer) == pytest.approx(1.0)

    older_events = [event for event in events_of(db_conn, older) if event.action == "consume"]
    newer_events = [event for event in events_of(db_conn, newer) if event.action == "consume"]
    assert [(event.qty_delta, event.qty_after) for event in older_events] == [(-2.0, 0.0)]
    assert [(event.qty_delta, event.qty_after) for event in newer_events] == [(-2.0, 1.0)]


def test_unaffected_batches_get_no_deduction_events(db_conn) -> None:
    first = add_item(db_conn, "番茄", 2, purchased_on="2026-10-01")
    second = add_item(db_conn, "番茄", 5, purchased_on="2026-10-05")
    third = add_item(db_conn, "番茄", 5, purchased_on="2026-10-09")
    recipe = make_recipe(ingredients=(("番茄", 1.0, "个"),))
    plan = make_plan(recipe)

    recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0})

    assert [event.action for event in events_of(db_conn, first)] == ["consume", "purchase_add"]
    assert [event.action for event in events_of(db_conn, second)] == ["purchase_add"]
    assert [event.action for event in events_of(db_conn, third)] == ["purchase_add"]


def test_event_records_source_plan_id_and_note(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(title="番茄炒蛋", ingredients=(("番茄", 2.0, "个"),))
    plan = make_plan(recipe, plan_id=555)

    recipe_service.apply_plan(db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0})

    event = events_of(db_conn, item_id)[0]
    assert event.action == "consume"
    assert event.source == "recipe_plan"
    assert event.plan_id == 555
    assert event.qty_delta == pytest.approx(-2.0)
    assert "番茄炒蛋" in (event.note or "")


def test_deducted_total_matches_event_deltas(db_conn) -> None:
    add_item(db_conn, "番茄", 3, purchased_on="2026-10-01")
    add_item(db_conn, "番茄", 3, purchased_on="2026-10-05")
    recipe = make_recipe(ingredients=(("番茄", 4.0, "个"),))
    plan = make_plan(recipe)

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 4.0}
    )

    deducted = consume_events(db_conn)
    assert len(deducted) == result.event_count == result.batch_count == 2
    assert sum(event.qty_delta for event in deducted) == pytest.approx(
        -result.total_deducted
    )


# --- 事务与回滚 -------------------------------------------------------------


def test_rollback_when_a_later_ingredient_fails(db_conn, monkeypatch) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    recipe = make_recipe(ingredients=(("番茄", 2.0, "个"), ("鸡蛋", 2.0, "个")))
    plan = make_plan(recipe)

    original = inventory_repo.decrease_quantity
    calls = {"count": 0}

    def flaky(conn, item_id, amount, updated_at, *, new_status=None):
        calls["count"] += 1
        if calls["count"] == 2:
            return 0  # 模拟第二个食材的批次已被并发修改
        return original(conn, item_id, amount, updated_at, new_status=new_status)

    monkeypatch.setattr(inventory_repo, "decrease_quantity", flaky)

    with pytest.raises(recipe_service.RecipeConfirmationError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0, 1: 2.0}
        )

    assert quantity_of(db_conn, tomato) == pytest.approx(5.0), "第一项扣减必须一起回滚"
    assert quantity_of(db_conn, egg) == pytest.approx(6.0)
    assert consume_events(db_conn) == [], "消费事件也必须回滚"


def test_rollback_when_event_write_fails(db_conn, monkeypatch) -> None:
    tomato = add_item(db_conn, "番茄", 3, purchased_on="2026-10-01")
    second_batch = add_item(db_conn, "番茄", 3, purchased_on="2026-10-05")
    recipe = make_recipe(ingredients=(("番茄", 4.0, "个"),))
    plan = make_plan(recipe)

    original = inventory_repo.insert_event
    calls = {"count": 0}

    def flaky(conn, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("模拟事件写入失败")
        return original(conn, **kwargs)

    monkeypatch.setattr(inventory_repo, "insert_event", flaky)

    with pytest.raises(RuntimeError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 4.0}
        )

    assert quantity_of(db_conn, tomato) == pytest.approx(3.0), "两个批次的扣减都要回滚"
    assert quantity_of(db_conn, second_batch) == pytest.approx(3.0), "第二个批次也必须回滚"
    assert consume_events(db_conn) == []


def test_no_negative_stock_and_no_partial_commit_on_conditional_failure(
    db_conn, monkeypatch
) -> None:
    item_id = add_item(db_conn, "番茄", 5)
    recipe = make_recipe(ingredients=(("番茄", 3.0, "个"),))
    plan = make_plan(recipe)

    monkeypatch.setattr(
        inventory_repo, "decrease_quantity", lambda *args, **kwargs: 0
    )

    with pytest.raises(recipe_service.RecipeConfirmationError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 3.0}
        )

    assert quantity_of(db_conn, item_id) == pytest.approx(5.0)
    assert consume_events(db_conn) == []


def test_locked_database_is_mapped_to_a_business_error(db_conn, monkeypatch) -> None:
    add_item(db_conn, "番茄", 5)
    recipe = make_recipe()
    plan = make_plan(recipe)

    def locked(*args, **kwargs):
        raise __import__("sqlite3").OperationalError("database is locked")

    monkeypatch.setattr(inventory_repo, "decrease_quantity", locked)

    with pytest.raises(recipe_service.RecipeInventoryBusyError) as excinfo:
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0}
        )

    assert "占用" in str(excinfo.value)


# --- 手动消费的向后兼容 -----------------------------------------------------


def test_manual_consume_keeps_manual_source(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)

    inventory_service.consume(db_conn, quantity=2, item_id=item_id)

    event = events_of(db_conn, item_id)[0]
    assert event.source == "manual"
    assert event.plan_id is None


def test_invalid_source_or_plan_id_is_rejected(db_conn) -> None:
    item_id = add_item(db_conn, "番茄", 5)

    with pytest.raises(inventory_service.InventoryError):
        inventory_service.consume(db_conn, quantity=1, item_id=item_id, source="hack")
    with pytest.raises(inventory_service.InventoryError):
        inventory_service.consume(db_conn, quantity=1, item_id=item_id, plan_id="abc")

    assert quantity_of(db_conn, item_id) == pytest.approx(5.0)


def test_error_hierarchy_kinds_are_stable() -> None:
    assert issubclass(recipe_service.RecipePlanningError, recipe_service.RecipeError)
    assert issubclass(recipe_service.RecipeConfirmationError, recipe_service.RecipeError)
    kinds = {
        recipe_service.RecipeNotDeductibleError.kind: "not_deductible",
        recipe_service.RecipeStockShortageError.kind: "insufficient_stock",
        recipe_service.RecipeAllergenConflictError.kind: "allergen_conflict",
        recipe_service.RecipeAllergenAcknowledgementError.kind: "allergen_unverified",
        recipe_service.RecipePlanAlreadyAppliedError.kind: "already_applied",
        recipe_service.RecipeInventoryBusyError.kind: "inventory_busy",
    }
    assert all(kind == expected for kind, expected in kinds.items())
    assert isinstance(recipe_service.new_plan_id(), int)
