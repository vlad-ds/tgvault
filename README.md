# tgvault

**Your Telegram messages, in a local database your AI can work with.**

tgvault logs into *your* Telegram account (the same way Telegram Desktop does),
downloads messages from chats you explicitly choose, and stores them in a
SQLite database on your computer. Any AI assistant (Claude, ChatGPT with a
terminal, etc.) can then be pointed at this repo's [skill](skill/SKILL.md) to
read, search, and summarize your messages — and to *draft* replies that only
you can approve and send.

## Why you can trust it

1. **Open source.** Every line is here. Point your own AI at this repo and ask
   it to audit the code before you use it.
2. **Local-first.** Messages are stored only in `~/.tgvault/` on your machine.
   There is no server, no cloud, no telemetry, no analytics. The only network
   connection this tool ever makes is to Telegram itself.
3. **Allowlist-only ingestion.** Nothing is downloaded unless you explicitly
   `watch` a chat.
4. **Human-in-the-loop sending.** AI agents can only create *drafts*. Sending
   requires you, in a terminal, reading the message and typing `SEND`.
   The send command refuses to run non-interactively.
5. **Hardened storage.** The database and Telegram session file are created
   owner-readable only (on systems that support it).

## Install

**Mac / Linux** — paste into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/vlad-ds/tgvault/main/install.sh | bash
```

**Windows** — paste into PowerShell:

```powershell
irm https://raw.githubusercontent.com/vlad-ds/tgvault/main/install.ps1 | iex
```

No Git, no Python setup needed — the installer takes care of everything.

## Quick start

```bash
tgvault login          # shows a QR code — scan it with the Telegram app
                       #   (phone: Telegram → Settings → Devices → Link Desktop Device)
tgvault chats          # list your chats
tgvault watch "Family" @somefriend   # choose what to archive
tgvault sync           # download new messages (run any time; only fetches new ones)
tgvault read "Family"  # read from the local archive
tgvault search "dinner plans"        # full-text search across everything
tgvault status         # health check
```

Run `tgvault sync` whenever you want fresh data (or schedule it — see below).

## Using it with an AI assistant

Tell your assistant (e.g. Claude Code):

> Read the skill at https://github.com/vlad-ds/tgvault/blob/main/skill/SKILL.md
> and use tgvault to summarize what happened in my chats this week.

The skill teaches the AI:
- how to read and search the vault (CLI with `--json`, or direct read-only SQL)
- how to track which messages it has already handled (`processed` table),
  so recurring jobs (daily digests etc.) never re-process old messages
- that it may **draft** outgoing messages but never send them — you review
  drafts with `tgvault outbox list` and send with `tgvault outbox send <id>`

## Sending messages (human-in-the-loop)

```bash
tgvault outbox list            # see drafts your AI wrote
tgvault outbox send 3          # shows recipient + text, asks you to type SEND
tgvault outbox reject 3        # veto a draft
```

## Telegram API credentials

Telegram clients identify themselves with an `api_id`/`api_hash` pair. tgvault
ships with the publicly published Telegram Desktop pair, so **you don't need to
register anything** — your login is still your own private session. If you
prefer your own credentials (Telegram's ToS technically asks each developer to
register their own at [my.telegram.org](https://my.telegram.org), though that
page often refuses new apps with an unexplained ERROR), you can use them:

```bash
tgvault login --api-id 12345 --api-hash abcdef...
```

or set `TGVAULT_API_ID` / `TGVAULT_API_HASH`.

## Data layout

Everything lives in `~/.tgvault/` (override with `TGVAULT_HOME`):

| File | Contents |
|---|---|
| `vault.sqlite3` | chats, messages, processed-tracking, outbox |
| `telegram.session` | your Telegram login session — **treat like a password** |
| `config.json` | optional custom API credentials |

To wipe everything: `tgvault logout` then delete the `~/.tgvault` folder.

## Development

```bash
git clone https://github.com/vlad-ds/tgvault && cd tgvault
uv sync
uv run pytest
uv run tgvault --help
```

## Security notes

- The Telegram session file grants full account access. tgvault restricts its
  permissions, but you should still treat `~/.tgvault` as sensitive.
- tgvault never uploads your data anywhere. Verify: `grep -rn "http" src/` —
  the only endpoints are Telegram's own MTProto servers via Telethon.
- Agents interacting with the vault get write access only to bookkeeping
  tables (`processed`, `outbox` drafts). The message archive is theirs to
  read, not modify.

## License

MIT
