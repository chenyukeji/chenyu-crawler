import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from crawler.amazon import AccessControlBlocked, BrowserSettings
from crawler.retry import collect_with_retry


class PageResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.visits = []
        self.source = dict(marketplace='US', category='beauty', url='https://www.amazon.com/gp/new-releases/beauty')
        self.second_attempt = 0

    def install_pages(self, failure):
        def respond(route):
            if route.request.resource_type != 'document':
                route.abort()
                return
            url = route.request.url
            self.visits.append(url)
            second = '?pg=2' in url
            if second:
                self.second_attempt += 1
                if failure == 'always_timeout' or (failure == 'timeout' and self.second_attempt == 1):
                    route.abort('timedout')
                    return
                if failure == 'blocked':
                    route.fulfill(status=200, content_type='text/html', body='<h4>unauthorized ai agent</h4>')
                    return
                if failure == 'empty' and self.second_attempt == 1:
                    route.fulfill(status=200, content_type='text/html', body='<h1>Temporarily unavailable</h1>')
                    return
            body = ''.join(f'<div class="zg-grid-general-faceout"><span class="zg-bdg-text">#{i}</span><a href="/dp/B{i:09d}">Product {i}</a><img alt="Product {i}" src="https://example.test/{i}.png"></div>' for i in (range(51,101) if second else range(1,51)))
            if not second:
                body += '<ul class="a-pagination"><li class="a-last"><a href="?pg=2">Next</a></li></ul>'
            route.fulfill(status=200, content_type='text/html', body=body)
        self.page.route('**/*', respond)

    def collect(self):
        with patch('crawler.retry.time.sleep'):
            return collect_with_retry(self.page, self.source, BrowserSettings(scroll_pause_ms=0), 100, self.path)

    def test_numbered_button_is_clicked_after_scrolling(self):
        from crawler.amazon import _click_next_page
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<div style="height:4000px">Products</div><ul class="a-pagination"><li><a href="?pg=2" onclick="sessionStorage.setItem(\'clicked\',\'2\'); sessionStorage.setItem(\'scroll\', window.scrollY)">2</a></li><li class="a-last"><a href="?pg=2" onclick="sessionStorage.setItem(\'clicked\',\'next\')">Next</a></li></ul>'))
        self.page.goto(self.source['url'])
        response = _click_next_page(self.page, self.source['url']+'?pg=2', BrowserSettings(scroll_pause_ms=20))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('clicked')"), '2')
        self.assertGreater(int(self.page.evaluate("sessionStorage.getItem('scroll')")), 0)
        self.assertTrue(self.page.url.endswith('?pg=2'))

    def test_second_page_uses_independent_page_and_real_rank(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        self.page = context.new_page()
        visits = []

        def respond(route):
            if route.request.resource_type != 'document':
                route.abort()
                return
            is_second = '?pg=2' in route.request.url
            visits.append((route.request.frame.page, is_second))
            start = 51 if is_second else 1
            cards = ''.join(
                f'<div class="zg-grid-general-faceout"><span class="zg-bdg-text">#{rank}</span>'
                f'<a href="/dp/B{rank:09d}">Product {rank}</a>'
                f'<img alt="Product {rank}" src="https://example.test/{rank}.png"></div>'
                for rank in range(start, start + 50)
            )
            next_link = '' if is_second else '<ul class="a-pagination"><li><a href="?pg=2">2</a></li><li class="a-last"><a href="?pg=2">Next</a></li></ul>'
            route.fulfill(status=200, content_type='text/html', body=cards + next_link)

        self.page.context.route('**/*', respond)
        opened = []

        def open_next_page():
            new_page = self.page.context.new_page()
            opened.append(new_page)
            return new_page

        with patch('crawler.retry.time.sleep'):
            result = collect_with_retry(
                self.page, self.source, BrowserSettings(scroll_pause_ms=0), 100,
                self.path, open_next_page=open_next_page,
            )
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([item['rank'] for item in result['items']], list(range(1, 101)))
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.page.url, self.source['url'])
        self.assertTrue(opened[0].url.endswith('?pg=2'))
        self.assertEqual([page for page, second in visits if second], [opened[0]])
        for page in opened:
            page.close()

    def test_timeout_retries_only_second_page(self):
        self.install_pages('timeout')
        result = self.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([i['rank'] for i in result['items']], list(range(1,101)))
        self.assertEqual(self.visits, [self.source['url'], self.source['url']+'?pg=2', self.source['url']+'?pg=2'])
        self.assertEqual(result['pages_visited'], 2)
        self.assertEqual(len(result['page_diagnostics']), 2)
        state = json.loads((self.path/'resume-checkpoint.json').read_text())
        self.assertEqual(state['url'], self.source['url']+'?pg=2')
        self.assertEqual(len(state['items']), 50)

    def test_empty_second_page_retries_second_page(self):
        self.install_pages('empty')
        result = self.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.visits.count(self.source['url']), 1)
        self.assertEqual(self.second_attempt, 2)

    def test_exhausted_retries_keep_first_page_as_partial(self):
        self.install_pages('always_timeout')
        result = self.collect()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['items']), 50)
        self.assertEqual(self.visits.count(self.source['url']), 1)
        self.assertEqual(self.second_attempt, 3)
        self.assertIn('已尝试 3 次', result['error_message'])

    def test_restriction_stops_without_page_retry(self):
        self.install_pages('blocked')
        with self.assertRaises(AccessControlBlocked) as caught:
            self.collect()
        self.assertEqual(len(caught.exception.partial_snapshot['items']), 50)
        self.assertEqual(self.second_attempt, 1)
        self.assertEqual(self.visits.count(self.source['url']), 1)
