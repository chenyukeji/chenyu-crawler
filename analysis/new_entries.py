from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from database.repository import connect_readonly


def find_new_entries(
    db_path: Path | str,
    *,
    marketplace: str,
    days: int = 1,
    category: str | None = None,
    as_of: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return ASINs whose 90-day identity first_seen falls inside the requested window."""
    if days < 1:
        raise ValueError("days must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    with closing(connect_readonly(db_path)) as connection:
        market = marketplace.upper()
        latest = as_of or connection.execute(
            "SELECT MAX(snapshot_date) FROM observations WHERE marketplace = ?",
            (market,),
        ).fetchone()[0]
        if not latest:
            return []

        cutoff = (date.fromisoformat(str(latest)) - timedelta(days=days - 1)).isoformat()
        sql = """
            SELECT
                o.asin,
                o.snapshot_date,
                o.rank,
                o.title,
                o.category,
                o.product_type,
                o.product_url,
                o.image_url,
                o.price,
                o.review_count,
                o.rating,
                p.first_seen
            FROM observations o
            JOIN product_seen p
              ON p.marketplace = o.marketplace AND p.asin = o.asin
            WHERE o.marketplace = ?
              AND o.snapshot_date >= ?
              AND p.first_seen >= ?
        """
        params: list[Any] = [market, cutoff, cutoff]
        if category:
            sql += " AND o.category = ?"
            params.append(category)
        sql += " ORDER BY o.asin, o.snapshot_date DESC, o.rank ASC"
        rows = connection.execute(sql, params).fetchall()

    newest_by_asin: dict[str, dict[str, Any]] = {}
    for row in rows:
        asin = str(row["asin"])
        if asin in newest_by_asin:
            continue
        newest_by_asin[asin] = {
            "asin": asin,
            "first_seen": str(row["first_seen"]),
            "latest_seen": str(row["snapshot_date"]),
            "current_rank": int(row["rank"]),
            "title": str(row["title"]),
            "category": str(row["category"]),
            "product_type": str(row["product_type"]),
            "product_url": str(row["product_url"]),
            "image_url": str(row["image_url"]),
            "price": float(row["price"]),
            "review_count": int(row["review_count"]),
            "rating": float(row["rating"]),
        }

    return sorted(
        newest_by_asin.values(),
        key=lambda item: (item["first_seen"], -item["current_rank"]),
        reverse=True,
    )[:limit]
