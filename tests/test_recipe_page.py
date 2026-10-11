"""食谱规划页面测试（S5）：用 Streamlit AppTest 真实执行页面脚本。

模型调用被替换为假函数；库存使用一次性数据库。不发真实网络请求。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai.client import AIAPIError
from services import ai_service, inventory_service

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "2_食谱规划.py"
TODAY = date(2026, 10, 11)

RECIPE_JSON = json.dumps(
    {
        "recipes": [
            {
                "title": "番茄炒蛋",
                "servings": 2,
                "rationale": "用掉冰箱里的番茄。",
                "ingredients": [
                    {"name": "番茄", "quantity": 2, "unit": "个"},
                    {"name": "松露", "quantity": 1, "unit": "块"},
                ],
                "steps": ["番茄切块", "下锅炒"],
            }
        ]
    },
    ensure_ascii=False,
)


def find(collection, key):
    for widget in collection:
        if getattr(widget, "key", None) == key:
            return widget
    raise KeyError(f"未找到控件：{key}")


@pytest.fixture
def page(tmp_db_path, monkeypatch):
    """指向一次性数据库、预置一条库存，并固定模型配置。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_db_path))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setattr(ai_service, "_env_loaded", True)  # 不读磁盘上的真实 .env

    conn = inventory_service.open_connection(tmp_db_path)
    try:
        inventory_service.add_batch(
            conn,
            name="番茄",
            category="蔬菜",
            quantity=5,
            unit="个",
            storage="冷藏",
            purchased_on="2026-10-01",
            today=TODAY,
        )
    finally:
        conn.close()

    app = AppTest.from_file(str(PAGE), default_timeout=60)
    app.run()
    return app


def generate(app) -> None:
    find(app.text_area, "recipe_request").set_value("想吃清淡的家常菜")
    find(app.button, "recipe_generate").click()
    app.run()


def test_page_loads_without_exception(page) -> None:
    assert len(page.exception) == 0
    assert any("食谱规划" in title.value for title in page.title)


