"""One row per seller and marketplace, with optional company-name similarity search."""
from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import closing
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

PAGE_SIZE = 50
SIMILARITY_OPTIONS = (0, 70, 80, 90, 100)
SITE_ORDER = ("amazon.it", "amazon.fr", "amazon.de", "amazon.pl", "amazon.es")
EMPTY = {"", "---", "—", "N/A", "NA"}
SUFFIXES = (
    "limitedliabilitycompany", "youxiangongsi", "companylimited", "corporation",
    "colimited", "coltd", "limited", "gmbh", "srl", "llc", "ltd", "inc",
)


def _text(value: object) -> str:
    result = str(value or "").strip()
    return "" if result.upper() in EMPTY else result


@lru_cache(maxsize=100_000)
def _normalize_company(value: str) -> str:
    normalized = "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
    )
    for suffix in SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 2:
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def company_similarity(query: str, *names: str, minimum: int = 0) -> int:
    """Full-name similarity after punctuation and legal-form normalization."""
    needle = _normalize_company(query)
    if len(needle) < 3:
        return 0
    best = 0
    for name in names:
        candidate = _normalize_company(_text(name))
        if not candidate:
            continue
        if candidate == needle:
            return 100
        bound = 2 * min(len(needle), len(candidate)) / (len(needle) + len(candidate))
        floor = max(best, minimum) - 0.5
        if bound * 100 < floor:
            continue
        matcher = SequenceMatcher(None, needle, candidate, autojunk=False)
        if matcher.quick_ratio() * 100 < floor:
            continue
        best = max(best, round(matcher.ratio() * 100))
    return best


def _page_url(query: str, site: str, similarity: int, recency: str, page: int) -> str:
    return "/seller-vat?" + urlencode({
        "q": query, "site": site, "similarity": similarity, "recency": recency, "page": page,
    })


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _column(table: str, field: str, present: bool) -> str:
    return f"{table}.{field}" if present else "NULL"


def _base_sql(tables: set[str]) -> str:
    checks = "seller_site_checks" in tables
    discoveries = "seller_discoveries" in tables
    vies = "vat_checks" in tables
    keys = "SELECT marketplace,seller_id FROM sellers"
    if checks:
        keys += " UNION SELECT marketplace,seller_id FROM seller_site_checks"
    if discoveries:
        keys += " UNION SELECT 'amazon.it' AS marketplace,seller_id FROM seller_discoveries"
    return f"""
        WITH keys AS ({keys}), base AS (
          SELECT k.marketplace,k.seller_id,
            {_column('d','seller_name',discoveries)} AS seller_name,
            COALESCE(NULLIF(NULLIF(TRIM(s.company_name),''),'---'),
                     NULLIF(NULLIF(TRIM({_column('c','company_name',checks)}),''),'---'),
                     NULLIF(NULLIF(TRIM({_column('d','company_snapshot',discoveries)}),''),'---'),'') AS company_name,
            CASE WHEN NULLIF(NULLIF(TRIM(s.company_name),''),'---') IS NOT NULL THEN ''
                 WHEN NULLIF(NULLIF(TRIM({_column('c','company_name',checks)}),''),'---') IS NOT NULL THEN 'Amazon 店铺'
                 WHEN NULLIF(NULLIF(TRIM({_column('d','company_snapshot',discoveries)}),''),'---') IS NOT NULL THEN '卖家精灵'
                 ELSE '' END AS company_source,
            COALESCE(NULLIF(TRIM(s.business_address),''),
                     NULLIF(TRIM({_column('c','business_address',checks)}),''),'') AS business_address,
            COALESCE(s.vat_number,'') AS vat_number,
            COALESCE(NULLIF(NULLIF(TRIM(s.vies_company_name),''),'---'),
                     NULLIF(NULLIF(TRIM({_column('v','vies_company_name',vies)}),''),'---'),'') AS vies_company_name,
            {_column('v','vies_valid',vies)} AS vies_valid,
            {_column('v','country',vies)} AS vies_country,
            COALESCE(NULLIF(TRIM({_column('c','source_url',checks)}),''),
                     NULLIF(TRIM(s.source_url),''),'') AS source_url,
            {"COALESCE(c.status,CASE WHEN d.seller_id IS NOT NULL AND s.seller_id IS NULL THEN 'pending' END)" if checks and discoveries else _column('c','status',checks)} AS status,
            {_column('c','error',checks)} AS check_error,
            COALESCE({_column('c','checked_at',checks)},s.last_seen_at,'') AS checked_at,
            {_column('c','checked_at',checks)} AS site_checked_at,
            {'d.rowid' if discoveries else 'NULL'} AS discovery_order
          FROM keys k
          LEFT JOIN sellers s ON s.marketplace=k.marketplace AND s.seller_id=k.seller_id
          {'LEFT JOIN seller_site_checks c ON c.marketplace=k.marketplace AND c.seller_id=k.seller_id' if checks else ''}
          {'LEFT JOIN seller_discoveries d ON d.seller_id=k.seller_id' if discoveries else ''}
          {'LEFT JOIN vat_checks v ON v.vat_number=s.vat_number' if vies else ''}
        )
    """


