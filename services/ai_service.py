"""AI 模型调用的服务层入口（阶段 4）。

职责：把配置与客户端串起来，给界面一个简单的入口——界面不接触 SDK，
也不需要知道 `ai/` 的内部结构（docs/architecture.md 第 4、8 节）。

本模块不含库存/保质期/过敏原逻辑，也不做菜谱生成（阶段 5）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import config
from ai.client import (
    AIAPIError,
    AIClient,
    AIConnectionError,
    AIEmptyResponseError,
    AIError,
    AIInvalidRequestError,
    AIMisconfiguredError,
    AITimeoutError,
)

__all__ = [
    "AIError",
    "AIMisconfiguredError",
    "AIInvalidRequestError",
    "AITimeoutError",
    "AIConnectionError",
    "AIAPIError",
    "AIEmptyResponseError",
    "load_env_if_present",
    "is_configured",
    "describe_configuration",
    "generate_text",
]

_env_loaded = False


def load_env_if_present() -> bool:
    """首次使用时把项目根目录的 `.env` 读进环境变量（只尝试一次）。"""
    global _env_loaded
    if _env_loaded:
        return True
    loaded = config.load_env_file()
    _env_loaded = True
    return loaded


def is_configured() -> bool:
    """是否具备发起调用所需的最小配置（API Key + 模型名）。"""
    load_env_if_present()
    return config.get_ai_settings().configured


def describe_configuration() -> str:
    """给界面看的配置摘要。**不包含** API Key 本身，只说明是否已配置。"""
    load_env_if_present()
    settings = config.get_ai_settings()
    base_url = settings.base_url or "（未设置，使用 SDK 默认地址）"
    return (
        f"模型：{settings.model or '未配置'}；"
        f"服务地址：{base_url}；"
        f"超时：{settings.timeout:g} 秒；"
        f"API Key：{'已配置' if settings.api_key else '未配置'}"
    )


def generate_text(
    messages: Sequence[Mapping[str, str]], *, client: Optional[Any] = None
) -> str:
    """调用模型并返回文本回复。

    `client` 用于测试注入假客户端；生产路径下按配置构造 `AIClient`。
    所有失败都以 `AIError` 的子类抛出，界面可以直接捕获 `AIError` 展示原因。
    """
    load_env_if_present()
    settings = config.get_ai_settings()
    if not settings.configured:
        missing = "、".join(settings.missing_items) or "AI 配置"
        raise AIMisconfiguredError(
            f"尚未配置 AI 模型（缺少 {missing}）：请参考 .env.example 在 .env 中填写。"
        )
    active_client = client if client is not None else AIClient.from_settings(settings)
    return active_client.generate(messages)