def test_unconfigured_model_shows_hint_and_generates_nothing(page, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    page.run()

    assert any("尚未配置 AI 模型" in error.value for error in page.error)
    assert not any("已生成" in success.value for success in page.success)


def test_successful_generation_shows_recipe_and_gaps(page, monkeypatch) -> None:
    calls: list = []

    def fake_generate(messages, client=None):
        calls.append(messages)
        return RECIPE_JSON

    monkeypatch.setattr(ai_service, "generate_text", fake_generate)

    generate(page)

    assert len(page.exception) == 0
    assert len(calls) == 1
    assert any("已生成" in success.value for success in page.success)
    assert any("番茄炒蛋" in block.value for block in page.markdown)
    assert any("用掉冰箱里的番茄" in block.value for block in page.markdown)

    rows = page.dataframe[0].value.to_dict("records")
    assert rows[0]["食材"] == "番茄"
    assert rows[0]["需要"] == "2"
    assert rows[0]["现有"] == "5"
    assert rows[0]["还缺"] == "0"
    assert rows[1]["状态"] == "库存中未找到"
    assert rows[1]["现有"] == "—"
    assert rows[1]["还缺"] == "—"

    # 无过敏原声明时给出"未做个性化检查"的说明
    assert any("未做个性化过敏原检查" in caption.value for caption in page.caption)


def test_model_failure_is_shown_as_error_not_success(page, monkeypatch) -> None:
    def boom(messages, client=None):
        raise AIAPIError("模型服务返回错误（HTTP 500），请检查模型名、额度或服务地址。", status_code=500)

    monkeypatch.setattr(ai_service, "generate_text", boom)

    generate(page)

    assert any("生成失败" in error.value for error in page.error)
    assert not any("已生成" in success.value for success in page.success)


def test_invalid_output_is_reported_without_fake_success(page, monkeypatch) -> None:
    monkeypatch.setattr(ai_service, "generate_text", lambda messages, client=None: "不是 JSON")

    generate(page)

    assert any("invalid_output" in error.value for error in page.error)
    assert not any("已生成" in success.value for success in page.success)


def test_result_survives_rerun_without_calling_the_model_again(page, monkeypatch) -> None:
    calls: list = []

    def fake_generate(messages, client=None):
        calls.append(messages)
        return RECIPE_JSON

    monkeypatch.setattr(ai_service, "generate_text", fake_generate)

    generate(page)
    page.run()  # 模拟用户改动了别的控件 / 刷新页面

    assert len(calls) == 1, "结果应保存在会话里，不应因为刷新而重复调用模型"
    assert any("番茄炒蛋" in block.value for block in page.markdown)


# --- S6：确认实际消耗 --------------------------------------------------------


@pytest.fixture
def page_factory(tmp_db_path, monkeypatch):
    """按需准备库存与过敏原，然后加载页面。"""

    def build(*, items=(), allergens=()):
        monkeypatch.setenv("DATABASE_PATH", str(tmp_db_path))
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        monkeypatch.setenv("OPENAI_MODEL", "fake-model")
        monkeypatch.setattr(ai_service, "_env_loaded", True)
        conn = inventory_service.open_connection(tmp_db_path)
        try:
            for name, quantity, unit, declared in items:
                inventory_service.add_batch(
                    conn,
                    name=name,
                    category="蔬菜",
                    quantity=quantity,
                    unit=unit,
                    storage="冷藏",
                    purchased_on="2026-10-01",
                    declared_allergens=list(declared) if declared else None,
                    allergen_declared=declared is not None,
                    today=TODAY,
                )
            for allergen in allergens:
                inventory_service.add_user_allergen(conn, allergen)
        finally:
            conn.close()
        app = AppTest.from_file(str(PAGE), default_timeout=60)
        app.run()
        return app

    return build


def recipe_json(ingredients, title="番茄炒蛋") -> str:
    return json.dumps(
        {
            "recipes": [
                {
                    "title": title,
                    "servings": 2,
                    "rationale": "用掉冰箱里的食材。",
                    "ingredients": ingredients,
                    "steps": ["切", "炒"],
                }
            ]
        },
        ensure_ascii=False,
    )


def quantity_of(path, name: str) -> float:
    conn = inventory_service.open_connection(path)
    try:
        for item in inventory_service.list_batches(conn):
            if item.name == name:
                return item.quantity
    finally:
        conn.close()
    raise AssertionError(f"未找到食材：{name}")


def test_confirmation_form_deducts_after_explicit_check(page_factory, tmp_db_path, monkeypatch) -> None:
    app = page_factory(items=[("番茄", 5, "个", ["大豆"])], allergens=["花生"])
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json([{"name": "番茄", "quantity": 2, "unit": "个"}]),
    )
    find(app.text_area, "recipe_request").set_value("想吃番茄")
    find(app.button, "recipe_generate").click()
    app.run()

    assert any("确认实际消耗" in block.value for block in app.subheader)
    find(app.checkbox, "confirm_use_0").check()
    find(app.button, "confirm_submit").click()
    app.run()

    assert len(app.exception) == 0
    assert quantity_of(tmp_db_path, "番茄") == pytest.approx(3.0)
    assert any("已按你的确认扣减库存" in success.value for success in app.success)

    conn = inventory_service.open_connection(tmp_db_path)
    try:
        events = [
            event
            for event in inventory_service.list_events(conn, limit=100)
            if event.action == "consume"
        ]
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0].source == "recipe_plan"
    assert events[0].plan_id is not None


def test_confirmation_without_checking_any_row_changes_nothing(page_factory, tmp_db_path, monkeypatch) -> None:
    app = page_factory(items=[("番茄", 5, "个", ["大豆"])], allergens=["花生"])
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json([{"name": "番茄", "quantity": 2, "unit": "个"}]),
    )
    find(app.text_area, "recipe_request").set_value("想吃番茄")
    find(app.button, "recipe_generate").click()
    app.run()

    find(app.button, "confirm_submit").click()
    app.run()

    assert any("请至少确认一种食材" in error.value for error in app.error)
    assert quantity_of(tmp_db_path, "番茄") == pytest.approx(5.0)


