"""Use one headless SellerSprite browser to establish the collector's login session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from crawler.seller_vat import (
    DEFAULT_URL, ROOT, SESSION_FILE, TZ, acquire_collection_lock,
    release_collection_lock, restore_sellersprite_session, save_json, save_sellersprite_session,
)

PROFILE = ROOT / "data" / "sellersprite-profile"
LOCK = ROOT / "data" / "auto-collection.lock"
STATUS = ROOT / "data" / "seller-vat-login.json"
CREDENTIALS = Path(os.getenv("CHENYU_SELLERSPRITE_CONFIG_PATH", "/home/ubuntu/chenyu/.chenyu-secrets/sellersprite.json"))
LOGIN_URL = "https://www.sellersprite.com/cn/w/user/login"
TIMEOUT_SECONDS = 90


def default_credentials() -> tuple[str, str]:
    try:
        payload = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return str(payload.get("username") or ""), str(payload.get("password") or "")
    except (OSError, ValueError):
        pass
    return "", ""


def default_account() -> str:
    return default_credentials()[0]


def status() -> dict:
    try:
        value = json.loads(STATUS.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {"state": "not_started", "message": "尚未登录卖家精灵"}


def set_status(state: str, message: str) -> None:
    save_json(STATUS, {
        "state": state, "message": message,
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })


def start(account: str, password: str) -> tuple[bool, str]:
    """Pass credentials over a one-use pipe; never put them in argv, env or files."""
    from crawler.seller_vat import collection_lock_held
    if collection_lock_held(LOCK):
        return False, "浏览器正在登录或采集，请稍后重试"
    set_status("starting", "正在登录卖家精灵")
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps({"account": account, "password": password}).encode())
            process.stdin.flush()
        finally:
            process.stdin.close()
    except (OSError, BrokenPipeError):
        set_status("failed", "登录浏览器启动失败")
        return False, "登录浏览器启动失败"
    return True, "正在登录卖家精灵"


def start_from_config() -> tuple[bool, str]:
    account, password = default_credentials()
    if not account or not password:
        return False, "请先在员工与权限页面配置卖家精灵账号"
    return start(account, password)


def verified_identity(page, timeout_seconds=20) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        identity = page.locator(".profile-trigger:visible").first
        if identity.count():
            label = identity.inner_text(timeout=1000).strip()
            if label and not any(word in label.lower() for word in ("未登录", "游客", "登录/注册", "sign in", "log in")):
                return True
        page.wait_for_timeout(500)
    return False


def worker() -> int:
    lock_fd = acquire_collection_lock(LOCK)
    if lock_fd is None:
        set_status("busy", "浏览器正在采集或登录，请稍后重试")
        return 2
    try:
        credentials = json.load(sys.stdin)
        account = credentials.pop("account", "")
        password = credentials.pop("password", "")
        del credentials
        if not account or not password:
            set_status("failed", "请输入账号和密码")
            return 1
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            context = runtime.chromium.launch_persistent_context(
                str(PROFILE), headless=True, viewport={"width": 1440, "height": 1000},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                session_path = PROFILE / SESSION_FILE
                try:
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    session = {}
                if session.get("account") == account:
                    restore_sellersprite_session(context, PROFILE, account)
                    page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60000)
                    if verified_identity(page, 12):
                        save_sellersprite_session(context, PROFILE, account)
                        set_status("logged_in", "卖家精灵登录成功，可以启动采集")
                        return 0
                # A changed or expired account needs a fresh browser login.
                context.clear_cookies()
                session_path.unlink(missing_ok=True)
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                page.evaluate("localStorage.clear(); sessionStorage.clear()")
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(800)
                account_input = page.locator("input[name='email']:visible").first
                password_input = page.locator("input[type='password']:visible").first
                submit = page.get_by_role("button", name="立即登录").first
                if not account_input.count() or not password_input.count() or not submit.count():
                    set_status("failed", "未找到卖家精灵账号密码登录表单")
                    return 1
                account_input.fill(account)
                password_input.fill(password)
                del password
                submit.click()
                deadline = time.monotonic() + TIMEOUT_SECONDS
                challenge_seen = False
                while time.monotonic() < deadline:
                    body = page.locator("body").inner_text(timeout=1000)
                    challenge_seen = challenge_seen or any(marker in body for marker in ("请完成安全验证", "向右滑动完成验证", "人机验证"))
                    if "账号或密码错误" in body or "用户名或密码错误" in body:
                        set_status("failed", "卖家精灵拒绝登录，请检查账号密码")
                        return 1
                    if "/user/login" not in page.url and "/user/signin" not in page.url:
                        break
                    page.wait_for_timeout(500)
                else:
                    set_status("failed", "卖家精灵要求页面验证，请稍后在网页登录" if challenge_seen else "登录未完成，请检查账号密码或页面验证要求")
                    return 1
                page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=60000)
                # A redirect alone is insufficient; verify product-research identity.
                if verified_identity(page):
                    save_sellersprite_session(context, PROFILE, account)
                    set_status("logged_in", "卖家精灵登录成功，可以启动采集")
                    return 0
                set_status("failed", "选产品页面仍显示游客，请检查登录或账号权限")
                return 1
            finally:
                context.close()
    except Exception:
        set_status("failed", "登录失败，请检查账号密码、网络或页面验证要求")
        return 1
    finally:
        release_collection_lock(LOCK, lock_fd)


if __name__ == "__main__":
    raise SystemExit(worker() if sys.argv[1:] == ["worker"] else 2)
