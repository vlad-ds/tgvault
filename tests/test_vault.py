import asyncio

import pytest

from tgvault import db
from tgvault.sync import sync_monitored


def make_msg(mid, text="hello", sender="Alice", sent_at=None):
    return {
        "message_id": mid,
        "sent_at": sent_at or f"2026-07-0{min(mid, 9)}T10:00:00+00:00",
        "sender_id": 111,
        "sender_name": sender,
        "is_outgoing": False,
        "text": text,
        "media_type": None,
        "reply_to_id": None,
    }


CHAT = {
    "chat_id": 1,
    "entity_type": "user",
    "kind": "user",
    "access_hash": 42,
    "title": "Alice",
    "username": "alice",
    "phone": None,
}


@pytest.fixture
def conn(tmp_path):
    return db.init_db(tmp_path / "vault.sqlite3")


def test_ingest_dedup_and_checkpoint(conn):
    db.upsert_chat(conn, CHAT)
    result = db.ingest_messages(conn, 1, [make_msg(1), make_msg(2), make_msg(3)])
    assert result.inserted == 3
    assert db.checkpoint(conn, 1) == 3

    # Re-ingesting the same plus one new message: only the new one lands.
    result = db.ingest_messages(conn, 1, [make_msg(2), make_msg(3), make_msg(4)])
    assert result.inserted == 1
    assert result.skipped == 2
    assert db.checkpoint(conn, 1) == 4
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_checkpoint_never_regresses(conn):
    db.upsert_chat(conn, CHAT)
    db.ingest_messages(conn, 1, [make_msg(10)])
    db.ingest_messages(conn, 1, [make_msg(5)])  # backfill of an older message
    assert db.checkpoint(conn, 1) == 10


def test_fts_search(conn):
    db.upsert_chat(conn, CHAT)
    db.ingest_messages(
        conn,
        1,
        [
            make_msg(1, "let's have dinner on Friday"),
            make_msg(2, "the weather is nice"),
        ],
    )
    hits = db.search(conn, "dinner")
    assert len(hits) == 1
    assert hits[0]["message_id"] == 1
    assert hits[0]["chat_title"] == "Alice"
    assert db.search(conn, "opera") == []


def test_fts_not_duplicated_on_reingest(conn):
    db.upsert_chat(conn, CHAT)
    db.ingest_messages(conn, 1, [make_msg(1, "unique dinner phrase")])
    db.ingest_messages(conn, 1, [make_msg(1, "unique dinner phrase")])
    assert len(db.search(conn, "dinner")) == 1


def test_processed_tracking(conn):
    db.upsert_chat(conn, CHAT)
    db.ingest_messages(conn, 1, [make_msg(i) for i in range(1, 6)])

    pending = db.unprocessed(conn, "digest")
    assert len(pending) == 5

    db.mark_processed(conn, "digest", [(1, 1), (1, 2), (1, 3)])
    pending = db.unprocessed(conn, "digest")
    assert [m["message_id"] for m in pending] == [4, 5]

    # Idempotent, and namespaces are independent.
    assert db.mark_processed(conn, "digest", [(1, 3)]) == 0
    assert len(db.unprocessed(conn, "other-workflow")) == 5


def test_mark_all_unprocessed(conn):
    db.upsert_chat(conn, CHAT)
    chat2 = dict(CHAT, chat_id=2, title="Bob", username="bob")
    db.upsert_chat(conn, chat2)
    db.ingest_messages(conn, 1, [make_msg(i) for i in range(1, 4)])
    db.ingest_messages(conn, 2, [make_msg(1), make_msg(2)])

    db.mark_processed(conn, "agent", [(1, 1)])
    # Scoped to chat 1: marks its remaining two, leaves chat 2 alone.
    assert db.mark_all_unprocessed(conn, "agent", chat_id=1) == 2
    assert len(db.unprocessed(conn, "agent")) == 2
    # Unscoped: catches the rest; second run is a no-op.
    assert db.mark_all_unprocessed(conn, "agent") == 2
    assert db.mark_all_unprocessed(conn, "agent") == 0
    assert db.unprocessed(conn, "agent") == []


