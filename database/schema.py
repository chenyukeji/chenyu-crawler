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
    product_type TEXT NOT NULL,
    product_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    selling_points_json TEXT NOT NULL DEFAULT '[]',
    selling_points_source TEXT NOT NULL DEFAULT 'title',
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
    title TEXT NOT NULL DEFAULT '',
    product_type TEXT NOT NULL DEFAULT '',
    product_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (marketplace, asin)
);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create or migrate the local database without discarding existing history."""
    connection.execute(OBSERVATIONS_TABLE_SQL)

    existing_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(observations)").fetchall()
    }
    migrations = {
        "category": "TEXT NOT NULL DEFAULT ''",
        "price_text": "TEXT NOT NULL DEFAULT ''",
        "rating": "REAL NOT NULL DEFAULT 0",
        "product_url": "TEXT NOT NULL DEFAULT ''",
        "image_url": "TEXT NOT NULL DEFAULT ''",
        "selling_points_json": "TEXT NOT NULL DEFAULT '[]'",
        "selling_points_source": "TEXT NOT NULL DEFAULT 'title'",
    }
    for column, definition in migrations.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE observations ADD COLUMN {column} {definition}")

    connection.execute(PRODUCT_SEEN_TABLE_SQL)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_lookup
            ON observations(source_url, snapshot_date, product_type, rank);
        CREATE INDEX IF NOT EXISTS idx_observations_market_date
            ON observations(marketplace, snapshot_date, asin);
        CREATE INDEX IF NOT EXISTS idx_observations_asin_date
            ON observations(marketplace, asin, snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_observations_category_date
            ON observations(marketplace, category, snapshot_date, rank);
        CREATE INDEX IF NOT EXISTS idx_product_seen_first
            ON product_seen(marketplace, first_seen);
        CREATE INDEX IF NOT EXISTS idx_product_seen_last
            ON product_seen(marketplace, last_seen);
        """
    )

    seen_count = connection.execute("SELECT COUNT(*) FROM product_seen").fetchone()[0]
    if seen_count == 0:
        connection.execute(
            """
            INSERT INTO product_seen (
                marketplace, asin, first_seen, last_seen,
                title, product_type, product_url, image_url
            )
            SELECT
                marketplace,
                asin,
                MIN(snapshot_date),
                MAX(snapshot_date),
                MAX(title),
                MAX(product_type),
                MAX(product_url),
                MAX(image_url)
            FROM observations
            GROUP BY marketplace, asin
            """
        )
