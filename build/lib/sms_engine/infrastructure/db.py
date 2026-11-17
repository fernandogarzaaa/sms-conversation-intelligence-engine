"""SQLite database: schema, migrations, and a thread-safe transactional connection.

Small scale (~10 clients, hundreds of messages/week) — a single SQLite file is
the right tool. Everything is append-mostly; conversation state is never stored,
it is always rebuilt from `events` + `messages`.

Concurrency model
-----------------
FastAPI runs sync handlers in a threadpool, and the optional background worker
runs in its own thread, so one connection is shared across threads. Two rules
keep that safe:

1. Every statement runs under a process-wide re-entrant lock and its rows are
   materialised before the lock is released (`SafeConnection.execute`).
2. Multi-statement units of work (one inbound message, one review decision, one
   send-worker pass) run inside `Database.transaction()`, which holds the lock
   for the whole unit and commits once at the end — or rolls everything back on
   error, so a crash half-way through never leaves a partial decision behind.

Repositories call `conn.commit()` after each write; inside a transaction that
call is a no-op, outside one it commits immediately (the old behaviour).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS clients (
    client_id   TEXT PRIMARY KEY,
    json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    contact_id  TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    phone       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    direction       TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    variant_id      TEXT,
    human_edited    INTEGER NOT NULL DEFAULT 0,
    meta_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    contact_id      TEXT NOT NULL,
    type            TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    caused_by       TEXT,
    UNIQUE(conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_events_conv ON events(conversation_id, seq);

CREATE TABLE IF NOT EXISTS planned_responses (
    id                    TEXT PRIMARY KEY,
    conversation_id       TEXT NOT NULL,
    contact_id            TEXT NOT NULL,
    in_reply_to           TEXT NOT NULL,
    built_at_version      INTEGER NOT NULL,
    text                  TEXT NOT NULL,
    variant_id            TEXT,
    validation_json       TEXT NOT NULL DEFAULT '{}',
    send_after            TEXT,
    created_at            TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'PLANNED',
    model_used            TEXT NOT NULL DEFAULT 'unknown',
    generation_latency_ms REAL NOT NULL DEFAULT 0,
    actions_json          TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_planned_conv ON planned_responses(conversation_id, status);

CREATE TABLE IF NOT EXISTS reviews (
    id                       TEXT PRIMARY KEY,
    conversation_id          TEXT NOT NULL,
    message_id               TEXT NOT NULL,
    planned_response_id      TEXT,
    created_at               TEXT NOT NULL,
    decided_at               TEXT,
    status                   TEXT NOT NULL DEFAULT 'OPEN',
    action                   TEXT,
    recommended_action       TEXT,
    risk_level               TEXT,
    model_classification_json TEXT NOT NULL DEFAULT '{}',
    model_confidence         REAL,
    generated_response       TEXT,
    human_edits              TEXT,
    final_response           TEXT,
    reviewer                 TEXT,
    notes                    TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_status ON reviews(status, created_at);

CREATE TABLE IF NOT EXISTS traces (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    json            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_traces_conv ON traces(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS followups (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    contact_id      TEXT NOT NULL,
    due_at          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'SCHEDULED',
    created_at      TEXT NOT NULL
);
"""

# Kept for backwards compatibility with code that imported `SCHEMA`.
SCHEMA = SCHEMA_V1

# Ordered, append-only list of migrations. Index i upgrades user_version i+1 -> i+2.
# Never edit a released migration; add a new one.
MIGRATIONS: list[str] = [
    # v2: idempotency, neighborhoods, send retries, review kinds, follow-up linkage
    """
    ALTER TABLE messages ADD COLUMN provider_message_id TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_provider_id
        ON messages(provider_message_id) WHERE provider_message_id IS NOT NULL;
    ALTER TABLE conversations ADD COLUMN neighborhood TEXT;
    CREATE INDEX IF NOT EXISTS ix_conversations_contact ON conversations(client_id, contact_id);
    CREATE INDEX IF NOT EXISTS ix_conversations_phone ON conversations(phone);
    ALTER TABLE planned_responses ADD COLUMN kind TEXT NOT NULL DEFAULT 'REPLY';
    ALTER TABLE planned_responses ADD COLUMN send_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE planned_responses ADD COLUMN last_error TEXT;
    CREATE INDEX IF NOT EXISTS ix_planned_due ON planned_responses(status, send_after);
    ALTER TABLE reviews ADD COLUMN kind TEXT NOT NULL DEFAULT 'REPLY';
    CREATE INDEX IF NOT EXISTS ix_reviews_conv ON reviews(conversation_id, status);
    ALTER TABLE followups ADD COLUMN review_id TEXT;
    ALTER TABLE followups ADD COLUMN updated_at TEXT;
    CREATE INDEX IF NOT EXISTS ix_followups_due ON followups(status, due_at);
    """,
]

SCHEMA_VERSION = 1 + len(MIGRATIONS)


class _Result:
    """Materialised result of one statement (so the lock can be released)."""

    __slots__ = ("_rows", "_i", "rowcount", "lastrowid")

    def __init__(self, rows: list[sqlite3.Row], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> sqlite3.Row | None:
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchall(self) -> list[sqlite3.Row]:
        rest = self._rows[self._i :]
        self._i = len(self._rows)
        return rest

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self.fetchall())


class SafeConnection:
    """A thread-safe facade over one `sqlite3.Connection` with nested transactions."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        self._lock = threading.RLock()
        self._depth = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> _Result:
        with self._lock:
            cur = self._raw.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._raw.executescript(script)

    def commit(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._raw.commit()

    def rollback(self) -> None:
        with self._lock:
            self._raw.rollback()

    @contextmanager
    def transaction(self) -> Iterator[SafeConnection]:
        """Run a unit of work atomically. Nests: only the outermost level commits."""
        with self._lock:
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._raw.rollback()
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._raw.commit()

    def close(self) -> None:
        with self._lock:
            self._raw.close()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def schema_version(conn: sqlite3.Connection | SafeConnection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(conn: sqlite3.Connection | SafeConnection) -> int:
    """Create the base schema, then apply pending migrations. Returns the version."""
    conn.executescript(SCHEMA_V1)
    version = schema_version(conn)
    if version == 0:
        version = 1
    for i, script in enumerate(MIGRATIONS, start=2):
        if version >= i:
            continue
        for stmt in _split(script):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:  # pragma: no cover - re-run safety
                if "duplicate column name" not in str(exc):
                    raise
        version = i
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return version


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


class Database:
    """Owns one connection. Repositories take `db.conn`."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = SafeConnection(connect(self.path))
        self.version = init_db(self.conn)

    @contextmanager
    def transaction(self) -> Iterator[SafeConnection]:
        with self.conn.transaction() as c:
            yield c

    def ping(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.conn.close()
