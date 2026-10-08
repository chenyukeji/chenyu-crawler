import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


def fake_page(status: int):
    page = Mock()
    page.goto = AsyncMock(return_value=SimpleNamespace(status=status))
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value.inner_text = AsyncMock(return_value="Amazon seller profile")
    page.content = AsyncMock(return_value="<html></html>")
    page.title = AsyncMock(return_value="Amazon seller")
    page.close = AsyncMock()
    return page


class SellerProfileRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_202_with_public_details_is_success(self):
        page = fake_page(202)
        context = Mock(new_page=AsyncMock(return_value=page))
        browser = Mock(new_context=AsyncMock())
        details = {"company": "Example GmbH", "address": "", "vats": []}
        with patch.object(stores, "parse_seller", return_value=details):
            result = await stores.fetch_public_seller(browser, context, "https://www.amazon.fr/sp?seller=A1234567890")
        self.assertEqual(result["details"]["company"], "Example GmbH")
        browser.new_context.assert_not_called()
        page.close.assert_awaited_once()

    async def test_empty_http_202_retries_in_fresh_context(self):
        first = fake_page(202)
        second = fake_page(200)
        context = Mock(new_page=AsyncMock(return_value=first))
        fresh = Mock(new_page=AsyncMock(return_value=second), close=AsyncMock())
        browser = Mock(new_context=AsyncMock(return_value=fresh))
        with patch.object(stores, "parse_seller", side_effect=[
            {"company": "", "address": "", "vats": []},
            {"company": "Example GmbH", "address": "", "vats": []},
        ]), patch.object(stores.asyncio, "sleep", new=AsyncMock()):
            result = await stores.fetch_public_seller(browser, context, "https://www.amazon.fr/sp?seller=A1234567890")
        self.assertEqual(result["details"]["company"], "Example GmbH")
        fresh.close.assert_awaited_once()
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()

    async def test_repeated_empty_http_202_remains_retryable_failure(self):
        first = fake_page(202)
        second = fake_page(202)
        context = Mock(new_page=AsyncMock(return_value=first))
        fresh = Mock(new_page=AsyncMock(return_value=second), close=AsyncMock())
        browser = Mock(new_context=AsyncMock(return_value=fresh))
        with patch.object(stores, "parse_seller", return_value={"company": "", "address": "", "vats": []}), \
             patch.object(stores.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 202"):
                await stores.fetch_public_seller(browser, context, "https://www.amazon.fr/sp?seller=A1234567890")
        fresh.close.assert_awaited_once()
