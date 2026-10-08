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


class _FakePlaywright:
    def __init__(self):
        self.context = Mock(close=AsyncMock())
        self.browser = Mock(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        self.chromium = Mock(launch=AsyncMock(return_value=self.browser))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class StoreBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_restarts_driver_between_batches_and_saves_each_site(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / 'stores.sqlite3'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE seller_discoveries (seller_id TEXT)')
                db.executemany('INSERT INTO seller_discoveries VALUES (?)',
                               [(f'A{index:010d}',) for index in range(3)])
            runtimes = []
            saved = []
            def new_runtime():
                runtime = _FakePlaywright()
                runtimes.append(runtime)
                return runtime
            details = {'company': 'Company', 'address': '', 'vats': []}
            with patch.object(stores, 'OUTPUT', root), patch.object(stores, 'STORE_DB', database), \
                 patch.object(stores, 'SITE_CHECK_BATCH', 2), \
                 patch('playwright.async_api.async_playwright', side_effect=new_runtime), \
                 patch.object(stores, 'fetch_public_seller', new=AsyncMock(return_value={'details': details, 'title': 'Shop'})), \
                 patch.object(stores, 'save_site_result', side_effect=lambda _db, item: saved.append(item)), \
                 patch.object(stores, 'save_results', return_value=root / 'result.csv'), \
                 patch.object(stores.asyncio, 'sleep', new=AsyncMock()):
                await stores.main_async(0, 2, {'fr', 'de', 'pl', 'es'}, check_vies=False)
            self.assertEqual(len(runtimes), 2)
            self.assertEqual(len(saved), 3)
            self.assertTrue(all(item['status'] == 'no_public_vat' for item in saved))
            self.assertEqual(len((root / '店铺公开信息_逐站证据.jsonl').read_text().splitlines()), 3)
            for runtime in runtimes:
                runtime.context.close.assert_awaited_once()
                runtime.browser.close.assert_awaited_once()

    async def test_dead_driver_stops_batch_without_recording_fake_failures(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / 'stores.sqlite3'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE seller_discoveries (seller_id TEXT)')
                db.execute('INSERT INTO seller_discoveries VALUES (?)', ('A1234567890',))
            with patch.object(stores, 'OUTPUT', root), patch.object(stores, 'STORE_DB', database), \
                 patch('playwright.async_api.async_playwright', side_effect=_FakePlaywright), \
                 patch.object(stores, 'fetch_public_seller',
                              new=AsyncMock(side_effect=RuntimeError('Connection closed while reading from the driver'))), \
                 patch.object(stores, 'save_site_result') as save, \
                 patch.object(stores.asyncio, 'sleep', new=AsyncMock()):
                with self.assertRaises(ExceptionGroup):
                    await stores.main_async(0, 1, {'fr', 'de', 'pl', 'es'}, check_vies=False)
            save.assert_not_called()
            self.assertEqual((root / '店铺公开信息_逐站证据.jsonl').read_text(), '')
