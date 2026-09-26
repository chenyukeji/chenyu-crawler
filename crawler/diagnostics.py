"""Allowlisted diagnostic metadata; never infer the effective network route."""
from __future__ import annotations

import os
import platform
from collections.abc import Mapping

SAFE_RESPONSE_HEADERS = frozenset({
    "retry-after", "content-type", "date", "age", "server", "via", "x-cache",
    "x-amz-rid", "x-amzn-requestid", "x-amz-cf-id",
})


def safe_response_headers(headers):
    if not isinstance(headers, Mapping):
        return {}
    return {str(k).lower(): v for k, v in headers.items()
            if str(k).lower() in SAFE_RESPONSE_HEADERS}


def process_context():
    # Record presence only: proxy URLs and bypass lists can contain credentials.
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "systemd_invocation": bool(os.environ.get("INVOCATION_ID")),
        "proxy_environment_present": [name for name in proxy_names if os.environ.get(name)],
        "proxy_bypass_configured": bool(os.environ.get("NO_PROXY") or os.environ.get("no_proxy")),
        "effective_network_route": "not_measured",
    }
