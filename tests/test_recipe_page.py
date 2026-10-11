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
