from __future__ import annotations

import subprocess
import sys
import time
import traceback
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread

from crawler.config import load_config
from database.connection import connect_database


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "sources.json"
SCHEDULE_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
RUN_LOCK = Lock()


def configured_now() -> tuple[datetime, str]:
    config = load_config(CONFIG_PATH)
    return datetime.now(SCHEDULE_TIMEZONE), str(config["daily_schedule"])


def claim_daily_run(now: datetime, schedule: str) -> bool:
    today = now.date().isoformat()
    if now.strftime("%H:%M") < schedule:
        return False
    with closing(connect_database()) as connection:
        row = connection.execute(
            "SELECT 1 FROM collection_runs WHERE snapshot_date = ? LIMIT 1",
            (today,),
        ).fetchone()
    return row is None


def run_daily() -> int:
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_daily.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    return result.returncode


def check_once() -> bool:
    now, schedule = configured_now()
    if not RUN_LOCK.acquire(blocking=False):
        return False
    try:
        if not claim_daily_run(now, schedule):
            return False
        print(f"starting daily collection for {now.date().isoformat()}", flush=True)
        try:
            return_code = run_daily()
        except Exception:
            raise
        print(f"daily collection finished with exit code {return_code}", flush=True)
        return True
    finally:
        RUN_LOCK.release()


def _manual_run_worker() -> None:
    try:
        run_daily()
    except Exception as exc:
        print(f"manual collection error: {exc}", flush=True)
    finally:
        RUN_LOCK.release()


def start_manual_run() -> bool:
    if not RUN_LOCK.acquire(blocking=False):
        return False
    Thread(target=_manual_run_worker, name="manual-collector", daemon=True).start()
    return True


def loop(interval: int = 60) -> None:
    print("daily scheduler started", flush=True)
    while True:
        try:
            check_once()
        except Exception as exc:
            print(f"scheduler error: {exc}", flush=True)
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    loop()
