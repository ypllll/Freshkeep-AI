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

import math
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ai import prompts as recipe_prompts
from ai import schemas as recipe_schemas
from ai.client import AIError
from core import allergens as allergen_rules
from core import allocation as allocation_rules
from core import recipe_math
from core.models import InventoryItem
from core.recipe_math import GapSummary, IngredientGap
from core.validation import normalize_name
from services import ai_service, inventory_service
from storage import db as db_module

DEFAULT_SERVINGS = 2
MAX_RECIPES = 3
MAX_REQUEST_LENGTH = 500
EPSILON = 1e-9


# ---------------------------------------------------------------------------
# 错误：让界面能区分"模型失败 / 输出不合法 / 库存问题 / 需求问题"
# ---------------------------------------------------------------------------


class RecipeError(Exception):
    """菜谱相关流程（规划 / 确认）的错误基类。`kind` 供界面分支展示。"""

    kind = "recipe_error"


class RecipePlanningError(RecipeError):
    """生成菜谱候选时的失败。"""

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


class RecipeConfirmationError(RecipeError):
    """确认实际消耗时的失败基类。"""

    kind = "confirmation_error"


class RecipeNotDeductibleError(RecipeConfirmationError):
    """食材无法与库存可靠匹配（未匹配或单位无法比较），不允许扣减。"""

    kind = "not_deductible"


class RecipeStockShortageError(RecipeConfirmationError):
    """确认的实际用量超过最新可用库存。"""

    kind = "insufficient_stock"


class RecipeAllergenConflictError(RecipeConfirmationError):
    """菜谱与用户的过敏原冲突，不允许确认消费。"""

    kind = "allergen_conflict"


class RecipeAllergenAcknowledgementError(RecipeConfirmationError):
    """菜谱含无法核实的成分，需要用户明确确认后才能扣减。"""

    kind = "allergen_unverified"


class RecipePlanAlreadyAppliedError(RecipeConfirmationError):
    """同一规划在当前会话中已经应用过。"""

    kind = "already_applied"


class RecipeInventoryBusyError(RecipeConfirmationError):
    """数据库被其他写操作占用，稍后重试即可。"""

    kind = "inventory_busy"


def new_plan_id() -> int:
    """生成会话级规划编号。

    由 UUID4 派生为整数，可直接写入现有 `inventory_event.plan_id`（INTEGER）列，
    因此不需要新增迁移。**它只在当前会话/事件表内可追溯**：应用重启或跨会话
    无法依赖它做全局防重复。
    """
    return (uuid.uuid4().int % (2**63 - 1)) + 1


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
    plan_id: int
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
# 确认实际消耗（S6）的结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationIngredientCheck:
    """确认前对一种食材的最新检查结果（只反映当前数据库状态）。"""

    index: int
    name: str
    unit: str
    planned_quantity: float
    match_state: str
    available_quantity: Optional[float]
    missing_quantity: Optional[float]
    note: Optional[str]
    allergen_status: str
    allergen_message: str

    @property
    def is_deductible(self) -> bool:
        """只有可靠匹配到库存的食材才允许扣减。"""
        return self.match_state == recipe_math.MATCH_MATCHED

    @property
    def max_confirmable(self) -> Optional[float]:
        """最多可确认的数量；不可扣减时为 None（未知，不是 0）。"""
        return self.available_quantity if self.is_deductible else None


@dataclass(frozen=True)
class ConfirmationCheck:
    """一次确认前的整体检查结果。"""

    recipe_title: str
    ingredients: Tuple[ConfirmationIngredientCheck, ...]
    allergen_status: str
    allergen_message: str
    allergen_reasons: Tuple[str, ...]
    allergen_names: Tuple[str, ...]
    requires_acknowledgement: bool
    blocked: bool
    block_reason: Optional[str] = None


@dataclass(frozen=True)
class BatchDeduction:
    """某个批次上实际发生的扣减。"""

    batch_id: int
    quantity: float
    quantity_after: float
    unit: str
    expiry_date: Optional[str] = None


