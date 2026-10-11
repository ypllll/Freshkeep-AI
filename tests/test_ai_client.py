"""AI 接入层测试（阶段 4）：配置读取、请求参数、文本提取与错误映射。

全部使用假 SDK / 假客户端，不依赖真实 API Key，也不发起任何网络请求。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from ai import client as ai_client
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
from services import ai_service

SECRET = "sk-fake-do-not-leak-0123456789"
MESSAGES = [{"role": "user", "content": "你好"}]


# --- 假 SDK -----------------------------------------------------------------


class APITimeoutError(Exception):  # noqa: N801 （与 SDK 同名，验证按类型名映射）
    """与 openai.APITimeoutError 同名的假异常。"""


class APIConnectionError(Exception):  # noqa: N801
    """与 openai.APIConnectionError 同名的假异常。"""


class FakeAPIStatusError(Exception):
    """模拟带 status_code 的服务端错误。"""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


def make_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeSDK:
    """模拟 openai 模块：只暴露 OpenAI(...) 入口并记录收到的参数。"""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.record: dict = {}

    def OpenAI(self, **kwargs):  # noqa: N802 （模拟 SDK 的类名）
        self.record["client_kwargs"] = kwargs
        sdk = self

        class _Completions:
            def create(self, **create_kwargs):
                sdk.record["create_kwargs"] = create_kwargs
                if sdk.error is not None:
                    raise sdk.error
                return sdk.response

        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def build_client(**overrides) -> tuple:
    sdk = FakeSDK(
        response=overrides.pop("response", make_response("连接正常")),
        error=overrides.pop("error", None),
    )
    params = {"model": "fake-model", "api_key": "fake-key", "base_url": "https://example.invalid/v1"}
    params.update(overrides)
    return AIClient(sdk=sdk, **params), sdk


# --- 1. 配置读取 ------------------------------------------------------------


def test_get_ai_settings_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "  key-from-env  ")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_MODEL", "model-from-env")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "12.5")

    settings = config.get_ai_settings()

    assert settings.api_key == "key-from-env"
    assert settings.base_url == "https://example.invalid/v1"
    assert settings.model == "model-from-env"
    assert settings.timeout == pytest.approx(12.5)
    assert settings.configured is True
    assert settings.missing_items == ()


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "inf", "nan", "  "])
def test_invalid_timeout_falls_back_to_default(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", raw)

    settings = config.get_ai_settings()

    assert settings.timeout == config.DEFAULT_AI_TIMEOUT_SECONDS


def test_missing_items_lists_what_is_needed(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert config.get_ai_settings().missing_items == ("OPENAI_API_KEY", "OPENAI_MODEL")

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert config.get_ai_settings().missing_items == ("OPENAI_MODEL",)
    assert config.get_ai_settings().configured is False


def test_load_env_file_returns_false_when_missing(tmp_path: Path) -> None:
    assert config.load_env_file(tmp_path / "no-such-file.env") is False


def test_load_env_file_reads_values(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("dotenv")
    env_file = tmp_path / "fake.env"
    env_file.write_text(
        "OPENAI_API_KEY=fake-from-file\nOPENAI_MODEL=fake-model-from-file\n", encoding="utf-8"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    try:
        assert config.load_env_file(env_file) is True
        settings = config.get_ai_settings()
        assert settings.api_key == "fake-from-file"
        assert settings.model == "fake-model-from-file"
    finally:
        # load_dotenv 直接写 os.environ，monkeypatch 不会自动清理
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_MODEL", None)


# --- 2. 缺少配置 ------------------------------------------------------------


def test_missing_api_key_raises_misconfigured() -> None:
    client, sdk = build_client(api_key="")

    with pytest.raises(AIMisconfiguredError) as excinfo:
        client.generate(MESSAGES)

    assert "OPENAI_API_KEY" in str(excinfo.value)
    assert "create_kwargs" not in sdk.record, "配置不全时不得发起请求"


def test_missing_model_raises_misconfigured() -> None:
    client, _ = build_client(model="")

    with pytest.raises(AIMisconfiguredError) as excinfo:
        client.generate(MESSAGES)

    assert "OPENAI_MODEL" in str(excinfo.value)


def test_service_reports_missing_configuration(monkeypatch) -> None:
    # 让"未配置"用例不受磁盘上可能存在的真实 .env 影响
    monkeypatch.setattr(ai_service, "_env_loaded", True)
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(AIMisconfiguredError) as excinfo:
        ai_service.generate_text(MESSAGES)

    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message and "OPENAI_MODEL" in message
    assert ai_service.is_configured() is False


# --- 3. 请求参数 ------------------------------------------------------------


def test_client_passes_model_and_messages_to_sdk() -> None:
    client, sdk = build_client(timeout=7.5)

    assert client.generate([{"role": "user", "content": " 你好 "}]) == "连接正常"

    assert sdk.record["create_kwargs"] == {
        "model": "fake-model",
        "messages": [{"role": "user", "content": "你好"}],
    }
    assert sdk.record["client_kwargs"]["api_key"] == "fake-key"
    assert sdk.record["client_kwargs"]["base_url"] == "https://example.invalid/v1"
    assert sdk.record["client_kwargs"]["timeout"] == pytest.approx(7.5)


def test_from_settings_forwards_configuration() -> None:
    settings = config.AISettings(
        api_key="k", base_url="https://example.invalid/v1", model="m", timeout=9
    )
    client = AIClient.from_settings(settings)

    assert client._model == "m"  # noqa: SLF001 （直接断言内部装配结果）
    assert client._api_key == "k"  # noqa: SLF001
    assert client._timeout == 9  # noqa: SLF001


# --- 4/5. 返回内容 ----------------------------------------------------------


def test_successful_call_returns_text() -> None:
    client, _ = build_client(response=make_response("  连接正常  "))
    assert client.generate(MESSAGES) == "连接正常"


def test_content_parts_are_joined() -> None:
    response = make_response([{"text": "连接"}, SimpleNamespace(text="正常")])
    client, _ = build_client(response=response)
    assert client.generate(MESSAGES) == "连接正常"


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=None),
        make_response(None),
        make_response("   "),
    ],
)
def test_empty_response_is_reported(response) -> None:
    client, _ = build_client(response=response)

    with pytest.raises(AIEmptyResponseError):
        client.generate(MESSAGES)


# --- 6/7. 错误映射 ----------------------------------------------------------


@pytest.mark.parametrize("error", [APITimeoutError("slow"), TimeoutError("slow")])
def test_timeout_is_mapped(error: Exception) -> None:
    client, _ = build_client(error=error)

    with pytest.raises(AITimeoutError):
        client.generate(MESSAGES)


@pytest.mark.parametrize("error", [APIConnectionError("boom"), ConnectionError("boom")])
def test_connection_failure_is_mapped(error: Exception) -> None:
    client, _ = build_client(error=error)

    with pytest.raises(AIConnectionError):
        client.generate(MESSAGES)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_status_error_is_mapped_with_status_code(status: int) -> None:
    client, _ = build_client(error=FakeAPIStatusError("server said no", status_code=status))

    with pytest.raises(AIAPIError) as excinfo:
        client.generate(MESSAGES)

    assert excinfo.value.status_code == status
    assert str(status) in str(excinfo.value)


def test_unknown_error_is_mapped_by_type_name_only() -> None:
    client, _ = build_client(error=RuntimeError(f"unexpected with {SECRET}"))

    with pytest.raises(AIAPIError) as excinfo:
        client.generate(MESSAGES)

    assert "RuntimeError" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def _real_http_module():
    """openai 3.x 依赖的 HTTP 库模块名可能是 httpx 或 httpx2。"""
    for name in ("httpx2", "httpx"):
        try:
            import importlib

            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("未安装 openai 的 HTTP 依赖，无法构造真实 SDK 异常")


def _real_request():
    return _real_http_module().Request("POST", "https://example.invalid/v1/chat/completions")


def test_real_sdk_exceptions_are_classified() -> None:
    """用真实的 openai 异常类验证映射（不发任何请求）。"""
    openai = pytest.importorskip("openai")
    request = _real_request()

    timeout_client, _ = build_client(error=openai.APITimeoutError(request=request))
    with pytest.raises(AITimeoutError):
        timeout_client.generate(MESSAGES)

    connection_client, _ = build_client(error=openai.APIConnectionError(request=request))
    with pytest.raises(AIConnectionError):
        connection_client.generate(MESSAGES)

    http = _real_http_module()
    response = http.Response(
        401, request=request, json={"error": {"message": f"invalid key {SECRET}"}}
    )
    status_client, _ = build_client(
        api_key=SECRET, error=openai.APIStatusError("invalid key", response=response, body=None)
    )
    with pytest.raises(AIAPIError) as excinfo:
        status_client.generate(MESSAGES)
    assert excinfo.value.status_code == 401
    assert SECRET not in str(excinfo.value)


def test_original_exception_is_chained_for_debugging() -> None:
    original = FakeAPIStatusError("server said no", status_code=500)
    client, _ = build_client(error=original)

    with pytest.raises(AIAPIError) as excinfo:
        client.generate(MESSAGES)

    assert excinfo.value.__cause__ is original


# --- 8. 不泄露密钥 + 不打印 --------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        FakeAPIStatusError(f"invalid key: {SECRET}", status_code=401),
        APIConnectionError(f"failed with {SECRET}"),
        APITimeoutError(f"slow with {SECRET}"),
        RuntimeError(f"weird with {SECRET}"),
    ],
)
def test_error_messages_never_contain_the_api_key(error: Exception) -> None:
    client, _ = build_client(api_key=SECRET, error=error)

    with pytest.raises(AIError) as excinfo:
        client.generate(MESSAGES)

    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


def test_client_does_not_print_anything(capsys) -> None:
    client, _ = build_client(error=FakeAPIStatusError("server said no", status_code=500))
    with pytest.raises(AIError):
        client.generate(MESSAGES)
    assert capsys.readouterr().out == ""


# --- 输入校验 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_messages",
    [
        [],
        "你好",
        None,
        [{"role": "user"}],
        [{"role": "user", "content": "   "}],
        [{"role": "system", "content": "ok"}, {"role": "tool", "content": "x"}],
        [None],
    ],
)
def test_invalid_messages_are_rejected_without_calling_sdk(bad_messages) -> None:
    client, sdk = build_client()

    with pytest.raises(AIInvalidRequestError):
        client.generate(bad_messages)

    assert "create_kwargs" not in sdk.record, "输入非法时不得发起请求"


# --- 服务层入口 -------------------------------------------------------------


def test_service_uses_injected_client(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    captured = {}

    class FakeClient:
        def generate(self, messages):
            captured["messages"] = messages
            return "来自假客户端的回复"

    assert ai_service.generate_text(MESSAGES, client=FakeClient()) == "来自假客户端的回复"
    assert captured["messages"] == MESSAGES


def test_service_description_hides_key(monkeypatch) -> None:
    pytest.importorskip("dotenv")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    description = ai_service.describe_configuration()

    assert SECRET not in description
    assert "API Key：已配置" in description
    assert "fake-model" in description
    assert ai_service.is_configured() is True


def test_ai_client_has_no_ui_or_database_dependencies() -> None:
    source = (Path(__file__).resolve().parents[1] / "ai" / "client.py").read_text(encoding="utf-8")

    assert "import streamlit" not in source
    assert "import sqlite3" not in source
    assert "import storage" not in source


def test_check_script_stops_without_configuration(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ai_service, "_env_loaded", True)
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    import importlib
    import sys

    if str(config.PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(config.PROJECT_ROOT))
    script = importlib.import_module("scripts.check_ai_connection")

    exit_code = script.main(["check_ai_connection.py"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "未发起任何请求" in output
    assert SECRET not in output