def test_applied_plan_cannot_be_submitted_twice_in_the_page(page_factory, tmp_db_path, monkeypatch) -> None:
    app = page_factory(items=[("番茄", 5, "个", ["大豆"])], allergens=["花生"])
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json([{"name": "番茄", "quantity": 2, "unit": "个"}]),
    )
    find(app.text_area, "recipe_request").set_value("想吃番茄")
    find(app.button, "recipe_generate").click()
    app.run()

    find(app.checkbox, "confirm_use_0").check()
    find(app.button, "confirm_submit").click()
    app.run()
    assert quantity_of(tmp_db_path, "番茄") == pytest.approx(3.0)

    app.run()  # 再刷新一次
    assert any("已应用过" in success.value for success in app.success)
    submit_buttons = [w for w in app.button if getattr(w, "key", None) == "confirm_submit"]
    assert submit_buttons == [], "已应用的规划不应再提供确认按钮"
    assert quantity_of(tmp_db_path, "番茄") == pytest.approx(3.0)


def test_conflict_recipe_offers_no_confirmation_entry(page_factory, monkeypatch) -> None:
    app = page_factory(
        items=[("花生酱", 2, "瓶", ["花生"])],
        allergens=["花生"],
    )
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json(
            [{"name": "花生酱", "quantity": 1, "unit": "瓶"}], title="花生酱拌面"
        ),
    )
    find(app.text_area, "recipe_request").set_value("想吃拌面")
    find(app.button, "recipe_generate").click()
    app.run()

    assert any("已排除" in block.value for block in app.subheader)
    assert any(
        "没有可确认的菜谱" in block.value for block in list(app.info) + list(app.warning)
    )
    assert [w for w in app.button if getattr(w, "key", None) == "confirm_submit"] == []


def test_unverifiable_recipe_requires_acknowledgement_in_the_page(
    page_factory, tmp_db_path, monkeypatch
) -> None:
    app = page_factory(items=[("土豆", 3, "个", None)], allergens=["花生"])
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json([{"name": "土豆", "quantity": 2, "unit": "个"}]),
    )
    find(app.text_area, "recipe_request").set_value("想吃土豆")
    find(app.button, "recipe_generate").click()
    app.run()

    assert any("成分无法核实" in warning.value for warning in app.warning)
    find(app.checkbox, "confirm_use_0").check()
    find(app.button, "confirm_submit").click()
    app.run()

    assert any("需要你明确确认" in error.value for error in app.error)
    assert quantity_of(tmp_db_path, "土豆") == pytest.approx(3.0)

    find(app.checkbox, "confirm_ack").check()
    find(app.checkbox, "confirm_use_0").check()
    find(app.button, "confirm_submit").click()
    app.run()

    assert quantity_of(tmp_db_path, "土豆") == pytest.approx(1.0)
    assert any("已按你的确认扣减库存" in success.value for success in app.success)


def test_unit_mismatch_is_shown_as_not_deductible(page_factory, tmp_db_path, monkeypatch) -> None:
    app = page_factory(items=[("牛奶", 1, "L", ["牛奶"])], allergens=["花生"])
    monkeypatch.setattr(
        ai_service,
        "generate_text",
        lambda messages, client=None: recipe_json([{"name": "牛奶", "quantity": 200, "unit": "ml"}]),
    )
    find(app.text_area, "recipe_request").set_value("想喝牛奶")
    find(app.button, "recipe_generate").click()
    app.run()

    rows = app.dataframe[-1].value.to_dict("records")
    mismatched = [row for row in rows if row["食材"] == "牛奶"]
    assert mismatched and mismatched[0]["可扣减"] == "否"
    assert "单位" in mismatched[0]["说明"]

    # 即使误勾选也扣不动：那一行的输入控件被禁用，服务层同样会拒绝
    assert quantity_of(tmp_db_path, "牛奶") == pytest.approx(1.0)
