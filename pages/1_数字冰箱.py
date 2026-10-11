"""数字冰箱页面（阶段 2）。

本文件只做输入与展示：校验、计算、事务与事件写入全部由 `services` + `core` 完成
（docs/architecture.md 第 4 节）。表单不承担关键校验，非法输入仍会被数据层拒绝。
"""

from __future__ import annotations

from datetime import date
import sqlite3
from typing import Callable, List, Optional, Sequence, Tuple

import streamlit as st

from core.allocation import BatchAllocation, InsufficientStockError
from core.models import CATEGORIES, STORAGE_KINDS, UNITS, InventoryItem, StockSummaryRow
from core.validation import ValidationError
from services import inventory_service

ACTION_LABELS = {"消耗": inventory_service.ACTION_CONSUME, "丢弃": inventory_service.ACTION_DISCARD}

# 入库表单的控件键；保存成功后清空，避免把上一批的值误当成本批数据
ADD_FORM_KEYS = (
    "add_name",
    "add_quantity",
    "add_shelf_days",
    "add_allergen_text",
    "add_notes",
    "add_has_expiry_date",
    "add_allergen_declared",
    "add_know_purchase_date",
    "add_expiry_date",
    "add_purchase_date",
    "add_category",
    "add_unit",
    "add_storage",
)


def _clear_add_form() -> None:
    for key in ADD_FORM_KEYS:
        st.session_state.pop(key, None)


def _flash(kind: str, message: str, table: Optional[list] = None) -> None:
    st.session_state["_flash"] = {"kind": kind, "message": message, "table": table}


def _show_flash() -> None:
    flash = st.session_state.pop("_flash", None)
    if not flash:
        return
    if flash["kind"] == "ok":
        st.success(flash["message"])
    else:
        st.error(flash["message"])
    if flash["table"]:
        st.dataframe(flash["table"], use_container_width=True, hide_index=True)


def _run(operation: Callable[[], Tuple[str, Optional[list]]]) -> None:
    """执行一次操作：失败时展示可读原因，成功时展示结果，然后刷新页面。"""
    try:
        message, table = operation()
    except (ValidationError, InsufficientStockError, inventory_service.InventoryError) as exc:
        _flash("error", str(exc))
    else:
        _flash("ok", message, table)
    st.rerun()


def _summary_rows(summaries: Sequence[StockSummaryRow]) -> list:
    return [
        {
            "食材": row.name,
            "单位": row.unit,
            "汇总数量": row.total_quantity,
            "批次数": row.batch_count,
        }
        for row in summaries
    ]


def _batch_rows(views: Sequence[inventory_service.BatchView]) -> list:
    return [
        {
            "批次 ID": view.item.id,
            "食材": view.item.name,
            "数量": view.item.quantity,
            "单位": view.item.unit,
            "购买日期": view.item.purchased_on or "未知",
            "购买日期为默认值": "是" if view.item.purchase_date_defaulted else "否",
            "储存": view.item.storage,
            "保质期": view.expiry.formal_text,
            "期限来源": view.expiry.source_label,
            "新鲜度": view.expiry.status_label,
            "参考信息": view.expiry.reference_text or "—",
            "AI 估算": view.expiry.ai_estimate_text or "—",
            "过敏原": view.allergen.status_label,
            "过敏原说明": view.allergen.message,
            "提示": view.expiry.warning or "—",
            "状态": view.item.status,
            "入库时间": view.item.created_at,
        }
        for view in views
    ]


def _allocation_rows(allocations: Sequence[BatchAllocation]) -> list:
    return [
        {
            "批次 ID": item.item_id,
            "食材": item.name,
            "扣减前": item.quantity_before,
            "扣减量": item.take,
            "扣减后": item.quantity_after,
            "单位": item.unit,
            "购买日期": item.purchased_on or "未知",
        }
        for item in allocations
    ]


