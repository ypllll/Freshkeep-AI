"""菜谱规划服务测试（S5）：模型调用、输出校验、过敏原分流、缺口与"不改库存"。

全部使用假的模型客户端与一次性数据库：不发真实网络请求，也不需要真实 API Key。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ai import prompts as recipe_prompts
from ai.client import AIAPIError
from core import recipe_math
from services import ai_service, inventory_service, recipe_service

TODAY = date(2026, 10, 11)
PURCHASED_ON = "2026-10-01"


@pytest.fixture
def db_conn(tmp_db_path):
    connection = inventory_service.open_connection(tmp_db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def fake_ai_config(monkeypatch):
    """给模型调用提供"已配置"的环境，并阻止读取磁盘上真实的 .env。"""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setattr(ai_service, "_env_loaded", True)


class FakeModelClient:
    """假的模型客户端：记录收到的 messages，返回预设文本或抛预设异常。"""

    def __init__(self, *, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list = []

    def generate(self, messages):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.text


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


def recipe_json(*, title="番茄炒蛋", servings=2, ingredients=None, rationale="简单家常") -> str:
    payload = {
        "recipes": [
            {
                "title": title,
                "servings": servings,
                "rationale": rationale,
                "ingredients": ingredients
                or [{"name": "番茄", "quantity": 2, "unit": "个"}],
                "steps": ["切块", "下锅炒"],
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def plan(conn, client, **overrides):
    params = {"user_request": "想吃清淡的家常菜", "servings": 2, "client": client}
    params.update(overrides)
    return recipe_service.plan_recipes(conn, **params)


# --- 成功路径 ---------------------------------------------------------------


def test_plan_returns_recommended_recipe_with_program_calculated_gaps(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(text=recipe_json())

    result = plan(db_conn, client)

    assert len(result.recommended) == 1
    planned = result.recommended[0]
    assert planned.title == "番茄炒蛋"
    assert planned.servings_match is True
    assert planned.gap_summary.matched_sufficient == 1
    assert planned.ingredients[0].gap.available_quantity == pytest.approx(5.0)
    assert planned.ingredients[0].gap.missing_quantity == pytest.approx(0.0)
    assert result.servings == 2
    assert result.prompt_version == recipe_prompts.PROMPT_VERSION
    assert result.inventory_batch_count == 1
    assert len(client.calls) == 1


def test_prompt_carries_request_inventory_allergens_and_rules(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    inventory_service.add_user_allergen(db_conn, "花生")
    client = FakeModelClient(text=recipe_json())

    plan(db_conn, client)

    system_message, user_message = client.calls[0]
    assert system_message["role"] == "system"
    assert "不要输出解释文字" in system_message["content"]
    assert "库存充足" in system_message["content"]  # 明确禁止模型对库存下结论
    assert "不要判断菜谱是否" in system_message["content"]

    assert user_message["role"] == "user"
    assert "想吃清淡的家常菜" in user_message["content"]
    assert "番茄：5 个" in user_message["content"]
    assert "花生" in user_message["content"]


def test_model_claims_do_not_override_program_gaps(db_conn) -> None:
    add_item(db_conn, "番茄", 2)
    client = FakeModelClient(
        text=recipe_json(
            rationale="库存充足，随便做",
            ingredients=[{"name": "番茄", "quantity": 100, "unit": "个"}],
        )
    )

    result = plan(db_conn, client, servings=2)

    gap = result.recommended[0].ingredients[0].gap
    assert gap.available_quantity == pytest.approx(2.0)
    assert gap.missing_quantity == pytest.approx(98.0)


def test_unit_mismatch_is_surfaced_on_the_planned_recipe(db_conn) -> None:
    add_item(db_conn, "牛奶", 1, unit="L")
    client = FakeModelClient(
        text=recipe_json(ingredients=[{"name": "牛奶", "quantity": 200, "unit": "ml"}])
    )

    result = plan(db_conn, client)

    gap = result.recommended[0].ingredients[0].gap
    assert gap.match_state == recipe_math.MATCH_UNIT_MISMATCH
    assert gap.available_quantity is None
    assert gap.missing_quantity is None


def test_unmatched_ingredient_is_unknown_not_zero(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(
        text=recipe_json(ingredients=[{"name": "松露", "quantity": 1, "unit": "块"}])
    )

    result = plan(db_conn, client)

    gap = result.recommended[0].ingredients[0].gap
    assert gap.match_state == recipe_math.MATCH_UNMATCHED
    assert gap.available_quantity is None


def test_servings_mismatch_is_flagged(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(text=recipe_json(servings=4))

    result = plan(db_conn, client, servings=2)

    assert result.recommended[0].servings == 4
    assert result.recommended[0].servings_match is False
    assert result.recommended[0].requested_servings == 2


def test_empty_recipe_list_is_reported_as_a_note(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(text=json.dumps({"recipes": []}))

    result = plan(db_conn, client)

    assert result.candidate_count == 0
    assert any("没有返回任何菜谱候选" in note for note in result.notes)


# --- 错误路径 ---------------------------------------------------------------


def test_invalid_json_raises_output_error(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(text="这不是 JSON")

    with pytest.raises(recipe_service.RecipeOutputError) as excinfo:
        plan(db_conn, client)

    assert excinfo.value.kind == "invalid_output"
    assert "JSON" in str(excinfo.value)
    assert isinstance(excinfo.value, recipe_service.RecipePlanningError)


def test_schema_violation_raises_output_error(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(text=recipe_json(servings="2"))  # 字符串份数不符合契约

    with pytest.raises(recipe_service.RecipeOutputError):
        plan(db_conn, client)


def test_model_failure_is_wrapped_as_ai_error(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    client = FakeModelClient(error=AIAPIError("模型服务返回错误（HTTP 500）", status_code=500))

    with pytest.raises(recipe_service.RecipeAIError) as excinfo:
        plan(db_conn, client)

    assert excinfo.value.kind == "ai_error"
    assert "HTTP 500" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, AIAPIError)


def test_unconfigured_model_does_not_call_the_client(db_conn, monkeypatch) -> None:
    add_item(db_conn, "番茄", 5)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    client = FakeModelClient(text=recipe_json())

    with pytest.raises(recipe_service.RecipeAIError) as excinfo:
        plan(db_conn, client)

    assert "OPENAI_API_KEY" in str(excinfo.value)
    assert client.calls == [], "缺配置时不得调用模型"


def test_inventory_read_failure_is_wrapped(db_conn, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise inventory_service.InventoryError("数据库读失败")

    monkeypatch.setattr(recipe_service.inventory_service, "list_batches", boom)

    with pytest.raises(recipe_service.RecipeInventoryError) as excinfo:
        plan(db_conn, FakeModelClient(text=recipe_json()))

    assert excinfo.value.kind == "inventory_error"


def test_empty_inventory_does_not_call_the_model(db_conn) -> None:
    client = FakeModelClient(text=recipe_json())

    with pytest.raises(recipe_service.RecipeInventoryEmptyError) as excinfo:
        plan(db_conn, client)

    assert excinfo.value.kind == "empty_inventory"
    assert client.calls == []


@pytest.mark.parametrize("bad_request", ["", "   "])
def test_blank_request_is_rejected(db_conn, bad_request: str) -> None:
    add_item(db_conn, "番茄", 5)

    with pytest.raises(recipe_service.RecipeRequestError):
        plan(db_conn, FakeModelClient(text=recipe_json()), user_request=bad_request)


@pytest.mark.parametrize("bad_servings", [0, -1, True, "2"])
def test_invalid_servings_is_rejected(db_conn, bad_servings) -> None:
    add_item(db_conn, "番茄", 5)

    with pytest.raises(recipe_service.RecipeRequestError):
        plan(db_conn, FakeModelClient(text=recipe_json()), servings=bad_servings)


def test_too_long_request_is_rejected(db_conn) -> None:
    add_item(db_conn, "番茄", 5)

    with pytest.raises(recipe_service.RecipeRequestError):
        plan(db_conn, FakeModelClient(text=recipe_json()), user_request="需" * 501)


# --- 过敏原分流 -------------------------------------------------------------


def test_known_allergen_conflict_is_rejected_not_recommended(db_conn) -> None:
    add_item(db_conn, "花生酱", 1, unit="瓶", category="调味", storage="常温")
    inventory_service.add_user_allergen(db_conn, "花生")
    client = FakeModelClient(
        text=recipe_json(
            title="花生酱拌面",
            ingredients=[{"name": "花生酱", "quantity": 2, "unit": "瓶"}],
        )
    )

    result = plan(db_conn, client)

    assert result.recommended == ()
    assert result.needs_review == ()
    assert len(result.rejected) == 1
    assert "花生酱" in result.rejected[0].reason
    assert result.rejected[0].recipe.allergen_status == "conflict"


def test_unverifiable_ingredient_goes_to_needs_review(db_conn) -> None:
    add_item(db_conn, "土豆", 3)  # 未声明成分的批次
    inventory_service.add_user_allergen(db_conn, "花生")
    client = FakeModelClient(
        text=recipe_json(ingredients=[{"name": "土豆", "quantity": 2, "unit": "个"}])
    )

    result = plan(db_conn, client)

    assert result.recommended == ()
    assert len(result.needs_review) == 1
    assert result.needs_review[0].allergen_status == "unverifiable"
    assert "土豆" in result.needs_review[0].allergen_message
    assert result.needs_review[0].allergen_reasons


def test_ingredient_missing_from_inventory_is_unverifiable(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    inventory_service.add_user_allergen(db_conn, "花生")
    client = FakeModelClient(
        text=recipe_json(ingredients=[{"name": "松露", "quantity": 1, "unit": "块"}])
    )

    result = plan(db_conn, client)

    assert len(result.needs_review) == 1
    assert "松露" in result.needs_review[0].allergen_message
    assert "无法核实" in result.needs_review[0].allergen_message


def test_declared_ingredient_without_conflict_is_recommended(db_conn) -> None:
    inventory_service.add_batch(
        db_conn,
        name="番茄",
        category="蔬菜",
        quantity=5,
        unit="个",
        storage="冷藏",
        purchased_on=PURCHASED_ON,
        declared_allergens=["大豆"],  # 已声明成分、但与用户的过敏原（花生）无关
        allergen_declared=True,
        today=TODAY,
    )
    inventory_service.add_user_allergen(db_conn, "花生")

    result = plan(db_conn, FakeModelClient(text=recipe_json()))

    assert len(result.recommended) == 1
    assert result.recommended[0].allergen_status == "none_known"
    assert "不代表安全" in result.recommended[0].allergen_message


def test_no_declared_allergens_means_no_personalised_check(db_conn) -> None:
    add_item(db_conn, "土豆", 3)
    client = FakeModelClient(
        text=recipe_json(ingredients=[{"name": "土豆", "quantity": 2, "unit": "个"}])
    )

    result = plan(db_conn, client)

    assert len(result.recommended) == 1
    assert result.needs_review == ()
    assert result.allergen_names == ()
    assert any("未做个性化过敏原检查" in note for note in result.notes)


# --- 不修改库存 -------------------------------------------------------------


def test_planning_does_not_modify_inventory_or_write_events(db_conn, monkeypatch) -> None:
    first = add_item(db_conn, "番茄", 5)
    add_item(db_conn, "鸡蛋", 6)
    before = {
        item.id: (item.quantity, item.status) for item in inventory_service.list_batches(db_conn)
    }
    events_before = len(inventory_service.list_events(db_conn, limit=1000))

    def forbidden(*args, **kwargs):
        raise AssertionError("菜谱规划不得调用任何写库存的操作")

    for name in ("consume", "discard", "update_quantity", "add_batch"):
        monkeypatch.setattr(recipe_service.inventory_service, name, forbidden)

    plan(db_conn, FakeModelClient(text=recipe_json()))

    after = {
        item.id: (item.quantity, item.status) for item in inventory_service.list_batches(db_conn)
    }
    assert after == before
    assert inventory_service.list_events(db_conn, limit=1000).__len__() == events_before
    assert inventory_service.get_batch(db_conn, first).quantity == pytest.approx(5.0)
