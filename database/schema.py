from __future__ import annotations

import sqlite3


OBSERVATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS observations (
    source_url TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    snapshot_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    asin TEXT NOT NULL,
    title TEXT NOT NULL,
    review_count INTEGER NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    price_text TEXT NOT NULL DEFAULT '',
    rating REAL NOT NULL DEFAULT 0,
    product_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_url, snapshot_date, asin)
);
"""

PRODUCT_SEEN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS product_seen (
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (marketplace, asin)
);
"""

COLLECTION_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    category TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETE', 'PARTIAL', 'BLOCKED', 'SKIPPED', 'PARSE_ERROR', 'FAILED', 'INTERRUPTED')),
    item_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    retry_round INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT
);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(OBSERVATIONS_TABLE_SQL)
    connection.execute(PRODUCT_SEEN_TABLE_SQL)
    connection.execute(COLLECTION_RUNS_TABLE_SQL)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_source_date_rank
            ON observations(source_url, snapshot_date, rank);
        CREATE INDEX IF NOT EXISTS idx_observations_market_date_asin
            ON observations(marketplace, snapshot_date, asin);
        CREATE INDEX IF NOT EXISTS idx_observations_asin_date
            ON observations(marketplace, asin, snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_observations_category_date_rank
            ON observations(marketplace, category, snapshot_date, rank);
        CREATE INDEX IF NOT EXISTS idx_collection_runs_source_date
            ON collection_runs(source_url, snapshot_date, started_at);
        CREATE INDEX IF NOT EXISTS idx_collection_runs_date_status
            ON collection_runs(snapshot_date, status);
        """
    )
    connection.commit()
