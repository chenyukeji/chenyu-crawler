"""Read-only, bounded queries for the SellerSprite BuyBox seller VAT page."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

from crawler.seller_vat import ASIN_VALUE, SELLER, seller_url

PAGE_SIZE = 50
STATUSES = {"pending", "ok", "no_public_vat", "failed"}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page_url(run_id: str, tab: str, query: str, status: str, page: int) -> str:
    return "/seller-vat?" + urlencode({
        "run_id": run_id, "tab": tab, "q": query, "status": status, "page": page,
    })


def load_seller_vat_page(
    db_path: Path, *, run_id: str = "", tab: str = "sellers",
    query: str = "", status: str = "", page: int = 1,
) -> dict:
    """Return one page of existing data without creating or changing the database."""
    tab = tab if tab in {"sellers", "products"} else "sellers"
    query = query.strip()[:100]
    status = status if status in STATUSES and tab == "sellers" else ""
    page = max(1, page)
    empty = {
        "available": False, "error": "", "runs": [], "run_id": "", "tab": tab,
        "query": query, "status": status, "page": 1, "pages": 1,
        "total": 0, "rows": [], "summary": {
            "products": 0, "sellers": 0, "vats": 0, "unresolved": 0,
        },
        "seller_tab_url": "/seller-vat", "product_tab_url": "/seller-vat?tab=products",
        "previous_url": "", "next_url": "",
    }
    if not db_path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('auto_products','auto_sellers','auto_vats')"
            )}
            if tables != {"auto_products", "auto_sellers", "auto_vats"}:
                return {**empty, "error": "税号数据库暂时无法读取，请检查文件和数据表。"}
            runs = [dict(row) for row in db.execute(
                "SELECT run_id, COUNT(*) AS products FROM auto_products GROUP BY run_id ORDER BY run_id DESC"
            )]
            if not runs:
                return {**empty, "available": True}
            known_runs = {row["run_id"] for row in runs}
            run_id = run_id if run_id in known_runs else runs[0]["run_id"]
            summary = dict(db.execute("""
                SELECT COUNT(*) AS products,
                       COUNT(DISTINCT NULLIF(seller_id, '')) AS sellers,
                       SUM(CASE WHEN seller_id='' THEN 1 ELSE 0 END) AS unresolved
                FROM auto_products WHERE run_id=?
            """, (run_id,)).fetchone())
            summary["vats"] = db.execute("""
                SELECT COUNT(*) FROM auto_vats WHERE seller_id IN
                (SELECT seller_id FROM auto_products WHERE run_id=? AND seller_id!='')
            """, (run_id,)).fetchone()[0]
            summary["unresolved"] = summary["unresolved"] or 0
            pattern = _like(query)
            if tab == "sellers":
                where = ["p.run_id=?", "p.seller_id!=''"]
                args: list = [run_id]
                if status:
                    where.append("COALESCE(s.status, 'pending')=?")
                    args.append(status)
                if query:
                    where.append("""(p.seller_id LIKE ? ESCAPE '\\' OR
                        p.seller_name LIKE ? ESCAPE '\\' OR
                        p.asin LIKE ? ESCAPE '\\' OR
                        s.company_name LIKE ? ESCAPE '\\' OR
                        EXISTS (SELECT 1 FROM auto_vats v WHERE v.seller_id=p.seller_id
                                AND v.vat_number LIKE ? ESCAPE '\\'))""")
                    args.extend([pattern] * 5)
                filter_sql = " AND ".join(where)
                from_sql = f"FROM auto_products p LEFT JOIN auto_sellers s ON s.seller_id=p.seller_id WHERE {filter_sql}"
                total = db.execute("SELECT COUNT(DISTINCT p.seller_id) " + from_sql, args).fetchone()[0]
                pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                page = min(page, pages)
                rows = [dict(row) for row in db.execute("""
                    SELECT p.seller_id, MAX(COALESCE(s.company_name,'')) AS company_name,
                           MAX(COALESCE(s.business_address,'')) AS business_address,
                           MAX(COALESCE(s.status,'pending')) AS status,
                           MAX(COALESCE(s.error,'')) AS error,
                           MAX(COALESCE(s.checked_at,'')) AS checked_at,
                           MAX(COALESCE(p.seller_name,'')) AS seller_name,
                           (SELECT COUNT(DISTINCT px.asin) FROM auto_products px
                            WHERE px.run_id=? AND px.seller_id=p.seller_id) AS product_count
                    """ + from_sql + " GROUP BY p.seller_id ORDER BY p.seller_id LIMIT ? OFFSET ?",
                    [run_id, *args, PAGE_SIZE, (page - 1) * PAGE_SIZE])]
                ids = [row["seller_id"] for row in rows]
                vats: dict[str, list[dict]] = {seller_id: [] for seller_id in ids}
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    for vat in db.execute(
                        f"SELECT seller_id,vat_number,country,raw_value FROM auto_vats WHERE seller_id IN ({placeholders}) ORDER BY vat_number",
                        ids,
                    ):
                        vats[vat["seller_id"]].append(dict(vat))
                for row in rows:
                    row["vats"] = vats[row["seller_id"]]
                    row["seller_url"] = seller_url(str(row["seller_id"])) if SELLER.fullmatch(str(row["seller_id"] or "")) else ""
            else:
                where = ["p.run_id=?"]
                args = [run_id]
                if query:
                    where.append("""(p.asin LIKE ? ESCAPE '\\' OR p.seller_id LIKE ? ESCAPE '\\'
                                  OR p.seller_name LIKE ? ESCAPE '\\')""")
                    args.extend([pattern] * 3)
                filter_sql = " AND ".join(where)
                total = db.execute("SELECT COUNT(*) FROM auto_products p WHERE " + filter_sql, args).fetchone()[0]
                pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                page = min(page, pages)
                rows = [dict(row) for row in db.execute("""
                    SELECT p.asin,p.seller_id,p.seller_name,p.available_date,p.source_ref,
                           COALESCE(s.company_name,'') AS company_name,
                           COALESCE(s.status,'pending') AS status
                    FROM auto_products p LEFT JOIN auto_sellers s ON s.seller_id=p.seller_id
                    WHERE """ + filter_sql + " ORDER BY p.asin,p.seller_id LIMIT ? OFFSET ?",
                    [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE])]
                for row in rows:
                    row["product_url"] = f"https://www.amazon.it/dp/{row['asin']}" if ASIN_VALUE.fullmatch(str(row["asin"] or "")) else ""
                    row["seller_url"] = seller_url(str(row["seller_id"])) if SELLER.fullmatch(str(row["seller_id"] or "")) else ""
            return {
                "available": True, "runs": runs, "run_id": run_id, "tab": tab,
                "query": query, "status": status, "page": page, "pages": pages,
                "total": total, "rows": rows, "summary": summary,
                "seller_tab_url": _page_url(run_id, "sellers", "", "", 1),
                "product_tab_url": _page_url(run_id, "products", "", "", 1),
                "previous_url": _page_url(run_id, tab, query, status, page - 1) if page > 1 else "",
                "next_url": _page_url(run_id, tab, query, status, page + 1) if page < pages else "",
            }
    except (sqlite3.DatabaseError, OSError):
        return {**empty, "error": "税号数据库暂时无法读取，请稍后重试。"}
