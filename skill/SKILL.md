---
name: tgvault
description: Read, search, and summarize the user's local Telegram message vault; track processed messages; draft (never send) replies. Use when the user asks about their Telegram messages, chats, digests, or wants a reply drafted.
---

# tgvault — Telegram vault skill

tgvault stores the user's Telegram messages in a local SQLite database at
`~/.tgvault/vault.sqlite3` (or `$TGVAULT_HOME/vault.sqlite3`). You interact
with it through the `tgvault` CLI (preferred) or read-only SQL.

## Hard rules

1. **You must NEVER send a message.** You may create drafts with
   `tgvault outbox draft`. Only the human can send them (`tgvault outbox send`
   is interactive-only and will refuse to run for you — do not try).
2. **Never modify the `messages` or `chats` tables.** Your only writes are:
   marking messages processed, and creating outbox drafts. Use the CLI for
   both rather than raw SQL.
3. **This is private data.** Never copy message contents into anything that
   leaves the machine (web requests, commits, issue trackers) unless the user
   explicitly asks.
4. Do not read `~/.tgvault/telegram.session` or `config.json`. You never need
   them.

## Getting fresh data

```bash
tgvault status --json          # is the vault set up? how many messages?
tgvault sync --json            # pull new messages for all watched chats
```

`sync` is incremental and deduplicated — safe to run any time. If `status`
shows `logged_in: false`, stop and ask the user to run `tgvault login`
themselves in a terminal.

## Reading

```bash
tgvault chats --no-refresh --json                 # chat list from local DB
tgvault read "Family" --limit 100 --json          # one chat, newest last
tgvault read @alice --since 2026-07-01 --json
tgvault search "flight booking" --json            # FTS5 across all chats
```

Chat refs can be a chat_id, @username, or title (substring works if unique).

For anything more complex, query SQLite directly — read-only:

```bash
sqlite3 -json ~/.tgvault/vault.sqlite3 \
  "SELECT c.title, m.sent_at, m.sender_name, m.text
   FROM messages m JOIN chats c USING (chat_id)
   WHERE m.sent_at >= date('now', '-7 days')
   ORDER BY m.sent_at"
```

Schema: `chats(chat_id, kind, title, username, monitored, ...)`,
`messages(chat_id, message_id, sent_at, sender_id, sender_name, is_outgoing,
text, media_type, reply_to_id)`, `messages_fts(text, chat_id, message_id)`
(FTS5), `processed(namespace, chat_id, message_id, processed_at, note)`,
`outbox(id, chat_id, text, status, ...)`.

`is_outgoing = 1` means the user sent it; `sent_at` is ISO-8601 UTC.

## Your read registry: processed tracking

You have your own registry of which messages you have already read and
handled. It is completely independent of Telegram's read receipts — tgvault
never touches those, and a message being "read on Telegram" tells you nothing
about whether *you* processed it. Always use this registry via the CLI (not
raw SQL) so marking stays idempotent.

The core loop:

```bash
tgvault processed pending --limit 200        # messages you haven't read yet (JSON)
# ... read them, do your work ...
tgvault processed mark --all                 # "I'm caught up on everything"
# or, selectively:
tgvault processed mark --chat 12345 --ids 101,102,103 --note "summarized"
tgvault processed mark --all --chat "Family" # caught up on one chat only
```

`pending` returns stored messages you haven't marked, oldest first. Marking
is idempotent — re-marking is harmless.

The default registry namespace is `agent`. If you run several distinct
recurring workflows (say a daily digest and a todo extractor), give each its
own `--namespace daily-digest` etc. on both `pending` and `mark`, so they
track independently.

## Drafting replies (human sends, not you)

```bash
tgvault outbox draft @alice "Sounds good, see you at 7!" --created-by claude
tgvault outbox draft "Family" "..." --reply-to 4711 --created-by claude
```

Then tell the user: "I drafted a reply — review it with `tgvault outbox list`
and send with `tgvault outbox send <id>`." Never imply the message was sent.

## Typical tasks

- **"What did I miss today?"** — `tgvault sync`, then read each watched chat
  `--since` today, summarize per chat, most important first.
- **Daily digest job** — `sync`, then `processed pending --namespace daily-digest`,
  summarize, `processed mark`, present the digest.
- **"Find that message about X"** — `tgvault search "X" --json`; if FTS misses,
  fall back to SQL `LIKE` over `messages.text`.
- **"Reply to Alice that ..."** — draft it, show the user the draft id, stop.
