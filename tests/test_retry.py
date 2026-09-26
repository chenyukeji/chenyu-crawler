import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from crawler.amazon import AccessControlBlocked, BrowserSettings, detect_access_blocker, _check_page_access
from crawler.retry import collect_with_retry
from database.repository import SnapshotStore


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.page = MagicMock()
        self.page.content.return_value = '<html>failure evidence</html>'
        self.page.url = 'https://www.amazon.de/gp/new-releases/kitchen'
        self.page.title.return_value = 'Amazon'
        self.source = dict(marketplace='DE', category='kitchen')

    def run_retry(self):
        return collect_with_retry(self.page, self.source, BrowserSettings(), 100, self.path,
                                  open_next_page=MagicMock)

    @patch('crawler.retry.time.sleep')
    @patch('crawler.retry.collect_source')
    def test_transient_failure_then_success(self, collect, sleep):
        collect.side_effect = [RuntimeError('timeout'), dict(status='ok', items=[{}])]
        result = self.run_retry()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(len(result['attempts']), 2)
        self.assertTrue((self.path / 'attempt-1.html').exists())

    @patch('crawler.retry.time.sleep')
    @patch('crawler.retry.collect_source')
    def test_partial_returns_for_delayed_retry_without_hot_reload(self, collect, sleep):
        collect.return_value = dict(status='partial', items=[{}]*79, error_message='missing ranks')
        result = self.run_retry()
        self.assertEqual(len(result['items']), 79)
        self.assertEqual(collect.call_count, 1)
        sleep.assert_not_called()

    @patch('crawler.retry.time.sleep')
    @patch('crawler.retry.collect_source')
    def test_explicit_block_stops_without_retry(self, collect, sleep):
        collect.side_effect = AccessControlBlocked('unauthorized ai agent')
        with self.assertRaises(AccessControlBlocked):
            self.run_retry()
        self.assertEqual(collect.call_count, 1)
        sleep.assert_not_called()
        self.assertTrue((self.path / 'attempts.json').exists())

    @patch('crawler.retry.time.sleep')
    @patch('crawler.retry.collect_source')
    def test_partial_data_survives_later_access_block(self, collect, sleep):
        items = [dict(asin='B000000001', rank=1, title='Product', image_url='https://example.com/image')]
        def blocked_after_page(*args, on_checkpoint, **kwargs):
            on_checkpoint(dict(status='partial', items=items, error_message='pending page'))
            raise AccessControlBlocked('unauthorized ai agent')
        collect.side_effect = blocked_after_page
        with self.assertRaises(AccessControlBlocked) as caught:
            self.run_retry()
        self.assertEqual(caught.exception.partial_snapshot['items'], items)
        self.assertEqual(collect.call_count, 1)
        sleep.assert_not_called()
        import json
        saved = json.loads((self.path / 'best-snapshot.json').read_text())
        self.assertEqual(saved['items'], items)
        self.assertIn('保留此前有效结果 1 条', str(caught.exception))

    @patch('crawler.retry.time.sleep')
    @patch('crawler.retry.collect_source')
    def test_retries_are_bounded_and_explain_all_attempts(self, collect, sleep):
        collect.side_effect = RuntimeError('no cards')
        with self.assertRaisesRegex(RuntimeError, '已尝试 3 次'):
            self.run_retry()
        self.assertEqual(collect.call_count, 3)

    def test_detects_http_200_ai_restriction(self):
        self.assertEqual(detect_access_blocker("Continued access by an unauthorized AI agent violates Amazon's Conditions of Use."), 'unauthorized ai agent')

    def test_generic_503_is_retryable_but_403_is_access_restriction(self):
        page = MagicMock()
        page.url = self.page.url
        page.title.return_value = "Service unavailable"
        page.locator.return_value.inner_text.return_value = "Try again later"
        with self.assertRaises(RuntimeError) as error:
            _check_page_access(page, MagicMock(status=503))
        self.assertNotIsInstance(error.exception, AccessControlBlocked)
        with self.assertRaises(AccessControlBlocked):
            _check_page_access(page, MagicMock(status=403))

    def test_retry_does_not_replace_more_complete_snapshot(self):
        store = SnapshotStore(self.path / 'test.db')
        args = dict(source_url=self.page.url,marketplace='DE',category='kitchen',snapshot_date='2026-09-24')
        rows = [dict(asin=f'B{i:09d}',rank=i+1,title='Product',image_url='https://example.com/image') for i in range(3)]
        store.ingest(rows, **args)
        changed = store.ingest(rows[:1], **args, preserve_more_complete=True)
        self.assertEqual(changed, 0)
        with closing(store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 3)

if __name__ == '__main__':
    unittest.main()
