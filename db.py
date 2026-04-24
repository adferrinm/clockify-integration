"""
db.py — SQLite persistence layer
=================================
Two tables:

  sync_state  key/value store for runtime state (e.g. last_sync_date).
  push_log    immutable audit log of every entry successfully pushed to Clockify.

The push_log is the primary duplicate-detection mechanism: before creating an
entry we check locally instead of making an extra Clockify API call.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path("clockify_sync.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date        TEXT    NOT NULL,
    project_name      TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    hours             REAL    NOT NULL,
    clockify_entry_id TEXT,
    pushed_at         TEXT    NOT NULL,
    UNIQUE (entry_date, project_name, description)
);
"""


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create tables if they don't exist yet."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


# ─── sync_state ──────────────────────────────────────────────────────────────

def get_last_sync_date(db_path: Path = DB_PATH) -> Optional[date]:
    """Return the last successfully synced date, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_sync_date'"
        ).fetchone()
    if row:
        try:
            return date.fromisoformat(row["value"])
        except ValueError:
            return None
    return None


def set_last_sync_date(d: date, db_path: Path = DB_PATH) -> None:
    """Persist the last successfully synced date."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO sync_state (key, value, updated_at)
               VALUES ('last_sync_date', ?, ?)
               ON CONFLICT(key) DO UPDATE
               SET value = excluded.value, updated_at = excluded.updated_at""",
            (str(d), datetime.now().isoformat()),
        )


# ─── push_log ────────────────────────────────────────────────────────────────

def was_pushed(
    entry_date: date,
    project_name: str,
    description: str,
    db_path: Path = DB_PATH,
) -> bool:
    """Return True if this exact entry was already pushed to Clockify."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM push_log
               WHERE entry_date = ? AND project_name = ? AND description = ?""",
            (str(entry_date), project_name, description),
        ).fetchone()
    return row is not None


def log_push(
    entry_date: date,
    project_name: str,
    description: str,
    hours: float,
    clockify_entry_id: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """Record a successfully pushed entry. Silently ignores duplicates."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO push_log
               (entry_date, project_name, description, hours, clockify_entry_id, pushed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(entry_date),
                project_name,
                description,
                hours,
                clockify_entry_id,
                datetime.now().isoformat(),
            ),
        )


def get_push_history(
    limit: int = 50,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Return the most recent push_log entries, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM push_log ORDER BY entry_date DESC, pushed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ─── Migration helper ─────────────────────────────────────────────────────────

def migrate_from_json(json_path: Path, db_path: Path = DB_PATH) -> None:
    """
    One-time migration: read last_sync_date from the old .sync_state.json
    and write it to SQLite if SQLite doesn't already have a value.
    """
    import json

    if not json_path.exists():
        return
    if get_last_sync_date(db_path) is not None:
        return   # SQLite already has a value, nothing to migrate

    try:
        with json_path.open(encoding="utf-8") as f:
            data = json.load(f)
        d = date.fromisoformat(data["last_sync_date"])
        set_last_sync_date(d, db_path)
    except (KeyError, ValueError, OSError):
        pass
