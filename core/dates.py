"""保质期与新鲜度领域逻辑（product_spec §6.3；R-01 ~ R-04、R-25、R-26）。

本模块是纯函数集合：不访问数据库、不依赖系统当前时间（`today` 由调用方传入），
因此边界行为可以被稳定测试。

四条硬性规则：

- 正式期限（`expiry_date`）**只**存用户明确填写的日期，系统绝不推算、绝不覆盖；
- 没有明确日期但填了购买日期时，可以给出针对该批次的 **AI 估算**：只用于展示、
  不写入 `expiry_date`、不参与新鲜度分级，且必须标注为"AI 估算"；
- 没有购买日期时**不生成**任何 AI 估算日期（不得默认今天）；
- 通用参考区间（经验区间）与针对本批次的 AI 估算必须分开呈现，两者都不得
  被当成确定的有效期，也不得宣称食品安全得到保证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple, Union

from core.validation import ValidationError

DateLike = Union[str, date, datetime, None]

# 正式期限来源（与 storage.schema 的 expiry_source 取值一致）
SOURCE_USER_DATE = "user_date"
SOURCE_USER_DAYS = "user_days"  # 历史数据：旧版本曾把"用户天数 + 购买日期"换算成日期
SOURCE_USER_DAYS_UNRESOLVED = "user_days_unresolved"
SOURCE_UNKNOWN = "unknown"

# 新鲜度分级
STATUS_EXPIRED = "expired"
STATUS_DUE_SOON = "due_soon"
STATUS_FRESH = "fresh"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS: Dict[str, str] = {
    STATUS_EXPIRED: "已过期",
    STATUS_DUE_SOON: "临期",
    STATUS_FRESH: "新鲜",
    STATUS_UNKNOWN: "未知",
}

SOURCE_LABELS: Dict[str, str] = {
    SOURCE_USER_DATE: "用户提供",
    SOURCE_USER_DAYS: "用户提供（按天数换算，历史数据）",
    SOURCE_USER_DAYS_UNRESOLVED: "用户提供天数（无正式期限）",
    SOURCE_UNKNOWN: "未提供",
}

# 参考区间必须始终与这句标注同时出现（R-02、R-03）
DISCLAIMER = "参考信息，非食品安全保证"
AI_ESTIMATE_LABEL = "AI 估算"
AI_ESTIMATE_DISCLAIMER = "这是系统估算，不是你提供的有效期，也不是食品安全保证"
UNKNOWN_TEXT = "未知"


@dataclass(frozen=True)
class ReferenceRange:
    """某个 (类别, 储存方式) 组合的参考天数量程与其依据。"""

    days_min: int
    days_max: int
    basis: str


# 参考期限数据属于日期领域逻辑，只在此处维护一份（architecture §6.5）。
# 仅收录有限的常见组合；数值是粗略经验区间，**不是**食品安全依据，
# 因此展示时必须带 DISCLAIMER；未收录的组合一律返回未知。
REFERENCE_SHELF_LIFE: Dict[Tuple[str, str], ReferenceRange] = {
    ("蔬菜", "冷藏"): ReferenceRange(3, 7, "常见叶菜类 / 冷藏"),
    ("蔬菜", "冷冻"): ReferenceRange(30, 90, "焯水后冷冻 / 常见做法"),
    ("水果", "冷藏"): ReferenceRange(5, 14, "多数常见水果 / 冷藏"),
    ("肉类", "冷藏"): ReferenceRange(1, 2, "生鲜畜禽肉 / 冷藏"),
    ("肉类", "冷冻"): ReferenceRange(60, 90, "生鲜畜禽肉 / 冷冻"),
    ("海鲜", "冷藏"): ReferenceRange(1, 2, "生鲜水产 / 冷藏"),
    ("海鲜", "冷冻"): ReferenceRange(30, 90, "生鲜水产 / 冷冻"),
    ("蛋奶", "冷藏"): ReferenceRange(5, 21, "鲜奶与鲜蛋 / 冷藏（两者差异较大）"),
    ("熟食", "冷藏"): ReferenceRange(1, 3, "已烹调熟食 / 冷藏"),
    ("主食", "常温"): ReferenceRange(7, 30, "常见米面主食 / 常温"),
    ("调味", "常温"): ReferenceRange(180, 365, "常见调味品 / 常温（以包装标示为准）"),
}

REFERENCE_TABLE_NOTE = "V1 只收录有限的常见组合，未收录即为未知。"


def _parse_date(value: DateLike, *, label: str) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValidationError(f"{label}格式必须是 YYYY-MM-DD") from None
    raise ValidationError(f"{label}必须是日期或 YYYY-MM-DD 文本")


def validate_shelf_life_days(raw: object) -> Optional[int]:
    """保质期天数必须是大于 0 的整数（允许来自表单的纯数字文本）。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            raw = int(text)
        except ValueError:
            raise ValidationError("保质期天数必须是整数") from None
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, float) and raw.is_integer():
            raw = int(raw)
        else:
            raise ValidationError("保质期天数必须是整数")
    if raw <= 0:
        raise ValidationError("保质期天数必须大于 0")
    return int(raw)


