import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.store_vat_view import load_amazon_db
from merge_store_vat_database import merge
from tests.test_auth import request_app


SELLERS_SCHEMA = '''
CREATE TABLE sellers (
  id INTEGER PRIMARY KEY, marketplace TEXT NOT NULL, seller_id TEXT NOT NULL,
  company_name TEXT, vat_number TEXT, vies_company_name TEXT,
  business_address TEXT, source_url TEXT, first_seen_at TEXT, last_seen_at TEXT,
  UNIQUE(marketplace, seller_id)
);
'''


class StoreVatDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'amazon_it.sqlite3'
        with sqlite3.connect(self.db_path) as db:
            db.executescript(SELLERS_SCHEMA)
            db.execute('''INSERT INTO sellers
                (marketplace,seller_id,company_name,vat_number,vies_company_name)
                VALUES (?,?,?,?,?)''',
                ('amazon.it', 'A1234567890', '<script>alert(1)</script>',
                 'IT12345678901', 'Verified Company'))

    def test_saved_fields_search_filters_pagination_and_escaping(self):
        with sqlite3.connect(self.db_path) as db:
            db.executemany('INSERT INTO sellers(marketplace,seller_id,vat_number) VALUES (?,?,?)', [
                ('amazon.fr', f'A{index:010d}', f'FR{index:011d}') for index in range(51)
            ])
        result = load_amazon_db(self.db_path, query='Verified Company')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['rows'][0]['vies_company_name'], 'Verified Company')
        self.assertIn('vies_company_name', [column['key'] for column in result['columns']])
        self.assertEqual(load_amazon_db(self.db_path, country='IT')['total'], 1)
        second = load_amazon_db(self.db_path, country='FR', page=2)
        self.assertEqual((second['total'], second['pages'], len(second['rows'])), (51, 2, 1))
        self.assertTrue(second['previous_url'])
        self.assertFalse(second['next_url'])
        self.assertEqual(load_amazon_db(self.db_path, query='%')['total'], 0)
        with patch('api.main.AMAZON_IT_DB_PATH', self.db_path), \
             patch('api.main.verify_admin_session', return_value=True):
            status, _, body = asyncio.run(request_app(
                '/seller-vat',
                headers={'cookie': 'chenyu_session=test'},
            ))
        self.assertEqual(status, 200)
        self.assertIn('Verified Company'.encode(), body)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;'.encode(), body)
        self.assertNotIn('<script>alert(1)</script>'.encode(), body)

    def test_collector_checks_new_vats_after_saving(self):
        from run_store_eu_vat import main_async
        legacy_db = self.root / 'collector.sqlite3'
        with sqlite3.connect(legacy_db) as db:
            db.execute('CREATE TABLE auto_sellers (seller_id TEXT)')
            db.execute("INSERT INTO auto_sellers VALUES ('A1234567890')")
        with patch('run_store_eu_vat.DB', legacy_db), \
             patch('run_store_eu_vat.OUTPUT', self.root / 'report'), \
             patch('run_store_eu_vat.STORE_DB', self.db_path), \
             patch('run_store_eu_vat.save_results', return_value=self.root / 'report.csv') as save, \
             patch('run_store_eu_vat.run_vies', return_value={'checked': 1}) as vies:
            asyncio.run(main_async(0, 1, {'it', 'fr', 'de', 'pl', 'es'}))
        save.assert_called_once()
        vies.assert_called_once_with(self.db_path)

    def test_missing_database_is_not_created(self):
        missing = self.root / 'missing.sqlite3'
        self.assertIsNone(load_amazon_db(missing))
        self.assertFalse(missing.exists())

    def test_merge_preserves_original_and_is_idempotent(self):
        report = self.root / 'report'
        report.mkdir()
        (report / '店铺候选.json').write_text(json.dumps({'stores': [
            {'seller_id': 'A1234567890', 'seller_name': 'Old', 'first_page': 1},
            {'seller_id': 'A1234567891', 'seller_name': 'New', 'first_page': 2},
        ]}), encoding='utf-8')
        rows = [
            {'seller_id': 'A1234567890', 'site': 'it', 'status': 'local_vat',
             'company': 'Changed', 'address': 'Rome', 'url': 'https://www.amazon.it/sp?seller=A1234567890',
             'vats': [{'number': 'IT12345678901', 'country': 'IT'}], 'checked_at': '2026-10-08'},
            {'seller_id': 'A1234567891', 'site': 'fr', 'status': 'local_vat',
             'company': 'New Company', 'address': 'Paris', 'url': 'https://www.amazon.fr/sp?seller=A1234567891',
             'vats': [{'number': 'FR12345678901', 'country': 'FR'}], 'checked_at': '2026-10-08'},
        ]
        (report / '店铺公开信息_逐站证据.jsonl').write_text(
            '\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        first = merge(self.db_path, report)
        second = merge(self.db_path, report)
        self.assertEqual(first, second)
        self.assertEqual(first['sellers_total'], 2)
        with sqlite3.connect(self.db_path) as db:
            old = db.execute('SELECT company_name,vies_company_name FROM sellers WHERE seller_id=?',
                             ('A1234567890',)).fetchone()
            self.assertEqual(old, ('<script>alert(1)</script>', 'Verified Company'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM seller_vat_evidence').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM seller_site_checks').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        evidence = load_amazon_db(self.db_path, table='seller_vat_evidence')
        self.assertEqual(evidence['total'], 2)
        self.assertIn('vat_country', [column['key'] for column in evidence['columns']])


if __name__ == '__main__':
    unittest.main()
