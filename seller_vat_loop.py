"""Run the seller VAT collector hourly, one browser session at a time."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from crawler.seller_vat import ROOT, TZ, collection_lock_held, save_json

PROFILE = ROOT / "data" / "sellersprite-profile"
STATUS = ROOT / "data" / "seller-vat-scheduler.json"
OUTPUTS = ROOT / "outputs" / "seller-vat"
INTERVAL = timedelta(hours=1)
WAIT_FOR_LOGIN_SECONDS = 60
FAILURE_BACKOFF_SECONDS = 900


def profile_ready(path: Path = PROFILE) -> bool:
    """A missing or empty browser profile cannot contain a login session."""
    return path.is_dir() and any(path.iterdir())


def profile_stamp(path: Path = PROFILE) -> tuple:
    """Only a fresh browser login can clear an observed login failure."""
    if not path.is_dir():
        return ()
    candidates = (
        path / "Local State", path / "Default" / "Preferences",
        path / "Default" / "Cookies", path / "Default" / "Cookies-wal",
        path / "Default" / "Network" / "Cookies",
        path / "Default" / "Network" / "Cookies-wal",
    )
    return tuple((str(candidate.relative_to(path)), candidate.stat().st_mtime_ns)
                 for candidate in candidates if candidate.is_file())


def run_id_at(started: datetime, folder: Path = OUTPUTS) -> str:
    stem = started.strftime("%Y-%m-%d_%H%M%S")
    candidate = stem
    suffix = 2
    while (folder / candidate).exists():
        candidate = f"{stem}_{suffix}"
        suffix += 1
    return candidate


def is_login_failure(manifest: dict) -> bool:
    error = str(manifest.get("error") or "").casefold()
    return any(marker in error for marker in (
        "尚未登录", "请登录", "登录失效", "验证码", "访问验证", "not logged in",
    ))


def _status(state: str, **details) -> None:
    save_json(STATUS, {"state": state, "updated_at": datetime.now(TZ).isoformat(timespec="seconds"), **details})


def _manifest(run_id: str) -> dict:
    path = OUTPUTS / run_id / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="每小时自动采集意大利站 BuyBox 店铺公开税号")
    parser.add_argument("--once", action="store_true", help="只执行一次调度判断，供诊断使用")
    args = parser.parse_args(argv)
    stopping = threading.Event()
    active: subprocess.Popen | None = None
    login_failed_stamp: tuple | None = None
    next_run_at: datetime | None = None
    last_run_id = ""

    def stop(_signum, _frame):
        stopping.set()
        if active and active.poll() is None:
            active.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping.is_set():
        if not profile_ready():
            _status("waiting_for_login", message="服务器尚无卖家精灵网页登录资料", last_run_id=last_run_id)
            if args.once:
                return 0
            stopping.wait(WAIT_FOR_LOGIN_SECONDS)
            continue
        if collection_lock_held(ROOT / "data" / "auto-collection.lock"):
            _status("waiting_for_login", message="服务器正在进行卖家精灵登录或其他税号采集", last_run_id=last_run_id)
            if args.once:
                return 0
            stopping.wait(WAIT_FOR_LOGIN_SECONDS)
            continue
        current_stamp = profile_stamp()
        if login_failed_stamp is not None and current_stamp == login_failed_stamp:
            _status("waiting_for_login", message="卖家精灵登录失效；请在服务器重新登录", last_run_id=last_run_id)
            if args.once:
                return 0
            stopping.wait(WAIT_FOR_LOGIN_SECONDS)
            continue
        login_failed_stamp = None
        now = datetime.now(TZ)
        if next_run_at and now < next_run_at:
            _status("scheduled", message="等待下一轮采集", next_run_at=next_run_at.isoformat(timespec="seconds"), last_run_id=last_run_id)
            if args.once:
                return 0
            stopping.wait(min(WAIT_FOR_LOGIN_SECONDS, (next_run_at - now).total_seconds()))
            continue
        started = now
        run_id = run_id_at(started)
        last_run_id = run_id
        _status("running", message="正在采集意大利站店铺税号", run_id=run_id, started_at=started.isoformat(timespec="seconds"))
        command = [sys.executable, str(ROOT / "run_seller_vat.py"), "run", "--channel", "chromium", "--headless", "--run-id", run_id]
        print(f"[税号调度] 开始 {run_id}", flush=True)
        try:
            active = subprocess.Popen(command, cwd=ROOT, env=os.environ.copy())
            exit_code = active.wait()
        except OSError as exc:
            exit_code = 2
            print(f"[税号调度] 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        finally:
            active = None
        if stopping.is_set():
            _status("stopped", message="后台服务已停止", last_run_id=run_id)
            break
        manifest = _manifest(run_id)
        if is_login_failure(manifest):
            login_failed_stamp = profile_stamp()
            _status("waiting_for_login", message="卖家精灵登录失效；请在服务器重新登录", last_run_id=run_id)
            next_run_at = None
        elif exit_code == 2 and not manifest:
            # Another process may have taken the browser profile lock between
            # our check and launch (for example, an interactive login).
            next_run_at = datetime.now(TZ) + timedelta(seconds=WAIT_FOR_LOGIN_SECONDS)
            _status("retry_wait", message="浏览器正被其他任务使用，稍后重试",
                    last_run_id=run_id, next_run_at=next_run_at.isoformat(timespec="seconds"))
        else:
            delay = INTERVAL if exit_code in (0, 3) else timedelta(seconds=FAILURE_BACKOFF_SECONDS)
            next_run_at = max(started + INTERVAL, datetime.now(TZ) + (delay if exit_code not in (0, 3) else timedelta()))
            _status("scheduled" if exit_code in (0, 3) else "retry_wait",
                    message="上一轮已完成" if exit_code == 0 else "上一轮部分完成" if exit_code == 3 else "上一轮失败，等待重试",
                    last_run_id=run_id, last_exit_code=exit_code,
                    last_result=manifest.get("status", ""),
                    last_error=str(manifest.get("error") or "")[:300],
                    next_run_at=next_run_at.isoformat(timespec="seconds"))
        print(f"[税号调度] {run_id} 退出码 {exit_code}", flush=True)
        if args.once:
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
