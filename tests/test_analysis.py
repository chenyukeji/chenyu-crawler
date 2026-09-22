import tempfile
import unittest
from pathlib import Path

from database.repository import TrendStore
from services.selection_queries import (
    get_hot_clusters,
    get_new_entries,
    get_rank_history,
    get_repeat_products,
    get_rising_products,
    get_selection_candidates,
)


SOURCE = "https://www.amazon.de/gp/new-releases/example"


def item(rank, asin, title, product_type):
    return {
        "rank": rank,
        "asin": asin,
        "title": title,
        "product_type": product_type,
        "product_url": f"https://www.amazon.de/dp/{asin}",
    }


class SelectionAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "new_releases.db"
        self.store = TrendStore(self.db, retention_days=10, identity_retention_days=90)

        self.store.ingest(
            [
                item(43, "B000000001", "Magnetic Cup Holder A", "Magnetic Cup Holder"),
                item(5, "B000000002", "Bottle Brush", "Bottle Brush"),
            ],
            source_url=SOURCE,
            marketplace="DE",
            category="automotive",
            snapshot_date="2026-09-19",
        )
        self.store.ingest(
            [
                item(17, "B000000001", "Magnetic Cup Holder A", "Magnetic Cup Holder"),
                item(6, "B000000002", "Bottle Brush", "Bottle Brush"),
                item(50, "B000000003", "Magnetic Cup Holder C", "Magnetic Cup Holder"),
            ],
            source_url=SOURCE,
            marketplace="DE",
            category="automotive",
            snapshot_date="2026-09-20",
        )
        self.store.ingest(
            [
                item(8, "B000000001", "Magnetic Cup Holder A", "Magnetic Cup Holder"),
                item(8, "B000000002", "Bottle Brush", "Bottle Brush"),
                item(20, "B000000003", "Magnetic Cup Holder C", "Magnetic Cup Holder"),
                item(15, "B000000004", "Magnetic Cup Holder D", "Magnetic Cup Holder"),
            ],
            source_url=SOURCE,
            marketplace="DE",
            category="automotive",
            snapshot_date="2026-09-21",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_new_entries_uses_identity_first_seen(self) -> None:
        rows = get_new_entries(
            "DE",
            days=1,
            category="automotive",
            db_path=self.db,
        )
        self.assertEqual([row["asin"] for row in rows], ["B000000004"])

    def test_repeat_products_counts_snapshots(self) -> None:
        rows = get_repeat_products(
            "DE",
            days=10,
            min_days=3,
            category="automotive",
            db_path=self.db,
        )
        by_asin = {row["asin"]: row for row in rows}
        self.assertEqual(by_asin["B000000001"]["days_present"], 3)
        self.assertEqual(by_asin["B000000001"]["consecutive_snapshots"], 3)
        self.assertEqual(by_asin["B000000002"]["days_present"], 3)

    def test_rank_trend_is_computed_from_history_not_stored_previous_rank(self) -> None:
        history = get_rank_history(
            "B000000001",
            "DE",
            days=10,
            category="automotive",
            db_path=self.db,
        )
        self.assertEqual([row["rank"] for row in history], [43, 17, 8])

        rows = get_rising_products(
            "DE",
            days=7,
            min_improvement=5,
            category="automotive",
            db_path=self.db,
        )
        by_asin = {row["asin"]: row for row in rows}
        self.assertEqual(by_asin["B000000001"]["previous_rank"], 17)
        self.assertEqual(by_asin["B000000001"]["period_rank_change"], 35)
        self.assertNotIn("B000000002", by_asin)

    def test_product_clusters_and_candidate_signals(self) -> None:
        clusters = get_hot_clusters(
            "DE",
            days=10,
            min_unique_asins=2,
            category="automotive",
            db_path=self.db,
        )
        self.assertEqual(clusters[0]["product_type"], "Magnetic Cup Holder")
        self.assertEqual(clusters[0]["unique_asins"], 3)

        candidates = get_selection_candidates(
            "DE",
            category="automotive",
            db_path=self.db,
        )
        by_asin = {row["asin"]: row for row in candidates}
        self.assertIn("RISING", by_asin["B000000001"]["signals"])
        self.assertIn("REPEAT", by_asin["B000000001"]["signals"])
        self.assertIn("NEW", by_asin["B000000004"]["signals"])


if __name__ == "__main__":
    unittest.main()
