from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter

from database.schema import initialize_schema

router = APIRouter(prefix="/api/runs", tags=["runs"])
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "new_releases.db"


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


@router.get("")
def list_runs():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,task_id,start_time,end_time,status,message FROM crawl_runs ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [dict(row) for row in rows]
