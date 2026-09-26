import asyncio
import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.parse import urlencode

from api.auth import login_admin, verify_admin_session
from api.main import app


class FakeResponse:
    def __init__(self, body, cookie=None):
        self.body = json.dumps(body).encode()
        self.headers = Message()
        if cookie:
            self.headers.add_header("Set-Cookie", cookie)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self.body


async def request_app(path, method="GET", fields=None, headers=None):
    body = urlencode(fields or {}).encode()
    request_headers = [(b"host", b"127.0.0.1:8100")]
    request_headers.extend((key.lower().encode(), value.encode()) for key, value in (headers or {}).items())
    if fields is not None:
        request_headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": request_headers,
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8100),
    }
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(message for message in messages if message["type"] == "http.response.start")
    result_headers = {key.decode(): value.decode() for key, value in start["headers"]}
    result_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], result_headers, result_body


class WebsiteAuthContractTests(unittest.TestCase):
    def test_login_uses_only_website_admin_and_returns_website_cookie(self):
        captured = []

        def open_request(request):
            captured.append(json.loads(request.data))
            return FakeResponse(
                {"account": "admin", "is_admin": True},
                "chenyu_session=payload.signature; HttpOnly; SameSite=lax; Path=/",
            )

        with patch("api.auth._open", side_effect=open_request):
            self.assertEqual(login_admin("example-password"), "payload.signature")
        self.assertEqual(captured, [{"account": "admin", "password": "example-password"}])

    def test_session_requires_admin_account_and_role(self):
        with patch("api.auth._open", return_value=FakeResponse({"account": "staff", "is_admin": True})):
            self.assertFalse(verify_admin_session("payload.signature"))
        with patch("api.auth._open", return_value=FakeResponse({"account": "admin", "is_admin": True})):
            self.assertTrue(verify_admin_session("payload.signature"))
        self.assertFalse(verify_admin_session("malformed-cookie"))


class CrawlerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthenticated_pages_and_apis_are_blocked(self):
        for path in ("/", "/today"):
            status, headers, _ = await request_app(path)
            self.assertEqual(status, 303)
            self.assertTrue(headers["location"].startswith("/login?next="))
        status, _, body = await request_app("/status")
        self.assertEqual(status, 401)
        self.assertIn("请先以管理员登录", json.loads(body)["error"])
        with patch("api.main.start_manual_run") as run:
            status, _, _ = await request_app("/run", "POST", fields={})
            self.assertEqual(status, 303)
            run.assert_not_called()
        with patch("api.main.save_config") as save:
            status, _, _ = await request_app("/config", "POST", fields={}, headers={"accept": "application/json"})
            self.assertEqual(status, 401)
            save.assert_not_called()

    async def test_admin_login_and_logout(self):
        status, _, body = await request_app("/login")
        self.assertEqual(status, 200)
        self.assertIn(b"admin", body)
        with patch("api.main.login_admin") as login:
            status, _, _ = await request_app("/login", "POST", fields={"account": "staff", "password": "anything"})
            self.assertEqual(status, 401)
            login.assert_not_called()
            login.return_value = "payload.signature"
            status, headers, _ = await request_app("/login", "POST", fields={"account": "admin", "password": "example", "next": "/today"})
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "/today")
            self.assertIn("chenyu_session=payload.signature", headers["set-cookie"])
        with patch("api.main.verify_admin_session", return_value=True):
            status, headers, _ = await request_app("/logout", "POST", fields={}, headers={"cookie": "chenyu_session=payload.signature"})
            self.assertEqual(status, 303)
            self.assertIn("Max-Age=0", headers["set-cookie"])
