"""One bounded diagnostic run using the ordinary collector, with no browser masking."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_daily
from crawler.retry import collect_with_retry
from crawler.diagnostics import safe_response_headers


def safe_url(url):
    parts = urlsplit(url)
    # Pagination is useful to diagnosis; omit tracking/session query parameters.
    query = parse_qs(parts.query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode({k: v[0] for k, v in query.items() if k in {'pg', 'pageNumber'}}), ''))


def collect_diagnostic(page, source, settings, limit, evidence_dir, *, open_next_page):
    started = time.monotonic()
    responses = []
    pending = {}
    failures = []
    tracked_pages = []
    allowed_host = urlsplit(source['url']).hostname

    def relevant(request):
        return (urlsplit(request.url).hostname == allowed_host
                and request.resource_type in {'document', 'xhr', 'fetch'}
                and request.frame.page in tracked_pages
                and request.frame == request.frame.page.main_frame)

    def on_response(response):
        try:
            if relevant(response.request):
                headers = response.headers
                entry = {'url': safe_url(response.url), 'type': response.request.resource_type,
                         'status': response.status, 'elapsed_seconds': round(time.monotonic()-started, 3),
                         'response_headers': safe_response_headers(headers)}
                responses.append(entry)
                pending[response.request] = entry
        except Exception:
            pass

    def on_finished(request):
        entry = pending.pop(request, None)
        if entry is None:
            return
        try:
            # Read now; Chromium can discard old bodies after the next navigation.
            body = request.response().body()
            entry.update(bytes=len(body), explicit_agent_restriction=b'unauthorized ai agent' in body.lower(),
                         product_card_markers=body.count(b'zg-grid-general-faceout'))
            if entry['explicit_agent_restriction']:
                text = body.decode('utf-8', errors='replace')
                pos = text.casefold().find('unauthorized ai agent')
                entry['restriction_excerpt'] = text[max(0,pos-80):pos+180]
        except Exception as exc:
            entry['body_unavailable'] = type(exc).__name__ + ': ' + str(exc)

    def on_failure(request):
        try:
            if relevant(request):
                failures.append({'url': safe_url(request.url), 'type': request.resource_type,
                                 'failure': request.failure, 'elapsed_seconds': round(time.monotonic() - started, 3)})
        except Exception:
            pass

    def track_page(new_page):
        tracked_pages.append(new_page)
        new_page.on('response', on_response)
        new_page.on('requestfailed', on_failure)
        new_page.on('requestfinished', on_finished)
        return new_page

    track_page(page)

    def open_tracked_page():
        return track_page(open_next_page())

    try:
        return collect_with_retry(page, source, settings, limit, evidence_dir,
                                  open_next_page=open_tracked_page, max_attempts=1)
    finally:
        for tracked_page in tracked_pages:
            tracked_page.remove_listener('response', on_response)
            tracked_page.remove_listener('requestfailed', on_failure)
            tracked_page.remove_listener('requestfinished', on_finished)
        # The final document's finished event can still be queued at load time.
        for request in list(pending):
            if request.resource_type == 'document':
                on_finished(request)
        for entry in pending.values():
            entry['body_unavailable'] = 'Request not finished before collection stopped'
        events = responses
        report = {'source': source['category'], 'mode': 'fresh_browser_page_one_attempt',
                  'events': events, 'request_failures': failures}
        path = Path(evidence_dir) / 'network-diagnostic.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('NETWORK_DIAGNOSTIC ' + json.dumps({'source': source['category'], 'requests': len(events),
              'restricted_responses': [{'url': e['url'], 'type': e['type'], 'status':e['status'],
                                      'elapsed_seconds': e['elapsed_seconds']} for e in events if e.get('explicit_agent_restriction')],
              'failed_requests': len(failures), 'evidence':str(path)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-id', action='append', required=True)
    args = parser.parse_args()
    command = [item for source in args.source_id for item in ['--source-id', source]]
    with patch('run_daily.collect_with_retry', collect_diagnostic):
        return run_daily.main(command)


if __name__ == '__main__':
    raise SystemExit(main())