def _where(query: str, site: str, similarity: int, recency: str, tables: set[str]) -> tuple[str, list[str]]:
    clauses: list[str] = []
    args: list[str] = []
    if site:
        clauses.append("base.marketplace=?")
        args.append(site)
    if recency == "latest":
        clauses.append("base.discovery_order IS NOT NULL")
    elif recency == "checked":
        clauses.append("NULLIF(base.site_checked_at,'') IS NOT NULL")
    if query and not similarity:
        fields = ("seller_id", "seller_name", "company_name", "business_address",
                  "vat_number", "vies_company_name", "marketplace")
        clauses.append("(" + " OR ".join(
            f"COALESCE(base.{field},'') LIKE ? ESCAPE '\\'" for field in fields
        ) + (
            " OR EXISTS (SELECT 1 FROM seller_vat_evidence e "
            "WHERE e.marketplace=base.marketplace AND e.seller_id=base.seller_id "
            "AND e.vat_number LIKE ? ESCAPE '\\')" if "seller_vat_evidence" in tables else ""
        ) + ")")
        args.extend([_like(query)] * (len(fields) + (1 if "seller_vat_evidence" in tables else 0)))
        prefixed = query.upper().replace(" ", "")
        if prefixed.startswith("IT") and _italian_vat_checksum(prefixed[2:]):
            raw = prefixed[2:]
            extra = " OR base.vat_number=?"
            args.append(raw)
            if "seller_vat_evidence" in tables:
                extra += (" OR EXISTS (SELECT 1 FROM seller_vat_evidence e "
                          "WHERE e.marketplace=base.marketplace AND e.seller_id=base.seller_id "
                          "AND e.vat_number=?)")
                args.append(raw)
            clauses[-1] = clauses[-1][:-1] + extra + ")"
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), args


def _valid_source(url: str, marketplace: str, seller_id: str) -> str:
    site = marketplace.removeprefix("amazon.")
    host = f"www.amazon.{site}"
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname == host:
            return url
    except ValueError:
        pass
    return f"https://{host}/sp?seller={quote(seller_id, safe='')}" if site.isalpha() else ""


def _italian_vat_checksum(number: str) -> bool:
    if len(number) != 11 or not number.isascii() or not number.isdigit():
        return False
    total = sum(int(number[index]) for index in (0, 2, 4, 6, 8))
    for index in (1, 3, 5, 7, 9):
        doubled = int(number[index]) * 2
        total += doubled - 9 if doubled > 9 else doubled
    return (10 - total % 10) % 10 == int(number[10])


