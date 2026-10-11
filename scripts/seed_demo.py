"""导入演示数据（可选：先删除演示数据库再重建）。

用法：

    python scripts/seed_demo.py            # 追加演示数据（已存在的数据不动）
    python scripts/seed_demo.py --reset    # 删除演示数据库文件后重建

说明：

- 只操作 `config.DATABASE_PATH` 指向的数据库文件（默认 `data/freshkeep.db`，已被 `.gitignore` 排除）；
- `--reset` 是**单独的、明确的人工操作**，只删除那个文件，不做任何迁移层面的删库，
  也不影响其他数据；执行前会打印被删除的绝对路径；
- 建立表结构走正常的版本化迁移（幂等），不会清空已有数据；
- 演示数据本身声明为可丢弃；脚本不调用模型、不打印任何密钥。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402  （需先修正 sys.path）
from services import inventory_service  # noqa: E402

# (名称, 类别, 数量, 单位, 储存方式, 购买日期偏移天数, 保质期天数或 None, 声明成分)
DEMO_BATCHES: List[Tuple[str, str, float, str, str, int, Optional[int], Optional[Sequence[str]]]] = [
    ("番茄", "蔬菜", 5, "个", "冷藏", -3, None, ["大豆"]),
    ("鸡蛋", "蛋奶", 6, "个", "冷藏", -5, 21, ["鸡蛋"]),
    ("土豆", "蔬菜", 4, "个", "冷藏", -7, None, None),
    ("大米", "主食", 2, "kg", "常温", -30, 180, None),
    ("牛奶", "蛋奶", 1, "L", "冷藏", -2, 7, ["牛奶"]),
    ("花生酱", "调味", 1, "瓶", "常温", -20, 180, ["花生"]),
    ("青菜", "蔬菜", 3, "把", "冷藏", -1, None, ["花生", "大豆"]),
    ("面条", "主食", 2, "包", "常温", -10, 90, ["小麦"]),
]

DEMO_ALLERGENS: List[str] = ["花生"]


def seed(conn, *, today: Optional[date] = None) -> Tuple[int, int]:
    """写入演示数据，返回 (批次条数, 过敏原条数)。"""
    base = today or date.today()
    for name, category, quantity, unit, storage, day_offset, shelf_life_days, declared in DEMO_BATCHES:
        inventory_service.add_batch(
            conn,
            name=name,
            category=category,
            quantity=quantity,
            unit=unit,
            storage=storage,
            purchased_on=base + timedelta(days=day_offset),
            shelf_life_days=shelf_life_days,
            declared_allergens=list(declared) if declared else None,
            allergen_declared=declared is not None,
            initial=True,
            today=base,
        )
    for allergen in DEMO_ALLERGENS:
        try:
            inventory_service.add_user_allergen(conn, allergen)
        except inventory_service.InventoryError:
            pass  # 已经存在就跳过，保持脚本可重复执行
    return len(DEMO_BATCHES), len(DEMO_ALLERGENS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="导入 FreshKeep AI 演示数据")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除演示数据库文件后重建（单独的显式操作；只影响该文件）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path = config.get_database_path()
    if args.reset:
        if db_path.exists():
            print(f"删除演示数据库：{db_path}")
            db_path.unlink()
        else:
            print(f"演示数据库不存在，直接新建：{db_path}")

    conn = inventory_service.open_connection(db_path)
    try:
        batches, allergens = seed(conn)
        summary = inventory_service.list_summary(conn)
    finally:
        conn.close()

    print(f"演示数据库：{db_path}")
    print(f"已写入 {batches} 条批次、{allergens} 条过敏原；当前库存共 {len(summary)} 种食材。")
    print("提示：演示数据可丢弃；需要清空重来时再执行一次 --reset。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
