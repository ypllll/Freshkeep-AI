"""过敏原领域逻辑（product_spec §6.4；R-05 ~ R-08、R-14）。

纯函数集合：不访问数据库、不依赖 AI。判定结果只有三态：

- `conflict`：已确认与用户声明的过敏原冲突；
- `unverifiable`：无法核实（成分未声明，或命中已知的复合调味料风险）；
- `none_known`：未发现已知冲突——**不等于安全**。

匹配边界（避免"任意相似字符串都算同一种过敏原"）：

1. 归一化后**完全相等**；或
2. 归一化后**包含**某个长度 >= 2 的别名词（别名表本身是显式维护的白名单边界）。

不做编辑距离、相似度或其他模糊匹配；未命中时返回 `none_known`，
并且在文案里明确"不代表安全"（R-08）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.validation import ValidationError, normalize_name

STATUS_CONFLICT = "conflict"
STATUS_UNVERIFIABLE = "unverifiable"
STATUS_NONE_KNOWN = "none_known"

DECLARATION_DECLARED = "declared"
DECLARATION_UNKNOWN = "unknown"

NONE_KNOWN_MESSAGE = "未发现已知冲突（不代表安全）"

# 严禁出现的表述（R-08）：不得把 none_known 说成安全。
FORBIDDEN_WORDINGS: Tuple[str, ...] = (
    "无过敏原",
    "过敏友好",
    "绝对安全",
    "完全安全",
    "适合过敏人群",
)

# 内置别名表：过敏原（规范名）→ 明确列出的别名词。
# 只收录可核实的常见别名；未收录的名称不猜测，也不与相似词合并。
ALLERGEN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "花生": ("花生", "花生米", "花生碎", "花生粉", "花生酱", "花生油"),
    "大豆": ("大豆", "黄豆", "黄豆芽", "豆浆", "豆腐", "豆干", "豆皮", "腐竹", "豆豉", "味噌"),
    "小麦": (
        "小麦",
        "面粉",
        "面条",
        "面筋",
        "面包",
        "麸质",
        "大麦",
        "黑麦",
        "麦芽",
        "挂面",
        "馒头",
        "饺子皮",
        "馄饨皮",
        "蛋糕",
        "方便面",
    ),
    "牛奶": (
        "牛奶",
        "鲜奶",
        "奶粉",
        "奶油",
        "黄油",
        "奶酪",
        "芝士",
        "炼乳",
        "乳清",
        "酸奶",
        "奶茶",
        "奶昔",
    ),
    "鸡蛋": ("鸡蛋", "蛋清", "蛋黄", "蛋液", "全蛋"),
    "鱼类": ("鱼肉", "三文鱼", "鲈鱼", "带鱼", "鳕鱼", "金枪鱼", "鱼露"),
    "甲壳类": ("虾", "虾仁", "虾米", "虾皮", "虾酱", "基围虾", "河虾", "蟹", "螃蟹", "龙虾"),
    "贝类": ("蛤", "蛤蜊", "花蛤", "牡蛎", "生蚝", "扇贝", "蚬", "鲍鱼", "鱿鱼", "章鱼"),
    "坚果": ("核桃", "杏仁", "腰果", "榛子", "开心果", "松子", "夏威夷果"),
    "芝麻": ("芝麻", "芝麻酱", "芝麻油"),
    "芒果": ("芒果",),
    "芹菜": ("芹菜",),
}

# 已知的复合调味料风险清单：名称无法确认完整成分，命中即 unverifiable。
COMPOUND_SEASONINGS: Dict[str, Tuple[str, ...]] = {
    "酱油": ("大豆", "小麦"),
    "生抽": ("大豆", "小麦"),
    "老抽": ("大豆", "小麦"),
    "豆瓣酱": ("大豆", "小麦"),
    "甜面酱": ("小麦", "大豆"),
    "豆豉酱": ("大豆", "小麦"),
    "叉烧酱": ("大豆", "小麦"),
    "蚝油": ("贝类", "甲壳类", "鱼类"),
    "海鲜酱": ("甲壳类", "鱼类", "贝类"),
    "沙茶酱": ("花生", "大豆", "甲壳类", "鱼类"),
    "火锅底料": ("大豆", "花生", "芝麻"),
    "咖喱块": ("小麦", "牛奶", "大豆"),
}

_SPLIT_PATTERN = re.compile(r"[,，、;；/\\|\s]+")


@dataclass(frozen=True)
class UserAllergen:
    """用户维护的过敏原清单条目（持久化在 `user_allergen` 表）。"""

    id: int
    display_name: str
    name_normalized: str
    created_at: str

    @classmethod
    def from_row(cls, row) -> "UserAllergen":
        return cls(
            id=int(row["id"]),
            display_name=row["display_name"],
            name_normalized=row["name_normalized"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class AllergenAssessment:
    """单个食材（批次）的过敏原判定结果。"""

    status: str
    declaration_state: str
    message: str
    reasons: Tuple[str, ...] = ()
    matched_allergens: Tuple[str, ...] = ()
    matched_tokens: Tuple[str, ...] = ()

    @property
    def is_conflict(self) -> bool:
        return self.status == STATUS_CONFLICT

    @property
    def needs_review(self) -> bool:
        return self.status == STATUS_UNVERIFIABLE


@dataclass(frozen=True)
class ItemAllergenView:
    """界面展示用的单批次过敏原信息。"""

    item_id: int
    name: str
    status: str
    status_label: str
    declaration_state: str
    message: str
    reasons: Tuple[str, ...]


STATUS_LABELS: Dict[str, str] = {
    STATUS_CONFLICT: "已确认冲突",
    STATUS_UNVERIFIABLE: "待核实",
    STATUS_NONE_KNOWN: "未发现已知冲突",
}


def normalize_allergen(value: object) -> str:
    """过敏原名称归一化：去括号内容、统一大小写、压缩空白（复用名称归一化规则）。"""
    if not isinstance(value, str):
        raise ValidationError("过敏原名称必须是文本")
    return normalize_name(value)


def canonical_allergen(value: str) -> str:
    """把任意写法映射到规范名；未收录的名称原样返回（不猜测）。"""
    normalized = normalize_allergen(value)
    for canonical, aliases in ALLERGEN_ALIASES.items():
        if normalized == normalize_name(canonical) or normalized in aliases:
            return canonical
    return normalized


def aliases_for(value: str) -> Tuple[str, ...]:
    """返回某个过敏原的显式别名集合（未收录时只有它自己）。"""
    canonical = canonical_allergen(value)
    return ALLERGEN_ALIASES.get(canonical, (normalize_allergen(canonical),))


def parse_allergen_input(raw: str) -> List[str]:
    """把用户输入（逗号、顿号、分号、空格分隔）解析为规范名列表，去重保序。"""
    if not isinstance(raw, str):
        raise ValidationError("过敏原清单必须是文本")
    result: List[str] = []
    for piece in _SPLIT_PATTERN.split(raw):
        if not piece.strip():
            continue
        canonical = canonical_allergen(piece)
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def _match_candidate(text: str, user_allergens: Sequence[str]) -> List[Tuple[str, str]]:
    """在给定文本里查找命中的用户过敏原，返回 (规范名, 命中词) 列表。"""
    normalized = normalize_name(text)
    if not normalized:
        return []
    hits: List[Tuple[str, str]] = []
    for allergen in user_allergens:
        for alias in aliases_for(allergen):
            alias_normalized = normalize_name(alias)
            if not alias_normalized:
                continue
            if alias_normalized == normalized or (
                len(alias_normalized) >= 2 and alias_normalized in normalized
            ):
                hits.append((canonical_allergen(allergen), alias))
                break
    return hits


def compound_risk(name: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """判断名称是否命中已知的复合调味料风险清单。"""
    normalized = normalize_name(name)
    if not normalized:
        return None
    for seasoning, risks in COMPOUND_SEASONINGS.items():
        key = normalize_name(seasoning)
        if key and (key == normalized or (len(key) >= 2 and key in normalized)):
            return seasoning, risks
    return None


def assess_ingredient(
    *,
    name: str,
    declared_allergens: Optional[Iterable[str]] = None,
    allergen_status: str = DECLARATION_UNKNOWN,
    user_allergens: Optional[Sequence[str]] = None,
) -> AllergenAssessment:
    """判定单个批次（或菜谱中的单个食材）的过敏原状态。

    `declared_allergens` 只代表**这一条批次自己**的声明，不跨批次继承；
    `allergen_status` 为 `unknown` 表示该批次成分未声明（R-14）。
    """
    users = [canonical_allergen(item) for item in (user_allergens or []) if normalize_allergen(item)]
    declared = [str(item) for item in (declared_allergens or [])]
    declaration_state = (
        DECLARATION_DECLARED if allergen_status == DECLARATION_DECLARED else DECLARATION_UNKNOWN
    )

    hits = _match_candidate(name, users)
    if not hits:
        for declared_item in declared:
            hits.extend(_match_candidate(declared_item, users))

    if hits:
        matched_allergens = tuple(dict.fromkeys(allergen for allergen, _ in hits))
        matched_tokens = tuple(dict.fromkeys(token for _, token in hits))
        return AllergenAssessment(
            status=STATUS_CONFLICT,
            declaration_state=declaration_state,
            message=(
                f"与你的过敏原「{ '、'.join(matched_allergens) }」冲突"
                f"（命中：{ '、'.join(matched_tokens) }）"
            ),
            reasons=(
                f"食材或批次声明中包含：{ '、'.join(matched_tokens) }",
            ),
            matched_allergens=matched_allergens,
            matched_tokens=matched_tokens,
        )

    if not users:
        return AllergenAssessment(
            status=STATUS_NONE_KNOWN,
            declaration_state=declaration_state,
            message=NONE_KNOWN_MESSAGE,
            reasons=("你还没有声明任何过敏原，因此未进行冲突判定。",),
        )

    risk = compound_risk(name)
    if risk is not None:
        seasoning, possible = risk
        overlap = [item for item in possible if item in users]
        detail = f"{seasoning} 属于复合调味料，可能含 { '、'.join(possible) }"
        if overlap:
            detail += f"，其中 { '、'.join(overlap) } 与你声明的过敏原相关"
        return AllergenAssessment(
            status=STATUS_UNVERIFIABLE,
            declaration_state=declaration_state,
            message=f"成分无法确认：{detail}，请自行核实",
            reasons=(detail,),
        )

    if declaration_state == DECLARATION_UNKNOWN:
        return AllergenAssessment(
            status=STATUS_UNVERIFIABLE,
            declaration_state=declaration_state,
            message=(
                f"该批次未声明成分，无法排除与「{ '、'.join(users) }」相关的风险，请自行核实"
            ),
            reasons=("该批次未声明成分（成分未声明）。",),
        )

    return AllergenAssessment(
        status=STATUS_NONE_KNOWN,
        declaration_state=declaration_state,
        message=NONE_KNOWN_MESSAGE,
        reasons=("已声明成分中未发现与你的过敏原匹配的词。",),
    )


def assess_item(item, user_allergens: Optional[Sequence[str]] = None) -> ItemAllergenView:
    """对一条库存批次做判定（`item` 需要提供 name/declared_allergens/allergen_status/id）。"""
    assessment = assess_ingredient(
        name=item.name,
        declared_allergens=decode_declared_allergens(getattr(item, "declared_allergens", None)),
        allergen_status=getattr(item, "allergen_status", DECLARATION_UNKNOWN),
        user_allergens=user_allergens,
    )
    return ItemAllergenView(
        item_id=int(item.id),
        name=item.name,
        status=assessment.status,
        status_label=STATUS_LABELS[assessment.status],
        declaration_state=assessment.declaration_state,
        message=assessment.message,
        reasons=assessment.reasons,
    )


def encode_declared_allergens(values: Optional[Sequence[str]]) -> Optional[str]:
    """把声明列表编码为 JSON；未声明时返回 None（而不是空数组，R-14）。"""
    if values is None:
        return None
    return json.dumps([str(value) for value in values], ensure_ascii=False)


def decode_declared_allergens(raw: Optional[str]) -> List[str]:
    """读取批次上保存的声明（JSON 数组）；NULL/空值返回空列表。"""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except ValueError:
            return []
        return [str(entry) for entry in loaded] if isinstance(loaded, list) else []
    return parse_allergen_input(text)