def _display_vat(number: str, country_hint: str = "") -> tuple[str, bool]:
    """Return a country-qualified VAT, or hide a number with no sound country basis."""
    number = _text(number).upper().replace(" ", "")
    if not number:
        return "", False
    if len(number) >= 3 and number[:2].isalpha() and number[:2].isascii():
        return number, False
    if number.isascii() and number.isdigit():
        # EU VAT bodies have at most 12 characters. Longer numeric identifiers
        # in the uploaded file cannot be labelled as an EU VAT here.
        if len(number) > 12:
            return "", False
        country = _text(country_hint).upper()
        if len(country) == 2 and country.isalpha() and country.isascii():
            return country + number, False
        if _italian_vat_checksum(number):
            return "IT" + number, True
        return "", False
    return "", False


def _add_vats(db: sqlite3.Connection, rows: list[dict], tables: set[str]) -> None:
    if not rows:
        return
    for row in rows:
        row["vats"] = []
        primary = _text(row.get("vat_number"))
        display, inferred = _display_vat(primary, row.get("vies_country") or "")
        if display:
            row["vats"].append({
                "number": display, "raw_number": primary,
                "country_inferred": inferred,
                "vies_name": _text(row.get("vies_company_name")),
                "vies_valid": row.get("vies_valid"),
            })
    if "seller_vat_evidence" not in tables:
        return
    keys = [(row["marketplace"], row["seller_id"]) for row in rows]
    placeholders = ",".join("(?,?)" for _ in keys)
    values = [part for pair in keys for part in pair]
    vies_join = "LEFT JOIN vat_checks v ON v.vat_number=e.vat_number" if "vat_checks" in tables else ""
    vies_name = "v.vies_company_name" if "vat_checks" in tables else "NULL"
    vies_valid = "v.vies_valid" if "vat_checks" in tables else "NULL"
    vies_country = "v.country" if "vat_checks" in tables else "NULL"
    evidence = db.execute(f"""SELECT e.marketplace,e.seller_id,e.vat_number,e.vat_country,
        {vies_name} AS vies_company_name,{vies_valid} AS vies_valid,{vies_country} AS vies_country
        FROM seller_vat_evidence e {vies_join}
        WHERE (e.marketplace,e.seller_id) IN ({placeholders})
        ORDER BY e.vat_number""", values)
    by_key = {(row["marketplace"], row["seller_id"]): row for row in rows}
    for item in evidence:
        row = by_key[(item["marketplace"], item["seller_id"])]
        raw = _text(item["vat_number"])
        display, inferred = _display_vat(raw, item["vat_country"] or item["vies_country"] or "")
        if not display:
            continue
        existing = next((vat for vat in row["vats"] if vat["number"] == display), None)
        if existing:
            if not existing["vies_name"]:
                existing["vies_name"] = _text(item["vies_company_name"])
            if existing["vies_valid"] is None:
                existing["vies_valid"] = item["vies_valid"]
            if not inferred:
                existing["country_inferred"] = False
        else:
            row["vats"].append({
                "number": display, "raw_number": raw,
                "country_inferred": inferred,
                "vies_name": _text(item["vies_company_name"]),
                "vies_valid": item["vies_valid"],
            })


