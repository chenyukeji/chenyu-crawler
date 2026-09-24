from __future__ import annotations

import subprocess
import os
import signal
import sys
import time
import traceback
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread

from crawler.config import load_config, source_id
from database.connection import connect_database, DEFAULT_DB_PATH
from database.repository import SnapshotStore
from crawler.lifecycle import CollectorLock, CollectorBusy, recover_interrupted, due_sources, queued_sources, cancel_unavailable_queue, now_shanghai


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
    config = load_config(CONFIG_PATH)
    with closing(connect_database()) as connection:
        seen = {row[0] for row in connection.execute("SELECT DISTINCT source_url FROM collection_runs WHERE snapshot_date=?", (today,))}
    return any(s["enabled"] and s["url"] not in seen for s in config["sources"])


def recover_abandoned_runs() -> int:
    with CollectorLock(DEFAULT_DB_PATH.parent / "collector.lock"):
        store = SnapshotStore(DEFAULT_DB_PATH)
        count = recover_interrupted(store)
        cancel_unavailable_queue(store, load_config(CONFIG_PATH)["sources"], now_shanghai())
        return count


def run_daily(*, retry_due: bool = False, scheduled: bool = False, queued: bool = False, timeout: int = 3600) -> int:
    command = [sys.executable, str(ROOT / "run_daily.py")]
    if queued:
        command.append("--queued")
    elif retry_due:
        command.append("--retry-due")
    elif scheduled:
        command.append("--scheduled")
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    if stdout:
        print(stdout, flush=True)
    if stderr:
        print(stderr, file=sys.stderr, flush=True)
    # Covers forced kills as well as normal signal handling in the child.
    try:
        recover_abandoned_runs()
    except CollectorBusy:
        pass
    return 124 if timed_out else process.returncode


def check_once() -> bool:
    now, schedule = configured_now()
    if not RUN_LOCK.acquire(blocking=False):
        return False
    try:
        try:
            recovered = recover_abandoned_runs()
        except CollectorBusy:
            return False
        if recovered:
            print(f"recovered {recovered} interrupted collection runs", flush=True)
        config = load_config(CONFIG_PATH)
        with closing(connect_database()) as connection:
            pending = queued_sources(connection, config["sources"], now)
        if pending:
            run_daily(queued=True)
            return True
        initial = claim_daily_run(now, schedule)
        if not initial:
            config = load_config(CONFIG_PATH)
            with closing(connect_database()) as connection:
                due = due_sources(connection, config["sources"], now)
            if not due:
                return False
        print(f"starting {'daily' if initial else 'delayed retry'} collection for {now.date().isoformat()}", flush=True)
        return_code = run_daily(retry_due=not initial, scheduled=initial)
        print(f"collection finished with exit code {return_code}", flush=True)
        return True
    finally:
        RUN_LOCK.release()


def _manual_run_worker() -> None:
    try:
        run_daily(queued=True)
    except Exception as exc:
        print(f"manual collection error: {exc}", flush=True)
    finally:
        RUN_LOCK.release()


def start_manual_run(target_id: str | None = None) -> bool:
    config = load_config(CONFIG_PATH)
    sources = [s for s in config["sources"] if s["enabled"] and (target_id is None or source_id(s)==target_id)]
    if not sources:
        raise ValueError("类目不存在或尚未启用，请先保存启用设置")
    now = now_shanghai()
    store = SnapshotStore(DEFAULT_DB_PATH)
    for source in sources:
        store.enqueue_run(source_url=source["url"],marketplace=source["marketplace"],category=source["category"],
                          snapshot_date=now.date().isoformat(),started_at=now.isoformat(timespec="microseconds"))
    if RUN_LOCK.acquire(blocking=False):
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
