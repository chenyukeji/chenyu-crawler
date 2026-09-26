"""Authenticate crawler administrators against the existing CHENYU website."""

from __future__ import annotations

import json
import os
import re
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


AUTH_BASE = os.getenv("CHENYU_WEB_AUTH_BASE", "http://127.0.0.1:8000").rstrip("/")
SESSION_COOKIE = "chenyu_session"
ADMIN_ACCOUNT = "admin"
_TIMEOUT = 4
_SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class AuthenticationUnavailable(Exception):
    """The website account service could not complete a login request."""


def _open(request: Request):
    # The account service is local; do not send credentials through an ambient proxy.
    return build_opener(ProxyHandler({})).open(request, timeout=_TIMEOUT)


def login_admin(password: str) -> str | None:
    """Return the website session cookie for admin, or None for bad credentials."""
    request = Request(
        f"{AUTH_BASE}/api/auth/login",
        data=json.dumps({"account": ADMIN_ACCOUNT, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open(request) as response:
            user = json.load(response)
            if not isinstance(user, dict):
                raise AuthenticationUnavailable
            if user.get("account") != ADMIN_ACCOUNT or user.get("is_admin") is not True:
                return None
            cookies = SimpleCookie()
            for header in response.headers.get_all("Set-Cookie", []):
                cookies.load(header)
            session = cookies.get(SESSION_COOKIE)
            if not session or not _SESSION_PATTERN.fullmatch(session.value):
                raise AuthenticationUnavailable
            return session.value
    except HTTPError as exc:
        if exc.code in (401, 403):
            return None
        raise AuthenticationUnavailable from exc
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationUnavailable from exc


def verify_admin_session(session: str) -> bool:
    """Recheck the website session so disabled accounts lose crawler access."""
    if not _SESSION_PATTERN.fullmatch(session):
        return False
    request = Request(
        f"{AUTH_BASE}/api/auth/me",
        headers={"Cookie": f"{SESSION_COOKIE}={session}"},
    )
    try:
        with _open(request) as response:
            user = json.load(response)
            return isinstance(user, dict) and user.get("account") == ADMIN_ACCOUNT and user.get("is_admin") is True
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return False
