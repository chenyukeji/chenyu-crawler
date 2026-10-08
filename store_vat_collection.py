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
            item["stores_found"] = len(candidate.get("stores", []))
        except (OSError, ValueError, TypeError):
            pass
        try:
            item["site_checks"] = sum(1 for _ in (folder / "店铺公开信息_逐站证据.jsonl").open(encoding="utf-8"))
        except OSError:
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
        run_id = "eu-store-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
        folder = REPORTS / run_id
        folder.mkdir(parents=True)
        initial = {"state": "queued", "run_id": run_id, "message": "已排队，准备扫描卖家精灵全部结果",
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


def _known_store_ids() -> set[str]:
    if not DB.is_file():
        raise FileNotFoundError(DB)
    with sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        known = {row[0] for row in db.execute("SELECT DISTINCT seller_id FROM sellers")}
        if "seller_discoveries" in tables:
            known.update(row[0] for row in db.execute("SELECT seller_id FROM seller_discoveries"))
        return known


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
        baseline = _known_store_ids()
        _phase(run_id, "discovering", "正在扫描卖家精灵近 30 天全部结果")
        _execute([sys.executable, str(ROOT / "discover_recent_buybox_stores.py"),
                  "--output-dir", str(folder)], folder)
        candidate = json.loads((folder / "店铺候选.json").read_text(encoding="utf-8"))
        new_ids = sorted({item["seller_id"] for item in candidate["stores"]} - baseline)
        save_json(folder / "新店铺ID.json", new_ids)
        if new_ids:
            _phase(run_id, "checking", f"已发现 {len(new_ids)} 家新店铺，正在核查欧洲站公开信息",
                   new_stores=len(new_ids), site_checks_total=len(new_ids) * 5)
            _execute([sys.executable, str(ROOT / "run_store_eu_vat.py"),
                      "--output-dir", str(folder), "--seller-ids-file", str(folder / "新店铺ID.json")], folder)
        _phase(run_id, "complete", f"采集完成，新增 {len(new_ids)} 家店铺",
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
