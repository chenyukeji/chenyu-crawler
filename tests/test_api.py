import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from starlette.requests import Request

from api.main import app, home, remove_source, run_now, scheduler_state, update_config
from crawler.config import load_config, save_config
from database.schema import initialize_schema
from database.repository import SnapshotStore
from scheduler_loop import claim_daily_run


class PublicRestrictionMessageTests(unittest.TestCase):
    def test_existing_raw_site_notice_is_reported_as_automation_restriction(self):
        from api.main import public_error_message, today_source_cards
        raw = ('ACCESS_BLOCKED: AGENT_RESTRICTED; Amazon 访问受限 (unauthorized ai agent); '
               '阶段=翻页后; 第2页; HTTP=200; URL=https://www.amazon.com/gp/new-releases/beauty; '
               '已尝试 1 次；保留此前有效结果 50 条；证据=/tmp/access-evidence')
        public = public_error_message(raw)
        self.assertIn('自动化访问受限', public)
        self.assertIn('第2页', public)
        self.assertIn('HTTP=200', public)
        self.assertIn('50 条', public)
        self.assertIn('/tmp/access-evidence', public)
        self.assertNotIn('AI agent', public)
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory) / 'test.db')
            source = {'marketplace':'US', 'category':'beauty',
                      'url':'https://www.amazon.com/gp/new-releases/beauty', 'enabled':True}
            when = datetime(2026, 9, 26, 12, tzinfo=timezone(timedelta(hours=8)))
            rid = store.start_run(source_url=source['url'], marketplace='US', category='beauty',
                                  snapshot_date='2026-09-26', started_at=when.isoformat())
            store.finish_run(rid,status='BLOCKED',item_count=50,error_message=raw,now=when)
            with patch('api.main.connect_database',side_effect=store.connect):
                card = today_source_cards({'sources':[source], 'collection_policy':{'max_items_per_source':100},
                                           'daily_schedule':'06:00'},now=when)[0]
            self.assertEqual(card['status_label'], '自动化访问受限')
            self.assertEqual(card['reason'], public)

    def test_site_omitted_rank_50_card_is_complete_99_of_99(self):
        from api.main import today_source_cards
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory) / 'test.db')
            source = {'marketplace': 'US', 'category': 'lawn-garden',
                      'url': 'https://www.amazon.com/gp/new-releases/lawn-garden',
                      'enabled': True}
            when = datetime(2026, 9, 26, 12,
                            tzinfo=timezone(timedelta(hours=8)))
            rows = [dict(rank=rank, asin=f'B{rank:09d}', title=f'Product {rank}',
                         image_url='https://example.test/image.png')
                    for rank in range(1, 101) if rank != 50]
            store.ingest(rows, source_url=source['url'], marketplace='US',
                         category='lawn-garden', snapshot_date='2026-09-26')
            run_id = store.start_run(source_url=source['url'], marketplace='US',
                                     category='lawn-garden', snapshot_date='2026-09-26',
                                     started_at=when.isoformat())
            reason = '网站未展示第50名；其余99个真实排名已采集至末页'
            store.finish_run(run_id, status='COMPLETE', item_count=99,
                             error_message=reason, now=when)
            with patch('api.main.connect_database', side_effect=store.connect):
                card = today_source_cards(
                    {'sources': [source],
                     'collection_policy': {'max_items_per_source': 100},
                     'daily_schedule': '06:00'}, now=when,
                )[0]
            self.assertEqual(card['status_label'], '已完成')
            self.assertEqual((card['saved_count'], card['target'], card['percent']),
                             (99, 99, 100))
            self.assertEqual(card['reason'], reason)
            self.assertEqual(card['next_retry_at'], '')

    def test_other_error_remains_unchanged(self):
        from api.main import public_error_message
        self.assertEqual(public_error_message('HTTP_ERROR: 500'), 'HTTP_ERROR: 500')