def load_store_matrix(
    path: Path, *, query: str = "", site: str = "", similarity: int = 0,
    recency: str = "all", page: int = 1,
) -> dict | None:
    if not path.is_file():
        return None
    query = query.strip()[:100]
    similarity = similarity if similarity in SIMILARITY_OPTIONS and query else 0
    recency = recency if recency in ("all", "latest", "checked") else "all"
    result = {
        "available": True, "error": "", "query": query, "site": site,
        "similarity": similarity, "similarity_options": SIMILARITY_OPTIONS,
        "recency": recency,
        "sites": [], "rows": [], "total": 0, "page": 1, "pages": 1,
        "previous_url": "", "next_url": "", "source_label": path.name,
    }
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sellers" not in tables:
                result.update(available=False, error="数据库缺少店铺记录。")
                return result
            if recency == "checked" and (
                "seller_site_checks" not in tables
                or not db.execute("SELECT 1 FROM seller_site_checks WHERE checked_at IS NOT NULL LIMIT 1").fetchone()
            ):
                recency = "all"
                result["recency"] = recency
            site_query = "SELECT DISTINCT marketplace FROM sellers"
            if "seller_site_checks" in tables:
                site_query += " UNION SELECT DISTINCT marketplace FROM seller_site_checks"
            sites = [row[0] for row in db.execute(site_query)]
            sites.sort(key=lambda value: (SITE_ORDER.index(value) if value in SITE_ORDER else len(SITE_ORDER), value))
            result["sites"] = [{"value": value, "label": value.removeprefix("amazon.").upper()} for value in sites]
            site = site if site in sites else ""
            result["site"] = site
            base_sql = _base_sql(tables)
            where, args = _where(query, site, similarity, recency, tables)
            if similarity:
                candidates = [dict(row) for row in db.execute(base_sql + "SELECT * FROM base" + where, args)]
                matched = []
                for row in candidates:
                    score = company_similarity(query, row["company_name"], row["vies_company_name"], minimum=similarity)
                    if score >= similarity:
                        row["similarity_score"] = score
                        matched.append(row)
                matched.sort(key=lambda row: (row["seller_id"], row["marketplace"]))
                if recency == "latest":
                    matched.sort(key=lambda row: row["discovery_order"] or 0, reverse=True)
                elif recency == "checked":
                    matched.sort(key=lambda row: row["site_checked_at"] or "", reverse=True)
                matched.sort(key=lambda row: row["similarity_score"], reverse=True)
                result["total"] = len(matched)
                result["pages"] = max(1, (len(matched) + PAGE_SIZE - 1) // PAGE_SIZE)
                result["page"] = min(max(1, page), result["pages"])
                result["rows"] = matched[(result["page"] - 1) * PAGE_SIZE:result["page"] * PAGE_SIZE]
            else:
                result["total"] = db.execute(base_sql + "SELECT COUNT(*) FROM base" + where, args).fetchone()[0]
                result["pages"] = max(1, (result["total"] + PAGE_SIZE - 1) // PAGE_SIZE)
                result["page"] = min(max(1, page), result["pages"])
                order = {
                    "latest": "discovery_order DESC, seller_id, marketplace",
                    "checked": ("MAX(site_checked_at) OVER (PARTITION BY seller_id) DESC, seller_id, "
                                "CASE marketplace WHEN 'amazon.it' THEN 0 WHEN 'amazon.fr' THEN 1 "
                                "WHEN 'amazon.de' THEN 2 WHEN 'amazon.pl' THEN 3 "
                                "WHEN 'amazon.es' THEN 4 ELSE 5 END"),
                    "all": "seller_id,marketplace",
                }[recency]
                result["rows"] = [dict(row) for row in db.execute(
                    base_sql + "SELECT * FROM base" + where +
                    f" ORDER BY {order} LIMIT ? OFFSET ?",
                    [*args, PAGE_SIZE, (result["page"] - 1) * PAGE_SIZE],
                )]
            for row in result["rows"]:
                row["company_name"] = _text(row["company_name"])
                row["similarity_url"] = _page_url(row["company_name"], "", 80, "all", 1) if row["company_name"] else ""
                row["business_address"] = _text(row["business_address"])
                row["source_url"] = _valid_source(_text(row["source_url"]), row["marketplace"], row["seller_id"])
            _add_vats(db, result["rows"], tables)
    except (sqlite3.DatabaseError, OSError, ValueError) as error:
        result.update(available=False, error=f"数据库读取失败：{type(error).__name__}")
        return result
    result["previous_url"] = _page_url(query, site, similarity, recency, result["page"] - 1) if result["page"] > 1 else ""
    result["next_url"] = _page_url(query, site, similarity, recency, result["page"] + 1) if result["page"] < result["pages"] else ""
    return result