@dataclass(frozen=True)
class AppliedIngredient:
    """一种食材的实际消费结果。"""

    name: str
    unit: str
    planned_quantity: float
    confirmed_quantity: float
    deductions: Tuple[BatchDeduction, ...]
    remaining_quantity: Optional[float]
    allergen_status: str
    allergen_message: str


@dataclass(frozen=True)
class AppliedPlanResult:
    """一次成功确认的结果（全部扣减已在同一事务内提交）。"""

    plan_id: int
    recipe_title: str
    servings: int
    ingredients: Tuple[AppliedIngredient, ...]
    skipped_ingredients: Tuple[str, ...]
    total_deducted: float
    batch_count: int
    event_count: int
    allergen_status: str
    allergen_message: str
    note: str


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
        plan_id=new_plan_id(),
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


# ---------------------------------------------------------------------------
# 确认实际消耗（S6）：一切扣减都在一个外层事务内完成
# ---------------------------------------------------------------------------

_APPLIED_NOTE = (
    "本次扣减已提交；plan_id 只在当前会话与库存事件表内可追溯（没有持久化规划表）。"
)


def inspect_recipe_for_confirmation(
    conn: sqlite3.Connection, *, recipe: PlannedRecipe
) -> ConfirmationCheck:
    """只读检查：用**最新**库存与最新过敏原声明重算可扣减状态。

    页面用它展示"计划用量 / 最新可用 / 最多可确认"，不做任何写入；
    真正的写前校验会在 `apply_plan()` 的事务内再跑一次。
    """
    return _confirmation_check(conn, recipe)


def apply_plan(
    conn: sqlite3.Connection,
    *,
    plan: RecipePlan,
    recipe: PlannedRecipe,
    confirmed_quantities: Mapping[int, object],
    allergen_acknowledged: bool = False,
    applied_plan_ids: Collection[int] = (),
) -> AppliedPlanResult:
    """按用户确认的实际数量扣减库存（S6）。

    - `confirmed_quantities`：`{食材下标: 用户确认的实际消耗量}`；缺键表示"未确认"，
      值为 0 表示"确认没有消耗"，两者都不扣减、不写事件；
    - `allergen_acknowledged`：菜谱处于 `unverifiable` 时，必须由用户明确确认才允许扣减；
    - `applied_plan_ids`：当前会话中已成功应用的规划编号集合（会话级防重复）。

    多食材确认是**一个外层事务**：任一食材扣减、事件写入或校验失败，全部回滚。
    """
    candidates = (
        list(plan.recommended)
        + list(plan.needs_review)
        + [rejected.recipe for rejected in plan.rejected]
    )
    if recipe not in candidates:
        raise RecipeConfirmationError("该菜谱不属于当前规划，请重新生成后再确认。")
    if any(recipe is rejected.recipe for rejected in plan.rejected):
        raise RecipeAllergenConflictError(
            "该菜谱与你的过敏原冲突，不能确认消费（请重新生成规划）。"
        )
    if plan.plan_id in tuple(applied_plan_ids or ()):
        raise RecipePlanAlreadyAppliedError("该规划已经应用过，不能重复扣减。")

    requested = _normalise_confirmations(confirmed_quantities, len(recipe.ingredients))

    try:
        with db_module.transaction(conn):
            # 写前重新读取最新库存与最新过敏原声明（不复用生成规划时的快照）
            check = _confirmation_check(conn, recipe)
            _enforce_allergen_gate(check, allergen_acknowledged)
            _validate_quantities(check, requested)

            applied: List[AppliedIngredient] = []
            batch_count = 0
            for index in sorted(requested):
                item = check.ingredients[index]
                quantity = requested[index]
                try:
                    allocations = inventory_service.consume(
                        conn,
                        quantity=quantity,
                        name_normalized=normalize_name(item.name),
                        unit=item.unit,
                        source="recipe_plan",
                        plan_id=plan.plan_id,
                        note=f"菜谱确认：{recipe.title}",
                    )
                except allocation_rules.InsufficientStockError as exc:
                    raise RecipeStockShortageError(str(exc)) from exc
                except RecipeConfirmationError:
                    raise
                except sqlite3.OperationalError as exc:
                    raise _map_sqlite_error(exc) from exc
                except inventory_service.InventoryError as exc:
                    raise RecipeConfirmationError(f"「{item.name}」扣减失败：{exc}") from exc

                deductions = tuple(
                    BatchDeduction(
                        batch_id=allocation.item_id,
                        quantity=allocation.take,
                        quantity_after=allocation.quantity_after,
                        unit=allocation.unit,
                        expiry_date=allocation.expiry_date,
                    )
                    for allocation in allocations
                )
                batch_count += len(deductions)
                applied.append(
                    AppliedIngredient(
                        name=item.name,
                        unit=item.unit,
                        planned_quantity=item.planned_quantity,
                        confirmed_quantity=quantity,
                        deductions=deductions,
                        remaining_quantity=_remaining_quantity(
                            conn, name=item.name, unit=item.unit
                        ),
                        allergen_status=item.allergen_status,
                        allergen_message=item.allergen_message,
                    )
                )
    except sqlite3.OperationalError as exc:
        raise _map_sqlite_error(exc) from exc

    skipped = tuple(
        item.name
        for item in check.ingredients
        if item.index not in requested
    )
    return AppliedPlanResult(
        plan_id=plan.plan_id,
        recipe_title=recipe.title,
        servings=recipe.servings,
        ingredients=tuple(applied),
        skipped_ingredients=skipped,
        total_deducted=round(sum(item.confirmed_quantity for item in applied), 6),
        batch_count=batch_count,
        event_count=batch_count,
        allergen_status=check.allergen_status,
        allergen_message=check.allergen_message,
        note=_APPLIED_NOTE,
    )


