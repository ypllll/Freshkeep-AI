"""OpenAI 兼容 API 的模型客户端（阶段 4）。

职责边界：

- 只负责"发一次聊天请求，把文本回复取回来"；
- 不写 Streamlit UI、不访问数据库、不打印任何日志；
- 不做 Agent 循环、工具调用、RAG、多 Provider 注册或重试策略。

错误处理：所有失败都转换成 `AIError` 的子类，便于上层区分与展示；
错误信息里**不包含** API Key、请求头或其他敏感配置。
"""

from __future__ import annotations

import config
from typing import Any, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AIError(Exception):
    """AI 调用相关错误的基类。"""


class AIMisconfiguredError(AIError):
    """配置缺失或不可用（缺 API Key / 缺模型名 / 未安装 SDK）。"""


class AIInvalidRequestError(AIError):
    """本地输入不合法（例如空 messages），请求尚未发出。"""


class AITimeoutError(AIError):
    """请求超时。"""


class AIConnectionError(AIError):
    """网络连接失败（无法到达服务地址）。"""


class AIAPIError(AIError):
    """模型服务返回了错误响应。"""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AIEmptyResponseError(AIError):
    """调用成功，但回复为空或无法读取。"""


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ALLOWED_ROLES: Tuple[str, ...] = ("system", "user", "assistant")

_TIMEOUT_EXCEPTION_NAMES = frozenset(
    {"APITimeoutError", "Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout"}
)
_CONNECTION_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "ConnectionError",
        "ConnectError",
        "NetworkError",
        "ProxyError",
        "SSLError",
    }
)

_TIMEOUT_MESSAGE = "模型请求超时，请稍后重试或增大 AI_TIMEOUT_SECONDS。"
_CONNECTION_MESSAGE = "无法连接到模型服务，请检查网络或 OPENAI_BASE_URL。"


def _classify_exception(exc: BaseException) -> AIError:
    """把 SDK 抛出的异常映射成清晰的类型。

    只使用异常的类型名与 `status_code`，**不使用** `str(exc)`，
    以免把请求头、密钥或服务端回显的敏感内容带到错误信息里。
    """
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)

    if name in _TIMEOUT_EXCEPTION_NAMES or isinstance(exc, TimeoutError):
        return AITimeoutError(_TIMEOUT_MESSAGE)
    if name in _CONNECTION_EXCEPTION_NAMES or isinstance(exc, ConnectionError):
        return AIConnectionError(_CONNECTION_MESSAGE)
    if status_code is not None:
        try:
            code: Optional[int] = int(status_code)
        except (TypeError, ValueError):
            code = None
        return AIAPIError(
            f"模型服务返回错误（HTTP {code if code is not None else '未知'}），"
            "请检查模型名、额度或服务地址。",
            status_code=code,
        )
    return AIAPIError(f"模型调用失败（{name}），请检查配置与服务状态。")


# ---------------------------------------------------------------------------
# 输入与输出处理
# ---------------------------------------------------------------------------


def _validate_messages(messages: Any) -> list:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise AIInvalidRequestError("messages 必须是消息列表。")
    if len(messages) == 0:
        raise AIInvalidRequestError("messages 不能为空，至少需要一条消息。")

    payload = []
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping):
            raise AIInvalidRequestError(f"第 {index + 1} 条消息必须是包含 role 与 content 的字典。")
        role = str(item.get("role", "")).strip()
        content = item.get("content", "")
        if role not in ALLOWED_ROLES:
            raise AIInvalidRequestError(
                f"第 {index + 1} 条消息的 role 不合法（只能是 {'、'.join(ALLOWED_ROLES)}）。"
            )
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidRequestError(f"第 {index + 1} 条消息的 content 不能为空。")
        payload.append({"role": role, "content": content.strip()})
    return payload


def _extract_text(response: Any) -> str:
    """从 SDK 响应里取出文本回复；取不到就抛 `AIEmptyResponseError`。"""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise AIEmptyResponseError("模型没有返回任何候选结果。")

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)

    if isinstance(content, list):  # 部分兼容实现会返回内容分段列表
        parts = []
        for part in content:
            if isinstance(part, Mapping):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(getattr(part, "text", "")))
        content = "".join(parts)

    text = content.strip() if isinstance(content, str) else ""
    if not text:
        raise AIEmptyResponseError("模型返回了空回复，请重试或检查模型配置。")
    return text


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class AIClient:
    """最小可用的聊天客户端：一次调用，返回纯文本。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "",
        timeout: float = config.DEFAULT_AI_TIMEOUT_SECONDS,
        sdk: Any = None,
    ) -> None:
        self._model = (model or "").strip()
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").strip()
        self._timeout = timeout
        self._sdk = sdk

    @classmethod
    def from_settings(cls, settings: Any, *, sdk: Any = None) -> "AIClient":
        """用 `config.AISettings` 构造客户端（鸭子类型，不依赖具体类型）。"""
        return cls(
            model=getattr(settings, "model", ""),
            api_key=getattr(settings, "api_key", ""),
            base_url=getattr(settings, "base_url", ""),
            timeout=getattr(settings, "timeout", config.DEFAULT_AI_TIMEOUT_SECONDS),
            sdk=sdk,
        )

    def _resolve_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            import openai  # 延迟导入：未安装 openai 也不影响应用启动
        except ImportError as exc:
            raise AIMisconfiguredError(
                "未安装 openai 依赖，请先执行 pip install -r requirements.txt。"
            ) from exc
        return openai

    def generate(self, messages: Sequence[Mapping[str, str]]) -> str:
        """发送一次聊天请求并返回文本回复。"""
        payload = _validate_messages(messages)
        if not self._api_key:
            raise AIMisconfiguredError(
                f"缺少 {config.AI_API_KEY_ENV}：请在 .env 或环境变量里配置 API Key。"
            )
        if not self._model:
            raise AIMisconfiguredError(
                f"缺少 {config.AI_MODEL_ENV}：请在 .env 或环境变量里配置模型名。"
            )

        sdk = self._resolve_sdk()
        client = sdk.OpenAI(
            api_key=self._api_key,
            base_url=self._base_url or None,
            timeout=self._timeout,
        )
        try:
            response = client.chat.completions.create(model=self._model, messages=payload)
        except Exception as exc:  # SDK 会抛出多种内部异常，这里统一映射成清晰类型
            raise _classify_exception(exc) from exc
        return _extract_text(response)
