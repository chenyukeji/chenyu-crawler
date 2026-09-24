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
    # Rebuild the old CHECK constraint atomically, preserving every run id.
    connection.execute("BEGIN IMMEDIATE")
    try:
        old = connection.execute("SELECT sql FROM sqlite_master WHERE name='collection_runs'").fetchone()
        if old and "QUEUED" not in old[0]:
            reliable_schema = "INTERRUPTED" in old[0]
            connection.execute("ALTER TABLE collection_runs RENAME TO collection_runs_legacy")
            connection.execute(COLLECTION_RUNS_TABLE_SQL)
            if reliable_schema:
                connection.execute("""
                    INSERT INTO collection_runs SELECT * FROM collection_runs_legacy
                """)
            else:
                connection.execute("""
                    INSERT INTO collection_runs (run_id,source_url,marketplace,category,snapshot_date,
                        started_at,finished_at,status,item_count,error_message)
                    SELECT run_id,source_url,marketplace,category,snapshot_date,started_at,finished_at,
                        CASE WHEN status != 'FAILED' THEN status
                             WHEN error_message LIKE '未请求：%' OR error_message = 'Run stopped after access control' THEN 'SKIPPED'
                             WHEN error_message LIKE '%ACCESS_BLOCKED%' OR error_message LIKE '%access-control%' OR error_message LIKE 'Amazon returned HTTP %' THEN 'BLOCKED'
                             WHEN error_message LIKE '本轮诊断主动中止%' THEN 'INTERRUPTED'
                             WHEN item_count > 0 THEN 'PARTIAL'
                             WHEN error_message LIKE '%no visible New Releases product cards%' OR error_message LIKE '%no valid ASIN/title%' THEN 'PARSE_ERROR'
                             ELSE 'FAILED' END,
                        item_count,error_message FROM collection_runs_legacy
                """)
            connection.execute("DROP TABLE collection_runs_legacy")
            if not reliable_schema:
                # Arm only today's latest retryable results, once during migration.
                from crawler.lifecycle import now_shanghai, next_retry_time
                now = now_shanghai()
                rows = connection.execute("""
                    SELECT run_id,status,snapshot_date FROM collection_runs r
                    WHERE snapshot_date=? AND r.rowid=(
                        SELECT n.rowid FROM collection_runs n
                        WHERE n.source_url=r.source_url AND n.snapshot_date=r.snapshot_date
                        ORDER BY n.started_at DESC,n.rowid DESC LIMIT 1)
                """, (now.date().isoformat(),)).fetchall()
                for run_id, status, snapshot_date in rows:
                    connection.execute("UPDATE collection_runs SET next_retry_at=? WHERE run_id=?",
                                       (next_retry_time(status, snapshot_date, 0, now), run_id))
        else:
            connection.execute(COLLECTION_RUNS_TABLE_SQL)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
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
