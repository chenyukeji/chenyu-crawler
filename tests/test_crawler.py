import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from crawler.amazon import (
    deduplicate_items,
    detect_access_blocker,
    infer_title_selling_points,
    parse_first_number,
    validate_source,
)
from database.repository import TrendStore


class CollectorTests(unittest.TestCase):
    def test_detects_access_control_pages(self) -> None:
        self.assertEqual(detect_access_blocker("Robot Check"), "robot check")
        self.assertEqual(
            detect_access_blocker("ordinary page", "https://www.amazon.com/ap/signin", "Amazon Sign-In"),
            "Amazon login page",
        )
        self.assertIsNone(detect_access_blocker("Amazon Hot New Releases"))

    def test_deduplicates_asins_and_rebuilds_rank(self) -> None:
        rows = [
            {"rank": 2, "asin": "B000000001", "title": "Small Pet Water Bowl", "review_count": "12"},
            {"rank": 3, "asin": "B000000001", "title": "Small Pet Water Bowl", "review_count": "12"},
            {"rank": 9, "asin": "B000000002", "title": "Travel Pet Water Bowl", "price": "$9.99"},
        ]
        result = deduplicate_items(rows, 60)
        self.assertEqual([row["asin"] for row in result], ["B000000001", "B000000002"])
        self.assertEqual([row["rank"] for row in result], [1, 2])
        self.assertEqual(result[1]["price"], 9.99)

    def test_deduplicate_items_sorts_by_rank_before_deduping(self) -> None:
        rows = [
            {"rank": 5, "asin": "B000000003", "title": "Later Product"},
            {"rank": 2, "asin": "B000000001", "title": "First Product"},
            {"rank": 2, "asin": "B000000002", "title": "Second Product"},
        ]
        result = deduplicate_items(rows, 3)
        self.assertEqual([row["asin"] for row in result], ["B000000001", "B000000002", "B000000003"])
        self.assertEqual([row["rank"] for row in result], [1, 2, 3])

    def test_extracts_title_selling_points(self) -> None:
        title = "Insulated Bottle | Leakproof Lid | Easy Carry Handle | BPA Free"
        self.assertEqual(
            infer_title_selling_points(title),
            ["Leakproof Lid", "Easy Carry Handle", "BPA Free"],
        )
        self.assertEqual(infer_title_selling_points("Plain Product Name"), [])

    def test_parses_localized_rating(self) -> None:
        self.assertEqual(parse_first_number("4.7 out of 5 stars"), 4.7)
        self.assertEqual(parse_first_number("4,6 von 5 Sternen"), 4.6)

    def test_rejects_non_new_release_urls(self) -> None:
        with self.assertRaises(ValueError):
            validate_source(
                {"marketplace": "US", "category": "baby", "url": "https://example.com/gp/new-releases/baby"}
            )

    def test_store_replaces_same_day_snapshot_and_keeps_latest_30_calendar_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new_releases.db"
            store = TrendStore(path, retention_days=30)
            source = "https://www.amazon.de/gp/new-releases/example"
            first_run = [
                {"rank": 1, "asin": "B000000001", "title": "First Run Product"},
                {"rank": 2, "asin": "B000000002", "title": "Dropped Product"},
            ]
            second_run = [
                {"rank": 1, "asin": "B000000001", "title": "Updated Product"},
                {"rank": 2, "asin": "B000000003", "title": "New Product"},
            ]
            store.ingest(first_run, source_url=source, marketplace="DE", snapshot_date="2026-09-01")
            store.ingest(second_run, source_url=source, marketplace="DE", snapshot_date="2026-09-01")
            store.ingest(second_run, source_url=source, marketplace="DE", snapshot_date="2026-09-01")
            store.ingest(first_run, source_url=source, marketplace="DE", snapshot_date="2026-07-01")
            store.prune(date.fromisoformat("2026-09-01"))
            self.assertEqual(store.snapshot_dates(source), ["2026-09-01"])
            with closing(store.connect()) as connection:
                rows = connection.execute(
                    "SELECT asin, title FROM observations WHERE snapshot_date = '2026-09-01' ORDER BY rank"
                ).fetchall()
                count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(observations)").fetchall()
                }
            self.assertEqual(count, 2)
            self.assertEqual([row[0] for row in rows], ["B000000001", "B000000003"])
            self.assertEqual(rows[0][1], "Updated Product")
            self.assertIn("selling_points_json", columns)


if __name__ == "__main__":
    unittest.main()
