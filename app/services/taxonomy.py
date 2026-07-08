from __future__ import annotations

import sqlite3


def get_parent_map(db: sqlite3.Connection, category_id: int) -> dict[int, int | None]:
    rows = db.execute(
        "SELECT id, parent_id FROM category_elements WHERE category_id = ?",
        (category_id,),
    ).fetchall()
    return {int(row["id"]): (int(row["parent_id"]) if row["parent_id"] is not None else None) for row in rows}


def ancestors_of(element_id: int, parent_map: dict[int, int | None]) -> list[int]:
    ancestors: list[int] = []
    seen: set[int] = set()
    current = parent_map.get(element_id)
    while current is not None and current not in seen:
        ancestors.append(current)
        seen.add(current)
        current = parent_map.get(current)
    return ancestors


def is_ancestor(ancestor_id: int, descendant_id: int, parent_map: dict[int, int | None]) -> bool:
    return ancestor_id in ancestors_of(descendant_id, parent_map)


def is_descendant(descendant_id: int, ancestor_id: int, parent_map: dict[int, int | None]) -> bool:
    return is_ancestor(ancestor_id, descendant_id, parent_map)
