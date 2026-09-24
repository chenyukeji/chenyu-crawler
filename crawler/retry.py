from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from crawler.amazon import AccessControlBlocked, BrowserSettings, ParseError, PageCollectionError, collect_source


def collect_with_retry(page: Any, source: dict, settings: BrowserSettings, limit: int,
                       evidence_dir: Path, max_attempts: int = 3) -> dict:
    """Retry transient failures; never retry an explicit access restriction."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    history = []
    best = None
    resume_from = None

    def retain(snapshot):
        nonlocal best
        if not snapshot['items']:
            return
        if best is None or len(snapshot['items']) >= len(best['items']) or snapshot['status'] == 'ok':
            best = snapshot
            temporary = evidence_dir / 'best-snapshot.tmp'
            temporary.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(evidence_dir / 'best-snapshot.json')

    environment = {"channel": settings.channel, "headless": settings.headless,
                   "between_sources_seconds": settings.between_sources_seconds,
                   "between_pages_seconds": settings.between_pages_seconds}
    try:
        environment["browser_version"] = str(page.context.browser.version)
        environment["navigator"] = page.evaluate("({userAgent:navigator.userAgent, webdriver:navigator.webdriver, language:navigator.language})")
        # Test doubles and unavailable browser contexts should not break collection.
        json.dumps(environment)
    except Exception:
        environment.pop("navigator", None)
    (evidence_dir / 'environment.json').write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(1, max_attempts + 1):
        blocked = False
        access = None
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        try:
            options = {"on_checkpoint": retain}
            if resume_from is not None:
                options["resume_from"] = resume_from
            snapshot = collect_source(page, source, settings, limit, **options)
            retain(snapshot)
            reason = snapshot.get('error_message', '')
            status = snapshot['status']
        except AccessControlBlocked as exc:
            reason, status, blocked = str(exc), 'blocked', True
            access = exc.diagnostics
            if exc.partial_snapshot:
                retain(exc.partial_snapshot)
        except PageCollectionError as exc:
            reason = str(exc)
            status = "parse_error" if exc.parse_error else "failed"
            resume_from = exc.resume_from
            (evidence_dir / 'resume-checkpoint.json').write_text(json.dumps(resume_from, ensure_ascii=False, indent=2), encoding='utf-8')
        except ParseError as exc:
            reason, status = str(exc), "parse_error"
        except (PlaywrightError, RuntimeError, OSError) as exc:
            reason, status = f'{type(exc).__name__}: {exc}', 'failed'
        entry = {'attempt': attempt, 'status': status, 'reason': reason,
                 'started_at': started_at, 'elapsed_seconds': round(time.monotonic() - started, 3),
                 'retained_items': len(best['items']) if best else 0,
                 'retry_page_url': resume_from['url'] if resume_from is not None else None}
        if access:
            entry['access'] = access
        if status != 'ok':
            try:
                (evidence_dir / f'attempt-{attempt}.html').write_text(page.content(), encoding='utf-8')
                entry['url'] = page.url
                entry['title'] = page.title()
            except Exception as exc:
                entry['evidence_error'] = str(exc)
        history.append(entry)
        (evidence_dir / 'attempts.json').write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"{source['marketplace']}/{source['category']} attempt {attempt}/{max_attempts}: {status}; {reason}", flush=True)
        if blocked:
            raise AccessControlBlocked(f'{reason}; 已尝试 {attempt} 次，明确访问限制，停止该类目重试；保留此前有效结果 {len(best["items"]) if best else 0} 条；证据={evidence_dir}', partial_snapshot=best, diagnostics=access)
        if status in {'ok', 'partial'}:
            best['attempts'] = history
            return best  # Partial results use the persistent delayed retry, not a hot page reload.
        if attempt < max_attempts:
            time.sleep(5 * attempt)
    reason = f'已尝试 {max_attempts} 次；' + ' | '.join(f"第{x['attempt']}次: {x['reason']}" for x in history)
    if best:
        best['attempts'] = history
        best['error_message'] = reason
        return best
    error_type = ParseError if history[-1]['status'] == 'parse_error' else RuntimeError
    raise error_type(f'{reason}; 证据={evidence_dir}')
