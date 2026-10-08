import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from api.main import seller_vat_page
from api.seller_vat_view import PAGE_SIZE, load_seller_vat_page
from crawler.seller_vat import Store


class SellerVatPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'seller_vat.sqlite3'

    def make_store(self):
        store = Store(self.path)
        self.addCleanup(store.db.close)
        return store

    def test_missing_database_shows_empty_state_without_creating_file(self):
        result = load_seller_vat_page(self.path)
        self.assertFalse(result['available'])
        self.assertEqual(result['rows'], [])
        self.assertFalse(self.path.exists())

    def test_invalid_database_reports_read_error(self):
        self.path.write_text('not a sqlite database')
        result = load_seller_vat_page(self.path)
        self.assertFalse(result['available'])
        self.assertIn('无法读取', result['error'])

    def test_store_records_search_status_and_batch_scope(self):
        store = self.make_store()
        store.products('2026-10-07', [{'asin': 'B000000001', 'sellerId': 'A1234567890'}], 'older')
        store.products('2026-10-08', [
            {'asin': 'B000000002', 'sellerId': 'A1234567890', 'sellerName': 'ACME'},
            {'asin': 'B000000003', 'sellerId': 'A1234567890', 'sellerName': 'ACME'},
            {'asin': 'B000000004', 'sellerId': 'A1234567891', 'sellerName': 'Pending'},
            {'asin': 'B000000005'},
        ], 'seller-sprite')
        store.save_seller('A1234567890', {
            'company': '<script>alert(1)</script>',
            'address': 'Italy',
            'vats': [
                {'number': '01234567890', 'country': '', 'raw': '01234567890'},
                {'number': 'DE123456789', 'country': 'DE', 'raw': 'DE123456789'},
            ],
        }, 'ok')
        result = load_seller_vat_page(self.path)
        self.assertEqual(result['run_id'], '2026-10-08')
        self.assertEqual(result['summary'], {'products': 4, 'sellers': 2, 'unresolved': 1, 'vats': 2})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['rows'][0]['product_count'], 2)
        self.assertEqual([v['vat_number'] for v in result['rows'][0]['vats']], ['01234567890', 'DE123456789'])
        self.assertEqual(load_seller_vat_page(self.path, query='01234567890')['total'], 1)
        self.assertEqual(load_seller_vat_page(self.path, query='B000000002')['rows'][0]['product_count'], 2)
        self.assertEqual(load_seller_vat_page(self.path, status='pending')['rows'][0]['seller_id'], 'A1234567891')
        self.assertEqual(load_seller_vat_page(self.path, query='%')['total'], 0)
        products = load_seller_vat_page(self.path, tab='products')
        self.assertEqual(products['total'], 4)
        self.assertEqual(sum(not row['seller_id'] for row in products['rows']), 1)
        older = load_seller_vat_page(self.path, run_id='2026-10-07')
        self.assertEqual((older['summary']['products'], older['total']), (1, 1))

    def test_authenticated_page_serves_read_only_view(self):
        import asyncio
        from tests.test_auth import request_app
        store = self.make_store()
        store.products('2026-10-08', [{'asin': 'B000000001', 'sellerId': 'A1234567890'}], 'test')
        with patch('api.main.SELLER_VAT_DB_PATH', self.path), \
             patch('api.main.verify_admin_session', return_value=True):
            status, headers, body = asyncio.run(request_app(
                '/seller-vat', headers={'cookie': 'chenyu_session=test'},
            ))
        self.assertEqual(status, 200)
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertIn('A1234567890'.encode(), body)
        self.assertIn('店铺税号'.encode(), body)

    def test_pagination_and_html_escape(self):
        store = self.make_store()
        for index in range(PAGE_SIZE + 1):
            store.products('2026-10-08', [
                {'asin': f'B{index:09d}', 'sellerId': f'A{index:010d}'},
            ], 'test')
        store.save_seller('A0000000000', {'company': '<script>alert(1)</script>'}, 'no_public_vat')
        second = load_seller_vat_page(self.path, page=2)
        self.assertEqual((second['total'], second['pages'], len(second['rows'])), (PAGE_SIZE + 1, 2, 1))
        self.assertTrue(second['previous_url'])
        self.assertFalse(second['next_url'])
        self.assertEqual(load_seller_vat_page(self.path, page=999)['page'], 2)
        request = Request({
            'type': 'http', 'http_version': '1.1', 'method': 'GET',
            'scheme': 'http', 'path': '/seller-vat', 'raw_path': b'/seller-vat',
            'query_string': b'', 'headers': [], 'client': ('127.0.0.1', 1),
            'server': ('127.0.0.1', 8000),
        })
        with patch('api.main.SELLER_VAT_DB_PATH', self.path):
            response = seller_vat_page(request)
        html = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('店铺税号', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
