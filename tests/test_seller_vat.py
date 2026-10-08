import tempfile
import unittest
from pathlib import Path
from crawler import seller_vat as auto

ROOT = Path(__file__).resolve().parents[1]


class AutoTests(unittest.TestCase):
    def test_public_vat_label_only_and_leading_zero(self):
        html = '''<div class="a-row"><span class="a-text-bold">Ragione sociale:</span><span>ACME SRL</span></div>
        <div class="a-row"><span class="a-text-bold">Numero di registro:</span>99999999999</div>
        <div class="a-row"><span class="a-text-bold">Partita IVA:</span><span>05172020264</span></div>
        <div class="a-row"><span class="a-text-bold">VAT number:</span><span>DE123456789</span></div>'''
        result = auto.parse_seller(html)
        self.assertEqual(result['company'], 'ACME SRL')
        self.assertEqual([v['number'] for v in result['vats']], ['05172020264', 'DE123456789'])
        self.assertEqual([v['country'] for v in result['vats']], ['', 'DE'])

    def test_spanish_seller_page(self):
        html = '''<div class="a-row"><span class="a-text-bold">Nombre de empresa:</span><span>ACME</span></div>
        <div class="a-row"><span class="a-text-bold">Número de IVA:</span><span>05172020264</span></div>'''
        result = auto.parse_seller(html)
        self.assertEqual(result['company'], 'ACME')
        self.assertEqual(result['vats'][0]['number'], '05172020264')

    def test_url_rejects_foreign_host(self):
        self.assertEqual(auto.seller_id_from_url('https://www.amazon.it/sp?seller=A1234567890'), 'A1234567890')
        self.assertEqual(auto.seller_id_from_url('https://evil.test/sp?seller=A1234567890'), '')

    def test_dedup_resume_and_separate_runs(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            store = auto.Store(Path(folder) / 'test.sqlite3')
            item = {'asin': 'B012345678', 'sellerId': 'A1234567890'}
            store.products('today', [item, item], 'test')
            store.products('yesterday', [{'asin': 'B012345679', 'sellerId': 'A1234567891'}], 'test')
            store.save_seller('A1234567890', {'company': 'ACME', 'vats': [{'number': '01234567890', 'country': '', 'raw': '01234567890'}]}, 'ok')
            counts = store.export('today', Path(folder) / 'out')
            self.assertEqual(counts['products'], 1)
            self.assertEqual(counts['sellers'], 1)
            self.assertEqual(counts['vat_records'], 1)
            self.assertEqual(counts['pending_or_failed'], 0)
            self.assertIn('01234567890', (Path(folder) / 'out' / '店铺税号.csv').read_text(encoding='utf-8-sig'))
            store.db.close()

    def test_api_query_contract(self):
        from unittest.mock import patch
        import io
        response = io.BytesIO(b'{"code":"OK","data":{"items":[],"total":0}}')
        with patch.object(auto, 'urlopen', return_value=response) as request:
            auto.api_page('not-a-real-key', {}, 1)
        import json
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual((body['marketplace'], body['month'], body['availableMonth']), ('IT', 'nearly', 1))

    def test_price_partitions_overlap_without_gaps(self):
        from decimal import Decimal
        self.assertEqual(auto.price_split(None, None), ((None, Decimal('100')), (Decimal('100'), None)))
        left, right = auto.price_split(Decimal('0'), Decimal('1'))
        self.assertEqual(left[1], right[0])
        self.assertIsNone(auto.price_split(Decimal('5.00'), Decimal('5.01')))

    def test_table_detail_row_recovers_seller(self):
        class Page:
            def content(self):
                return '''<table><tbody>
                <tr><td><a href="https://www.amazon.it/dp/B012345678">ASIN B012345678</a></td></tr>
                <tr><td>BuyBox 卖家 B012345678 <a href="https://www.amazon.it/sp?seller=A1234567890">ACME</a></td></tr>
                </tbody></table>'''
        rows = auto.read_products(Page())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sellerId'], 'A1234567890')

    def test_rate_limit_stops_before_next_seller(self):
        from unittest.mock import Mock
        page = Mock()
        page.goto.return_value.status = 429
        with self.assertRaises(auto.StopCollection):
            auto.navigate(page, 'https://www.amazon.it/sp?seller=A1234567890')

    def test_unrecognized_seller_page_is_not_no_vat(self):
        self.assertEqual(auto.parse_seller('<html><body>Something went wrong</body></html>'),
                         {'company': '', 'address': '', 'vats': []})


if __name__ == '__main__':
    unittest.main()