@dataclass(frozen=True)
class ResolvedExpiry:
    """用户数据解析出的正式期限（不含任何系统估算）。

    `message` 承载两类必须让用户看到的文本：日期与天数冲突时的提示（`conflict=True`），
    以及"只填了天数、但没有正式期限"的说明（`conflict=False`）。两者都不得静默丢弃。
    """

    expiry_date: Optional[date]
    shelf_life_days: Optional[int]
    expiry_source: str
    conflict: bool
    message: Optional[str]


def resolve_expiry(
    *,
    purchased_on: DateLike = None,
    expiry_date: DateLike = None,
    shelf_life_days: object = None,
) -> ResolvedExpiry:
    """按 product_spec §6.3 的唯一判定顺序解析用户提供的保质期信息。

    1. 用户提供了具体日期 → 原样采用（`user_date`），不要求购买日期已知，绝不重算；
    2. 只提供天数、没有具体日期 → 保留天数，正式期限保持为空
       （`user_days_unresolved`）；**系统不把天数换算成正式日期**；
    3. 日期与天数都提供且不一致 → 以日期为正式记录，标记冲突并保留天数原值（R-04）；
    4. 两者都未提供 → 正式期限为空（`unknown`）。

    购买日期只参与"日期与天数是否冲突"的比对和 AI 估算，不参与正式期限的生成。
    """
    purchase = _parse_date(purchased_on, label="购买日期")
    explicit = _parse_date(expiry_date, label="保质期日期")
    days = validate_shelf_life_days(shelf_life_days)

    if purchase is not None and explicit is not None and explicit < purchase:
        raise ValidationError("保质期日期不能早于购买日期")

    if explicit is not None:
        conflict = False
        message: Optional[str] = None
        if days is not None and purchase is not None:
            derived = purchase + timedelta(days=days)
            if derived != explicit:
                conflict = True
                message = (
                    f"你输入的保质期日期（{explicit.isoformat()}）与保质期天数"
                    f"（{days} 天，按购买日期推算为 {derived.isoformat()}）不一致；"
                    "已按你填写的日期记录，请确认哪个正确。"
                )
        return ResolvedExpiry(
            expiry_date=explicit,
            shelf_life_days=days,
            expiry_source=SOURCE_USER_DATE,
            conflict=conflict,
            message=message,
        )

    if days is not None:
        # 用户只给了天数：保留天数，但正式期限保持未知（系统不推算正式日期）。
        reason = f"已记录你提供的 {days} 天保质期，但没有填写明确的有效期日期，正式期限保持未知。"
        reason += (
            "购买日期也未填写，因此不会生成任何估算日期。"
            if purchase is None
            else "下面的 AI 估算仅供参考，不是你提供的有效期。"
        )
        return ResolvedExpiry(
            expiry_date=None,
            shelf_life_days=days,
            expiry_source=SOURCE_USER_DAYS_UNRESOLVED,
            conflict=False,
            message=reason,
        )

    return ResolvedExpiry(
        expiry_date=None,
        shelf_life_days=None,
        expiry_source=SOURCE_UNKNOWN,
        conflict=False,
        message=None,
    )


def reference_range(category: Optional[str], storage: Optional[str]) -> Optional[ReferenceRange]:
    """查询参考区间；类别或储存方式缺失、为"其他"，或未收录时返回 None（不得猜测）。"""
    if not isinstance(category, str) or not isinstance(storage, str):
        return None
    key = (category.strip(), storage.strip())
    return REFERENCE_SHELF_LIFE.get(key)


@dataclass(frozen=True)
class AiEstimate:
    """针对**当前这一条批次**生成的 AI 估算（只用于展示）。"""

    days_min: int
    days_max: int
    basis: str
    date_min: date
    date_max: date


def ai_estimate(
    *,
    category: Optional[str],
    storage: Optional[str],
    purchased_on: DateLike = None,
) -> Optional[AiEstimate]:
    """按类别 + 储存方式的经验区间，针对该批次给出估算日期区间。

    规则（product_spec §6.3）：

    - 没有购买日期 → 返回 None（不得默认今天、不得生成估算日期）；
    - 未收录该类别与储存方式 → 返回 None（不猜测）；
    - 结果只用于展示，**永远不写入 `expiry_date`**，也不参与新鲜度分级。
    """
    purchase = _parse_date(purchased_on, label="购买日期")
    if purchase is None:
        return None
    reference = reference_range(category, storage)
    if reference is None:
        return None
    return AiEstimate(
        days_min=reference.days_min,
        days_max=reference.days_max,
        basis=reference.basis,
        date_min=purchase + timedelta(days=reference.days_min),
        date_max=purchase + timedelta(days=reference.days_max),
    )


