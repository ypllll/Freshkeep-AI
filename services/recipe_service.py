"""菜谱规划的业务编排（S5）。

流程（每一步都对应一个可测试的环节）：

1. 校验用户需求与份数；
2. 通过 `inventory_service` 读取活动批次、汇总与用户过敏原（服务层不写 SQL）；
3. 用 `ai/prompts.py` 动态构造 messages，经 `ai_service.generate_text()` 调用模型；
4. 用 `ai/schemas.py` 校验模型输出（结构校验）；
5. 用 `core/allergens.py` 做**确定性**过敏原判定（业务校验）；
6. 用 `core/recipe_math.py` 计算库存缺口（程序算，不用模型给的数字）；
7. 汇总成 `RecipePlan` 返回给界面。

本模块**不修改库存**（S6 才做确认消耗），也不实现份数重规划、持久化或 Agent 循环。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ai import prompts as recipe_prompts
from ai import schemas as recipe_schemas
from ai.client import AIError
from core import allergens as allergen_rules
from core import recipe_math
from core.models import InventoryItem
from core.recipe_math import GapSummary, IngredientGap
from core.validation import normalize_name
from services import ai_service, inventory_service

DEFAULT_SERVINGS = 2
MAX_RECIPES = 3
MAX_REQUEST_LENGTH = 500


# ---------------------------------------------------------------------------
# 错误：让界面能区分"模型失败 / 输出不合法 / 库存问题 / 需求问题"
# ---------------------------------------------------------------------------


class RecipePlanningError(Exception):
    """菜谱规划失败的基类。`kind` 供界面分支展示。"""

    kind = "planning_error"


class RecipeRequestError(RecipePlanningError):
    kind = "invalid_request"


class RecipeInventoryError(RecipePlanningError):
    kind = "inventory_error"


class RecipeInventoryEmptyError(RecipeInventoryError):
    kind = "empty_inventory"


class RecipeAIError(RecipePlanningError):
    kind = "ai_error"


class RecipeOutputError(RecipePlanningError):
    kind = "invalid_output"


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedIngredient:
    name: str
    quantity: float
    unit: str
    gap: IngredientGap
    allergen_status: str
    allergen_message: str


@dataclass(frozen=True)
class PlannedRecipe:
    title: str
    servings: int
    requested_servings: int
    servings_match: bool
    rationale: str
    steps: Tuple[str, ...]
    ingredients: Tuple[PlannedIngredient, ...]
    allergen_status: str
    allergen_message: str
    allergen_reasons: Tuple[str, ...]

    @property
    def gap_summary(self) -> GapSummary:
        return recipe_math.summarize_gaps([item.gap for item in self.ingredients])


@dataclass(frozen=True)
class RejectedRecipe:
    """因为已知过敏原冲突而被排除的菜谱（不作为推荐结果）。"""

    recipe: PlannedRecipe
    reason: str


@dataclass(frozen=True)
class RecipePlan:
    user_request: str
    servings: int
    prompt_version: str
    inventory_batch_count: int
    allergen_names: Tuple[str, ...]
    recommended: Tuple[PlannedRecipe, ...]
    needs_review: Tuple[PlannedRecipe, ...]
    rejected: Tuple[RejectedRecipe, ...]
    notes: Tuple[str, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.recommended) + len(self.needs_review) + len(self.rejected)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def plan_recipes(
    conn: sqlite3.Connection,
    *,
    user_request: str,
    servings: int = DEFAULT_SERVINGS,
    max_recipes: int = MAX_RECIPES,
    client: Optional[object] = None,
) -> RecipePlan:
    """生成并校验菜谱候选。`client` 用于测试注入假模型客户端。"""
    request = _validate_request(user_request, servings, max_recipes)
    all_batches, summaries, allergen_names = _load_context(conn)
    active_batches = [item for item in all_batches if item.status == "active"]
    if not active_batches:
        raise RecipeInventoryEmptyError(
            "冰箱里还没有食材，请先在「数字冰箱」页面录入，再来规划菜谱。"
        )

    inventory_items = [(row.name, row.unit, row.total_quantity) for row in summaries]
    messages = recipe_prompts.build_recipe_messages(
        user_request=request,
        servings=servings,
        inventory_items=inventory_items,
        user_allergens=allergen_names,
        max_recipes=max_recipes,
    )
    raw_text = _generate(messages, client=client)
    response = _parse(raw_text)

    batches_by_name: Dict[str, List[InventoryItem]] = {}
    for item in active_batches:
        batches_by_name.setdefault(item.name_normalized, []).append(item)

    recommended: List[PlannedRecipe] = []
    needs_review: List[PlannedRecipe] = []
    rejected: List[RejectedRecipe] = []
    for recipe in response.recipes:
        planned = _build_planned_recipe(
            recipe,
            batches=all_batches,
            batches_by_name=batches_by_name,
            allergen_names=allergen_names,
            requested_servings=servings,
        )
        if planned.allergen_status == allergen_rules.STATUS_CONFLICT:
            rejected.append(RejectedRecipe(recipe=planned, reason=planned.allergen_message))
        elif planned.allergen_status == allergen_rules.STATUS_UNVERIFIABLE:
            needs_review.append(planned)
        else:
            recommended.append(planned)

    notes = [
        "本次规划只读取库存，不会修改库存；确认实际消耗在后续阶段处理。",
        "库存缺口与过敏原判定由程序计算，模型只负责提出菜谱与用量。",
    ]
    if not allergen_names:
        notes.append(
            "你还没有声明任何过敏原，本次未做个性化过敏原检查（「未发现已知冲突」不代表安全）。"
        )
    if not response.recipes:
        notes.append("模型没有返回任何菜谱候选，可以调整需求后重试。")

    return RecipePlan(
        user_request=request,
        servings=servings,
        prompt_version=recipe_prompts.PROMPT_VERSION,
        inventory_batch_count=len(active_batches),
        allergen_names=allergen_names,
        recommended=tuple(recommended),
        needs_review=tuple(needs_review),
        rejected=tuple(rejected),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 内部步骤
# ---------------------------------------------------------------------------


def _validate_request(user_request: str, servings: int, max_recipes: int) -> str:
    request = (user_request or "").strip()
    if not request:
        raise RecipeRequestError("请描述你的做菜需求，例如：想吃清淡的家常菜。")
    if len(request) > MAX_REQUEST_LENGTH:
        raise RecipeRequestError(f"需求描述不能超过 {MAX_REQUEST_LENGTH} 个字符。")
    if isinstance(servings, bool) or not isinstance(servings, int) or servings <= 0:
        raise RecipeRequestError("份数必须是正整数。")
    if isinstance(max_recipes, bool) or not isinstance(max_recipes, int) or max_recipes <= 0:
        raise RecipeRequestError("候选数量必须是正整数。")
    return request


def _load_context(conn: sqlite3.Connection):
    """读取批次（含历史，供名称识别）、库存汇总（仅活动批次）与用户过敏原。

    全部经现有服务层接口，服务层不写 SQL。
    """
    try:
        batches = inventory_service.list_batches(conn)
        summaries = inventory_service.list_summary(conn)
        allergen_names = tuple(inventory_service.list_user_allergen_names(conn))
    except (sqlite3.Error, inventory_service.InventoryError) as exc:
        raise RecipeInventoryError(f"读取库存失败（{type(exc).__name__}），请重试。") from exc

    return batches, summaries, allergen_names


def _generate(messages: Sequence[object], *, client: Optional[object]) -> str:
    try:
        return ai_service.generate_text(messages, client=client)
    except AIError as exc:
        # AIError 家族的消息已经做过脱敏（不含 API Key）
        raise RecipeAIError(str(exc)) from exc


def _parse(raw_text: str) -> recipe_schemas.RecipeResponse:
    try:
        return recipe_schemas.parse_recipe_response(raw_text)
    except recipe_schemas.RecipeSchemaError as exc:
        raise RecipeOutputError(str(exc)) from exc


def _build_planned_recipe(
    recipe,
    *,
    batches: Sequence[InventoryItem],
    batches_by_name: Dict[str, List[InventoryItem]],
    allergen_names: Sequence[str],
    requested_servings: int,
) -> PlannedRecipe:
    gaps = recipe_math.compute_gaps(requirements=list(recipe.ingredients), inventory=batches)
    gap_by_name = {normalize_name(gap.name): gap for gap in gaps}

    planned_ingredients: List[PlannedIngredient] = []
    ingredient_assessments: List[Tuple[str, allergen_rules.AllergenAssessment]] = []
    for ingredient in recipe.ingredients:
        assessment = _assess_ingredient_allergens(
            ingredient.name,
            batches_by_name=batches_by_name,
            user_allergens=allergen_names,
        )
        ingredient_assessments.append((ingredient.name, assessment))
        planned_ingredients.append(
            PlannedIngredient(
                name=ingredient.name,
                quantity=float(ingredient.quantity),
                unit=ingredient.unit,
                gap=gap_by_name[normalize_name(ingredient.name)],
                allergen_status=assessment.status,
                allergen_message=assessment.message,
            )
        )

    status, message, reasons = _merge_recipe_allergens(ingredient_assessments)
    return PlannedRecipe(
        title=recipe.title,
        servings=int(recipe.servings),
        requested_servings=requested_servings,
        servings_match=int(recipe.servings) == requested_servings,
        rationale=recipe.rationale,
        steps=tuple(recipe.steps),
        ingredients=tuple(planned_ingredients),
        allergen_status=status,
        allergen_message=message,
        allergen_reasons=reasons,
    )


def _assess_ingredient_allergens(
    name: str,
    *,
    batches_by_name: Dict[str, List[InventoryItem]],
    user_allergens: Sequence[str],
) -> allergen_rules.AllergenAssessment:
    """对菜谱里的一种食材做确定性判定。

    - 用户未声明过敏原：不做个性化判定（返回 none_known，并说明原因）；
    - 名称或复合调味料本身命中用户的过敏原：conflict；
    - 库存里有同名批次：逐个批次判定后合并（任一批次冲突即冲突，否则任一无法核实即无法核实）；
    - 库存里没有同名批次：无法核实其成分 → unverifiable（不得当成无冲突）。
    """
    if not user_allergens:
        return allergen_rules.assess_ingredient(
            name=name,
            declared_allergens=None,
            allergen_status=allergen_rules.DECLARATION_UNKNOWN,
            user_allergens=[],
        )

    matching = batches_by_name.get(normalize_name(name), [])
    if matching:
        views = [allergen_rules.assess_item(item, user_allergens) for item in matching]
        return _merge_assessments(views)

    name_level = allergen_rules.assess_ingredient(
        name=name,
        declared_allergens=None,
        allergen_status=allergen_rules.DECLARATION_DECLARED,
        user_allergens=user_allergens,
    )
    if name_level.status != allergen_rules.STATUS_NONE_KNOWN:
        return name_level
    return allergen_rules.AllergenAssessment(
        status=allergen_rules.STATUS_UNVERIFIABLE,
        declaration_state=allergen_rules.DECLARATION_UNKNOWN,
        message=f"库存里没有「{name}」的记录，成分无法核实，请自行确认",
        reasons=(f"「{name}」不在库存记录中，无法核实它是否含有相关过敏原。",),
    )


def _merge_assessments(views: Iterable[object]) -> allergen_rules.AllergenAssessment:
    """合并同一食材在多个批次上的判定结果。"""
    conflicts = [view for view in views if view.status == allergen_rules.STATUS_CONFLICT]
    if conflicts:
        return allergen_rules.AllergenAssessment(
            status=allergen_rules.STATUS_CONFLICT,
            declaration_state=allergen_rules.DECLARATION_DECLARED,
            message=conflicts[0].message,
            reasons=_dedupe(reason for view in conflicts for reason in view.reasons),
        )

    unverifiable = [view for view in views if view.status == allergen_rules.STATUS_UNVERIFIABLE]
    if unverifiable:
        return allergen_rules.AllergenAssessment(
            status=allergen_rules.STATUS_UNVERIFIABLE,
            declaration_state=allergen_rules.DECLARATION_UNKNOWN,
            message=unverifiable[0].message,
            reasons=_dedupe(reason for view in unverifiable for reason in view.reasons),
        )

    return allergen_rules.AllergenAssessment(
        status=allergen_rules.STATUS_NONE_KNOWN,
        declaration_state=allergen_rules.DECLARATION_DECLARED,
        message=allergen_rules.NONE_KNOWN_MESSAGE,
        reasons=("该食材在库存中的批次已声明成分，未发现与你的过敏原匹配的词。",),
    )


def _merge_recipe_allergens(
    assessments: Sequence[Tuple[str, allergen_rules.AllergenAssessment]]
) -> Tuple[str, str, Tuple[str, ...]]:
    """把食材级判定汇总成菜谱级结论：conflict > unverifiable > none_known。"""
    conflicts = [(name, item) for name, item in assessments if item.status == allergen_rules.STATUS_CONFLICT]
    if conflicts:
        names = "、".join(name for name, _ in conflicts)
        return (
            allergen_rules.STATUS_CONFLICT,
            f"含与你声明的过敏原冲突的食材：{names}",
            _dedupe(reason for _, item in conflicts for reason in item.reasons),
        )

    unverifiable = [
        (name, item) for name, item in assessments if item.status == allergen_rules.STATUS_UNVERIFIABLE
    ]
    if unverifiable:
        names = "、".join(name for name, _ in unverifiable)
        return (
            allergen_rules.STATUS_UNVERIFIABLE,
            f"有 {len(unverifiable)} 种食材的成分无法核实：{names}（不代表安全，请自行确认）",
            _dedupe(reason for _, item in unverifiable for reason in item.reasons),
        )

    return (
        allergen_rules.STATUS_NONE_KNOWN,
        allergen_rules.NONE_KNOWN_MESSAGE,
        ("已声明的成分中未发现与你的过敏原匹配的词。",),
    )


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)
