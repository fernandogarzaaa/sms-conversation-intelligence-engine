"""SQLite database: schema + connection helper.

Small scale (~10 clients, hundreds of messages/week) — a single SQLite file is
the right tool. Everything is append-mostly; conversation state is never stored,
it is always rebuilt from `events` + `messages`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
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


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync handlers in a threadpool. This is a
    # low-volume prototype; SQLite serialises writes internally and we keep handlers
    # short. For higher throughput, move to a per-request connection or a pool.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


class Database:
    """Owns one connection. Repositories take this."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = connect(self.path)
        init_db(self.conn)

    def close(self) -> None:
        self.conn.close()
