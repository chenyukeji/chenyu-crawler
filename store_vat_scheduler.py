"""Hourly, non-overlapping automatic European store collection trigger."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta

import seller_vat_login
import store_vat_collection
from crawler.seller_vat import TZ

SUCCESS_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=1)
LOGIN_TIMEOUT = 120


def due(item: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    state = item.get("state")
    if state in store_vat_collection.ACTIVE:
        return False
    if state not in ("complete", "failed"):
        return True
    stamp = item.get("finished_at") or item.get("started_at")
    try:
        finished = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    delay = SUCCESS_INTERVAL if state == "complete" else RETRY_INTERVAL
    return now >= finished + delay


def main() -> int:
    item = store_vat_collection.status()
    if not due(item):
        print(f"automatic collection: waiting; state={item.get('state')}", flush=True)
        return 0
    # A saved cookie may still exist after the server has invalidated the login.
    # Refresh with the configured account before opening a long collection run.
    started, message = seller_vat_login.start_from_config(force=True)
    if not started:
        print(f"automatic collection: login unavailable: {message}", flush=True)
        return 1
    deadline = time.monotonic() + LOGIN_TIMEOUT
    while time.monotonic() < deadline:
        current = seller_vat_login.status()
        if current.get("state") in ("logged_in", "failed", "busy"):
            break
        time.sleep(2)
    else:
        print("automatic collection: login timed out", flush=True)
        return 1
    if current.get("state") != "logged_in":
        print(f"automatic collection: {current.get('message')}", flush=True)
        return 1
    started, message, item = store_vat_collection.start()
    print(f"automatic collection: {message}; run={item.get('run_id', '')}", flush=True)
    return 0 if started else 1


if __name__ == "__main__":
    raise SystemExit(main())
