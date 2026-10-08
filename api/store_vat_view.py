"""Read-only totals for the saved seller and VAT database."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def load_store_overview(path: Path) -> dict:
    """Return small, read-only totals for the collection overview."""
    result = {"available": False, "stores": 0, "vats": 0, "sites": 0,
              "checks": 0, "last_checked_at": ""}
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sellers','seller_site_checks')"
            )}
            if "sellers" not in tables:
                return result
            stores, vats, sites = db.execute("""
                SELECT COUNT(DISTINCT NULLIF(seller_id,'')),
                       COUNT(DISTINCT NULLIF(TRIM(vat_number),'')),
                       COUNT(DISTINCT marketplace)
                FROM sellers
            """).fetchone()
            result.update(available=True, stores=stores, vats=vats, sites=sites)
            if "seller_site_checks" in tables:
                checks, last_checked_at = db.execute(
                    "SELECT COUNT(*), MAX(checked_at) FROM seller_site_checks"
                ).fetchone()
                result.update(checks=checks, last_checked_at=last_checked_at or "")
    except (sqlite3.DatabaseError, OSError, ValueError):
        return {"available": False, "stores": 0, "vats": 0, "sites": 0,
                "checks": 0, "last_checked_at": ""}
    return result
