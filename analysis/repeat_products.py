from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from database.repository import connect_readonly


def find_repeat_products(
    db_path: Path | str,
    *,
    marketplace: str,
    days: int = 10,
    min_days: int = 3,
    category: str | None = None,
    as_of: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Find products repeatedly present across the available daily snapshots."""
    if days < 1:
        raise ValueError("days must be at least 1")
    if min_days < 1:
        raise ValueError("min_days must be at least 1")

    market = marketplace.upper()
    with closing(connect_readonly(db_path)) as connection:
        latest = as_of or connection.execute(
            "SELECT MAX(snapshot_date) FROM observations WHERE marketplace = ?",
            (market,),
        ).fetchone()[0]
        if not latest:
            return []

        cutoff = (date.fromisoformat(str(latest)) - timedelta(days=days - 1)).isoformat()
        date_sql = """
            SELECT DISTINCT snapshot_date
            FROM observations
            WHERE marketplace = ? AND snapshot_date >= ? AND snapshot_date <= ?
        """
        date_params: list[Any] = [market, cutoff, latest]
        if category:
            date_sql += " AND category = ?"
            date_params.append(category)
        date_sql += " ORDER BY snapshot_date DESC"
        available_dates = [str(row[0]) for row in connection.execute(date_sql, date_params).fetchall()]

        sql = """
            SELECT
                asin,
                snapshot_date,
                MIN(rank) AS rank,
                MAX(title) AS title,
                MAX(category) AS category,
                MAX(product_type) AS product_type,
                MAX(product_url) AS product_url,
                MAX(image_url) AS image_url
            FROM observations
            WHERE marketplace = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
        """
        params: list[Any] = [market, cutoff, latest]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " GROUP BY asin, snapshot_date ORDER BY asin, snapshot_date"
        rows = connection.execute(sql, params).fetchall()

    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[str(row["asin"])].append(row)

    results: list[dict[str, Any]] = []
    for asin, history_rows in grouped.items():
        if len(history_rows) < min_days:
            continue

        by_date = {str(row["snapshot_date"]): row for row in history_rows}
        consecutive = 0
        for snapshot_date in available_dates:
            if snapshot_date not in by_date:
                break
            consecutive += 1

        latest_row = max(history_rows, key=lambda row: str(row["snapshot_date"]))
        current_row = by_date.get(available_dates[0]) if available_dates else None
        results.append(
            {
                "asin": asin,
                "days_present": len(history_rows),
                "snapshots_available": len(available_dates),
                "repeat_rate": round(len(history_rows) / max(1, len(available_dates)), 4),
                "consecutive_snapshots": consecutive,
                "current_rank": int(current_row["rank"]) if current_row is not None else None,
                "best_rank": min(int(row["rank"]) for row in history_rows),
                "average_rank": round(
                    sum(int(row["rank"]) for row in history_rows) / len(history_rows),
                    2,
                ),
                "last_seen": str(latest_row["snapshot_date"]),
                "title": str(latest_row["title"]),
                "category": str(latest_row["category"]),
                "product_type": str(latest_row["product_type"]),
                "product_url": str(latest_row["product_url"]),
                "image_url": str(latest_row["image_url"]),
            }
        )

    results.sort(
        key=lambda item: (
            item["current_rank"] is not None,
            item["days_present"],
            item["consecutive_snapshots"],
            -(item["current_rank"] or 9999),
        ),
        reverse=True,
    )
    return results[:limit]
