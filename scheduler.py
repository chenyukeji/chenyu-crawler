from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from database.schema import initialize_schema

DB_PATH = Path(__file__).resolve().parent / "data" / "new_releases.db"
ROOT = Path(__file__).resolve().parent


def connect():
    conn = sqlite3.connect(DB_PATH)
    initialize_schema(conn)
    return conn


def create_run(task_id: int) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO crawl_runs(task_id,start_time) VALUES(?,?)",
            (task_id, datetime.now().isoformat()),
        )
        return int(cursor.lastrowid)


def finish_run(run_id: int, status: str, message: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE crawl_runs SET end_time=?, status=?, message=? WHERE id=?",
            (datetime.now().isoformat(), status, message, run_id),
        )


def run_task(task_id: int) -> int:
    run_id = create_run(task_id)
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "run_daily.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode == 0:
            finish_run(run_id, "success", result.stdout[-500:])
        else:
            finish_run(run_id, "failed", result.stderr[-500:])
    except Exception as exc:
        finish_run(run_id, "failed", str(exc))
    return run_id


if __name__ == "__main__":
    print("scheduler ready")
