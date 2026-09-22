from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from database.repository import connect_readonly


def get_rank_history(
    db_path: Path | str,
    *,
    asin: str,
    marketplace: str,
    days: int = 10,
    category: str | None = None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    market = marketplace.upper()
    asin_value = asin.strip().upper()
    with closing(connect_readonly(db_path)) as connection:
        latest = as_of or connection.execute(
            "SELECT MAX(snapshot_date) FROM observations WHERE marketplace = ?",
            (market,),
        ).fetchone()[0]
        if not latest:
            return []
        cutoff = (date.fromisoformat(str(latest)) - timedelta(days=days - 1)).isoformat()
        sql = """
            SELECT snapshot_date, MIN(rank) AS rank
            FROM observations
            WHERE marketplace = ?
              AND asin = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
        """
        params: list[Any] = [market, asin_value, cutoff, latest]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " GROUP BY snapshot_date ORDER BY snapshot_date"
        rows = connection.execute(sql, params).fetchall()
    return [{"date": str(row["snapshot_date"]), "rank": int(row["rank"])} for row in rows]


def find_rising_products(
    db_path: Path | str,
    *,
    marketplace: str,
    days: int = 7,
    min_improvement: int = 5,
    category: str | None = None,
    as_of: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Find products still on the latest snapshot whose rank improved across the window."""
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
        if len(history_rows) < 2:
            continue
        history_rows.sort(key=lambda row: str(row["snapshot_date"]))
        if str(history_rows[-1]["snapshot_date"]) != str(latest):
            continue

        ranks = [int(row["rank"]) for row in history_rows]
        period_change = ranks[0] - ranks[-1]
        if period_change < min_improvement:
            continue

        previous_rank = ranks[-2]
        current_rank = ranks[-1]
        one_step_change = previous_rank - current_rank
        velocity = period_change / max(1, len(ranks) - 1)
        latest_row = history_rows[-1]
        results.append(
            {
                "asin": asin,
                "current_rank": current_rank,
                "previous_rank": previous_rank,
                "rank_1step_change": one_step_change,
                "period_rank_change": period_change,
                "rank_velocity": round(velocity, 2),
                "trend": "rising",
                "rank_history": [
                    {"date": str(row["snapshot_date"]), "rank": int(row["rank"])}
                    for row in history_rows
                ],
                "title": str(latest_row["title"]),
                "category": str(latest_row["category"]),
                "product_type": str(latest_row["product_type"]),
                "product_url": str(latest_row["product_url"]),
                "image_url": str(latest_row["image_url"]),
            }
        )

    results.sort(
        key=lambda item: (item["period_rank_change"], item["rank_velocity"], -item["current_rank"]),
        reverse=True,
    )
    return results[:limit]
