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
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETE', 'FAILED')),
    item_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
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

        INSERT INTO product_seen (marketplace, asin, first_seen, last_seen)
        SELECT marketplace, UPPER(asin), MIN(snapshot_date), MAX(snapshot_date)
        FROM observations
        GROUP BY marketplace, UPPER(asin)
        ON CONFLICT(marketplace, asin) DO UPDATE SET
            first_seen = MIN(product_seen.first_seen, excluded.first_seen),
            last_seen = MAX(product_seen.last_seen, excluded.last_seen);

        INSERT INTO collection_runs (
            run_id, source_url, marketplace, category, snapshot_date,
            started_at, finished_at, status, item_count, error_message
        )
        SELECT
            'backfill:' || marketplace || ':' || category || ':' || snapshot_date || ':' || source_url,
            source_url,
            marketplace,
            category,
            snapshot_date,
            MIN(created_at),
            MAX(created_at),
            'COMPLETE',
            COUNT(*),
            NULL
        FROM observations AS source_observations
        WHERE NOT EXISTS (
            SELECT 1 FROM collection_runs
            WHERE collection_runs.source_url = source_observations.source_url
              AND collection_runs.snapshot_date = source_observations.snapshot_date
        )
        GROUP BY source_url, marketplace, category, snapshot_date;

        DROP TABLE IF EXISTS scheduler_state;
        """
    )
    connection.commit()
