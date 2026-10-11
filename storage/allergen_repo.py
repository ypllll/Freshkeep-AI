"""用户过敏原清单（`user_allergen` 表）的 SQL。

只负责持久化，不做匹配与判定（判定在 `core/allergens.py`）。
清单是用户级数据，与库存批次相互独立；批次自己的过敏原声明保存在
`inventory_item` 上，不在此表中（R-14：声明按批次独立，不跨批次继承）。
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from core.allergens import UserAllergen

_COLUMNS = "id, display_name, name_normalized, created_at"


def insert_allergen(
    conn: sqlite3.Connection, *, display_name: str, name_normalized: str, created_at: str
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO user_allergen (display_name, name_normalized, created_at)
        VALUES (?, ?, ?)
        """,
        (display_name, name_normalized, created_at),
    )
    return int(cursor.lastrowid or 0)


def get_allergen(conn: sqlite3.Connection, allergen_id: int) -> Optional[UserAllergen]:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM user_allergen WHERE id = ?", (allergen_id,)
    ).fetchone()
    return UserAllergen.from_row(row) if row is not None else None


def find_by_normalized(
    conn: sqlite3.Connection, name_normalized: str
) -> Optional[UserAllergen]:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM user_allergen WHERE name_normalized = ?", (name_normalized,)
    ).fetchone()
    return UserAllergen.from_row(row) if row is not None else None


def list_allergens(conn: sqlite3.Connection) -> List[UserAllergen]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM user_allergen ORDER BY id")
    return [UserAllergen.from_row(row) for row in rows]


def delete_allergen(conn: sqlite3.Connection, allergen_id: int) -> int:
    """删除一条清单记录，返回受影响行数（0 表示不存在）。"""
    return conn.execute("DELETE FROM user_allergen WHERE id = ?", (allergen_id,)).rowcount