def test_outbox_flow(conn):
    db.upsert_chat(conn, CHAT)
    draft_id = db.create_draft(conn, 1, "Sounds good, see you then!", "agent")
    row = conn.execute("SELECT * FROM outbox WHERE id=?", (draft_id,)).fetchone()
    assert row["status"] == "draft"

    assert db.update_draft_status(conn, draft_id, "sent", sent_message_id=99)
    row = conn.execute("SELECT * FROM outbox WHERE id=?", (draft_id,)).fetchone()
    assert row["status"] == "sent"
    assert row["sent_message_id"] == 99

    # A sent draft can't change status again.
    assert not db.update_draft_status(conn, draft_id, "rejected")


class FakeGateway:
    """Simulates Telegram: has a fixed message history per chat."""

    def __init__(self, history):
        self.history = history
        self.calls = []

    async def fetch_messages(self, chat, after_message_id, limit):
        self.calls.append((chat["chat_id"], after_message_id, limit))
        msgs = self.history.get(chat["chat_id"], [])
        if after_message_id:
            msgs = [m for m in msgs if m["message_id"] > after_message_id]
        else:
            msgs = sorted(msgs, key=lambda m: m["message_id"], reverse=True)
            if limit:
                msgs = msgs[:limit]
            msgs = sorted(msgs, key=lambda m: m["message_id"])
        return msgs


def test_incremental_sync(conn):
    db.upsert_chat(conn, CHAT)
    db.set_monitored(conn, 1, True)

    gateway = FakeGateway({1: [make_msg(i) for i in range(1, 8)]})

    # First sync: no checkpoint, initial_limit caps to newest 5.
    report = asyncio.run(
        sync_monitored(conn, gateway, initial_limit=5)
    )
    assert report.total_inserted == 5
    assert db.checkpoint(conn, 1) == 7
    assert gateway.calls[0] == (1, None, 5)

    # New messages arrive; second sync fetches only past the checkpoint.
    gateway.history[1].extend([make_msg(8), make_msg(9)])
    report = asyncio.run(sync_monitored(conn, gateway))
    assert report.total_inserted == 2
    assert gateway.calls[1][1] == 7
    assert db.checkpoint(conn, 1) == 9

    # Third sync: nothing new.
    report = asyncio.run(sync_monitored(conn, gateway))
    assert report.total_inserted == 0

    runs = conn.execute("SELECT status FROM sync_runs").fetchall()
    assert [r["status"] for r in runs] == ["ok", "ok", "ok"]


def test_sync_survives_per_chat_errors(conn):
    db.upsert_chat(conn, CHAT)
    chat2 = dict(CHAT, chat_id=2, title="Bob", username="bob")
    db.upsert_chat(conn, chat2)
    db.set_monitored(conn, 1, True)
    db.set_monitored(conn, 2, True)

    class FlakyGateway(FakeGateway):
        async def fetch_messages(self, chat, after_message_id, limit):
            if chat["chat_id"] == 1:
                raise RuntimeError("boom")
            return await super().fetch_messages(chat, after_message_id, limit)

    gateway = FlakyGateway({2: [make_msg(1, "hi from bob")]})
    report = asyncio.run(sync_monitored(conn, gateway))
    assert report.total_inserted == 1
    assert len(report.errors) == 1
    assert report.errors[0].chat_id == 1
    run = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert run["status"] == "error"
    assert "boom" in run["error"]


def test_unmonitored_chats_never_synced(conn):
    db.upsert_chat(conn, CHAT)  # known but NOT watched
    gateway = FakeGateway({1: [make_msg(1)]})
    report = asyncio.run(sync_monitored(conn, gateway))
    assert report.chats == []
    assert gateway.calls == []
