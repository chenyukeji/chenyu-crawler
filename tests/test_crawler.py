import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from crawler.amazon import (
    deduplicate_items,
    detect_access_blocker,
    parse_first_number,
    validate_source,
)
from database.repository import SnapshotStore


class CollectorTests(unittest.TestCase):
    def test_detects_access_control_pages(self) -> None:
        self.assertEqual(detect_access_blocker("Robot Check"), "robot check")
        self.assertEqual(
            detect_access_blocker("ordinary page", "https://www.amazon.com/ap/signin", "Amazon Sign-In"),
            "Amazon login page",
        )
        self.assertIsNone(detect_access_blocker("Amazon Hot New Releases"))

    def test_deduplicates_asins_and_preserves_rank(self) -> None:
        rows = [
            {"rank": 2, "asin": "B000000001", "title": "Small Pet Water Bowl", "review_count": "12"},
            {"rank": 3, "asin": "B000000001", "title": "Small Pet Water Bowl", "review_count": "12"},
            {"rank": 9, "asin": "B000000002", "title": "Travel Pet Water Bowl", "price": "$9.99"},
        ]
        result = deduplicate_items(rows, 60)
        self.assertEqual([row["asin"] for row in result], ["B000000001", "B000000002"])
        self.assertEqual([row["rank"] for row in result], [2, 9])
        self.assertEqual(result[1]["price"], 9.99)

    def test_deduplicate_items_sorts_by_rank_before_deduping(self) -> None:
        rows = [
            {"rank": 5, "asin": "B000000003", "title": "Later Product"},
            {"rank": 2, "asin": "B000000001", "title": "First Product"},
            {"rank": 2, "asin": "B000000002", "title": "Second Product"},
        ]
        result = deduplicate_items(rows, 3)
        self.assertEqual([row["asin"] for row in result], ["B000000001", "B000000002", "B000000003"])
        self.assertEqual([row["rank"] for row in result], [2, 2, 5])

    def test_parses_localized_rating(self) -> None:
        self.assertEqual(parse_first_number("4.7 out of 5 stars"), 4.7)
        self.assertEqual(parse_first_number("4,6 von 5 Sternen"), 4.6)

    def test_rejects_non_new_release_urls(self) -> None:
        with self.assertRaises(ValueError):
            validate_source(
                {"marketplace": "US", "category": "baby", "url": "https://example.com/gp/new-releases/baby"}
            )

    def test_rejects_marketplace_domain_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            validate_source(
                {
                    "marketplace": "US",
                    "category": "baby",
                    "url": "https://www.amazon.de/gp/new-releases/baby",
                }
            )

    def test_store_replaces_same_day_snapshot_and_keeps_latest_7_calendar_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new_releases.db"
            store = SnapshotStore(path)
            self.assertEqual(store.retention_days, 7)
            source = "https://www.amazon.de/gp/new-releases/example"
            first_run = [
                {
                    "rank": 1,
                    "asin": "b000000001",
                    "title": "First Run Product",
                    "image_url": "https://images.example/1.jpg",
                },
                {
                    "rank": 2,
                    "asin": "B000000002",
                    "title": "Dropped Product",
                    "image_url": "https://images.example/2.jpg",
                },
            ]
            second_run = [
                {
                    "rank": 1,
                    "asin": "B000000001",
                    "title": "Updated Product",
                    "image_url": "https://images.example/1.jpg",
                },
                {
                    "rank": 2,
                    "asin": "B000000003",
                    "title": "New Product",
                    "image_url": "https://images.example/3.jpg",
                },
            ]
            for items, snapshot_date in (
                (first_run, "2026-09-01"),
                (second_run, "2026-09-01"),
                (second_run, "2026-09-01"),
                (first_run, "2026-07-01"),
            ):
                store.ingest(
                    items,
                    source_url=source,
                    marketplace="DE",
                    category="example",
                    snapshot_date=snapshot_date,
                )
            store.prune(date.fromisoformat("2026-09-01"))
            with closing(store.connect()) as connection:
                dates = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT snapshot_date FROM observations WHERE source_url = ? ORDER BY snapshot_date",
                        (source,),
                    ).fetchall()
                ]
                rows = connection.execute(
                    "SELECT asin, title FROM observations WHERE snapshot_date = '2026-09-01' ORDER BY rank"
                ).fetchall()
                count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(observations)").fetchall()
                }
                seen = connection.execute(
                    "SELECT asin, first_seen, last_seen FROM product_seen ORDER BY asin"
                ).fetchall()
            self.assertEqual(dates, ["2026-09-01"])
            self.assertEqual(count, 2)
            self.assertEqual([row[0] for row in rows], ["B000000001", "B000000003"])
            self.assertEqual(rows[0][1], "Updated Product")
            self.assertEqual(
                [tuple(row) for row in seen],
                [
                    ("B000000001", "2026-07-01", "2026-09-01"),
                    ("B000000002", "2026-07-01", "2026-09-01"),
                    ("B000000003", "2026-09-01", "2026-09-01"),
                ],
            )
            self.assertNotIn("product_type", columns)
            self.assertNotIn("selling_points_json", columns)

    def test_collection_run_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory) / "new_releases.db")
            run_id = store.start_run(
                source_url="https://www.amazon.com/gp/new-releases/example",
                marketplace="US",
                category="example",
                snapshot_date="2026-09-23",
                started_at="2026-09-23T08:15:00+08:00",
            )
            store.finish_run(run_id, status="COMPLETE", item_count=100)
            with closing(store.connect()) as connection:
                row = connection.execute(
                    "SELECT status, item_count, error_message FROM collection_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            self.assertEqual(tuple(row), ("COMPLETE", 100, None))


if __name__ == "__main__":
    unittest.main()
