import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_store_eu_vat as stores


class StoreEuVatTests(unittest.TestCase):
    def test_one_row_per_store_vats_follow_country_prefix(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(stores, 'OUTPUT', Path(folder)):
            records = {
                ('A1234567890', 'it'): {
                    'status': 'local_vat', 'company': 'ACME', 'address': 'Street 1',
                    'checked_at': '2026-10-08T12:00:00+08:00',
                    'vats': [{'number': 'IT01234567890', 'country': 'IT'}],
                },
                ('A1234567890', 'pl'): {
                    'status': 'other_vat', 'company': 'ACME', 'address': 'Street 1',
                    'checked_at': '2026-10-08T12:01:00+08:00',
                    'vats': [{'number': 'DE123456789', 'country': 'DE'}],
                },
            }
            path = stores.export(['A1234567890'], {'A1234567890': {'seller_name': 'ACME Shop'}}, records)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['店铺名称'], 'ACME Shop')
            self.assertEqual(rows[0]['IT税号'], 'IT01234567890')
            self.assertEqual(rows[0]['DE税号'], 'DE123456789')
            self.assertEqual(rows[0]['PL税号'], '')
            self.assertIn('pl:仅其他国税号', rows[0]['核查状态'])
            self.assertEqual(rows[0]['波兰店铺页'], 'https://www.amazon.pl/sp?seller=A1234567890')

    def test_resume_uses_latest_site_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'evidence.jsonl'
            with path.open('w', encoding='utf-8') as stream:
                stream.write(json.dumps({'seller_id': 'A1234567890', 'site': 'fr', 'status': 'failed'}) + '\n')
                stream.write(json.dumps({'seller_id': 'A1234567890', 'site': 'fr', 'status': 'local_vat'}) + '\n')
            self.assertEqual(stores.load_records(path)[('A1234567890', 'fr')]['status'], 'local_vat')
