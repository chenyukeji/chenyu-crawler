"""Merge seller-level research and public VAT evidence into amazon_it.sqlite3."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from crawler.seller_vat import SELLER

SITES = {"it", "fr", "de", "pl", "es"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS seller_discoveries (
  seller_id TEXT PRIMARY KEY, seller_name TEXT, company_snapshot TEXT,
  source TEXT NOT NULL, first_page INTEGER, discovered_at TEXT
);
CREATE TABLE IF NOT EXISTS seller_site_checks (
  marketplace TEXT NOT NULL, seller_id TEXT NOT NULL, status TEXT NOT NULL,
  checked_at TEXT, error TEXT, company_name TEXT, business_address TEXT, source_url TEXT,
  PRIMARY KEY (marketplace, seller_id)
);
CREATE TABLE IF NOT EXISTS seller_vat_evidence (
  marketplace TEXT NOT NULL, seller_id TEXT NOT NULL, vat_number TEXT NOT NULL,
  vat_country TEXT, company_name TEXT, business_address TEXT, source_url TEXT,
  checked_at TEXT, PRIMARY KEY (marketplace, seller_id, vat_number)
);
CREATE INDEX IF NOT EXISTS seller_vat_evidence_number_idx ON seller_vat_evidence(vat_number);
CREATE TABLE IF NOT EXISTS vat_checks (
  vat_number TEXT PRIMARY KEY, country TEXT, vies_valid INTEGER,
  vies_company_name TEXT, vies_address TEXT, checked_at TEXT, error TEXT
);
"""


def read_latest_evidence(path: Path) -> dict:
    latest = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        site = item.get("site")
        seller_id = str(item.get("seller_id") or "").upper()
        if site in SITES and SELLER.fullmatch(seller_id):
            latest[(seller_id, site)] = item
    return latest


def upsert_discoveries(db: sqlite3.Connection, candidates: list[dict]) -> int:
    count = 0
    for item in candidates:
        seller_id = str(item.get("seller_id") or "").upper()
        if not SELLER.fullmatch(seller_id):
            continue
        db.execute("""INSERT INTO seller_discoveries(seller_id,seller_name,company_snapshot,source,first_page,discovered_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(seller_id) DO UPDATE SET
            seller_name=excluded.seller_name,company_snapshot=excluded.company_snapshot,
            source=excluded.source,first_page=excluded.first_page""",
            (seller_id, item.get("seller_name", ""), item.get("company_snapshot", ""),
             "SellerSprite IT 近30天上架 BuyBox", item.get("first_page"), None))
        count += 1
    return count


def save_discoveries(db_path: Path, candidates: list[dict]) -> int:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        db.executescript(SCHEMA)
        with db:
            return upsert_discoveries(db, candidates)
    finally:
        db.close()


def upsert_site_result(db: sqlite3.Connection, item: dict,
                       existing_vies: dict[str, str] | None = None) -> int:
    """Write one completed store-site result within the caller's transaction."""
    site = str(item.get("site") or "")
    seller_id = str(item.get("seller_id") or "").upper()
    if site not in SITES or not SELLER.fullmatch(seller_id):
        raise ValueError("invalid store-site result")
    marketplace = "amazon." + site
    company = str(item.get("company") or "")
    address = str(item.get("address") or "")
    url = str(item.get("url") or f"https://www.amazon.{site}/sp?seller={seller_id}")
    status = str(item.get("status") or "")
    checked_at = str(item.get("checked_at") or "")
    db.execute("""INSERT INTO seller_site_checks
        (marketplace,seller_id,status,checked_at,error,company_name,business_address,source_url)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(marketplace,seller_id) DO UPDATE SET
        status=excluded.status,checked_at=excluded.checked_at,error=excluded.error,
        company_name=excluded.company_name,business_address=excluded.business_address,
        source_url=excluded.source_url""",
        (marketplace, seller_id, status, checked_at, str(item.get("error") or ""), company, address, url))
    vats = [vat for vat in item.get("vats", []) if isinstance(vat, dict) and vat.get("number")]
    for vat in vats:
        number = str(vat["number"])
        db.execute("""INSERT INTO seller_vat_evidence
            (marketplace,seller_id,vat_number,vat_country,company_name,business_address,source_url,checked_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(marketplace,seller_id,vat_number) DO UPDATE SET
            vat_country=excluded.vat_country,company_name=excluded.company_name,
            business_address=excluded.business_address,source_url=excluded.source_url,
            checked_at=excluded.checked_at""",
            (marketplace, seller_id, number, str(vat.get("country") or ""), company, address, url, checked_at))
    if status != "failed":
        primary = next((vat for vat in vats if vat.get("country") == site.upper()), vats[0] if vats else {})
        number = str(primary.get("number") or "")
        if existing_vies is not None:
            vies_name = existing_vies.get(number, "")
        else:
            row = db.execute("""SELECT vies_company_name FROM sellers WHERE vat_number=?
                AND TRIM(COALESCE(vies_company_name,'')) NOT IN ('','---','—') LIMIT 1""", (number,)).fetchone()
            vies_name = row[0] if row else ""
        db.execute("""INSERT INTO sellers
            (marketplace,seller_id,company_name,vat_number,vies_company_name,business_address,source_url,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(marketplace,seller_id) DO UPDATE SET
            company_name=CASE WHEN TRIM(COALESCE(sellers.company_name,''))='' THEN excluded.company_name ELSE sellers.company_name END,
            vat_number=CASE WHEN TRIM(COALESCE(sellers.vat_number,''))='' THEN excluded.vat_number ELSE sellers.vat_number END,
            vies_company_name=CASE WHEN TRIM(COALESCE(sellers.vies_company_name,'')) IN ('','---','—') THEN excluded.vies_company_name ELSE sellers.vies_company_name END,
            business_address=CASE WHEN TRIM(COALESCE(sellers.business_address,''))='' THEN excluded.business_address ELSE sellers.business_address END,
            source_url=CASE WHEN TRIM(COALESCE(sellers.source_url,''))='' THEN excluded.source_url ELSE sellers.source_url END,
            last_seen_at=excluded.last_seen_at""",
            (marketplace, seller_id, company, number, vies_name, address, url, checked_at))
    return len(vats)


def save_site_result(db_path: Path, item: dict) -> int:
    """Commit one result so a stopped collection keeps every completed check."""
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
    try:
        db.executescript(SCHEMA)
        with db:
            return upsert_site_result(db, item)
    finally:
        db.close()


def merge(db_path: Path, report_dir: Path) -> dict:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    candidates_path = report_dir / "店铺候选.json"
    evidence_path = report_dir / "店铺公开信息_逐站证据.jsonl"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))["stores"]
    evidence = read_latest_evidence(evidence_path)
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        existing_vies = {
            number: name for number, name in db.execute(
                "SELECT vat_number,vies_company_name FROM sellers WHERE vat_number IS NOT NULL "
                "AND vies_company_name IS NOT NULL AND TRIM(vies_company_name) NOT IN ('','---','—')"
            )
        }
        discoveries = upsert_discoveries(db, candidates)
        vat_rows = sum(upsert_site_result(db, item, existing_vies) for item in evidence.values())
        db.commit()
        return {"discoveries": discoveries, "site_checks": len(evidence), "vat_evidence": vat_rows,
                "sellers_total": db.execute("SELECT COUNT(*) FROM sellers").fetchone()[0]}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    print(merge(args.db, args.report_dir))


if __name__ == "__main__":
    main()
