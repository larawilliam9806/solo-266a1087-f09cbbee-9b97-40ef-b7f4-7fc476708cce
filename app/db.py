"""SQLite persistence layer.

Each request gets its own connection.  Atomic read-modify-write cycles use
``BEGIN IMMEDIATE`` so concurrent editors are serialised by SQLite itself, and
the ``UNIQUE`` constraints on ``bulletins.quarter`` / ``detail_versions`` are
the final arbiter when two publishes race.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS quarters(
    quarter           TEXT PRIMARY KEY,
    district_labels   TEXT NOT NULL,
    industry_labels   TEXT NOT NULL,
    row_totals        TEXT NOT NULL,
    col_totals        TEXT NOT NULL,
    registered_at     TEXT NOT NULL,
    registered_by     INTEGER
);

CREATE TABLE IF NOT EXISTS detail_versions(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter       TEXT    NOT NULL REFERENCES quarters(quarter),
    version       INTEGER NOT NULL,
    values_json   TEXT    NOT NULL,
    must_public   TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    created_by    INTEGER,
    UNIQUE(quarter, version)
);

CREATE TABLE IF NOT EXISTS mask_plans(
    quarter        TEXT    NOT NULL REFERENCES quarters(quarter),
    detail_version INTEGER NOT NULL,
    mask_version   INTEGER NOT NULL,
    cells_json     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    updated_by     INTEGER,
    PRIMARY KEY(quarter, detail_version)
);

CREATE TABLE IF NOT EXISTS bulletins(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter          TEXT    NOT NULL UNIQUE REFERENCES quarters(quarter),
    detail_version   INTEGER NOT NULL,
    mask_version     INTEGER,
    payload_json     TEXT    NOT NULL,
    published_at     TEXT    NOT NULL,
    published_by     INTEGER
);

CREATE TABLE IF NOT EXISTS editor_tokens(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL,
    token_hash  TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL,
    revoked_at  TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def resolve_db_path(path: str) -> str:
    """Anchor relative paths at the project root, not the process cwd."""
    if path == ":memory:" or path.startswith("file:"):
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(Path(__file__).resolve().parent.parent / p)


def db_path_from_env() -> str:
    return resolve_db_path(os.environ.get("BULLETIN_DB", "./data/bulletins.sqlite3"))
