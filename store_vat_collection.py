"""Background, store-only SellerSprite discovery and public EU store checks."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from contextlib import closing
from pathlib import Path

from crawler.seller_vat import (ROOT, TZ, acquire_collection_lock,
                                collection_lock_held, release_collection_lock, save_json)
from seller_vat_loop import profile_ready

STATUS = ROOT / "data" / "store-vat-collection.json"
REPORTS = ROOT / "outputs" / "seller-vat"
DB = ROOT / "data" / "amazon_it.sqlite3"
LOCK = ROOT / "data" / "auto-collection.lock"
_START_LOCK = threading.Lock()
ACTIVE = {"queued", "discovering", "checking"}
RUN_ID = re.compile(r"^eu-store-\d{8}-\d{6}-\d{6}$")
SITES = ("it", "fr", "de", "pl", "es")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _read_status() -> dict:
    try:
        data = json.loads(STATUS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _running(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def status() -> dict:
    item = _read_status()
    if not item:
        return {"state": "idle", "message": "尚未启动新店铺采集"}
    if item.get("state") in ACTIVE and item.get("pid") and not _running(item["pid"]):
        item.update(state="failed", message="采集进程意外退出，请查看日志", finished_at=_now())
        save_json(STATUS, item)
    run_id = item.get("run_id", "")
    if RUN_ID.fullmatch(run_id):
        folder = REPORTS / run_id
        try:
            candidate = json.loads((folder / "店铺候选.json").read_text(encoding="utf-8"))
            item["pages_checked"] = candidate.get("pages_checked", 0)
            item["reported_result_total"] = candidate.get("reported_result_total", 0)
            seller_ids = {store["seller_id"] for store in candidate.get("stores", [])}
            item["stores_found"] = len(seller_ids)
            # JSONL includes failed attempts and retries, so count distinct successful
            # store-site checks in the database instead of counting log lines.
            if seller_ids and DB.is_file():
                with closing(sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    completed = {(seller_id, marketplace.removeprefix("amazon."))
                                 for seller_id, marketplace in db.execute(
                                     "SELECT seller_id,marketplace FROM seller_site_checks WHERE status!='failed'")
                                 if seller_id in seller_ids}
                item["site_checks"] = len(completed)
                item["site_checks_total"] = len(seller_ids) * len(SITES)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            pass
    return item


def start() -> tuple[bool, str, dict]:
    with _START_LOCK:
        current = status()
        if current.get("state") in ACTIVE:
            return False, "新店铺采集正在进行", current
        if not profile_ready():
            return False, "卖家精灵登录会话未就绪，请先刷新登录", current
        if collection_lock_held(LOCK):
            return False, "浏览器正在登录或采集，请稍后重试", current
        previous_id = current.get("run_id", "")
        previous_folder = REPORTS / previous_id if RUN_ID.fullmatch(previous_id) else None
        resume = bool(current.get("state") == "failed" and previous_folder
                      and (previous_folder / "发现进度.json").is_file())
        run_id = previous_id if resume else "eu-store-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
        folder = REPORTS / run_id
        folder.mkdir(parents=True, exist_ok=True)
        initial = {"state": "queued", "run_id": run_id,
                   "message": "已排队，继续上次进度" if resume else "已排队，准备扫描卖家精灵全部结果",
                   "started_at": _now(), "pages_checked": 0, "stores_found": 0, "site_checks": 0}
        save_json(STATUS, initial)
        try:
            with (folder / "采集日志.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, str(ROOT / "store_vat_collection.py"), "worker", run_id],
                                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
        except OSError as error:
            initial.update(state="failed", message=f"无法启动采集进程：{error}", finished_at=_now())
            save_json(STATUS, initial)
            return False, initial["message"], initial
        current = _read_status()
        if current.get("run_id") == run_id:
            current["pid"] = process.pid
            save_json(STATUS, current)
        return True, "已启动全量扫描，页面会显示进度", status()


def _stores_to_check(candidate_ids: set[str]) -> tuple[list[str], int]:
    """Include unfinished stores from earlier runs and count missing site checks."""
    if not DB.is_file():
        raise FileNotFoundError(DB)
    with closing(sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks: dict[str, dict[str, str]] = {}
        if "seller_site_checks" in tables:
            for seller_id, marketplace, check_status in db.execute(
                    "SELECT seller_id,marketplace,status FROM seller_site_checks"):
                checks.setdefault(seller_id, {})[marketplace.removeprefix("amazon.")] = check_status
    selected = []
    pending_sites = 0
    for seller_id in sorted(candidate_ids):
        previous = checks.get(seller_id, {})
        missing = sum(previous.get(site) in (None, "failed") for site in SITES)
        if missing:
            selected.append(seller_id)
            pending_sites += missing
    return selected, pending_sites


def _phase(run_id: str, state: str, message: str, **extra) -> None:
    current = _read_status()
    if current.get("run_id") == run_id:
        current.update(state=state, message=message, **extra)
        save_json(STATUS, current)


def _execute(command: list[str], folder: Path) -> None:
    with (folder / "采集日志.log").open("ab") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def worker(run_id: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run ID")
    folder = REPORTS / run_id
    lock_fd = acquire_collection_lock(LOCK)
    if lock_fd is None:
        _phase(run_id, "failed", "浏览器正在登录或采集，请稍后重试", finished_at=_now())
        return
    try:
        _phase(run_id, "discovering", "正在扫描卖家精灵近 30 天全部结果")
        discovery_partial = False
        try:
            _execute([sys.executable, str(ROOT / "discover_recent_buybox_stores.py"),
                      "--output-dir", str(folder)], folder)
        except subprocess.CalledProcessError:
            discovery_partial = True
        candidate_path = folder / "店铺候选.json"
        if not candidate_path.is_file():
            _phase(run_id, "failed", "卖家精灵扫描未取得店铺；已保存进度，稍后自动重试",
                   finished_at=_now())
            return
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        new_ids, pending_sites = _stores_to_check({item["seller_id"] for item in candidate["stores"]})
        save_json(folder / "新店铺ID.json", new_ids)
        if new_ids:
            _phase(run_id, "checking", f"正在核查 {len(new_ids)} 家新店铺或未完成店铺的欧洲站公开信息",
                   new_stores=len(new_ids), site_checks_total=pending_sites)
            _execute([sys.executable, str(ROOT / "run_store_eu_vat.py"),
                      "--output-dir", str(folder), "--seller-ids-file", str(folder / "新店铺ID.json")], folder)
        if discovery_partial:
            _phase(run_id, "failed", f"卖家精灵扫描未完成；已保存发现店铺并核查 {len(new_ids)} 家，稍后自动续采",
                   new_stores=len(new_ids), finished_at=_now())
        else:
            _phase(run_id, "complete", f"采集完成，核查 {len(new_ids)} 家新店铺或未完成店铺",
                   new_stores=len(new_ids), finished_at=_now())
    except Exception as error:
        _phase(run_id, "failed", f"采集失败：{type(error).__name__}: {error}", finished_at=_now())
        raise
    finally:
        release_collection_lock(LOCK, lock_fd)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "worker":
        raise SystemExit("usage: store_vat_collection.py worker RUN_ID")
    worker(sys.argv[2])
