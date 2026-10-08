"""Check newly collected EU VAT numbers with the European Commission VIES service."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from merge_store_vat_database import SCHEMA

ROOT = Path(__file__).resolve().parent
EU_PREFIXES = {
    'AT', 'BE', 'BG', 'CY', 'CZ', 'DE', 'DK', 'EE', 'EL', 'ES', 'FI', 'FR',
    'HR', 'HU', 'IE', 'IT', 'LT', 'LU', 'LV', 'MT', 'NL', 'PL', 'PT', 'RO',
    'SE', 'SI', 'SK', 'XI',
}
VAT_PATTERN = re.compile(r'^([A-Z]{2})([A-Z0-9]{2,20})$')
EMPTY_NAMES = {'', '---', '—', 'N/A', 'NA'}


def candidates(db: sqlite3.Connection) -> list[str]:
    """Only public VAT evidence missing both a saved VIES result and a known VIES name."""
    return [number for (number,) in db.execute('''
        SELECT e.vat_number FROM seller_vat_evidence e
        WHERE NOT EXISTS (SELECT 1 FROM vat_checks v WHERE v.vat_number=e.vat_number)
          AND NOT EXISTS (
            SELECT 1 FROM sellers s WHERE s.vat_number=e.vat_number
            AND TRIM(COALESCE(s.vies_company_name,'')) NOT IN ('','---','—','N/A','NA')
          )
        GROUP BY e.vat_number
        ORDER BY MAX(COALESCE(e.checked_at,'')) DESC,
          CASE SUBSTR(e.vat_number,1,2)
          WHEN 'IT' THEN 0 WHEN 'FR' THEN 1 WHEN 'DE' THEN 2
          WHEN 'PL' THEN 3 WHEN 'ES' THEN 4 ELSE 5 END,
          e.vat_number
    ''') if (match := VAT_PATTERN.fullmatch(number)) and match.group(1) in EU_PREFIXES]


def fetch_vies(number: str) -> dict:
    country, local = number[:2], number[2:]
    url = f'https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{local}'
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'CHENYU-store-vat-research/1.0'})
    with urlopen(request, timeout=18) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get('isValid'), bool):
        raise ValueError('VIES response has no isValid boolean')
    name = str(payload.get('name') or '').strip()
    address = str(payload.get('address') or '').strip()
    return {
        'valid': payload['isValid'],
        'name': name if name.upper() not in EMPTY_NAMES else '',
        'address': address if address.upper() not in EMPTY_NAMES else '',
    }


def save_result(db: sqlite3.Connection, number: str, result: dict | None, error: str = '') -> None:
    valid = int(result['valid']) if result is not None else None
    name = result['name'] if result is not None else ''
    address = result['address'] if result is not None else ''
    db.execute('''INSERT INTO vat_checks
        (vat_number,country,vies_valid,vies_company_name,vies_address,checked_at,error)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(vat_number) DO UPDATE SET
        vies_valid=excluded.vies_valid,vies_company_name=excluded.vies_company_name,
        vies_address=excluded.vies_address,checked_at=excluded.checked_at,error=excluded.error''',
        (number, number[:2], valid, name, address,
         datetime.now(timezone.utc).isoformat(timespec='seconds'), error))
    if valid and name:
        db.execute('''UPDATE sellers SET vies_company_name=? WHERE vat_number=?
            AND TRIM(COALESCE(vies_company_name,'')) IN ('','---','—','N/A','NA')''',
            (name, number))
    db.commit()


def run(path: Path, *, limit: int = 0, interval: float = 0.8,
        numbers: set[str] | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    counts = {'checked': 0, 'valid': 0, 'named': 0, 'invalid': 0, 'errors': 0}
    try:
        db.executescript(SCHEMA)
        queue = candidates(db)
        if numbers is not None:
            queue = [number for number in queue if number in numbers]
        if limit:
            queue = queue[:limit]
        print(f'vies_queue={len(queue)}', flush=True)
        consecutive_errors = 0
        for number in queue:
            try:
                result = fetch_vies(number)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError) as exc:
                message = f'{type(exc).__name__}: {exc}'[:180]
                # Temporary network or service errors should be retried in a later run.
                counts['errors'] += 1
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    print(f'VIES paused after repeated errors: {message}', flush=True)
                    break
                time.sleep(max(interval, 1.5))
                continue
            consecutive_errors = 0
            save_result(db, number, result)
            counts['checked'] += 1
            if result['valid']:
                counts['valid'] += 1
                if result['name']:
                    counts['named'] += 1
            else:
                counts['invalid'] += 1
            if counts['checked'] % 20 == 0:
                print(f"vies_checked={counts['checked']}/{len(queue)} named={counts['named']} errors={counts['errors']}", flush=True)
            time.sleep(interval)
        return counts
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'amazon_it.sqlite3')
    parser.add_argument('--limit', type=int, default=0, help='Maximum new VAT checks; 0 checks all')
    parser.add_argument('--interval', type=float, default=0.8, help='Seconds between VIES requests')
    args = parser.parse_args()
    if args.limit < 0 or args.interval < 0.5:
        parser.error('limit must be nonnegative and interval must be at least 0.5 seconds')
    print(run(args.db, limit=args.limit, interval=args.interval), flush=True)


if __name__ == '__main__':
    main()
