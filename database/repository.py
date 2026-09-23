from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from crawler.amazon import parse_number
from database.schema import initialize_schema


class SnapshotStore:
    """Write Amazon New Releases snapshots to SQLite."""

    def __init__(
        self,
        path: Path,
        retention_days: int = 7,
    ) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
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
        for item in rows:
            asin = str(item.get("asin") or "").strip().upper()
            title = str(item.get("title") or "").strip()
            if not asin or not title:
                continue
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
                        review_count, price, price_text, rating, product_url, image_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                changed = connection.total_changes - before

        self.prune(date.fromisoformat(snapshot_date))
        return changed

    def prune(self, reference_date: date) -> int:
        snapshot_cutoff = reference_date - timedelta(days=self.retention_days - 1)
        with closing(self.connect()) as connection:
            with connection:
                before = connection.total_changes
                connection.execute(
                    "DELETE FROM observations WHERE snapshot_date < ?",
                    (snapshot_cutoff.isoformat(),),
                )
                return connection.total_changes - before
