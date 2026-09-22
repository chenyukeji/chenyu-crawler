from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from scheduler import create_run, finish_run
from database.schema import initialize_schema

DB_PATH = Path(__file__).resolve().parent / "data" / "new_releases.db"


def due_tasks():
    with sqlite3.connect(DB_PATH) as conn:
        initialize_schema(conn)
        rows = conn.execute(
            "SELECT id FROM crawl_tasks WHERE enabled=1"
        ).fetchall()
    return [row[0] for row in rows]


def execute_task(task_id: int):
    run_id = create_run(task_id)
    try:
        # 后续接入 run_daily.py 实际采集入口
        finish_run(run_id, "success", "scheduler execution placeholder")
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))


def loop(interval: int = 60):
    while True:
        for task_id in due_tasks():
            execute_task(task_id)
        time.sleep(interval)


if __name__ == "__main__":
    loop()
