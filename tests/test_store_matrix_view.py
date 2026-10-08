import sqlite3
import tempfile
import unittest
from pathlib import Path

from api.store_matrix_view import company_similarity, load_store_matrix
from merge_store_vat_database import SCHEMA
from tests.test_store_vat_database import SELLERS_SCHEMA


class StoreMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'amazon_it.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.executescript(SELLERS_SCHEMA + SCHEMA)
            db.executemany('''INSERT INTO sellers
                (marketplace,seller_id,company_name,vat_number,vies_company_name,business_address)
                VALUES (?,?,?,?,?,?)''', [
                ('amazon.it', 'A1111111111', 'Example Trading Co., Ltd.', 'IT11111111111', '', 'Rome'),
                ('amazon.fr', 'A1111111111', 'EXAMPLE TRADING LTD', 'FR11111111111', '', 'Paris'),
                ('amazon.de', 'A2222222222', 'Example Tradding GmbH', 'DE22222222222', '', 'Berlin'),
                ('amazon.es', 'A3333333333', 'Unrelated Retail SL', 'ES33333333333', '', 'Madrid'),
            ])
            db.execute("INSERT INTO seller_discoveries(seller_id,seller_name,source) VALUES ('A1111111111','Example Shop','test')")
            db.execute("INSERT INTO seller_site_checks(marketplace,seller_id,status,source_url) VALUES ('amazon.pl','A4444444444','failed','https://www.amazon.pl/sp?seller=A4444444444')")
            db.execute("INSERT INTO seller_vat_evidence(marketplace,seller_id,vat_number) VALUES ('amazon.it','A1111111111','PL9999999999')")
            db.execute("INSERT INTO vat_checks(vat_number,country,vies_valid,vies_company_name) VALUES ('PL9999999999','PL',1,'EXAMPLE TRADING SP Z OO')")

    def test_one_row_per_store_and_site_including_failed_checks(self):
        result = load_store_matrix(self.path)
        self.assertTrue(result['available'])
        self.assertEqual(result['total'], 5)
        self.assertEqual(len({(row['marketplace'], row['seller_id']) for row in result['rows']}), 5)
        same_store = [row for row in result['rows'] if row['seller_id'] == 'A1111111111']
        self.assertEqual({row['marketplace'] for row in same_store}, {'amazon.it', 'amazon.fr'})
        self.assertEqual({row['seller_name'] for row in same_store}, {'Example Shop'})
        self.assertIn('similarity=80', same_store[0]['similarity_url'])
        failed = next(row for row in result['rows'] if row['seller_id'] == 'A4444444444')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['vats'], [])
        self.assertEqual(load_store_matrix(self.path, site='amazon.it')['total'], 1)
        self.assertEqual(load_store_matrix(self.path, site='invalid')['total'], 5)

    def test_search_secondary_vat_and_its_vies_company(self):
        result = load_store_matrix(self.path, query='PL9999999999')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['rows'][0]['marketplace'], 'amazon.it')
        self.assertEqual([vat['number'] for vat in result['rows'][0]['vats']],
                         ['IT11111111111', 'PL9999999999'])
        self.assertEqual(result['rows'][0]['vats'][1]['vies_name'], 'EXAMPLE TRADING SP Z OO')
        self.assertEqual(load_store_matrix(self.path, query='%')['total'], 0)

    def test_company_similarity_filter_and_order(self):
        self.assertEqual(company_similarity('Example Trading Co., Ltd.', 'EXAMPLE TRADING LTD'), 100)
        self.assertLess(company_similarity('Example Trading Co., Ltd.', 'Unrelated Retail SL'), 70)
        result = load_store_matrix(self.path, query='Example Trading Ltd', similarity=80)
        self.assertEqual({row['seller_id'] for row in result['rows']}, {'A1111111111', 'A2222222222'})
        self.assertEqual([row['similarity_score'] for row in result['rows']],
                         sorted([row['similarity_score'] for row in result['rows']], reverse=True))
        exact = load_store_matrix(self.path, query='Example Trading Ltd', similarity=100)
        self.assertEqual(exact['total'], 2)
        self.assertEqual(load_store_matrix(self.path, query='', similarity=80)['similarity'], 0)

    def test_site_pagination_preserves_filter(self):
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO sellers(marketplace,seller_id) VALUES (?,?)', [
                ('amazon.it', f'A{index:010d}') for index in range(51)
            ])
        second = load_store_matrix(self.path, site='amazon.it', page=2)
        self.assertEqual((second['total'], second['pages'], len(second['rows'])), (52, 2, 2))
        self.assertIn('site=amazon.it', second['previous_url'])
        self.assertFalse(second['next_url'])

    def test_missing_database_is_read_only(self):
        missing = Path(self.temp.name) / 'missing.sqlite3'
        self.assertIsNone(load_store_matrix(missing))
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
