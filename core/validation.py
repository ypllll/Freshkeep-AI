"""输入校验与名称归一化（R-10、R-24；product_spec §6.2）。

关键校验由本模块（Python）负责，不依赖界面的表单限制。
校验失败一律抛出 `ValidationError`，由调用方决定如何提示用户。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Optional, Union

from core.models import CATEGORIES, STORAGE_KINDS, UNITS

MAX_NAME_LENGTH = 80

# 换行、制表符等 C0 控制字符一律拒绝（product_spec §6.2：拒绝换行等控制字符）
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_BLOCK_TEXT = re.compile(r"[（(][^（）()]*[)）]")
_WHITESPACE = re.compile(r"\s+")


class ValidationError(ValueError):
    """输入不合法。"""


def normalize_name(raw: str) -> str:
    """把食材名称归一化，用于匹配与汇总：去括号内容、转小写、压缩空白。"""
    text = _BLOCK_TEXT.sub(" ", raw)
    text = _WHITESPACE.sub(" ", text).strip()
    return text.casefold()


def validate_name(raw: object) -> str:
    """校验并返回展示用名称（保留用户写法，仅去首尾空白）。"""
    if not isinstance(raw, str):
        raise ValidationError("食材名称必须是文本")
    if _CONTROL_CHARS.search(raw):
        raise ValidationError("食材名称不能包含换行或控制字符")
    name = raw.strip()
    if not name:
        raise ValidationError("请输入食材名称")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"食材名称不能超过 {MAX_NAME_LENGTH} 个字符")
    if not normalize_name(name):
        raise ValidationError("食材名称不能只包含括号或空白")
    return name


def validate_quantity(raw: object) -> float:
    """校验数量：可解析、有限、且大于 0。"""
    if isinstance(raw, bool):
        raise ValidationError("数量必须是数字")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError("数量必须是数字") from None
    if not math.isfinite(value):
        raise ValidationError("数量必须是有限数字")
    if value <= 0:
        raise ValidationError("数量必须大于 0")
    return value


def _validate_in(raw: object, allowed, label: str) -> str:
    if not isinstance(raw, str):
        raise ValidationError(f"{label}必须是文本")
    value = raw.strip()
    if value not in allowed:
        raise ValidationError(f"{label}不在允许的取值范围内：{value or '空值'}")
    return value


def validate_unit(raw: object) -> str:
    return _validate_in(raw, UNITS, "单位")


def validate_category(raw: object) -> str:
    return _validate_in(raw, CATEGORIES, "类别")


def validate_storage(raw: object) -> str:
    return _validate_in(raw, STORAGE_KINDS, "储存方式")


def validate_notes(raw: object) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationError("备注必须是文本")
    text = raw.strip()
    return text or None


def validate_item_id(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("批次 ID 必须是整数")
    if raw <= 0:
        raise ValidationError("批次 ID 必须大于 0")
    return raw


def validate_purchase_date(
    raw: Union[str, date, None],
    *,
    today: date,
    allow_unknown: bool,
) -> Optional[date]:
    """校验购买日期。

    `raw` 为 None 表示"购买日期未知"：只有在 `allow_unknown` 为真时才接受
    （product_spec R-25：未知与"今天"是两种不同状态）。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_unknown:
            return None
        raise ValidationError("请选择购买日期，或明确标记为未知")

    if isinstance(raw, datetime):
        value = raw.date()
    elif isinstance(raw, date):
        value = raw
    elif isinstance(raw, str):
        try:
            value = date.fromisoformat(raw.strip())
        except ValueError:
            raise ValidationError("购买日期格式必须是 YYYY-MM-DD") from None
    else:
        raise ValidationError("购买日期必须是日期或 YYYY-MM-DD 文本")

    if value > today:
        raise ValidationError("购买日期不能晚于今天")
    return value
