# Security

tgvault's security model, in short:

- **Local only.** Your messages live in `~/.tgvault/` on your machine. tgvault
  makes no network connections except to Telegram's own servers (via
  [Telethon](https://github.com/LonamiWebs/Telethon)). No telemetry, no cloud.
- **The session file is the crown jewel.** `~/.tgvault/telegram.session`
  grants full access to your Telegram account. tgvault sets owner-only file
  permissions where the OS supports it. Never share this file or commit it.
- **Allowlist ingestion.** Only chats you explicitly `tgvault watch` are read.
- **Human-in-the-loop sending.** `tgvault outbox send` requires an interactive
  terminal and typed confirmation; AI agents are limited to creating drafts.

## Reporting a vulnerability

Please email **vlad.proex@gmail.com** with details. You can also open a
GitHub issue for anything that isn't sensitive. You'll get a response within
a few days.

## Auditing

The codebase is deliberately small (~1,500 lines of Python). To verify the
"local only" claim, start with `src/tgvault/telegram.py` — the only module
that touches the network — and `src/tgvault/db.py` for what gets stored.
Pointing an AI code assistant at the repo and asking for a security audit is
encouraged.
