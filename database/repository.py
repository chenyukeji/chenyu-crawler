from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from crawler.amazon import infer_product_type, parse_number
from database.schema import initialize_schema


class TrendStore:
    """Write-side store for daily snapshots plus a lightweight ASIN identity index."""

    def __init__(
        self,
        path: Path,
        retention_days: int = 10,
        identity_retention_days: int = 90,
    ) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.identity_retention_days = identity_retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            with connection:
                initialize_schema(connection)

    def backfill_categories(self, sources: Iterable[dict[str, Any]]) -> int:
        """Populate the new category column for historical rows using configured source URLs."""
        changed = 0
        with closing(self.connect()) as connection:
            with connection:
                before = connection.total_changes
                for source in sources:
                    connection.execute(
                        """
                        UPDATE observations
                        SET category = ?
                        WHERE source_url = ? AND COALESCE(category, '') = ''
                        """,
                        (str(source.get("category") or ""), str(source.get("url") or "")),
                    )
                changed = connection.total_changes - before
        return changed

    def ingest(
        self,
        items: Iterable[dict[str, Any]],
        *,
        source_url: str,
        marketplace: str,
        snapshot_date: str,
        category: str = "",
    ) -> int:
        rows = list(items)
        if not rows:
            return 0

        values: list[tuple[Any, ...]] = []
        identity_values: list[tuple[Any, ...]] = []
        for item in rows:
            asin = str(item.get("asin") or "").strip().upper()
            title = str(item.get("title") or "").strip()
            if not asin or not title:
                continue
            product_type = str(item.get("product_type") or "").strip() or infer_product_type(title)
            product_url = str(item.get("product_url") or "").strip()
            image_url = str(item.get("image_url") or "").strip()
            values.append(
                (
                    source_url,
                    marketplace.upper(),
                    category,
                    snapshot_date,
                    int(parse_number(item.get("rank"), 9999)),
                    asin,
                    title,
                    int(parse_number(item.get("review_count"), 0)),
                    parse_number(item.get("price"), 0),
                    str(item.get("price_text") or "").strip(),
                    parse_number(item.get("rating"), 0),
                    product_type,
                    product_url,
                    image_url,
                    json.dumps(item.get("selling_points") or [], ensure_ascii=False),
                    str(item.get("selling_points_source") or "title").strip(),
                )
            )
            identity_values.append(
                (
                    marketplace.upper(),
                    asin,
                    snapshot_date,
                    snapshot_date,
                    title,
                    product_type,
                    product_url,
                    image_url,
                )
            )

        with closing(self.connect()) as connection:
            with connection:
                before = connection.total_changes
                connection.execute(
                    "DELETE FROM observations WHERE source_url = ? AND snapshot_date = ?",
                    (source_url, snapshot_date),
                )
                connection.executemany(
                    """
                    INSERT INTO observations (
                        source_url, marketplace, category, snapshot_date, rank, asin, title,
                        review_count, price, price_text, rating, product_type,
                        product_url, image_url, selling_points_json, selling_points_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.executemany(
                    """
                    INSERT INTO product_seen (
                        marketplace, asin, first_seen, last_seen,
                        title, product_type, product_url, image_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(marketplace, asin) DO UPDATE SET
                        first_seen = MIN(product_seen.first_seen, excluded.first_seen),
                        last_seen = MAX(product_seen.last_seen, excluded.last_seen),
                        title = excluded.title,
                        product_type = excluded.product_type,
                        product_url = excluded.product_url,
                        image_url = excluded.image_url
                    """,
                    identity_values,
                )
                changed = connection.total_changes - before

        self.prune(date.fromisoformat(snapshot_date))
        return changed

    def prune(self, reference_date: date) -> int:
        snapshot_cutoff = reference_date - timedelta(days=self.retention_days - 1)
        identity_cutoff = reference_date - timedelta(days=self.identity_retention_days - 1)
        with closing(self.connect()) as connection:
            with connection:
                before = connection.total_changes
                connection.execute(
                    "DELETE FROM observations WHERE snapshot_date < ?",
                    (snapshot_cutoff.isoformat(),),
                )
                connection.execute(
                    "DELETE FROM product_seen WHERE last_seen < ?",
                    (identity_cutoff.isoformat(),),
                )
                return connection.total_changes - before

    def snapshot_dates(self, source_url: str) -> list[str]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT snapshot_date
                FROM observations
                WHERE source_url = ?
                ORDER BY snapshot_date
                """,
                (source_url,),
            ).fetchall()
        return [str(row[0]) for row in rows]


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Open the selection database read-only for analysis/MCP callers."""
    db_path = Path(path).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection
