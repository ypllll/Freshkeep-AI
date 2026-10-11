"""食谱规划页面（S5）。

页面只负责收集输入与展示结果：模型调用经 `services/recipe_service.py`，
库存与过敏原数据经现有服务层读取。页面**不会**创建 SDK 客户端，
也不会修改库存（确认消耗属于 S6）。
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

import streamlit as st

from core import allergens as allergen_rules
from core import recipe_math
from services import ai_service, inventory_service, recipe_service

PLAN_STATE_KEY = "recipe_plan"
APPLIED_STATE_KEY = "recipe_applied_plan_ids"
STATUS_STATE_KEY = "recipe_apply_status"
RESULT_STATE_KEY = "recipe_apply_result"


def _reset_plan() -> None:
    for key in (PLAN_STATE_KEY, RESULT_STATE_KEY):
        st.session_state.pop(key, None)
    st.session_state[STATUS_STATE_KEY] = "draft"


def _applied_plan_ids() -> list:
    return list(st.session_state.get(APPLIED_STATE_KEY, []))


def _gap_rows(recipe: recipe_service.PlannedRecipe) -> list:
    rows = []
    for ingredient in recipe.ingredients:
        gap = ingredient.gap
        rows.append(
            {
                "食材": ingredient.name,
                "需要": f"{ingredient.quantity:g}",
                "单位": ingredient.unit,
                # 未知一律显示"—"：整列保持同一种类型，避免表格序列化问题
                "现有": "—" if gap.available_quantity is None else f"{gap.available_quantity:g}",
                "还缺": "—" if gap.missing_quantity is None else f"{gap.missing_quantity:g}",
                "状态": recipe_math.MATCH_LABELS.get(gap.match_state, gap.match_state),
                "过敏原": ingredient.allergen_message,
            }
        )
    return rows


def _render_ingredient_table(recipe: recipe_service.PlannedRecipe) -> None:
    st.dataframe(_gap_rows(recipe), use_container_width=True, hide_index=True)

    summary = recipe.gap_summary
    st.caption(
        f"库存已匹配：充足 {summary.matched_sufficient} 项 / 不足 {summary.matched_missing} 项；"
        f"单位无法比较 {summary.unit_mismatch} 项；库存中未找到 {summary.unmatched} 项。"
        "「还缺」由程序按现有批次汇总计算，未做单位换算。"
    )
    for ingredient in recipe.ingredients:
        if ingredient.gap.note and ingredient.gap.match_state != recipe_math.MATCH_MATCHED:
            st.caption(f"· {ingredient.name}：{ingredient.gap.note}")


def _render_recipe(recipe: recipe_service.PlannedRecipe) -> None:
    st.markdown(f"#### {recipe.title}")
    if recipe.servings_match:
        st.caption(f"份数：{recipe.servings} 份")
    else:
        st.warning(
            f"份数不一致：你请求 {recipe.requested_servings} 份，模型给出 {recipe.servings} 份"
            "（按模型给出的份数展示用量）。"
        )

    st.write(f"**推荐理由（模型生成）**：{recipe.rationale}")

    _render_ingredient_table(recipe)

    if recipe.allergen_status == allergen_rules.STATUS_UNVERIFIABLE:
        st.warning(f"过敏原：{recipe.allergen_message}")
        for reason in recipe.allergen_reasons:
            st.write(f"- {reason}")
    else:
        st.caption(f"过敏原：{recipe.allergen_message}")

    if recipe.steps:
        st.write("**步骤**")
        for index, step in enumerate(recipe.steps, start=1):
            st.write(f"{index}. {step}")
    st.divider()


def _render_plan(conn: sqlite3.Connection, plan: recipe_service.RecipePlan) -> None:
    st.success(
        f"已生成 {plan.candidate_count} 道候选菜谱（读取 {plan.inventory_batch_count} 条活动批次；"
        f"提示词版本 {plan.prompt_version}）"
    )
    for note in plan.notes:
        st.caption(f"· {note}")
    if plan.allergen_names:
        st.caption("已声明的过敏原：" + "、".join(plan.allergen_names))

    if plan.recommended:
        st.subheader("推荐菜谱")
        for recipe in plan.recommended:
            _render_recipe(recipe)
    else:
        st.info("没有可直接推荐的菜谱（可能都被排除或需要核实）。")

    if plan.needs_review:
        st.subheader("待核实区域")
        st.warning(
            f"有 {len(plan.needs_review)} 道菜含无法核实的成分，**不作为普通推荐**："
            + "、".join(recipe.title for recipe in plan.needs_review)
        )
        if plan.allergen_names:
            st.caption(
                "想减少待核实？可以在「数字冰箱 → 入库」时为相关批次声明成分（过敏原），"
                "系统只按已声明的信息判断，无法核实时不会当成无冲突。"
            )
        for recipe in plan.needs_review:
            with st.expander(f"展开查看：{recipe.title}（成分无法核实）"):
                st.warning(recipe.allergen_message)
                for reason in recipe.allergen_reasons:
                    st.write(f"- {reason}")
                _render_recipe(recipe)

    if plan.rejected:
        st.subheader("已排除（与你的过敏原冲突）")
        for rejected in plan.rejected:
            st.error(f"{rejected.recipe.title}：{rejected.reason}")
            for reason in rejected.recipe.allergen_reasons:
                st.caption(f"· {reason}")

    _render_confirmation(conn, plan)


# ---------------------------------------------------------------------------
# 确认实际消耗（S6）
# ---------------------------------------------------------------------------


def _render_check(check: recipe_service.ConfirmationCheck) -> None:
    """展示"计划用量 / 最新可用 / 是否可扣减"，全部来自最新库存。"""
    st.dataframe(
        [
            {
                "食材": item.name,
                "计划用量": f"{item.planned_quantity:g}",
                "单位": item.unit,
                "最新可用": "—" if item.available_quantity is None else f"{item.available_quantity:g}",
                "还缺": "—" if item.missing_quantity is None else f"{item.missing_quantity:g}",
                "可扣减": "是" if item.is_deductible else "否",
                "说明": item.note or "",
            }
            for item in check.ingredients
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        f"过敏原判定（按最新声明重新评估）：{check.allergen_message}"
    )


def _render_applied_result(result: recipe_service.AppliedPlanResult) -> None:
    st.success(
        f"已按你的确认扣减库存：{result.recipe_title}（规划 {result.plan_id}）——"
        f"共 {len(result.ingredients)} 种食材、{result.batch_count} 个批次、"
        f"合计 {result.total_deducted:g}、{result.event_count} 条消费事件。"
    )
    st.dataframe(
        [
            {
                "食材": ingredient.name,
                "计划用量": f"{ingredient.planned_quantity:g}",
                "实际扣减": f"{ingredient.confirmed_quantity:g}",
                "单位": ingredient.unit,
                "扣减后剩余": "—"
                if ingredient.remaining_quantity is None
                else f"{ingredient.remaining_quantity:g}",
                "涉及批次": "、".join(
                    f"#{item.batch_id}（{item.quantity:g}）" for item in ingredient.deductions
                ),
            }
            for ingredient in result.ingredients
        ],
        use_container_width=True,
        hide_index=True,
    )
    if result.skipped_ingredients:
        st.caption("未扣减的食材：" + "、".join(result.skipped_ingredients))
    st.caption(result.note)
    st.caption(f"过敏原：{result.allergen_message}")


def _render_confirmation(conn, plan: recipe_service.RecipePlan) -> None:
    st.divider()
    st.subheader("确认实际消耗")
    st.caption(
        "计划用量来自模型，**不等于**你的实际消耗量。下面是按最新库存重新检查的结果："
        "只有你勾选并确认的数量才会扣减；不勾选的食材不会有任何变化。"
    )

    confirmable = list(plan.recommended) + list(plan.needs_review)
    if not confirmable:
        st.info("当前规划没有可确认的菜谱（都被排除或需要重新生成）。")
        return

    applied_ids = _applied_plan_ids()
    status = st.session_state.get(STATUS_STATE_KEY, "draft")
    if plan.plan_id in applied_ids:
        st.success(f"该规划已应用过（plan_id={plan.plan_id}），不能重复扣减。")
        cached = st.session_state.get(RESULT_STATE_KEY)
        if isinstance(cached, recipe_service.AppliedPlanResult) and cached.plan_id == plan.plan_id:
            _render_applied_result(cached)
        return
    if status == "failed":
        st.warning("上一次确认失败：库存与事件都没有变化，可以修改数量后重新确认。")

    labels = [
        f"{recipe.title}（{'待核实' if recipe in plan.needs_review else '推荐'}）"
        for recipe in confirmable
    ]
    chosen = st.selectbox("选择要确认的菜谱", labels, key="confirm_recipe_choice")
    recipe = confirmable[labels.index(chosen)]

    check = recipe_service.inspect_recipe_for_confirmation(conn, recipe=recipe)
    _render_check(check)

    if check.blocked:
        st.error(f"该菜谱与你的过敏原冲突，不提供确认入口：{check.allergen_message}")
        return
    if check.requires_acknowledgement:
        st.warning(
            "该菜谱有成分无法核实：" + check.allergen_message
            + "（无法核实不等于安全，请自行判断后再决定是否扣减）"
        )
        for reason in check.allergen_reasons:
            st.caption(f"· {reason}")

    with st.form("confirm_form"):
        selections: dict = {}
        for item in check.ingredients:
            st.markdown(f"**{item.name}**")
            columns = st.columns([2, 1])
            with columns[0]:
                if item.is_deductible:
                    st.caption(
                        f"计划用量 {item.planned_quantity:g} {item.unit}（建议值）；"
                        f"最新可用 {item.available_quantity:g} {item.unit}"
                    )
                else:
                    st.caption(f"计划用量 {item.planned_quantity:g} {item.unit}；{item.note or '无法与库存可靠匹配'}")
            with columns[1]:
                use = st.checkbox(
                    "确认扣减",
                    key=f"confirm_use_{item.index}",
                    disabled=not item.is_deductible,
                )
                quantity = st.number_input(
                    "实际消耗量",
                    min_value=0.0,
                    value=float(item.planned_quantity),
                    step=0.5,
                    format="%g",
                    key=f"confirm_qty_{item.index}",
                    disabled=not item.is_deductible,
                )
            selections[item.index] = (use, quantity)

        acknowledged = False
        if check.requires_acknowledgement:
            acknowledged = st.checkbox(
                "我已了解有成分无法核实，仍要按上面的数量扣减", key="confirm_ack"
            )
        submitted = st.form_submit_button("确认并扣减库存", key="confirm_submit")

    if not submitted:
        return

    confirmed = {index: quantity for index, (use, quantity) in selections.items() if use}
    st.session_state[STATUS_STATE_KEY] = "applying"
    try:
        result = recipe_service.apply_plan(
            conn,
            plan=plan,
            recipe=recipe,
            confirmed_quantities=confirmed,
            allergen_acknowledged=acknowledged,
            applied_plan_ids=applied_ids,
        )
    except recipe_service.RecipeError as exc:
        st.session_state[STATUS_STATE_KEY] = "failed"
        st.error(f"确认失败（{exc.kind}）：{exc}")
        st.caption("本次操作已取消：库存数量与库存事件都没有改变。")
        return

    st.session_state[APPLIED_STATE_KEY] = applied_ids + [plan.plan_id]
    st.session_state[RESULT_STATE_KEY] = result
    st.session_state[STATUS_STATE_KEY] = "applied"
    st.rerun()


def _render_form(conn: sqlite3.Connection, *, enabled: bool) -> None:
    with st.form("recipe_form"):
        request = st.text_area(
            "想做点什么？",
            placeholder="例如：想吃清淡的家常菜，最好 20 分钟内能做完",
            key="recipe_request",
        )
        servings = st.number_input(
            "计划份数", min_value=1, max_value=20, value=recipe_service.DEFAULT_SERVINGS, step=1,
            key="recipe_servings",
        )
        submitted = st.form_submit_button("生成菜谱候选", disabled=not enabled, key="recipe_generate")
    if not submitted:
        return

    try:
        with st.spinner("正在调用模型生成菜谱候选…"):
            plan = recipe_service.plan_recipes(
                conn, user_request=request, servings=int(servings)
            )
    except recipe_service.RecipeError as exc:
        _reset_plan()
        if exc.kind == "empty_inventory":
            st.warning(str(exc))
        elif exc.kind == "invalid_request":
            st.error(str(exc))
        elif exc.kind in ("ai_error", "invalid_output"):
            st.error(f"生成失败（{exc.kind}）：{exc}")
        else:
            st.error(f"生成失败：{exc}")
        return
    except ai_service.AIError as exc:  # 兜底（正常路径已被服务层包装）
        _reset_plan()
        st.error(f"生成失败：{type(exc).__name__}：{exc}")
        return

    st.session_state[PLAN_STATE_KEY] = plan


def _render(conn: sqlite3.Connection) -> None:
    st.title("食谱规划")
    st.caption(
        "根据真实库存生成菜谱候选；库存缺口与过敏原判定由程序计算。"
        "本次规划**不会**修改库存。"
    )

    st.caption("模型配置：" + ai_service.describe_configuration())
    configured = ai_service.is_configured()

    summaries = inventory_service.list_summary(conn)
    allergen_names = inventory_service.list_user_allergen_names(conn)
    if summaries:
        st.caption(
            f"当前库存：{len(summaries)} 种食材 / "
            f"{sum(row.batch_count for row in summaries)} 条活动批次。"
        )
    else:
        st.warning("冰箱里还没有食材，请先到「数字冰箱」页面录入。")

    if not allergen_names:
        st.info(
            "你还没有声明任何过敏原：本次不会做个性化过敏原检查，"
            "「未发现已知冲突」不代表安全。可在「数字冰箱 → 过敏原清单」补充。"
        )
    else:
        st.caption("已声明的过敏原：" + "、".join(allergen_names))

    if not configured:
        st.error(
            "尚未配置 AI 模型：请在项目根目录的 .env 中填写 OPENAI_API_KEY 与 OPENAI_MODEL"
            "（可参考 .env.example）。配置完成后即可生成菜谱。"
        )

    _render_form(conn, enabled=configured and bool(summaries))

    plan: Optional[recipe_service.RecipePlan] = st.session_state.get(PLAN_STATE_KEY)
    if plan is not None:
        st.divider()
        _render_plan(conn, plan)
        if st.button("清除本次结果", key="recipe_clear"):
            _reset_plan()
            st.rerun()


connection = inventory_service.open_connection()
try:
    _render(connection)
finally:
    connection.close()
