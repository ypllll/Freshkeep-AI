"""菜谱输出的数据结构与校验（S5）。

只描述 V1 契约：不做可选字段堆叠、不做继承层级、不做通用 Schema 工厂。
`extra="forbid"` 保证模型多写字段时直接失败，而不是被悄悄忽略。

校验分两步：`parse_recipe_response()` 负责"文本 → JSON → 契约对象"，
失败一律抛 `RecipeSchemaError`（带可读原因，不回显整段模型输出）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, List

from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError
from pydantic import field_validator

STRICT_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True)

_FENCE_START = re.compile(r"^```[A-Za-z0-9_-]*\s*")
_FENCE_END = re.compile(r"\s*```$")


class RecipeSchemaError(ValueError):
    """模型输出不符合菜谱契约（不是合法 JSON，或字段/取值不合法）。"""


def _not_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


class RecipeIngredient(BaseModel):
    """菜谱中的一种食材用量。"""

    model_config = STRICT_CONFIG

    name: str
    quantity: float
    unit: str

    @field_validator("name", "unit")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _not_blank(value, "食材名称与单位")

    @field_validator("quantity", mode="before")
    @classmethod
    def _positive_finite(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("所需数量必须是数字")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("所需数量必须是有限数字")
        if number <= 0:
            raise ValueError("所需数量必须大于 0")
        return number


class Recipe(BaseModel):
    """一道候选菜谱。"""

    model_config = STRICT_CONFIG

    title: str
    servings: int
    rationale: str
    ingredients: List[RecipeIngredient]
    steps: List[str]

    @field_validator("title", "rationale")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _not_blank(value, "菜谱名称与推荐理由")

    @field_validator("servings", mode="before")
    @classmethod
    def _positive_int(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("份数必须是正整数（不接受布尔值）")
        if isinstance(value, int):
            number = value
        elif isinstance(value, float) and value.is_integer():
            number = int(value)
        else:
            raise ValueError("份数必须是正整数")
        if number <= 0:
            raise ValueError("份数必须大于 0")
        return number

    @field_validator("ingredients")
    @classmethod
    def _ingredients_not_empty(cls, value: List[RecipeIngredient]) -> List[RecipeIngredient]:
        if not value:
            raise ValueError("用料列表不能为空")
        return value

    @field_validator("steps")
    @classmethod
    def _steps_not_empty(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("烹饪步骤不能为空")
        cleaned = []
        for step in value:
            if not isinstance(step, str) or not step.strip():
                raise ValueError("烹饪步骤不能是空白")
            cleaned.append(step.strip())
        return cleaned


class RecipeResponse(BaseModel):
    """一次模型回复的整体契约。"""

    model_config = ConfigDict(extra="forbid")

    recipes: List[Recipe]


def strip_code_fence(text: str) -> str:
    """去掉模型偶尔加上的 ``` 代码块包裹。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_START.sub("", stripped)
        stripped = _FENCE_END.sub("", stripped)
    return stripped.strip()


def _readable_pydantic_error(exc: PydanticValidationError) -> str:
    details = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "(根)"
        details.append(f"{location}: {error.get('msg', '不合法')}")
    remaining = len(exc.errors()) - len(details)
    if remaining > 0:
        details.append(f"另有 {remaining} 处问题")
    return "；".join(details)


def parse_recipe_response(raw_text: str) -> RecipeResponse:
    """把模型返回的文本解析成 `RecipeResponse`。

    - 空内容 → `RecipeSchemaError`
    - 不是合法 JSON → `RecipeSchemaError`
    - 顶层是数组时按 `{"recipes": [...]}` 处理（模型有时省略外层对象）
    - 结构或取值不符合契约 → `RecipeSchemaError`，并给出可读的字段级原因
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise RecipeSchemaError("模型返回了空内容。")

    payload = strip_code_fence(raw_text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RecipeSchemaError(
            f"模型输出不是合法 JSON（{exc.msg}，位置 {exc.pos}）。"
        ) from exc

    if isinstance(data, list):
        data = {"recipes": data}
    if not isinstance(data, dict):
        raise RecipeSchemaError("模型输出的顶层结构必须是含 recipes 字段的 JSON 对象。")

    try:
        return RecipeResponse.model_validate(data)
    except PydanticValidationError as exc:
        raise RecipeSchemaError(
            "模型输出不符合菜谱契约：" + _readable_pydantic_error(exc)
        ) from exc
