"""FreshKeep AI —— Streamlit 入口（首页）。

职责边界（docs/architecture.md 第 4 节）：UI 只做输入与展示，不含业务规则，
也不直接访问存储层。本页只显示功能概览、导航提示与只读运行参数。
"""

from __future__ import annotations

import streamlit as st

import config
from services import ai_service

st.set_page_config(page_title="FreshKeep AI", layout="centered")

st.title("FreshKeep AI")
st.caption("数字化管理冰箱里的食材，并根据真实库存规划菜谱，减少遗忘与浪费。")

st.subheader("功能概览")
st.write(
    "**FreshKeep AI**：管理冰箱食材库存，并根据真实库存生成菜谱；"
    "在你确认实际用量后，按批次扣减库存并留下消费记录。"
)

st.markdown(
    """
- **数字冰箱**（侧边栏）：批次入库、查看汇总与明细、消耗 / 丢弃、修改数量、事件记录
- **保质期**：用户填写的有效期原样保存；只填天数不生成正式期限；没有明确期限时给出标注清楚的「AI 估算」
- **过敏原**：维护个人过敏原清单，逐批次判定「已确认冲突 / 待核实 / 未发现已知冲突」
- **食谱规划**（侧边栏）：按需求调用模型生成菜谱候选，再由程序计算库存缺口与过敏原结论
- **确认消耗**：在食谱规划页面逐项确认实际用量，按现有分配规则跨批次扣减并留下消费事件

尚未实现：份数重规划、菜谱历史持久化、跨会话的规划级防重复。
"""
)

with st.expander("运行参数（只读）"):
    st.write(f"数据库路径：`{config.get_database_path()}`")
    st.write(
        f"临期阈值 `DUE_SOON_DAYS`：{config.DUE_SOON_DAYS}（固定业务常量，不提供配置）"
    )
    st.write("AI 模型配置：" + ai_service.describe_configuration())

st.info(
    "界面不直接访问数据库或模型接口：所有库存与菜谱操作都经服务层完成。"
    "生成菜谱候选只读取库存、不扣减；只有你在食谱规划页面确认实际用量后，才会按批次扣减并写入消费事件。"
)
