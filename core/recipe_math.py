"""菜谱用量与库存缺口的纯计算（S5）。

不依赖 Streamlit、网络或数据库连接：接收"菜谱需要的食材"与"当前活动批次"，
返回结构化的缺口结果，供服务层与界面使用。

三条保守原则：

- 名称匹配只做**归一化后完全相等**，不做模糊相似匹配；
- 单位不换算：同名但单位不同一律标记 `unit_mismatch`，不猜也不硬算；
- 找不到同名食材时可用量为"未知"（None），**不是 0**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.allocation import summarize
from core.models import InventoryItem, StockSummaryRow
from core.validation import ValidationError, normalize_name, validate_quantity

MATCH_MATCHED = "matched"
MATCH_UNMATCHED = "unmatched"
MATCH_UNIT_MISMATCH = "unit_mismatch"

MATCH_LABELS: Dict[str, str] = {
    MATCH_MATCHED: "库存已匹配",
    MATCH_UNMATCHED: "库存中未找到",
    MATCH_UNIT_MISMATCH: "单位无法比较",
}


@dataclass(frozen=True)
class IngredientRequirement:
    """菜谱需要的一种食材用量。"""

    name: str
    quantity: float
    unit: str


@dataclass(frozen=True)
class IngredientGap:
    """一种食材的库存缺口结果。"""

    name: str
    unit: str
    required_quantity: float
    match_state: str
    available_quantity: Optional[float]
    missing_quantity: Optional[float]
    matched_batch_ids: Tuple[int, ...] = ()
    note: Optional[str] = None

    @property
    def is_sufficient(self) -> bool:
        return self.match_state == MATCH_MATCHED and (self.missing_quantity or 0.0) <= 0

    @property
    def is_comparable(self) -> bool:
        return self.match_state == MATCH_MATCHED


@dataclass(frozen=True)
class GapSummary:
    """缺口结果的计数，便于界面展示与测试断言。"""

    matched_sufficient: int
    matched_missing: int
    unit_mismatch: int
    unmatched: int


def _as_requirement(raw: object) -> IngredientRequirement:
    """把任意带 name/quantity/unit 的对象（例如 pydantic 模型）转成需求。"""
    name = str(getattr(raw, "name", "") or "").strip()
    unit = str(getattr(raw, "unit", "") or "").strip()
    if not name:
        raise ValidationError("食材名称不能为空")
    if not unit:
        raise ValidationError("食材单位不能为空")
    quantity = validate_quantity(getattr(raw, "quantity", None))
    return IngredientRequirement(name=name, quantity=quantity, unit=unit)


def compute_gaps(
    *,
    requirements: Sequence[object],
    inventory: Iterable[InventoryItem],
) -> Tuple[IngredientGap, ...]:
    """计算每种食材的现有量与缺口。

    - 现有量只统计 `status == 'active'` 的批次，汇总规则复用 `core.allocation.summarize`
      （按"归一化名称 + 单位"求和），因此同名多批次会被正确相加；
    - 名称识别看**全部**历史批次：某食材曾经有记录、只是批次都被消耗或丢弃时，
      按"已知为零"处理（现有量 0、缺口等于需求量），而不是"未知"（R-19、§6.9）；
    - 完全没有记录过的食材才算"未匹配"，其现有量与缺口都是未知（不写成 0）；
    - 不修改传入的任何库存数据；
    - 数量非法（≤0、NaN、无穷、非数字）时抛 `ValidationError`，不让脏数据进入计算。
    """
    all_items = list(inventory)
    batches = [item for item in all_items if getattr(item, "status", "active") == "active"]
    totals: Dict[Tuple[str, str], float] = {
        (row.name_normalized, row.unit): row.total_quantity for row in summarize(batches)
    }
    known_keys = {(item.name_normalized, item.unit) for item in all_items}
    units_by_name: Dict[str, List[str]] = {}
    ids_by_key: Dict[Tuple[str, str], List[int]] = {}
    for item in batches:
        units_by_name.setdefault(item.name_normalized, [])
        if item.unit not in units_by_name[item.name_normalized]:
            units_by_name[item.name_normalized].append(item.unit)
        ids_by_key.setdefault((item.name_normalized, item.unit), []).append(item.id)

    gaps: List[IngredientGap] = []
    for raw in requirements:
        requirement = _as_requirement(raw)
        key = (normalize_name(requirement.name), requirement.unit)

        if key in totals:
            available = round(totals[key], 6)
            missing = round(max(0.0, requirement.quantity - available), 6)
            gaps.append(
                IngredientGap(
                    name=requirement.name,
                    unit=requirement.unit,
                    required_quantity=requirement.quantity,
                    match_state=MATCH_MATCHED,
                    available_quantity=available,
                    missing_quantity=missing,
                    matched_batch_ids=tuple(sorted(ids_by_key.get(key, []))),
                    note="现有量按活动批次汇总；本次规划不修改库存",
                )
            )
            continue

        if key in known_keys:
            # 曾经有记录、但活动批次已经全部被消耗/丢弃 → 已知为零
            gaps.append(
                IngredientGap(
                    name=requirement.name,
                    unit=requirement.unit,
                    required_quantity=requirement.quantity,
                    match_state=MATCH_MATCHED,
                    available_quantity=0.0,
                    missing_quantity=round(requirement.quantity, 6),
                    note="该食材的记录都已被消耗或丢弃，按已知无剩余处理（现有量 0）",
                )
            )
            continue

        other_units = sorted(units_by_name.get(key[0], []))
        if other_units:
            gaps.append(
                IngredientGap(
                    name=requirement.name,
                    unit=requirement.unit,
                    required_quantity=requirement.quantity,
                    match_state=MATCH_UNIT_MISMATCH,
                    available_quantity=None,
                    missing_quantity=None,
                    note=(
                        f"库存里同名食材的单位是 {'、'.join(other_units)}，"
                        f"与菜谱要求的 {requirement.unit} 无法直接比较（不做单位换算）"
                    ),
                )
            )
            continue

        gaps.append(
            IngredientGap(
                name=requirement.name,
                unit=requirement.unit,
                required_quantity=requirement.quantity,
                match_state=MATCH_UNMATCHED,
                available_quantity=None,
                missing_quantity=None,
                note="库存中没有同名食材记录，现有量未知（不代表 0）",
            )
        )

    return tuple(gaps)


def summarize_gaps(gaps: Sequence[IngredientGap]) -> GapSummary:
    """统计各状态的条目数（界面用来显示"缺几样、几样无法比较"）。"""
    sufficient = missing = unit_mismatch = unmatched = 0
    for gap in gaps:
        if gap.match_state == MATCH_MATCHED:
            if (gap.missing_quantity or 0.0) > 0:
                missing += 1
            else:
                sufficient += 1
        elif gap.match_state == MATCH_UNIT_MISMATCH:
            unit_mismatch += 1
        else:
            unmatched += 1
    return GapSummary(
        matched_sufficient=sufficient,
        matched_missing=missing,
        unit_mismatch=unit_mismatch,
        unmatched=unmatched,
    )