def _event_rows(events: Sequence) -> list:
    return [
        {
            "时间": event.at,
            "批次 ID": event.item_id,
            "食材": event.item_name or "—",
            "动作": event.action,
            "数量变化": event.qty_delta,
            "变化后数量": event.qty_after,
            "来源": event.source,
        }
        for event in events
    ]


def _render_overview(conn: sqlite3.Connection) -> None:
    summaries = inventory_service.list_summary(conn)
    if not summaries:
        st.info("冰箱里还没有食材。请在「入库」标签录入第一批食材。")
        return

    st.subheader("库存汇总")
    st.dataframe(_summary_rows(summaries), use_container_width=True, hide_index=True)
    st.caption("汇总数量 = 同名且同单位活动批次数量之和。")

    labels = [f"{row.name}（{row.unit}）" for row in summaries]
    chosen = st.selectbox("查看批次明细", labels, key="overview_batch")
    row = summaries[labels.index(chosen)]
    views = inventory_service.list_batch_views(
        conn, name_normalized=row.name_normalized, unit=row.unit
    )
    st.dataframe(_batch_rows(views), use_container_width=True, hide_index=True)
    st.caption(
        "保质期只采用你填写的日期；「参考信息」是通用经验区间，「AI 估算」是针对该批次的"
        "系统估算，两者都不是食品安全保证，也不会写成正式有效期。"
        "过敏原判定按批次独立进行；「未发现已知冲突」不代表安全。"
    )


def _render_add(conn: sqlite3.Connection) -> None:
    # 提交成功后清空表单：必须在创建控件之前执行
    if st.session_state.pop("_reset_add_form", False):
        _clear_add_form()

    st.subheader("入库（每次购买新增独立批次）")
    mode = st.radio(
        "入库方式",
        ["新购买入库", "首次录入已有食材"],
        horizontal=True,
        key="add_mode",
    )
    initial = mode == "首次录入已有食材"

    # 这些开关决定后续渲染哪些控件，必须放在表单外：
    # 表单内的控件只在提交时才把值传回脚本，勾选后不会立即生效。
    know_purchase_date = st.checkbox(
        "我知道购买日期（不勾选则留空；系统不会默认填今天）", key="add_know_purchase_date"
    )
    purchase_date: Optional[date] = None
    defaulted = False
    if know_purchase_date:
        chosen_date = st.date_input("购买日期", value=date.today(), key="add_purchase_date")
        purchase_date = chosen_date
        # 仍然等于系统预填的今天，说明用户没有改动这个默认值
        defaulted = chosen_date == date.today()

    has_expiry_date = st.checkbox(
        "我知道明确的有效期日期（不勾选则正式期限保持未知）", key="add_has_expiry_date"
    )
    expiry_date_value: Optional[date] = None
    if has_expiry_date:
        expiry_date_value = st.date_input(
            "保质期日期", value=date.today(), key="add_expiry_date"
        )

    allergen_declared = st.checkbox(
        "我确认这一批次的成分信息（已知过敏原）", key="add_allergen_declared"
    )

    with st.form("add_batch_form"):
        st.caption("带「必填」的字段必须填写；其余可使用默认值。每次保存都会新增一条独立批次。")
        name = st.text_input("食材名称（必填）", key="add_name")
        col1, col2, col3 = st.columns(3)
        with col1:
            category = st.selectbox("类别", list(CATEGORIES), key="add_category")
        with col2:
            unit = st.selectbox("单位", list(UNITS), key="add_unit")
        with col3:
            storage = st.selectbox("储存方式", list(STORAGE_KINDS), key="add_storage")
        quantity = st.number_input(
            "数量（必填，大于 0）", value=1.0, step=0.5, format="%g", key="add_quantity"
        )
        st.caption(
            "保质期只记录你提供的信息：只填天数不会生成正式有效期。"
            "没有明确有效期时，系统最多给出「AI 估算」，仅供参考，不是食品安全保证。"
            "过敏原默认标记为「未声明」。"
        )
        shelf_days_text = st.text_input(
            "保质期天数（可选，填写整数天数）", key="add_shelf_days"
        )
        allergen_text = st.text_input(
            "该批次含有的过敏原（用逗号分隔；已声明但确实不含可留空）",
            key="add_allergen_text",
        )
        notes = st.text_input("备注（可选）", key="add_notes")
        submitted = st.form_submit_button("保存批次", key="add_submit")

    if submitted:
        def _add() -> Tuple[str, Optional[list]]:
            item_id = inventory_service.add_batch(
                conn,
                name=name,
                category=category,
                quantity=quantity,
                unit=unit,
                storage=storage,
                purchased_on=purchase_date,
                purchase_date_defaulted=defaulted,
                notes=notes,
                expiry_date=expiry_date_value,
                shelf_life_days=shelf_days_text,
                declared_allergens=allergen_text,
                allergen_declared=allergen_declared,
                initial=initial,
            )
            view = inventory_service.get_batch_view(conn, item_id)
            message = f"已入库：{name.strip()}（新增独立批次）"
            if view.expiry.warning:
                message += f"\n\n提示：{view.expiry.warning}"
            if view.expiry.ai_estimate_text:
                message += f"\n\n{view.expiry.ai_estimate_text}"
            st.session_state["_reset_add_form"] = True
            return message, None

        _run(_add)


