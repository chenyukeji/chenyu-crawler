"""Backward-compatible entrypoint.

New code lives in crawler/, database/, analysis/, services/ and run_daily.py.
Keep this module so existing commands and tests that import collector.py continue to work.
"""

from contextlib import closing
from datetime import date

from crawler.amazon import (
    AccessControlBlocked,
    BrowserSettings,
    collect_source,
    deduplicate_items,
    detect_access_blocker,
    infer_product_type,
    infer_title_selling_points,
    parse_first_number,
    parse_number,
    read_json,
    validate_source,
    write_json,
)
from database.repository import TrendStore
from run_daily import build_parser, codex_notification, main


__all__ = [
    "AccessControlBlocked",
    "BrowserSettings",
    "TrendStore",
    "build_parser",
    "codex_notification",
    "collect_source",
    "deduplicate_items",
    "detect_access_blocker",
    "infer_product_type",
    "infer_title_selling_points",
    "main",
    "parse_first_number",
    "parse_number",
    "read_json",
    "validate_source",
    "write_json",
    "closing",
    "date",
]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        import sys

        print(f"CODEX_NOTIFY: Amazon 新品榜采集启动失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
