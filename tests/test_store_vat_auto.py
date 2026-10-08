import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import discover_recent_buybox_stores as discovery
import store_vat_scheduler as scheduler
from crawler.seller_vat import TZ


class _Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return {"code": "OK", "data": self.data}


class _Request:
    def __init__(self):
        self.calls = []

    def post(self, _url, data, **_kwargs):
        payload = json.loads(data)
        self.calls.append(payload)
        low = payload.get("minPrice")
        high = payload.get("maxPrice")
        if low is None and high is None:
            return _Response({"total": 2100, "size": 60, "items": [{"asin": "ROOT"}]})
        seller = "A1111111111" if high == "100" else "A2222222222"
        return _Response({"total": 1, "size": 60, "items": [{"asin": seller, "sellerId": seller}]})


class _Context:
    def __init__(self):
        self.request = _Request()


class StoreVatAutoTests(unittest.TestCase):
    def test_capped_results_are_split_and_resumed_without_repeating_saved_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            context = _Context()
            saved = []
            with patch.object(discovery, "REQUEST_DELAY", 0), \
                 patch.object(discovery, "save_discoveries", side_effect=lambda _db, rows: saved.extend(rows)):
                discovery.scan(context, {"page": 1, "size": 60}, {}, output, output / "db", max_pages=1)
                checkpoint = json.loads((output / "发现进度.json").read_text())
                self.assertEqual(checkpoint["pages_checked"], 1)
                self.assertFalse(checkpoint["complete"])
                self.assertEqual(len(saved), 1)
                self.assertEqual(len(json.loads((output / "店铺候选.json").read_text())["stores"]), 1)
                discovery.scan(context, {"page": 1, "size": 60}, {}, output, output / "db")
            self.assertEqual(len(saved), 2)
            self.assertEqual(len(context.request.calls), 3)
            self.assertTrue(json.loads((output / "发现进度.json").read_text())["complete"])

    def test_automatic_schedule_waits_for_active_or_recent_runs(self):
        now = datetime.now(TZ)
        self.assertFalse(scheduler.due({"state": "discovering"}, now))
        self.assertFalse(scheduler.due({"state": "failed", "finished_at": (now - timedelta(minutes=30)).isoformat()}, now))
        self.assertTrue(scheduler.due({"state": "failed", "finished_at": (now - timedelta(hours=2)).isoformat()}, now))
        self.assertFalse(scheduler.due({"state": "complete", "finished_at": (now - timedelta(hours=12)).isoformat()}, now))
        self.assertTrue(scheduler.due({"state": "complete", "finished_at": (now - timedelta(hours=25)).isoformat()}, now))


if __name__ == "__main__":
    unittest.main()
