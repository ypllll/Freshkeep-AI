"""运行参数与固定业务常量。

本模块只负责读取运行参数，不含业务逻辑。按 docs/architecture.md 第 10 节：

- 数据库路径通过环境变量 `DATABASE_PATH` 配置，默认 `data/freshkeep.db`；
- `DUE_SOON_DAYS` 是**固定业务常量**（2 天），不提供用户配置功能，因此不读取环境变量；
- 参考期限数据属于日期领域逻辑，保存在 `core/dates.py`，此处不保存副本；
- AI 配置（阶段 4）从环境变量读取，可由项目根目录的 `.env` 提供（见 `load_env_file()`）。

本模块不校验业务规则，也不发起任何网络请求；缺少 AI 配置时不会阻止应用启动。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parent

# 固定业务常量：只有基于用户提供的日期或天数计算出的期限才可用于确定性分级。
DUE_SOON_DAYS = 2

DEFAULT_DATABASE_PATH = "data/freshkeep.db"

# AI 配置（阶段 4）
DEFAULT_AI_TIMEOUT_SECONDS = 30.0
ENV_FILE_NAME = ".env"

AI_API_KEY_ENV = "OPENAI_API_KEY"
AI_BASE_URL_ENV = "OPENAI_BASE_URL"
AI_MODEL_ENV = "OPENAI_MODEL"
AI_TIMEOUT_ENV = "AI_TIMEOUT_SECONDS"


def get_database_path() -> Path:
    """返回数据库文件路径（绝对路径）。相对路径以项目根目录为基准。"""
    raw = os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip()
    if not raw:
        raw = DEFAULT_DATABASE_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_env_file(path: Path | None = None) -> bool:
    """把项目根目录的 `.env` 读进环境变量。

    返回是否真的加载了文件。`python-dotenv` 未安装或 `.env` 不存在时返回 False，
    绝不抛异常——没有 AI 配置也要能正常启动应用。
    """
    env_path = path or (PROJECT_ROOT / ENV_FILE_NAME)
    if not env_path.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(env_path, override=False))


@dataclass(frozen=True)
class AISettings:
    """AI 模型接入所需的运行参数（不含任何业务规则）。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = DEFAULT_AI_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        """是否能发起一次调用：至少要有 API Key 与模型名。"""
        return bool(self.api_key.strip() and self.model.strip())

    @property
    def missing_items(self) -> Tuple[str, ...]:
        missing = []
        if not self.api_key.strip():
            missing.append(AI_API_KEY_ENV)
        if not self.model.strip():
            missing.append(AI_MODEL_ENV)
        return tuple(missing)


def _parse_timeout(raw: object) -> float:
    """解析超时秒数；缺失或非法时回退到默认值（不因为环境变量写错而崩溃）。"""
    if raw is None:
        return DEFAULT_AI_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_AI_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_AI_TIMEOUT_SECONDS
    return value


def get_ai_settings() -> AISettings:
    """读取 AI 配置。只读环境变量，不访问网络、不校验密钥有效性。"""
    return AISettings(
        api_key=os.getenv(AI_API_KEY_ENV, "").strip(),
        base_url=os.getenv(AI_BASE_URL_ENV, "").strip(),
        model=os.getenv(AI_MODEL_ENV, "").strip(),
        timeout=_parse_timeout(os.getenv(AI_TIMEOUT_ENV)),
    )
