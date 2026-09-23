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

SCHEDULER_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_attempt_date TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(OBSERVATIONS_TABLE_SQL)
    connection.execute(SCHEDULER_STATE_TABLE_SQL)
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
        """
    )
    connection.commit()
