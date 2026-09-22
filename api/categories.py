from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from database.schema import initialize_schema

router = APIRouter(prefix="/api/categories", tags=["categories"])
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "new_releases.db"


class CategoryCreate(BaseModel):
    marketplace: str
    node_id: str
    name: str
    parent_id: str = ""
    level: int


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


@router.get("")
def list_categories():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, marketplace, node_id, name, parent_id, level, enabled FROM categories ORDER BY marketplace, level, id"
        ).fetchall()
    return [dict(row) for row in rows]


@router.delete("/{category_id}")
def disable_category(category_id: int):
    with connect() as conn:
        conn.execute("UPDATE categories SET enabled = 0 WHERE id = ?", (category_id,))
        conn.commit()
    return {"status": "disabled", "id": category_id}


@router.post("")
def create_category(item: CategoryCreate):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO categories (marketplace,node_id,name,parent_id,level) VALUES (?,?,?,?,?)",
            (item.marketplace, item.node_id, item.name, item.parent_id, item.level),
        )
        conn.commit()
    return {"status": "created", "node_id": item.node_id}
