"""Incremental sync: pull new messages for every monitored chat."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from tgvault import db


@dataclass
class ChatSyncReport:
    chat_id: int
    title: str
    inserted: int
    skipped: int
    error: str | None = None


@dataclass
class SyncReport:
    run_id: int
    chats: list[ChatSyncReport] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(c.inserted for c in self.chats)

    @property
    def errors(self) -> list[ChatSyncReport]:
        return [c for c in self.chats if c.error]


async def sync_monitored(
    conn: sqlite3.Connection,
    gateway,
    *,
    limit_per_chat: int | None = None,
    initial_limit: int = 500,
) -> SyncReport:
    """For each monitored chat, fetch messages after its checkpoint.

    First sync of a chat is capped at `initial_limit` (newest messages) so a
    huge group doesn't take forever; later syncs pull everything new.
    """
    run_id = db.start_sync_run(conn)
    report = SyncReport(run_id=run_id)
    error: str | None = None
    try:
        for chat in db.monitored_chats(conn):
            chat = dict(chat)
            after = chat["last_message_id"]
            limit = limit_per_chat if after else (limit_per_chat or initial_limit)
            try:
                messages = await gateway.fetch_messages(
                    chat, after_message_id=after, limit=limit
                )
                result = db.ingest_messages(conn, chat["chat_id"], messages)
                report.chats.append(
                    ChatSyncReport(
                        chat_id=chat["chat_id"],
                        title=chat["title"],
                        inserted=result.inserted,
                        skipped=result.skipped,
                    )
                )
            except Exception as exc:  # keep syncing other chats
                report.chats.append(
                    ChatSyncReport(
                        chat_id=chat["chat_id"],
                        title=chat["title"],
                        inserted=0,
                        skipped=0,
                        error=str(exc),
                    )
                )
        if report.errors:
            error = "; ".join(f"{c.title}: {c.error}" for c in report.errors)
    finally:
        db.finish_sync_run(
            conn,
            run_id,
            chats_synced=len(report.chats),
            messages_ingested=report.total_inserted,
            error=error,
        )
    return report
