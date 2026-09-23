import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from api.main import app, home, remove_source, run_now
from crawler.config import load_config, save_config
from database.schema import initialize_schema
from scheduler_loop import claim_daily_run


class ConfigurationTests(unittest.TestCase):
    def test_only_configuration_page_is_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertEqual(paths, {"/", "/config", "/run", "/source/delete"})

    def test_configuration_page_renders(self) -> None:
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 8000),
            }
        )
        response = home(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Amazon", response.body)
        self.assertIn("立即运行".encode(), response.body)
        self.assertIn("删除".encode(), response.body)

    def test_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "daily_schedule": "06:00",
                        "sources": [
                            {
                                "marketplace": "US",
                                "category": "baby-products",
                                "url": "https://www.amazon.com/gp/new-releases/baby-products",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            config["daily_schedule"] = "07:30"
            save_config(path, config)
            self.assertEqual(load_config(path)["daily_schedule"], "07:30")

    def test_remove_source_updates_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "daily_schedule": "06:00",
                        "sources": [
                            {
                                "marketplace": "US",
                                "category": "baby-products",
                                "url": "https://www.amazon.com/gp/new-releases/baby-products",
                                "enabled": True,
                            },
                            {
                                "marketplace": "DE",
                                "category": "beauty",
                                "url": "https://www.amazon.de/gp/new-releases/beauty",
                                "enabled": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            remove_source("US_baby-products", path)
            sources = load_config(path)["sources"]
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["category"], "beauty")

    def test_run_now_starts_background_collection(self) -> None:
        with patch("api.main.start_manual_run", return_value=True):
            response = run_now()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/?run_started=true")

    def test_schema_contains_only_snapshot_and_scheduler_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.db"
            connection = sqlite3.connect(path)
            try:
                initialize_schema(connection)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                connection.close()
            self.assertEqual(tables, {"observations", "scheduler_state"})

    def test_scheduler_claims_only_one_run_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.db"

            def connect():
                connection = sqlite3.connect(path)
                initialize_schema(connection)
                return connection

            shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
            before_schedule = datetime(2026, 9, 23, 5, 59, tzinfo=shanghai)
            after_schedule = datetime(2026, 9, 23, 6, 0, tzinfo=shanghai)
            with patch("scheduler_loop.connect_database", side_effect=connect):
                self.assertFalse(claim_daily_run(before_schedule, "06:00"))
                self.assertTrue(claim_daily_run(after_schedule, "06:00"))
                self.assertFalse(claim_daily_run(after_schedule, "06:00"))


if __name__ == "__main__":
    unittest.main()
