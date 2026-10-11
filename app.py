"""FreshKeep AI —— Streamlit 入口。

职责边界（docs/architecture.md 第 4 节）：UI 只做输入与展示，不含业务规则，
也不直接访问存储层。数据库初始化由 `storage.db.initialize_database()` 提供，
第一个调用者是阶段 2 的 `services` 层。
"""

from __future__ import annotations

import streamlit as st

import config

st.set_page_config(page_title="FreshKeep AI", layout="centered")

st.title("FreshKeep AI")
st.caption("数字化管理冰箱里的食材，并根据真实库存规划菜谱，减少遗忘与浪费。")

st.subheader("当前进度")
st.write(
    "**阶段 1：骨架与持久化底座** —— 目录骨架、运行配置与 SQLite 版本化迁移机制已就绪。"
)

st.markdown(
    """
- 数字冰箱（批次入库 / 查看 / 消耗 / 丢弃）：阶段 2
- 保质期与过敏原规则：阶段 3
- AI 连接层与输出校验：阶段 4
- 食谱规划：阶段 5
- 确认消耗与演示加固：阶段 6
"""
)

with st.expander("运行参数（只读）"):
    st.write(f"数据库路径：`{config.get_database_path()}`")
    st.write(
        f"临期阈值 `DUE_SOON_DAYS`：{config.DUE_SOON_DAYS}（固定业务常量，不提供配置）"
    )

st.info(
    "本阶段只提供界面骨架，尚未读写任何库存数据：库存操作会在后续阶段经服务层实现，"
    "不会由界面直接访问数据库。"
)
