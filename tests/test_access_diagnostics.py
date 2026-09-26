import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright
from crawler.amazon import AccessControlBlocked, BrowserSettings, _check_page_access
from crawler.retry import collect_with_retry


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_process_metadata_never_exposes_proxy_credentials(self):
        from crawler.diagnostics import process_context
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://user:private-password@proxy.invalid:7890/private-token', 'NO_PROXY': 'private-host.invalid'}, clear=True):
            metadata = process_context()
        encoded = json.dumps(metadata)
        self.assertEqual(metadata['proxy_environment_present'], ['HTTPS_PROXY'])
        self.assertEqual(metadata['effective_network_route'], 'not_measured')
        self.assertTrue(metadata['proxy_bypass_configured'])
        for sensitive in ['user:', 'private-password', 'proxy.invalid', 'private-token', 'private-host.invalid']:
            self.assertNotIn(sensitive, encoded)


class AccessDiagnosticsTests(unittest.TestCase):
    def test_rate_limit_diagnostics_allowlist_headers(self):
        page = MagicMock()
        page.url = 'https://www.amazon.com/gp/new-releases/beauty'
        page.title.return_value = 'Amazon'
        page.locator.return_value.inner_text.return_value = 'Too many requests'
        response = MagicMock(status=429, headers={'retry-after': '120', 'set-cookie': 'secret', 'authorization': 'secret'})
        with self.assertRaises(AccessControlBlocked) as caught:
            _check_page_access(page, response, stage='翻页后', page_number=2)
        access = caught.exception.diagnostics
        self.assertEqual(access['kind'], 'RATE_LIMITED')
        self.assertEqual(access['page_number'], 2)
        self.assertEqual(access['response_headers'], {'retry-after': '120'})

    def test_explicit_agent_denial_keeps_classification_on_429(self):
        page = MagicMock()
        page.url = 'https://www.amazon.com/gp/new-releases/beauty'
        page.title.return_value = 'Amazon'
        page.locator.return_value.inner_text.return_value = 'unauthorized ai agent'
        with self.assertRaises(AccessControlBlocked) as caught:
            _check_page_access(page, MagicMock(status=429, headers={}))
        self.assertEqual(caught.exception.diagnostics['kind'], 'AGENT_RESTRICTED')
        self.assertIn('AUTOMATED_ACCESS_RESTRICTED', str(caught.exception))
        self.assertIn('自动化访问受限', str(caught.exception))
        self.assertNotIn('unauthorized ai agent', str(caught.exception).casefold())
        self.assertIn('unauthorized ai agent', caught.exception.diagnostics['site_notice'].casefold())

    def test_second_page_block_keeps_first_page_and_records_stage_offline(self):
        # Every network request is fulfilled locally: this test never contacts Amazon.
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                calls = []
                first = ''.join(f'<div class="zg-grid-general-faceout"><span class="zg-bdg-text">#{i}</span><a href="/dp/B{i:09d}">Product {i}</a><img alt="Product {i}" src="https://example.test/{i}.png"></div>' for i in range(1, 51))
                first += '<ul class="a-pagination"><li class="a-last"><a href="?pg=2">Next</a></li></ul>'
                def respond(route):
                    if route.request.resource_type != 'document':
                        route.abort()
                        return
                    calls.append(route.request.url)
                    body = '<h4>Continued access by an unauthorized AI agent violates Amazon conditions.</h4>' if '?pg=2' in route.request.url else first
                    route.fulfill(status=200, content_type='text/html', body=body)
                context.route('**/*', respond)
                source = dict(marketplace='US', category='beauty', url='https://www.amazon.com/gp/new-releases/beauty')
                with self.assertRaises(AccessControlBlocked) as caught:
                    collect_with_retry(page, source, BrowserSettings(scroll_pause_ms=0), 100, Path(tmp),
                                       open_next_page=context.new_page)
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(caught.exception.partial_snapshot['items']), 50)
                self.assertEqual(caught.exception.diagnostics['stage'], '翻页后')
                self.assertEqual(caught.exception.diagnostics['page_number'], 2)
                snapshot = json.loads((Path(tmp)/'best-snapshot.json').read_text())
                self.assertEqual([i['rank'] for i in snapshot['items']], list(range(1, 51)))
                history = json.loads((Path(tmp)/'attempts.json').read_text())
                self.assertEqual(history[0]['retained_items'], 50)
                self.assertEqual(history[0]['access']['kind'], 'AGENT_RESTRICTED')
                self.assertEqual(history[0]['access']['http_status'], 200)
                environment = json.loads((Path(tmp)/'environment.json').read_text())
                self.assertIn('browser_version', environment)
                self.assertIn('userAgent', environment['navigator'])
            finally:
                browser.close()


