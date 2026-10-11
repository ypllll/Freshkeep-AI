"""手动检查 AI 配置与联通性（不在 pytest 中运行）。

用法：

    python scripts/check_ai_connection.py
    python scripts/check_ai_connection.py "用一句话打个招呼"

需要先在项目根目录的 `.env` 里配置 `OPENAI_API_KEY` / `OPENAI_MODEL`
（可参考 `.env.example`）。没有配置时脚本只会打印提示，不会发起任何请求。
输出里不会包含 API Key。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ai_service  # noqa: E402  （需先修正 sys.path）

DEFAULT_PROMPT = "请只回复四个字：连接正常"


def main(argv: list) -> int:
    print("当前配置：" + ai_service.describe_configuration())

    if not ai_service.is_configured():
        print("未配置 AI 模型，未发起任何请求。请参考 .env.example 完成配置后重试。")
        return 1

    prompt = argv[1] if len(argv) > 1 else DEFAULT_PROMPT
    try:
        reply = ai_service.generate_text([{"role": "user", "content": prompt}])
    except ai_service.AIError as exc:
        print(f"调用失败：{type(exc).__name__}：{exc}")
        return 2

    print("模型回复：" + reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