def _render_change(conn: sqlite3.Connection) -> None:
    summaries = inventory_service.list_summary(conn)
    active = inventory_service.list_batches(conn, active_only=True)
    if not summaries or not active:
        st.info("请先在「入库」标签录入食材，然后才能消耗、丢弃或修改数量。")
        return

    st.subheader("消耗 / 丢弃 / 修改数量")
    target = st.radio(
        "操作对象",
        ["按食材自动分配批次", "指定某个批次"],
        horizontal=True,
        key="change_target",
    )
    action_label = st.radio("动作", list(ACTION_LABELS), horizontal=True, key="change_action")
    action = ACTION_LABELS[action_label]

    summary_labels = [
        f"{row.name}（现有 {row.total_quantity:g} {row.unit}）" for row in summaries
    ]
    batch_labels = [
        f"#{item.id} {item.name}（现有 {item.quantity:g} {item.unit}）" for item in active
    ]

    if target == "按食材自动分配批次":
        with st.form("change_by_name_form"):
            chosen = st.selectbox("食材", summary_labels, key="change_name")
            quantity = st.number_input(
                "数量（消耗或丢弃）", value=1.0, step=0.5, format="%g", key="change_name_quantity"
            )
            submitted = st.form_submit_button(f"{action_label}", key="change_name_submit")
        if submitted:
            row = summaries[summary_labels.index(chosen)]
            label = f"已{action_label} {quantity:g} {row.unit}：{row.name}"
            _run(
                lambda: (
                    label,
                    _allocation_rows(
                        _apply_by_name(conn, action, quantity, row.name_normalized, row.unit)
                    ),
                )
            )
    else:
        with st.form("change_by_batch_form"):
            chosen_batch = st.selectbox("批次", batch_labels, key="change_batch")
            quantity = st.number_input(
                "数量（指定批次）", value=1.0, step=0.5, format="%g", key="change_batch_quantity"
            )
            submitted = st.form_submit_button(f"{action_label}", key="change_batch_submit")
        if submitted:
            batch = active[batch_labels.index(chosen_batch)]
            label = f"已{action_label} {quantity:g} {batch.unit}：批次 #{batch.id}"
            _run(
                lambda: (
                    label,
                    _allocation_rows(
                        _apply_by_batch(conn, action, quantity, batch.id)
                    ),
                )
            )

    with st.form("update_quantity_form"):
        edit_label = st.selectbox("修改数量：选择批次", batch_labels, key="change_edit_batch")
        new_quantity = st.number_input(
            "新的数量（> 0）", value=1.0, step=0.5, format="%g", key="change_new_quantity"
        )
        edit_submitted = st.form_submit_button("保存新数量", key="change_edit_submit")
    if edit_submitted:
        batch = active[batch_labels.index(edit_label)]

        def _edit() -> Tuple[str, Optional[list]]:
            before, after = inventory_service.update_quantity(conn, batch.id, new_quantity)
            return f"批次 #{batch.id} 数量已由 {before:g} 改为 {after:g} {batch.unit}", None

        _run(_edit)