class MixedBlockedPageTests(unittest.TestCase):
    def test_visible_items_on_blocked_page_are_retained_without_more_requests(self):
        from unittest.mock import patch
        from crawler.amazon import collect_source
        page = MagicMock()
        page.url = 'https://www.amazon.com/gp/new-releases/beauty?pg=2'
        page.locator.return_value.count.return_value = 30
        first = [dict(rank=i, asin=f'B{i:09d}', title='Product', image_url='image') for i in range(1,51)]
        second = [dict(rank=i, asin=f'B{i:09d}', title='Product', image_url='image') for i in range(51,81)]
        source = dict(marketplace='US', category='beauty', url='https://www.amazon.com/gp/new-releases/beauty')
        def blocked(*args, on_checkpoint, **kwargs):
            on_checkpoint(dict(items=first, page_diagnostics=[], status='partial'))
            kwargs['on_cursor']({'url':page.url,'items':first,'page_diagnostics':[],'visited_page_urls':[source['url']],'pages_visited':1})
            raise AccessControlBlocked('unauthorized ai agent', diagnostics={'page_number':2,'http_status':200})
        with patch('crawler.amazon._collect_source', side_effect=blocked), patch('crawler.amazon._extract_card', side_effect=second):
            with self.assertRaises(AccessControlBlocked) as caught:
                collect_source(page, source, BrowserSettings(), 100, open_next_page=MagicMock())
        self.assertEqual([i['rank'] for i in caught.exception.partial_snapshot['items']], list(range(1,81)))
        self.assertFalse(caught.exception.partial_snapshot['top_list_complete'])
        page.goto.assert_not_called()
        page.mouse.wheel.assert_not_called()


class DiagnosticCaptureTests(unittest.TestCase):
    def test_completed_body_is_retained_after_navigation(self):
        from unittest.mock import patch
        from scripts.diagnose_collection import collect_diagnostic
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<p>unauthorized ai agent</p>' if 'pg=2' in route.request.url else '<div class="zg-grid-general-faceout">Product</div>', headers={'x-amz-rid':'test-request-id','set-cookie':'session=private-secret'}))
                source = dict(marketplace='US', category='beauty', url='https://www.amazon.com/gp/new-releases/beauty')
                def navigate(page, *args, **kwargs):
                    page.goto(source['url'], wait_until='load')
                    page.wait_for_timeout(100)  # Dispatch completed-response callbacks before navigating.
                    second_page = kwargs['open_next_page']()
                    second_page.goto(source['url']+'?pg=2', wait_until='load')
                    second_page.wait_for_timeout(100)
                    return {'status':'blocked'}
                with patch('scripts.diagnose_collection.collect_with_retry', side_effect=navigate):
                    collect_diagnostic(page, source, BrowserSettings(), 100, Path(tmp),
                                       open_next_page=context.new_page)
                events = json.loads((Path(tmp)/'network-diagnostic.json').read_text())['events']
                self.assertEqual(len(events), 2)
                self.assertTrue(all(e['response_headers']['x-amz-rid'] == 'test-request-id' for e in events))
                self.assertNotIn('private-secret', json.dumps(events))
                self.assertEqual(events[0]['product_card_markers'], 1)
                self.assertFalse(events[0]['explicit_agent_restriction'])
                self.assertTrue(events[1]['explicit_agent_restriction'])
                self.assertTrue(all('body_unavailable' not in e for e in events))
            finally:
                browser.close()