def freshness_status(
    *,
    expiry_date: DateLike,
    expiry_source: str,
    today: date,
    due_soon_days: int,
) -> str:
    """新鲜度分级：只有正式期限（用户日期或天数换算）参与确定性分级。

    正式期限未知、或来源为 `user_days_unresolved` / `unknown` 时恒为"未知"。
    """
    if expiry_source not in (SOURCE_USER_DATE, SOURCE_USER_DAYS):
        return STATUS_UNKNOWN
    expiry = _parse_date(expiry_date, label="保质期日期")
    if expiry is None:
        return STATUS_UNKNOWN

    days_left = (expiry - today).days
    if days_left < 0:
        return STATUS_EXPIRED
    if days_left <= due_soon_days:
        return STATUS_DUE_SOON
    return STATUS_FRESH


@dataclass(frozen=True)
class ExpiryView:
    """界面渲染所需的全部保质期信息（页面只负责显示）。"""

    status: str
    status_label: str
    source: str
    source_label: str
    formal_text: str
    reference_text: Optional[str]
    ai_estimate_text: Optional[str]
    warning: Optional[str]
    conflict: bool


def build_expiry_view(
    *,
    purchased_on: DateLike = None,
    expiry_date: DateLike = None,
    shelf_life_days: object = None,
    category: Optional[str] = None,
    storage: Optional[str] = None,
    today: date,
    due_soon_days: int,
    stored_source: Optional[str] = None,
) -> ExpiryView:
    """把用户输入、库中来源与参考表合成为可直接展示的保质期视图。

    `stored_source` 是数据库里保存的 `expiry_source`：展示**以库中值为准**，
    避免把系统换算出来的日期重新标注成"用户提供"。
    """
    resolved = resolve_expiry(
        purchased_on=purchased_on, expiry_date=expiry_date, shelf_life_days=shelf_life_days
    )
    source = stored_source if stored_source in SOURCE_LABELS else resolved.expiry_source

    if source in (SOURCE_USER_DATE, SOURCE_USER_DAYS) and resolved.expiry_date is not None:
        formal_date: Optional[date] = resolved.expiry_date
        formal_text = formal_date.isoformat()
    elif source == SOURCE_USER_DAYS_UNRESOLVED and resolved.shelf_life_days:
        formal_date = None
        formal_text = f"未知（已记录你提供的 {resolved.shelf_life_days} 天，没有正式期限）"
    else:
        formal_date = None
        formal_text = UNKNOWN_TEXT

    reference = reference_range(category, storage)
    purchase = _parse_date(purchased_on, label="购买日期")

    # 通用参考区间：只给经验天数区间与依据，不换算成日期（避免与 AI 估算混为一谈）。
    # 仅在用户完全没有提供期限信息时展示，避免与用户填写的天数并列造成困惑。
    reference_text: Optional[str] = None
    if source == SOURCE_UNKNOWN:
        if reference is None:
            reference_text = f"{UNKNOWN_TEXT}（未收录该类别与储存方式的参考区间）"
        else:
            reference_text = (
                f"约 {reference.days_min}–{reference.days_max} 天"
                f"（依据：{reference.basis}）。{DISCLAIMER}"
            )

    # AI 估算：只针对"没有正式期限"的批次，且必须有购买日期
    estimate_text: Optional[str] = None
    if formal_date is None:
        estimate = ai_estimate(
            category=category, storage=storage, purchased_on=purchased_on
        )
        if estimate is None:
            reason = (
                "缺少购买日期，不默认使用今天"
                if purchase is None
                else "未收录该类别与储存方式的经验区间"
            )
            estimate_text = f"{AI_ESTIMATE_LABEL}：未生成（{reason}）。"
        else:
            estimate_text = (
                f"{AI_ESTIMATE_LABEL}：约 {estimate.date_min.isoformat()} ~ "
                f"{estimate.date_max.isoformat()}（约 {estimate.days_min}–{estimate.days_max} 天，"
                f"依据：购买日期 + {estimate.basis}）。{AI_ESTIMATE_DISCLAIMER}。"
            )

    warning = resolved.message
    status = freshness_status(
        expiry_date=formal_date,
        expiry_source=source,
        today=today,
        due_soon_days=due_soon_days,
    )

    return ExpiryView(
        status=status,
        status_label=STATUS_LABELS[status],
        source=source,
        source_label=SOURCE_LABELS[source],
        formal_text=formal_text,
        reference_text=reference_text,
        ai_estimate_text=estimate_text,
        warning=warning,
        conflict=resolved.conflict,
    )
