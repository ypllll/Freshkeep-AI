"""菜谱输出契约测试（S5）：合法解析、结构拒绝与可读错误。"""

from __future__ import annotations

import copy
import json

import pytest

from ai import prompts as recipe_prompts
from ai.schemas import RecipeSchemaError, parse_recipe_response, strip_code_fence

VALID_RESPONSE = {
    "recipes": [
        {
            "title": "番茄炒蛋",
            "servings": 2,
            "rationale": "用掉冰箱里的番茄。",
            "ingredients": [
                {"name": "番茄", "quantity": 2, "unit": "个"},
                {"name": "鸡蛋", "quantity": 3, "unit": "个"},
            ],
            "steps": ["番茄切块", "炒蛋", "同炒调味"],
        }
    ]
}


def payload(**overrides) -> str:
    data = copy.deepcopy(VALID_RESPONSE)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def recipe_with(**overrides) -> str:
    data = copy.deepcopy(VALID_RESPONSE)
    data["recipes"][0].update(overrides)
    return json.dumps(data, ensure_ascii=False)


def ingredient_with(**overrides) -> str:
    data = copy.deepcopy(VALID_RESPONSE)
    data["recipes"][0]["ingredients"][0].update(overrides)
    return json.dumps(data, ensure_ascii=False)


# --- 正常解析 ---------------------------------------------------------------


def test_valid_response_parses_into_contract_objects() -> None:
    response = parse_recipe_response(payload())

    assert len(response.recipes) == 1
    recipe = response.recipes[0]
    assert recipe.title == "番茄炒蛋"
    assert recipe.servings == 2
    assert [item.name for item in recipe.ingredients] == ["番茄", "鸡蛋"]
    assert recipe.ingredients[0].quantity == 2
    assert recipe.ingredients[0].unit == "个"
    assert recipe.steps == ["番茄切块", "炒蛋", "同炒调味"]


def test_prompt_example_matches_the_schema() -> None:
    """提示词里的示例结构必须真的能被契约接受（防止提示词与 Schema 漂移）。"""
    response = parse_recipe_response(recipe_prompts.EXAMPLE_RESPONSE_JSON)

    assert response.recipes[0].title
    for field in ("title", "servings", "rationale", "ingredients", "steps"):
        assert field in recipe_prompts.SCHEMA_HINT


def test_servings_accepts_integral_float() -> None:
    response = parse_recipe_response(recipe_with(servings=2.0))
    assert response.recipes[0].servings == 2


def test_code_fence_and_surrounding_whitespace_are_tolerated() -> None:
    fenced = "```json\n" + payload() + "\n```"
    assert parse_recipe_response(fenced).recipes[0].title == "番茄炒蛋"
    assert strip_code_fence("```\n{}\n```") == "{}"


def test_bare_array_is_wrapped() -> None:
    raw = json.dumps(VALID_RESPONSE["recipes"], ensure_ascii=False)
    assert parse_recipe_response(raw).recipes[0].title == "番茄炒蛋"


def test_empty_recipe_list_is_allowed_by_the_schema() -> None:
    assert parse_recipe_response(payload(recipes=[])).recipes == []


# --- 结构拒绝 ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", None, 123])
def test_empty_or_non_text_input_is_rejected(text) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(text)


@pytest.mark.parametrize(
    "text",
    [
        "{不是 JSON}",
        "title: 番茄炒蛋",
        '{"recipes": [}',
        '"just a string"',
        "42",
    ],
)
def test_invalid_json_is_rejected(text: str) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(text)


def test_missing_required_field_is_rejected() -> None:
    data = copy.deepcopy(VALID_RESPONSE)
    del data["recipes"][0]["rationale"]

    with pytest.raises(RecipeSchemaError) as excinfo:
        parse_recipe_response(json.dumps(data, ensure_ascii=False))

    assert "rationale" in str(excinfo.value)


@pytest.mark.parametrize("field", ["title", "rationale"])
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_text_fields_are_rejected(field: str, value: str) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(**{field: value}))


def test_empty_ingredients_and_steps_are_rejected() -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(ingredients=[]))
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(steps=[]))
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(steps=["   "]))


@pytest.mark.parametrize("value", [0, -1, -2.5, "2", None, True])
def test_invalid_quantity_is_rejected(value) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(ingredient_with(quantity=value))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_quantity_is_rejected(literal: str) -> None:
    raw = json.dumps(VALID_RESPONSE, ensure_ascii=False).replace('"quantity": 2', f'"quantity": {literal}', 1)

    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(raw)


@pytest.mark.parametrize("value", [0, -1, 2.5, True, "2"])
def test_invalid_servings_is_rejected(value) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(servings=value))


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_ingredient_name_or_unit_is_rejected(value) -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(ingredient_with(name=value))
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(ingredient_with(unit=value))


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(recipe_with(chef="某厨师"))
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(ingredient_with(calories=120))
    with pytest.raises(RecipeSchemaError):
        parse_recipe_response(payload(notes="多余字段"))


def test_error_message_is_readable_and_does_not_dump_the_whole_payload() -> None:
    long_title = "x" * 400
    with pytest.raises(RecipeSchemaError) as excinfo:
        parse_recipe_response(recipe_with(servings=0, title=long_title))

    message = str(excinfo.value)
    assert "份数" in message
    assert "recipes.0.servings" in message
    assert long_title not in message
