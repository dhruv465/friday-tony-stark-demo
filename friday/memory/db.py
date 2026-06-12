"""
Shared SQLite store for memory intelligence — FTS5 note index, recall
ledger, and temporal facts. The markdown vault stays the source of truth
for note content; this DB is the index/ledger over it.

Lives at <FRIDAY_KNOWLEDGE_DIR>/_memory/memory.db (same env override
pattern as the trust state and agents store, so tests can point it at a
tempdir).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from friday.config import config

logger = logging.getLogger("friday.memory.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_meta (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    path UNINDEXED,
    title,
    body
);

CREATE TABLE IF NOT EXISTS facts (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    date_start    TEXT NOT NULL,
    date_end      TEXT NOT NULL,
    importance    REAL DEFAULT 0.5,
    status        TEXT DEFAULT 'active',
    created_at    TEXT NOT NULL,
    reminder_sent TEXT,
    alerted_1h    TEXT,
    alerted_15m   TEXT,
    alerted_now   TEXT
);

CREATE TABLE IF NOT EXISTS recall_log (
    key               TEXT PRIMARY KEY,
    snippet           TEXT,
    recall_count      INTEGER DEFAULT 0,
    daily_count       INTEGER DEFAULT 0,
    total_score       REAL DEFAULT 0,
    max_score         REAL DEFAULT 0,
    first_recalled_at TEXT,
    last_recalled_at  TEXT,
    query_hashes      TEXT DEFAULT '[]',
    recall_days       TEXT DEFAULT '[]',
    concept_tags      TEXT DEFAULT '[]',
    promoted_at       TEXT
);
"""


def _knowledge_dir() -> Path:
    return Path(
        os.getenv("FRIDAY_KNOWLEDGE_DIR", config.FRIDAY_KNOWLEDGE_DIR)
    ).expanduser()


def db_path() -> Path:
    folder = _knowledge_dir() / "_memory"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "memory.db"


def connect() -> sqlite3.Connection:
    """Open a connection with WAL + schema ensured. Caller closes it."""
    conn = sqlite3.connect(db_path(), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            logger.warning(
                "WAL mode unavailable (got %r) — concurrent access may be slow", mode
            )
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
    except Exception:
        conn.close()
        raise
    return conn
