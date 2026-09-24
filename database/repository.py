from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from crawler.amazon import parse_number, actual_rank
from crawler.lifecycle import now_shanghai, next_retry_time, TERMINAL_STATUSES
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
        category: str,
        preserve_more_complete: bool = False,
    ) -> int:
        rows = list(items)
        if not rows:
            return 0

        values: list[tuple[Any, ...]] = []
        for item in rows:
            asin = str(item.get("asin") or "").strip().upper()
            title = str(item.get("title") or "").strip()
            image_url = str(item.get("image_url") or "").strip()
            if not asin or not title or not image_url:
                continue
            rank = actual_rank(item.get("rank"))
            if rank is None:
                raise ValueError("Cannot store product without an actual positive rank")
            product_url = str(item.get("product_url") or "").strip()
            created_at = datetime.now().astimezone().isoformat(timespec="seconds")
            values.append(
                (
                    source_url,
                    marketplace.upper(),
                    category,
                    snapshot_date,
                    rank,
                    asin,
                    title,
                    int(parse_number(item.get("review_count"), 0)),
                    parse_number(item.get("price"), 0),
                    str(item.get("price_text") or "").strip(),
                    parse_number(item.get("rating"), 0),
                    product_url,
                    image_url,
                    created_at,
                )
            )

        with closing(self.connect()) as connection:
            with connection:
                if preserve_more_complete:
                    existing = connection.execute(
                        "SELECT COUNT(*) FROM observations WHERE source_url = ? AND snapshot_date = ?",
                        (source_url, snapshot_date),
                    ).fetchone()[0]
                    if existing > len(values):
                        return 0
                connection.execute(
                    "DELETE FROM observations WHERE source_url = ? AND snapshot_date = ?",
                    (source_url, snapshot_date),
                )
                connection.executemany(
                    """
                    INSERT INTO observations (
                        source_url, marketplace, category, snapshot_date, rank, asin, title,
                        review_count, price, price_text, rating, product_url, image_url, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.executemany(
                    """
                    INSERT INTO product_seen (marketplace, asin, first_seen, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(marketplace, asin) DO UPDATE SET
                        first_seen = MIN(product_seen.first_seen, excluded.first_seen),
                        last_seen = MAX(product_seen.last_seen, excluded.last_seen)
                    """,
                    [
                        (marketplace.upper(), value[5], snapshot_date, snapshot_date)
                        for value in values
                    ],
                )

        self.prune(date.fromisoformat(snapshot_date))
        return len(values)

    def start_run(
        self,
        *,
        source_url: str,
        marketplace: str,
        category: str,
        snapshot_date: str,
        started_at: str,
        retry_round: int = 0,
    ) -> str:
        run_id = uuid4().hex
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO collection_runs (
                        run_id, source_url, marketplace, category, snapshot_date,
                        started_at, status, item_count, retry_round
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', 0, ?)
                    """,
                    (
                        run_id,
                        source_url,
                        marketplace.upper(),
                        category,
                        snapshot_date,
                        started_at,
                        retry_round,
                    ),
                )
        return run_id

    def enqueue_run(self, *, source_url, marketplace, category, snapshot_date, started_at, retry_round=None):
        """Persist one pending request per source/day, even while another source runs."""
        with closing(self.connect()) as c:
            with c:
                c.execute("BEGIN IMMEDIATE")
                active = c.execute("SELECT run_id FROM collection_runs WHERE source_url=? AND snapshot_date=? AND status IN ('QUEUED','RUNNING') ORDER BY started_at DESC,rowid DESC LIMIT 1", (source_url,snapshot_date)).fetchone()
                if active:
                    return active[0]
                if retry_round is None:
                    retry_round = c.execute("SELECT COALESCE(MAX(retry_round),0) FROM collection_runs WHERE source_url=? AND snapshot_date=?", (source_url,snapshot_date)).fetchone()[0]
                run_id = uuid4().hex
                c.execute("""INSERT INTO collection_runs (run_id,source_url,marketplace,category,snapshot_date,started_at,status,item_count,retry_round)
                             VALUES (?,?,?,?,?,?,'QUEUED',0,?)""", (run_id,source_url,marketplace,category,snapshot_date,started_at,retry_round))
                return run_id

    def mark_running(self, run_id):
        with closing(self.connect()) as c:
            with c:
                return c.execute("UPDATE collection_runs SET status='RUNNING', next_retry_at=NULL WHERE run_id=? AND status='QUEUED'", (run_id,)).rowcount == 1

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        item_count: int,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal run status: {status}")
        now = now or now_shanghai()
        finished_at = now.isoformat(timespec="seconds")
        with closing(self.connect()) as connection:
            with connection:
                row = connection.execute("SELECT snapshot_date,retry_round FROM collection_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Unknown run: {run_id}")
                next_retry_at = next_retry_time(status, row[0], row[1], now)
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET finished_at = ?, status = ?, item_count = ?, error_message = ?, next_retry_at = ?
                    WHERE run_id = ?
                    """,
                    (finished_at, status, item_count, error_message, next_retry_at, run_id),
                )

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