def _confirmation_check(conn: sqlite3.Connection, recipe: PlannedRecipe) -> ConfirmationCheck:
    """用当前数据库状态重算缺口与过敏原结论。"""
    all_batches = inventory_service.list_batches(conn)
    active_batches = [item for item in all_batches if item.status == "active"]
    allergen_names = tuple(inventory_service.list_user_allergen_names(conn))
    gaps = recipe_math.compute_gaps(requirements=list(recipe.ingredients), inventory=all_batches)
    gap_by_name = {normalize_name(gap.name): gap for gap in gaps}

    batches_by_name: Dict[str, List[InventoryItem]] = {}
    for item in active_batches:
        batches_by_name.setdefault(item.name_normalized, []).append(item)

    checks: List[ConfirmationIngredientCheck] = []
    assessments: List[Tuple[str, allergen_rules.AllergenAssessment]] = []
    for index, ingredient in enumerate(recipe.ingredients):
        assessment = _assess_ingredient_allergens(
            ingredient.name, batches_by_name=batches_by_name, user_allergens=allergen_names
        )
        assessments.append((ingredient.name, assessment))
        gap = gap_by_name[normalize_name(ingredient.name)]
        checks.append(
            ConfirmationIngredientCheck(
                index=index,
                name=ingredient.name,
                unit=ingredient.unit,
                planned_quantity=float(ingredient.quantity),
                match_state=gap.match_state,
                available_quantity=gap.available_quantity,
                missing_quantity=gap.missing_quantity,
                note=gap.note,
                allergen_status=assessment.status,
                allergen_message=assessment.message,
            )
        )

    status, message, reasons = _merge_recipe_allergens(assessments)
    blocked = status == allergen_rules.STATUS_CONFLICT
    return ConfirmationCheck(
        recipe_title=recipe.title,
        ingredients=tuple(checks),
        allergen_status=status,
        allergen_message=message,
        allergen_reasons=reasons,
        allergen_names=allergen_names,
        requires_acknowledgement=status == allergen_rules.STATUS_UNVERIFIABLE,
        blocked=blocked,
        block_reason=message if blocked else None,
    )