class ConfigurationTests(unittest.TestCase):
    def test_only_configuration_page_is_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertEqual(paths, {"/", "/today", "/login", "/logout", "/config", "/run", "/source/delete", "/source/run", "/status"})

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
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory)/"test.db")
            with patch("api.main.scheduler_state", return_value={}), patch("api.main.database_stats", return_value={"chart": []}), patch("api.main.connect_database",side_effect=store.connect):
                response = home(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Amazon", response.body)
        self.assertIn("立即运行".encode(), response.body)
        self.assertIn("删除".encode(), response.body)

    def test_partial_status_names_the_failed_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.db"
            connection = sqlite3.connect(path)
            initialize_schema(connection)
            connection.executemany(
                "INSERT INTO collection_runs (run_id,source_url,marketplace,category,snapshot_date,started_at,status,item_count,error_message) VALUES (?,?,?,?,?,?,?,?,?)",
                [("1", "https://www.amazon.de/gp/new-releases/baby", "DE", "baby", "2026-09-24", "2026-09-24T06:00:00+08:00", "COMPLETE", 100, None),
                 ("2", "https://www.amazon.com/gp/new-releases/fashion", "US", "fashion", "2026-09-24", "2026-09-24T06:00:00+08:00", "FAILED", 0, "ACCESS_BLOCKED"),
                 ("3", "https://www.amazon.de/gp/new-releases/baby", "DE", "baby", "2026-09-24", "2026-09-24T12:00:00+08:00", "COMPLETE", 100, None)],
            )
            connection.commit()
            connection.close()
            with patch("api.main.connect_database", side_effect=lambda: sqlite3.connect(path)):
                state = scheduler_state()
            self.assertEqual(state["last_status"], "partial")
            self.assertIn("US/fashion: ACCESS_BLOCKED", state["last_message"])
            self.assertIn("完整 1/2", state["last_message"])

    def test_json_config_save_updates_schedule_and_only_selected_sources(self) -> None:
        def request_for(fields):
            body = urlencode(fields, doseq=True).encode()
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            return Request({
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/config", "raw_path": b"/config",
                "query_string": b"", "server": ("127.0.0.1", 8000),
                "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                            (b"accept", b"application/json")],
            }, receive)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            sources = [
                {"marketplace": market, "category": "baby-products",
                 "url": f"https://www.amazon.{domain}/gp/new-releases/baby-products",
                 "enabled": True}
                for market, domain in (("US", "com"), ("DE", "de"))
            ]
            save_config(path, {"daily_schedule": "06:00", "sources": sources})
            with patch("api.main.CONFIG_PATH", path):
                response = asyncio.run(update_config(request_for({
                    "daily_schedule": "07:30", "enabled_sources": ["US_baby-products"],
                })))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), {"saved": True})
                saved = load_config(path)
                self.assertEqual(saved["daily_schedule"], "07:30")
                self.assertEqual([source["enabled"] for source in saved["sources"]], [True, False])

                invalid = asyncio.run(update_config(request_for({"daily_schedule": "08:00"})))
                self.assertEqual(invalid.status_code, 400)
                self.assertIn("至少需要启用一个数据源", json.loads(invalid.body)["error"])
                self.assertEqual(load_config(path), saved)

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

    def test_schema_contains_only_collection_fact_tables(self) -> None:
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
            self.assertEqual(tables, {"observations", "product_seen", "collection_runs"})

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
            with patch("scheduler_loop.connect_database", side_effect=connect), patch("scheduler_loop.load_config", return_value={"sources":[{"enabled":True,"url":"https://example.com"}]}):
                self.assertFalse(claim_daily_run(before_schedule, "06:00"))
                self.assertTrue(claim_daily_run(after_schedule, "06:00"))
                with closing(connect()) as connection:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO collection_runs (
                                run_id, source_url, marketplace, category, snapshot_date,
                                started_at, status, item_count
                            ) VALUES ('run-1', 'https://example.com', 'US', 'example',
                                      '2026-09-23', '2026-09-23T06:00:00+08:00', 'RUNNING', 0)
                            """
                        )
                self.assertFalse(claim_daily_run(after_schedule, "06:00"))


if __name__ == "__main__":
    unittest.main()
