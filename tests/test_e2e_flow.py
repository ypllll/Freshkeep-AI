"""S6 端到端：录入库存 → 生成规划（假模型）→ 确认实际消耗 → 校验库存/事件/状态。

另外验证 `scripts/seed_demo.py` 的演示数据导入与 --reset 行为（只作用于临时数据库）。
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import date

import pytest

from ai.client import AIAPIError
from services import ai_service, inventory_service, recipe_service

TODAY = date(2026, 10, 11)
PURCHASED_ON = "2026-10-01"

PLAN_JSON = json.dumps(
    {
        "recipes": [
            {
                "title": "番茄炒蛋",
                "servings": 2,
                "rationale": "用掉手边的番茄和鸡蛋。",
                "ingredients": [
                    {"name": "番茄", "quantity": 2, "unit": "个"},
                    {"name": "鸡蛋", "quantity": 3, "unit": "个"},
                ],
                "steps": ["番茄切块", "炒蛋", "同炒调味"],
            }
        ]
    },
    ensure_ascii=False,
)


class FakeModelClient:
    def __init__(self, *, text: str = PLAN_JSON, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list = []

    def generate(self, messages):
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture
def db_conn(tmp_db_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setattr(ai_service, "_env_loaded", True)
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


def consume_events(conn):
    return [
        event
        for event in inventory_service.list_events(conn, limit=1000)
        if event.action == "consume"
    ]


def test_full_loop_plan_then_confirm(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")

    plan = recipe_service.plan_recipes(
        db_conn, user_request="想吃番茄炒蛋", servings=2, client=FakeModelClient()
    )
    assert len(plan.recommended) == 1
    recipe = plan.recommended[0]

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0, 1: 3.0}
    )

    assert inventory_service.get_batch(db_conn, tomato).quantity == pytest.approx(3.0)
    assert inventory_service.get_batch(db_conn, egg).quantity == pytest.approx(3.0)
    assert result.total_deducted == pytest.approx(5.0)

    events = consume_events(db_conn)
    assert len(events) == 2
    assert {event.source for event in events} == {"recipe_plan"}
    assert {event.plan_id for event in events} == {plan.plan_id}
    assert sum(event.qty_delta for event in events) == pytest.approx(-5.0)

    remaining = {
        item.name: item.remaining_quantity for item in result.ingredients
    }
    assert remaining == {"番茄": pytest.approx(3.0), "鸡蛋": pytest.approx(3.0)}


def test_full_loop_partial_confirmation(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    egg = add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    plan = recipe_service.plan_recipes(
        db_conn, user_request="随便做点", servings=2, client=FakeModelClient()
    )
    recipe = plan.recommended[0]

    result = recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0, 1: 0.0}
    )

    assert inventory_service.get_batch(db_conn, tomato).quantity == pytest.approx(4.0)
    assert inventory_service.get_batch(db_conn, egg).quantity == pytest.approx(6.0)
    assert len(consume_events(db_conn)) == 1
    assert result.skipped_ingredients == ("鸡蛋",)


def test_stock_change_between_plan_and_confirm_is_detected(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)
    add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    plan = recipe_service.plan_recipes(
        db_conn, user_request="想吃番茄炒蛋", servings=2, client=FakeModelClient()
    )
    recipe = plan.recommended[0]

    # 用户在确认前自己去冰箱里用掉了 4 个番茄
    inventory_service.consume(db_conn, quantity=4, item_id=tomato, note="手动用掉")

    with pytest.raises(recipe_service.RecipeStockShortageError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 2.0, 1: 3.0}
        )

    assert inventory_service.get_batch(db_conn, tomato).quantity == pytest.approx(1.0)
    # 只有那次手动消耗；确认被整次拒绝，没有新的消费事件
    manual_events = [event for event in consume_events(db_conn) if event.source == "manual"]
    assert len(manual_events) == 1
    assert [event for event in consume_events(db_conn) if event.source == "recipe_plan"] == []


def test_model_failure_leaves_inventory_untouched(db_conn) -> None:
    tomato = add_item(db_conn, "番茄", 5)

    with pytest.raises(recipe_service.RecipeAIError):
        recipe_service.plan_recipes(
            db_conn,
            user_request="想吃番茄炒蛋",
            servings=2,
            client=FakeModelClient(error=AIAPIError("模型服务返回错误（HTTP 500）", status_code=500)),
        )

    assert inventory_service.get_batch(db_conn, tomato).quantity == pytest.approx(5.0)
    assert consume_events(db_conn) == []


def test_reapplying_the_same_plan_in_one_session_is_blocked(db_conn) -> None:
    add_item(db_conn, "番茄", 5)
    add_item(db_conn, "鸡蛋", 6, category="蛋奶")
    plan = recipe_service.plan_recipes(
        db_conn, user_request="想吃番茄炒蛋", servings=2, client=FakeModelClient()
    )
    recipe = plan.recommended[0]
    applied_ids: list = []

    recipe_service.apply_plan(
        db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0},
        applied_plan_ids=applied_ids,
    )
    applied_ids.append(plan.plan_id)  # 页面在成功后记录，模拟会话状态

    with pytest.raises(recipe_service.RecipePlanAlreadyAppliedError):
        recipe_service.apply_plan(
            db_conn, plan=plan, recipe=recipe, confirmed_quantities={0: 1.0},
            applied_plan_ids=applied_ids,
        )

    assert len(consume_events(db_conn)) == 1


def test_seed_demo_script_imports_and_resets_a_temp_database(tmp_db_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_db_path))
    if str(__import__("config").PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(__import__("config").PROJECT_ROOT))
    script = importlib.import_module("scripts.seed_demo")

    assert script.main([]) == 0
    first = capsys.readouterr().out
    assert "已写入" in first
    assert tmp_db_path.exists()

    conn = inventory_service.open_connection(tmp_db_path)
    try:
        seeded = len(inventory_service.list_batches(conn))
    finally:
        conn.close()
    assert seeded == len(script.DEMO_BATCHES)

    # 再执行一次（不带 --reset）是追加，不清空
    assert script.main([]) == 0
    conn = inventory_service.open_connection(tmp_db_path)
    try:
        assert len(inventory_service.list_batches(conn)) == seeded * 2
    finally:
        conn.close()

    # --reset 只删除演示库文件并重建
    assert script.main(["--reset"]) == 0
    out = capsys.readouterr().out
    assert "删除演示数据库" in out
    conn = inventory_service.open_connection(tmp_db_path)
    try:
        assert len(inventory_service.list_batches(conn)) == seeded
    finally:
        conn.close()


@pytest.mark.parametrize("script", ["app.py", "pages/1_数字冰箱.py", "pages/2_食谱规划.py"])
def test_entry_scripts_compile(script: str) -> None:
    """语法级守卫：AppTest 不会因为脚本编译失败而报 exception，必须单独检查。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / script).read_text(encoding="utf-8")
    compile(source, script, "exec")
