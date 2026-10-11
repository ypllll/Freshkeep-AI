"""数字冰箱页面回归测试（用 Streamlit 官方 AppTest 驱动真实页面脚本）。

覆盖三条只在界面上才能看到的语义：

1. 购买日期可以留空，且不会默认成今天；
2. 没有明确有效期时只显示「AI 估算」，正式有效期保持未知；
3. 日期/天数冲突的提示在页面刷新之后仍然可见（不能只出现在保存瞬间的提示条里）。

注意：本文件是 S3 修复轮新增的界面层测试；`pages/1_数字冰箱.py` 是白名单内的文件，
但 "tests/test_fridge_page.py" 本身不在原 S3 白名单里（见完成报告）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from services import inventory_service

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "1_数字冰箱.py"
TODAY = date(2026, 10, 11)


@pytest.fixture
def page(tmp_db_path, monkeypatch):
    """把页面指向一次性数据库，并返回已加载的 AppTest。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_db_path))
    app = AppTest.from_file(str(PAGE), default_timeout=60)
    app.run()
    return app


def find(collection, key):
    for widget in collection:
        if getattr(widget, "key", None) == key:
            return widget
    raise KeyError(f"未找到控件：{key}")


def add_batch(
    app,
    *,
    name: str,
    quantity: float = 1.0,
    know_purchase_date: bool = True,
    purchase_date: date | None = TODAY,
    has_expiry_date: bool = False,
    expiry_date: date | None = None,
    shelf_life_days: str | None = None,
) -> None:
    """按页面真实交互顺序录入一批食材（先切开关、再填表单、最后提交）。"""
    if know_purchase_date:
        find(app.checkbox, "add_know_purchase_date").check()
    else:
        find(app.checkbox, "add_know_purchase_date").uncheck()
    if has_expiry_date:
        find(app.checkbox, "add_has_expiry_date").check()
    else:
        find(app.checkbox, "add_has_expiry_date").uncheck()
    app.run()

    if know_purchase_date and purchase_date is not None:
        find(app.date_input, "add_purchase_date").set_value(purchase_date)
    if has_expiry_date and expiry_date is not None:
        find(app.date_input, "add_expiry_date").set_value(expiry_date)

    find(app.text_input, "add_name").set_value(name)
    find(app.number_input, "add_quantity").set_value(quantity)
    if shelf_life_days is not None:
        find(app.text_input, "add_shelf_days").set_value(shelf_life_days)
    find(app.button, "add_submit").click()
    app.run()


def detail_row(app, name: str) -> dict:
    """在「库存总览」里选中某个食材（按名称前缀匹配），返回它的批次明细行。"""
    label = next(
        option for option in find(app.selectbox, "overview_batch").options
        if option.startswith(name)
    )
    find(app.selectbox, "overview_batch").set_value(label)
    app.run()
    return app.dataframe[1].value.to_dict("records")[0]


def test_page_loads_without_exception(page) -> None:
    assert len(page.exception) == 0


def test_purchase_date_can_be_left_blank(page, tmp_db_path) -> None:
    add_batch(page, name="白菜", know_purchase_date=False)

    assert len(page.exception) == 0
    connection = inventory_service.open_connection(tmp_db_path)
    try:
        item = inventory_service.list_batches(connection)[0]
        assert item.name == "白菜"
        assert item.purchased_on is None, "留空时不得默认成今天"
        assert item.purchase_date_defaulted is False
        assert item.expiry_date is None
    finally:
        connection.close()

    row = detail_row(page, "白菜")
    assert row["购买日期"] == "未知"
    assert row["购买日期为默认值"] == "否"
    assert row["AI 估算"].startswith("AI 估算：未生成"), "缺少购买日期时不得生成估算日期"


def test_days_only_batch_shows_estimate_and_keeps_formal_expiry_unknown(
    page, tmp_db_path
) -> None:
    add_batch(page, name="酸奶", shelf_life_days="7")

    connection = inventory_service.open_connection(tmp_db_path)
    try:
        item = inventory_service.list_batches(connection)[0]
        assert item.expiry_date is None, "只填天数不得生成正式有效期"
        assert item.shelf_life_days == 7
        assert item.expiry_source == "user_days_unresolved"
    finally:
        connection.close()

    row = detail_row(page, "酸奶")
    assert row["保质期"].startswith("未知")
    assert row["期限来源"] == "用户提供天数（无正式期限）"
    assert row["AI 估算"].startswith("AI 估算：约")
    assert "食品安全保证" in row["AI 估算"]
    assert row["新鲜度"] == "未知", "AI 估算不参与新鲜度分级"


def test_conflict_notice_stays_visible_after_rerun(page) -> None:
    add_batch(
        page,
        name="罐头",
        purchase_date=date(2026, 10, 1),
        has_expiry_date=True,
        expiry_date=date(2026, 10, 5),
        shelf_life_days="30",
    )

    # 保存瞬间的提示条
    assert any("不一致" in message.value for message in page.success)

    # 任意一次刷新（模拟用户切换标签/重新打开页面）之后，提示必须仍在明细里
    page.run()
    row = detail_row(page, "罐头")
    assert "不一致" in row["提示"]
    assert "请确认哪个正确" in row["提示"]
    assert row["保质期"] == "2026-10-05", "正式记录仍采用用户填写的日期"
