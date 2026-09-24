import unittest
from crawler.amazon import _build_snapshot, BrowserSettings, collect_source
from playwright.sync_api import sync_playwright


class ShortListTests(unittest.TestCase):
    source = dict(marketplace='US', category='home-garden', url='https://www.amazon.com/gp/new-releases/home-garden')

    def snapshot(self, ranks, *, stable=True, rejected=0, stop='已到末页，页面没有可用下一页链接'):
        items = [dict(rank=r) for r in ranks]
        return _build_snapshot(self.source, items, 100, [dict(cards=len(items)+rejected, new_unique_items=len(items), rejected_cards=rejected, missing_rank_cards=0, load_stable=stable)], 1, stop)

    def test_short_contiguous_terminal_list_is_complete(self):
        for count in (1, 49, 50, 99, 100):
            with self.subTest(count=count):
                result = self.snapshot(range(1, count+1))
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['target_items'], count)
                self.assertEqual(result['error_message'], '')

    def test_missing_middle_rank_empty_unstable_or_parse_loss_stays_partial(self):
        cases = [self.snapshot([]), self.snapshot([1,3]), self.snapshot(range(51,100)),
                 self.snapshot(range(1,100), stable=False), self.snapshot(range(1,100), rejected=1),
                 self.snapshot(range(1,100), stop='下一页链接重复'),
                 self.snapshot(range(1,100), stop='后续分页尚未完成')]
        for result in cases:
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['top_list_complete'])

    def test_offline_two_pages_with_99_products_finish_without_retry(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visited = []
                def respond(route):
                    if route.request.resource_type != 'document':
                        route.abort()
                        return
                    visited.append(route.request.url)
                    second = '?pg=2' in route.request.url
                    body = ''.join(f'<div class="zg-grid-general-faceout"><span class="zg-bdg-text">#{i}</span><a href="/dp/B{i:09d}">Product</a><img alt="Product {i}" src="https://example.test/{i}.png"></div>' for i in (range(51,100) if second else range(1,51)))
                    if not second:
                        body += '<ul class="a-pagination"><li class="a-last"><a href="?pg=2">Next</a></li></ul>'
                    route.fulfill(status=200, content_type='text/html', body=body)
                page.route('**/*', respond)
                result = collect_source(page, self.source, BrowserSettings(scroll_pause_ms=0), 100)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['target_items'], 99)
                self.assertEqual(len(result['items']), 99)
                self.assertTrue(result['page_diagnostics'][-1]['load_stable'])
                self.assertEqual(len(visited), 2)
            finally:
                browser.close()


class LoadingWaitTests(unittest.TestCase):
    def test_waits_past_interactive_and_resets_when_products_change(self):
        from unittest.mock import MagicMock
        from crawler.amazon import _wait_for_page_settle
        page = MagicMock()
        page.evaluate.side_effect = ([{'ready':'interactive','products':['a']}] * 7
                                     + [{'ready':'complete','products':['a']}] * 4
                                     + [{'ready':'complete','products':['a','b']}] * 7)
        result = _wait_for_page_settle(page)
        self.assertTrue(result['settled'])
        self.assertEqual(result['waited_ms'], 8500)
        self.assertEqual(page.evaluate.call_count, 18)

    def test_timeout_is_bounded_and_not_settled(self):
        from unittest.mock import MagicMock
        from crawler.amazon import _wait_for_page_settle
        page = MagicMock()
        page.evaluate.return_value = {'ready':'interactive','products':['a']}
        result = _wait_for_page_settle(page, max_polls=5)
        self.assertFalse(result['settled'])
        self.assertEqual(page.evaluate.call_count, 5)
        self.assertEqual(page.wait_for_timeout.call_count, 4)


class PageDelayConfigTests(unittest.TestCase):
    def test_loads_and_validates_page_delay(self):
        import tempfile,json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'browser.json'
            path.write_text(json.dumps({'between_pages_seconds':60}))
            self.assertEqual(BrowserSettings.from_file(path).between_pages_seconds,60)
            for value in (-1,301,'NaN'):
                path.write_text(json.dumps({'between_pages_seconds':value}))
                with self.assertRaises(ValueError):
                    BrowserSettings.from_file(path)
