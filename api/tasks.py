from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from database.schema import initialize_schema

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "new_releases.db"


class TaskCreate(BaseModel):
    name: str
    marketplace: str
    category_id: int | None = None
    schedule: str = ""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


@router.get("")
def list_tasks():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,name,marketplace,category_id,schedule,enabled,last_run FROM crawl_tasks ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("")
def create_task(item: TaskCreate):
    with connect() as conn:
        conn.execute(
            "INSERT INTO crawl_tasks(name,marketplace,category_id,schedule) VALUES(?,?,?,?)",
            (item.name, item.marketplace, item.category_id, item.schedule),
        )
        conn.commit()
    return {"status": "created", "name": item.name}


@router.delete("/{task_id}")
def disable_task(task_id: int):
    with connect() as conn:
        conn.execute("UPDATE crawl_tasks SET enabled=0 WHERE id=?", (task_id,))
        conn.commit()
    return {"status": "disabled", "id": task_id}
