from __future__ import annotations

import sqlite3
from pathlib import Path

from database.schema import initialize_schema


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "new_releases.db"


def connect_database(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection
