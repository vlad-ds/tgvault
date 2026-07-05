"""SQLite vault: schema, ingestion, processed-tracking, outbox.

Design rules:
- (chat_id, message_id) is the primary key -> ingestion is idempotent, dedup is free.
- Sync is incremental via per-chat last_message_id checkpoints.
- AI agents get read access to everything, write access ONLY to `processed`
  and to drafting rows in `outbox`. Sending is a human action.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tgvault.paths import restrict_file

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,          -- user | chat | channel
    kind TEXT NOT NULL,                 -- user | group | channel
    access_hash INTEGER,
    title TEXT NOT NULL,
    username TEXT,
    phone TEXT,
    monitored INTEGER NOT NULL DEFAULT 0,
    last_message_id INTEGER,            -- sync checkpoint
    first_synced_at TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,              -- ISO-8601 UTC
    sender_id INTEGER,
    sender_name TEXT,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    media_type TEXT,
    reply_to_id INTEGER,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages (sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages (sender_id);

CREATE TABLE IF NOT EXISTS processed (
    namespace TEXT NOT NULL,            -- one per AI workflow, e.g. "daily-digest"
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (namespace, chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    reply_to_id INTEGER,
    created_by TEXT,                    -- who drafted it (agent name, "human", ...)
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | sent | rejected
    status_changed_at TEXT,
    sent_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    chats_synced INTEGER NOT NULL DEFAULT 0,
    messages_ingested INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    error TEXT
);

-- Standalone FTS table kept in sync by ingestion. (An external-content FTS
-- table needs a rowid, which the WITHOUT ROWID messages table doesn't have.)
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    chat_id UNINDEXED,
    message_id UNINDEXED
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class IngestResult:
    inserted: int
    skipped: int
    last_message_id: int | None


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    restrict_file(path)
    return conn


def upsert_chat(conn: sqlite3.Connection, chat: dict) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO chats (chat_id, entity_type, kind, access_hash, title, username, phone)
            VALUES (:chat_id, :entity_type, :kind, :access_hash, :title, :username, :phone)
            ON CONFLICT(chat_id) DO UPDATE SET
                entity_type=excluded.entity_type,
                kind=excluded.kind,
                access_hash=excluded.access_hash,
                title=excluded.title,
                username=excluded.username,
                phone=excluded.phone
            """,
            chat,
        )


def set_monitored(conn: sqlite3.Connection, chat_id: int, monitored: bool) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE chats SET monitored=? WHERE chat_id=?", (int(monitored), chat_id)
        )
    return cur.rowcount > 0


def monitored_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chats WHERE monitored=1 ORDER BY title"
    ).fetchall()


def ingest_messages(
    conn: sqlite3.Connection, chat_id: int, messages: list[dict]
) -> IngestResult:
    """Insert messages idempotently and advance the chat checkpoint."""
    inserted = 0
    skipped = 0
    max_id: int | None = None
    ingested_at = now_iso()
    with conn:
        for msg in messages:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, message_id, sent_at, sender_id, sender_name,
                     is_outgoing, text, media_type, reply_to_id, ingested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    chat_id,
                    msg["message_id"],
                    msg["sent_at"],
                    msg.get("sender_id"),
                    msg.get("sender_name"),
                    int(msg.get("is_outgoing") or 0),
                    msg.get("text") or "",
                    msg.get("media_type"),
                    msg.get("reply_to_id"),
                    ingested_at,
                ),
            )
            if cur.rowcount:
                inserted += 1
                if msg.get("text"):
                    conn.execute(
                        "INSERT INTO messages_fts (text, chat_id, message_id) VALUES (?,?,?)",
                        (msg["text"], chat_id, msg["message_id"]),
                    )
            else:
                skipped += 1
            if max_id is None or msg["message_id"] > max_id:
                max_id = msg["message_id"]

        if max_id is not None:
            conn.execute(
                """
                UPDATE chats SET
                    last_message_id = MAX(COALESCE(last_message_id, 0), ?),
                    first_synced_at = COALESCE(first_synced_at, ?),
                    last_synced_at = ?
                WHERE chat_id = ?
                """,
                (max_id, ingested_at, ingested_at, chat_id),
            )
        else:
            conn.execute(
                "UPDATE chats SET last_synced_at=? WHERE chat_id=?",
                (ingested_at, chat_id),
            )
    return IngestResult(inserted=inserted, skipped=skipped, last_message_id=max_id)


def checkpoint(conn: sqlite3.Connection, chat_id: int) -> int | None:
    row = conn.execute(
        "SELECT last_message_id FROM chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    return row["last_message_id"] if row else None


def search(
    conn: sqlite3.Connection, query: str, limit: int = 20
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.chat_id, c.title AS chat_title, m.message_id, m.sent_at,
               m.sender_name, m.text
        FROM messages_fts f
        JOIN messages m ON m.chat_id = f.chat_id AND m.message_id = f.message_id
        JOIN chats c ON c.chat_id = m.chat_id
        WHERE messages_fts MATCH ?
        ORDER BY m.sent_at DESC
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()


def mark_processed(
    conn: sqlite3.Connection,
    namespace: str,
    items: list[tuple[int, int]],
    note: str | None = None,
) -> int:
    ts = now_iso()
    with conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO processed (namespace, chat_id, message_id, processed_at, note)
            VALUES (?,?,?,?,?)
            """,
            [(namespace, c, m, ts, note) for c, m in items],
        )
    return cur.rowcount


def unprocessed(
    conn: sqlite3.Connection, namespace: str, chat_id: int | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    sql = """
        SELECT m.* FROM messages m
        LEFT JOIN processed p
            ON p.namespace = ? AND p.chat_id = m.chat_id AND p.message_id = m.message_id
        WHERE p.message_id IS NULL
    """
    params: list = [namespace]
    if chat_id is not None:
        sql += " AND m.chat_id = ?"
        params.append(chat_id)
    sql += " ORDER BY m.sent_at ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def create_draft(
    conn: sqlite3.Connection,
    chat_id: int,
    text: str,
    created_by: str,
    reply_to_id: int | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO outbox (chat_id, text, reply_to_id, created_by, created_at)
            VALUES (?,?,?,?,?)
            """,
            (chat_id, text, reply_to_id, created_by, now_iso()),
        )
    return cur.lastrowid


def update_draft_status(
    conn: sqlite3.Connection,
    draft_id: int,
    status: str,
    sent_message_id: int | None = None,
) -> bool:
    with conn:
        cur = conn.execute(
            """
            UPDATE outbox SET status=?, status_changed_at=?, sent_message_id=?
            WHERE id=? AND status='draft'
            """,
            (status, now_iso(), sent_message_id, draft_id),
        )
    return cur.rowcount > 0


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "chats_known": one("SELECT COUNT(*) FROM chats"),
        "chats_monitored": one("SELECT COUNT(*) FROM chats WHERE monitored=1"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "processed_marks": one("SELECT COUNT(*) FROM processed"),
        "outbox_drafts": one("SELECT COUNT(*) FROM outbox WHERE status='draft'"),
        "last_sync": conn.execute(
            "SELECT MAX(finished_at) FROM sync_runs WHERE status='ok'"
        ).fetchone()[0],
    }


def start_sync_run(conn: sqlite3.Connection) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO sync_runs (started_at) VALUES (?)", (now_iso(),)
        )
    return cur.lastrowid


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    chats_synced: int,
    messages_ingested: int,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE sync_runs
            SET finished_at=?, chats_synced=?, messages_ingested=?, status=?, error=?
            WHERE id=?
            """,
            (
                now_iso(),
                chats_synced,
                messages_ingested,
                "error" if error else "ok",
                error,
                run_id,
            ),
        )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def dump_json(rows: list[sqlite3.Row]) -> str:
    return json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2)