def _normalise_confirmations(
    confirmed_quantities: Mapping[int, object], ingredient_count: int
) -> Dict[int, float]:
    """把用户输入整理成 `{下标: 正数}`；未确认与 0 都不进入扣减。"""
    if confirmed_quantities is None:
        raise RecipeConfirmationError("请至少确认一种食材的实际消耗量。")
    if not isinstance(confirmed_quantities, Mapping):
        raise RecipeConfirmationError("实际消耗量必须按下标提供。")

    normalised: Dict[int, float] = {}
    for raw_index, raw_quantity in confirmed_quantities.items():
        index = _as_index(raw_index, ingredient_count)
        quantity = _as_confirmed_quantity(raw_quantity)
        if quantity <= 0:
            continue  # 确认为 0：不扣减、不写事件
        normalised[index] = quantity
    return normalised


def _as_index(raw_index: object, ingredient_count: int) -> int:
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        raise RecipeConfirmationError("食材下标必须是整数。")
    if raw_index < 0 or raw_index >= ingredient_count:
        raise RecipeConfirmationError("食材下标超出范围。")
    return raw_index


def _as_confirmed_quantity(raw_quantity: object) -> float:
    if raw_quantity is None:
        raise RecipeConfirmationError("实际消耗量不能是空值（未知不等于 0）。")
    if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, (int, float)):
        raise RecipeConfirmationError("实际消耗量必须是数字。")
    number = float(raw_quantity)
    if not math.isfinite(number):
        raise RecipeConfirmationError("实际消耗量必须是有限数字。")
    if number < 0:
        raise RecipeConfirmationError("实际消耗量不能是负数。")
    return number


def _enforce_allergen_gate(check: ConfirmationCheck, acknowledged: bool) -> None:
    if check.allergen_status == allergen_rules.STATUS_CONFLICT:
        raise RecipeAllergenConflictError(
            f"该菜谱与你的过敏原冲突，不能确认消费：{check.allergen_message}"
        )
    if check.allergen_status == allergen_rules.STATUS_UNVERIFIABLE and not acknowledged:
        raise RecipeAllergenAcknowledgementError(
            "该菜谱有成分无法核实，需要你明确确认后才能扣减："
            f"{check.allergen_message}"
        )


def _validate_quantities(check: ConfirmationCheck, requested: Mapping[int, float]) -> None:
    if not requested:
        raise RecipeConfirmationError("请至少确认一种食材的实际消耗量。")
    for index, quantity in sorted(requested.items()):
        item = check.ingredients[index]
        if not item.is_deductible:
            reason = item.note or "无法与库存可靠匹配"
            raise RecipeNotDeductibleError(
                f"「{item.name}」不能扣减：{reason}（可以把实际消耗量留空或设为 0）"
            )
        available = item.available_quantity or 0.0
        if quantity > available + EPSILON:
            raise RecipeStockShortageError(
                f"「{item.name}」最新可用 {available:g} {item.unit}，"
                f"你确认的 {quantity:g} {item.unit} 超出库存；本次操作已取消。"
            )


def _remaining_quantity(conn: sqlite3.Connection, *, name: str, unit: str) -> Optional[float]:
    """扣减后该食材的可用量（按汇总口径；没有活动批次时为 0）。"""
    key = normalize_name(name)
    for row in inventory_service.list_summary(conn):
        if row.name_normalized == key and row.unit == unit:
            return row.total_quantity
    return 0.0


def _map_sqlite_error(exc: sqlite3.OperationalError) -> RecipeConfirmationError:
    text = str(exc).lower()
    if "lock" in text or "busy" in text:
        return RecipeInventoryBusyError("库存正被其他操作占用，请稍后重试。")
    return RecipeConfirmationError(f"库存写入失败（{type(exc).__name__}），本次操作已取消。")
