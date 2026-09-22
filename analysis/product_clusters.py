from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from database.repository import connect_readonly


def find_hot_clusters(
    db_path: Path | str,
    *,
    marketplace: str,
    days: int = 10,
    min_unique_asins: int = 2,
    category: str | None = None,
    as_of: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Group products by normalized product_type to find repeated product concepts."""
    market = marketplace.upper()
    with closing(connect_readonly(db_path)) as connection:
        latest = as_of or connection.execute(
            "SELECT MAX(snapshot_date) FROM observations WHERE marketplace = ?",
            (market,),
        ).fetchone()[0]
        if not latest:
            return []
        cutoff = (date.fromisoformat(str(latest)) - timedelta(days=days - 1)).isoformat()

        sql = """
            SELECT
                product_type,
                COUNT(DISTINCT asin) AS unique_asins,
                COUNT(DISTINCT snapshot_date) AS active_days,
                MIN(rank) AS best_rank,
                ROUND(AVG(rank), 2) AS average_rank,
                MAX(snapshot_date) AS last_seen
            FROM observations
            WHERE marketplace = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
              AND COALESCE(product_type, '') <> ''
              AND product_type <> 'Unknown Product'
        """
        params: list[Any] = [market, cutoff, latest]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += """
            GROUP BY product_type
            HAVING COUNT(DISTINCT asin) >= ?
            ORDER BY unique_asins DESC, active_days DESC, best_rank ASC
            LIMIT ?
        """
        params.extend([min_unique_asins, limit])
        rows = connection.execute(sql, params).fetchall()

    return [
        {
            "product_type": str(row["product_type"]),
            "unique_asins": int(row["unique_asins"]),
            "active_days": int(row["active_days"]),
            "best_rank": int(row["best_rank"]),
            "average_rank": float(row["average_rank"]),
            "last_seen": str(row["last_seen"]),
        }
        for row in rows
    ]