def _apply_by_name(
    conn: sqlite3.Connection, action: str, quantity: float, name_normalized: str, unit: str
) -> List[BatchAllocation]:
    if action == inventory_service.ACTION_CONSUME:
        return inventory_service.consume(
            conn, quantity=quantity, name_normalized=name_normalized, unit=unit
        )
    return inventory_service.discard(
        conn, quantity=quantity, name_normalized=name_normalized, unit=unit
    )


def _apply_by_batch(
    conn: sqlite3.Connection, action: str, quantity: float, item_id: int
) -> List[BatchAllocation]:
    if action == inventory_service.ACTION_CONSUME:
        return inventory_service.consume(conn, quantity=quantity, item_id=item_id)
    return inventory_service.discard(conn, quantity=quantity, item_id=item_id)


def _render_events(conn: sqlite3.Connection) -> None:
    st.subheader("库存事件（所有数量变化都有记录）")
    events = inventory_service.list_events(conn, limit=100)
    if not events:
        st.info("还没有任何库存事件。")
        return
    st.dataframe(_event_rows(events), use_container_width=True, hide_index=True)
    st.caption("数量变化 = 变更后数量 − 变更前数量：入库为正，消耗与丢弃为负，修改数量记录差值。")


def _render_allergens(conn: sqlite3.Connection) -> None:
    st.subheader("我的过敏原清单")
    st.caption("清单用于判定库存批次的过敏原状态；判定由 Python 完成，不依赖 AI。")

    with st.form("add_allergen_form"):
        raw_name = st.text_input("新增过敏原（例如：花生、牛奶、大豆）", key="allergen_new")
        add_submitted = st.form_submit_button("加入清单", key="allergen_submit")
    if add_submitted:
        def _add_allergen() -> Tuple[str, Optional[list]]:
            inventory_service.add_user_allergen(conn, raw_name)
            return f"已加入过敏原清单：{raw_name.strip()}", None

        _run(_add_allergen)

    allergens = inventory_service.list_user_allergens(conn)
    if not allergens:
        st.info("清单为空：此时只标记食材「成分未声明」，不做冲突判定。")
        return

    st.dataframe(
        [
            {"ID": entry.id, "过敏原": entry.display_name, "加入时间": entry.created_at}
            for entry in allergens
        ],
        use_container_width=True,
        hide_index=True,
    )

    labels = [f"#{entry.id} {entry.display_name}" for entry in allergens]
    with st.form("remove_allergen_form"):
        chosen = st.selectbox("移除过敏原", labels, key="allergen_remove_choice")
        remove_submitted = st.form_submit_button("从清单移除", key="allergen_remove_submit")
    if remove_submitted:
        target = allergens[labels.index(chosen)]

        def _remove_allergen() -> Tuple[str, Optional[list]]:
            inventory_service.remove_user_allergen(conn, target.id)
            return f"已从清单移除：{target.display_name}", None

        _run(_remove_allergen)


st.title("数字冰箱")
st.caption("每次购买都会新增独立批次；未指定批次时按固定顺序自动分配。")
_show_flash()

connection = inventory_service.open_connection()
try:
    tab_overview, tab_add, tab_change, tab_allergens, tab_events = st.tabs(
        ["库存总览", "入库", "消耗 / 丢弃 / 修改", "过敏原清单", "事件记录"]
    )
    with tab_overview:
        _render_overview(connection)
    with tab_add:
        _render_add(connection)
    with tab_change:
        _render_change(connection)
    with tab_allergens:
        _render_allergens(connection)
    with tab_events:
        _render_events(connection)
finally:
    connection.close()
