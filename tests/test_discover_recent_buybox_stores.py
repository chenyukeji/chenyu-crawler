import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discover_recent_buybox_stores as discovery
from merge_store_vat_database import save_discoveries


class DiscoverRecentStoresTests(unittest.TestCase):
    def test_first_store_stays_committed_if_next_result_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'amazon_it.sqlite3'
            sqlite3.connect(db_path).close()
            candidates = {}
            calls = 0

            def save_one(path, stores):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError('interrupted')
                return save_discoveries(path, stores)

            items = [{'sellerId': 'A1234567890', 'sellerName': 'First'},
                     {'sellerId': 'A1234567891', 'sellerName': 'Second'}]
            with patch.object(discovery, 'save_discoveries', side_effect=save_one):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    discovery.add_page_candidates(items, candidates, 1, db_path)
            with sqlite3.connect(db_path) as db:
                stored = db.execute('SELECT seller_id FROM seller_discoveries').fetchall()
            self.assertEqual(stored, [('A1234567890',)])
            self.assertEqual(list(candidates), ['A1234567890'])


if __name__ == '__main__':
    unittest.main()
