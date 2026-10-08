import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from check_vies_vats import candidates, fetch_vies, run, save_result
from merge_store_vat_database import SCHEMA
from tests.test_store_vat_database import SELLERS_SCHEMA


class ViesVatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'amazon_it.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.executescript(SELLERS_SCHEMA + SCHEMA)
            db.execute("INSERT INTO sellers(marketplace,seller_id,vat_number) VALUES ('amazon.it','A1234567890','IT12345678901')")
            db.execute("INSERT INTO seller_vat_evidence(marketplace,seller_id,vat_number) VALUES ('amazon.it','A1234567890','IT12345678901')")
            db.execute("INSERT INTO seller_vat_evidence(marketplace,seller_id,vat_number) VALUES ('amazon.fr','A1234567891','FR12345678901')")

    def test_vies_result_updates_same_vat_without_overwriting_known_name(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(candidates(db), ['IT12345678901', 'FR12345678901'])
            save_result(db, 'IT12345678901', {'valid': True, 'name': 'OFFICIAL LTD', 'address': 'Rome'})
            self.assertEqual(db.execute("SELECT vies_company_name FROM sellers WHERE seller_id='A1234567890'").fetchone()[0], 'OFFICIAL LTD')
            save_result(db, 'IT12345678901', {'valid': False, 'name': '', 'address': ''})
            self.assertEqual(db.execute("SELECT vies_company_name FROM sellers WHERE seller_id='A1234567890'").fetchone()[0], 'OFFICIAL LTD')
            self.assertEqual(candidates(db), ['FR12345678901'])

    def test_recent_public_vat_is_queried_first_and_saved(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE seller_vat_evidence SET checked_at='2026-10-08T19:00:00+08:00' WHERE vat_number='FR12345678901'")
            self.assertEqual(candidates(db), ['FR12345678901', 'IT12345678901'])
        with patch('check_vies_vats.fetch_vies', return_value={
            'valid': True, 'name': 'FRENCH TRADING SARL', 'address': 'Paris',
        }), patch('check_vies_vats.time.sleep'):
            self.assertEqual(run(self.path, limit=1)['named'], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT vies_company_name FROM vat_checks WHERE vat_number=?',
                                        ('FR12345678901',)).fetchone()[0], 'FRENCH TRADING SARL')
            self.assertIsNone(db.execute('SELECT 1 FROM vat_checks WHERE vat_number=?',
                                         ('IT12345678901',)).fetchone())

    def test_response_without_name_is_not_invented(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _size=-1): return b'{"isValid":true,"name":"---","address":"---"}'
        with patch('check_vies_vats.urlopen', return_value=Response()):
            self.assertEqual(fetch_vies('IT12345678901'), {'valid': True, 'name': '', 'address': ''})
        with patch('check_vies_vats.fetch_vies', return_value={'valid': True, 'name': '', 'address': ''}), \
             patch('check_vies_vats.time.sleep'):
            self.assertEqual(run(self.path, limit=1)['named'], 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COALESCE(vies_company_name,'') FROM sellers WHERE seller_id='A1234567890'").fetchone()[0], '')
            self.assertEqual(db.execute("SELECT vies_valid FROM vat_checks WHERE vat_number='IT12345678901'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
