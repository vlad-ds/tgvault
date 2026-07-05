"""tgvault CLI — the surface both humans and AI agents use.

Human commands: login, chats, watch, sync, send (interactive approval).
Agent commands: everything with --json, plus draft/processed bookkeeping.
Agents must never call `tgvault outbox send` — it is interactive by design.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys

import typer
from rich.console import Console
from rich.table import Table

from tgvault import db as dbmod
from tgvault import sync as syncmod
from tgvault.config import load_credentials, save_credentials
from tgvault.paths import data_dir, db_path, ensure_data_dir, session_path
from tgvault.telegram import Gateway, GatewayError

app = typer.Typer(help="Your Telegram messages in a local, AI-readable vault.")
outbox_app = typer.Typer(help="Draft → human review → send.")
processed_app = typer.Typer(help="Track which messages an AI workflow has handled.")
app.add_typer(outbox_app, name="outbox")
app.add_typer(processed_app, name="processed")

console = Console()
err_console = Console(stderr=True)


def _conn() -> sqlite3.Connection:
    ensure_data_dir()
    return dbmod.init_db(db_path())


def _gateway() -> Gateway:
    creds = load_credentials()
    ensure_data_dir()
    return Gateway(session_path(), creds.api_id, creds.api_hash)


def _run(coro):
    try:
        return asyncio.run(coro)
    except GatewayError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


def _print_json(data) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def _resolve_chat(conn: sqlite3.Connection, ref: str) -> dict:
    """Resolve a chat ref (id, @username, or title substring) against local DB."""
    rows = conn.execute("SELECT * FROM chats").fetchall()
    lowered = ref.strip().lstrip("@").lower()
    exact, fuzzy = [], []
    for row in rows:
        values = {str(row["chat_id"]).lower(), (row["username"] or "").lower(),
                  row["title"].lower()}
        if lowered in values:
            exact.append(row)
        elif lowered in row["title"].lower():
            fuzzy.append(row)
    matches = exact or fuzzy
    if len(matches) == 1:
        return dict(matches[0])
    if not matches:
        err_console.print(
            f"[red]No chat matches '{ref}'.[/red] Run `tgvault chats --refresh` first."
        )
        raise typer.Exit(1)
    err_console.print(f"[red]Ambiguous ref '{ref}':[/red]")
    for m in matches[:10]:
        err_console.print(f"  {m['chat_id']}  {m['title']}")
    raise typer.Exit(1)


# ---------------------------------------------------------------- status/init


@app.command()
def status(as_json: bool = typer.Option(False, "--json")):
    """Show vault health: paths, login state, message counts."""
    conn = _conn()
    gateway = _gateway()
    authorized = _run(gateway.is_authorized())
    info = {
        "data_dir": str(data_dir()),
        "database": str(db_path()),
        "logged_in": authorized,
        **dbmod.stats(conn),
    }
    if as_json:
        _print_json(info)
        return
    for key, value in info.items():
        console.print(f"[bold]{key}[/bold]: {value}")


# --------------------------------------------------------------------- auth


@app.command()
def login(
    method: str = typer.Option("qr", help="qr (scan with phone) or code (SMS/app code)"),
    phone: str = typer.Option(None, help="Phone number for --method code"),
    api_id: int = typer.Option(None, help="Override Telegram api_id"),
    api_hash: str = typer.Option(None, help="Override Telegram api_hash"),
):
    """Log in to Telegram. Default: QR code, like linking Telegram Desktop."""
    if api_id and api_hash:
        save_credentials(api_id, api_hash)
        console.print("Saved custom API credentials.")
    gateway = _gateway()

    def show_qr(url: str) -> None:
        import qrcode

        console.print(
            "\n[bold]Scan this with your phone:[/bold] "
            "Telegram → Settings → Devices → Link Desktop Device\n"
        )
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
        console.print(f"(or open this link on the phone: {url})\n")

    def ask_password() -> str:
        return typer.prompt("Two-factor password", hide_input=True)

    if method == "qr":
        account = _run(gateway.login_qr(show_qr, ask_password))
    else:
        phone_number = phone or typer.prompt("Phone number (with country code)")
        account = _run(
            gateway.login_code(
                phone_number,
                lambda: typer.prompt("Login code you received"),
                ask_password,
            )
        )
    console.print(f"[green]Logged in as {account['title']}[/green]")


@app.command()
def logout():
    """Log out and invalidate the local session."""
    if _run(_gateway().logout()):
        console.print("Logged out.")
    else:
        console.print("No active session.")


# --------------------------------------------------------------------- chats


@app.command()
def chats(
    refresh: bool = typer.Option(True, help="Fetch the chat list from Telegram"),
    limit: int = typer.Option(200),
    as_json: bool = typer.Option(False, "--json"),
):
    """List your chats. Refreshes the local chat registry from Telegram."""
    conn = _conn()
    if refresh:
        dialogs = _run(_gateway().list_dialogs(limit=limit))
        for dialog in dialogs:
            dbmod.upsert_chat(conn, dialog)
    rows = conn.execute(
        "SELECT chat_id, kind, title, username, monitored FROM chats ORDER BY monitored DESC, title"
    ).fetchall()
    if as_json:
        _print_json(dbmod.rows_to_dicts(rows))
        return
    table = Table("watched", "chat_id", "kind", "title", "username")
    for row in rows:
        table.add_row(
            "✓" if row["monitored"] else "",
            str(row["chat_id"]),
            row["kind"],
            row["title"],
            row["username"] or "",
        )
    console.print(table)


@app.command()
def watch(refs: list[str] = typer.Argument(..., help="Chat ids, @usernames, or titles")):
    """Add chats to the sync allowlist. Only watched chats are ever ingested."""
    conn = _conn()
    for ref in refs:
        chat = _resolve_chat(conn, ref)
        dbmod.set_monitored(conn, chat["chat_id"], True)
        console.print(f"Watching [bold]{chat['title']}[/bold] ({chat['chat_id']})")


@app.command()
def unwatch(refs: list[str] = typer.Argument(...)):
    """Remove chats from the sync allowlist (already-stored messages stay)."""
    conn = _conn()
    for ref in refs:
        chat = _resolve_chat(conn, ref)
        dbmod.set_monitored(conn, chat["chat_id"], False)
        console.print(f"Stopped watching [bold]{chat['title']}[/bold]")


# ---------------------------------------------------------------------- sync


@app.command()
def sync(
    limit: int = typer.Option(None, help="Max messages per chat this run"),
    initial_limit: int = typer.Option(500, help="Cap for a chat's first sync"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Pull new messages for all watched chats (incremental, deduplicated)."""
    conn = _conn()
    report = _run(
        syncmod.sync_monitored(
            conn, _gateway(), limit_per_chat=limit, initial_limit=initial_limit
        )
    )
    if as_json:
        _print_json(
            {
                "run_id": report.run_id,
                "total_inserted": report.total_inserted,
                "chats": [vars(c) for c in report.chats],
            }
        )
        return
    if not report.chats:
        console.print("No watched chats. Add some with `tgvault watch <chat>`.")
        return
    for chat in report.chats:
        if chat.error:
            console.print(f"[red]✗ {chat.title}: {chat.error}[/red]")
        else:
            console.print(f"✓ {chat.title}: +{chat.inserted} new")
    console.print(f"[bold]Total new messages: {report.total_inserted}[/bold]")


# ---------------------------------------------------------------- read/search


@app.command()
def read(
    ref: str = typer.Argument(..., help="Chat id, @username, or title"),
    limit: int = typer.Option(50),
    since: str = typer.Option(None, help="ISO date, e.g. 2026-07-01"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Read stored messages from one chat (newest last)."""
    conn = _conn()
    chat = _resolve_chat(conn, ref)
    sql = "SELECT * FROM messages WHERE chat_id=?"
    params: list = [chat["chat_id"]]
    if since:
        sql += " AND sent_at >= ?"
        params.append(since)
    sql += " ORDER BY message_id DESC LIMIT ?"
    params.append(limit)
    rows = list(reversed(conn.execute(sql, params).fetchall()))
    if as_json:
        _print_json(dbmod.rows_to_dicts(rows))
        return
    for row in rows:
        sender = "me" if row["is_outgoing"] else (row["sender_name"] or row["sender_id"])
        console.print(
            f"[dim]{row['sent_at']}[/dim] [bold]{sender}[/bold] "
            f"[dim]#{row['message_id']}[/dim]: {row['text']}"
        )


@app.command()
def search(
    query: str,
    limit: int = typer.Option(20),
    as_json: bool = typer.Option(False, "--json"),
):
    """Full-text search across all stored messages."""
    conn = _conn()
    rows = dbmod.search(conn, query, limit=limit)
    if as_json:
        _print_json(dbmod.rows_to_dicts(rows))
        return
    for row in rows:
        console.print(
            f"[dim]{row['sent_at']}[/dim] [cyan]{row['chat_title']}[/cyan] "
            f"[bold]{row['sender_name'] or ''}[/bold]: {row['text'][:200]}"
        )


# ----------------------------------------------------------------- processed


@processed_app.command("mark")
def processed_mark(
    namespace: str = typer.Option(
        "agent", help="Read-registry name. Default 'agent'; use one per workflow."
    ),
    chat: str = typer.Option(None, help="Chat ref (required with --ids)"),
    ids: str = typer.Option(None, help="Comma-separated message ids"),
    all_pending: bool = typer.Option(
        False, "--all", help="Mark everything currently unread (optionally one --chat)"
    ),
    note: str = typer.Option(None),
):
    """Mark messages as read/processed by an AI workflow (idempotent).

    This is the agent's own read registry — completely independent of
    Telegram's read receipts, which tgvault never touches.
    """
    conn = _conn()
    if all_pending:
        chat_id = _resolve_chat(conn, chat)["chat_id"] if chat else None
        count = dbmod.mark_all_unprocessed(conn, namespace, chat_id=chat_id, note=note)
    elif chat and ids:
        chat_row = _resolve_chat(conn, chat)
        id_list = [int(i) for i in ids.split(",") if i.strip()]
        count = dbmod.mark_processed(
            conn, namespace, [(chat_row["chat_id"], i) for i in id_list], note=note
        )
    else:
        err_console.print("[red]Provide --chat and --ids, or use --all.[/red]")
        raise typer.Exit(1)
    console.print(f"Marked {count} message(s) in namespace '{namespace}'.")


@processed_app.command("pending")
def processed_pending(
    namespace: str = typer.Option("agent"),
    chat: str = typer.Option(None),
    limit: int = typer.Option(100),
    as_json: bool = typer.Option(True, "--json/--no-json"),
):
    """List stored messages a workflow has NOT processed yet."""
    conn = _conn()
    chat_id = _resolve_chat(conn, chat)["chat_id"] if chat else None
    rows = dbmod.unprocessed(conn, namespace, chat_id=chat_id, limit=limit)
    if as_json:
        _print_json(dbmod.rows_to_dicts(rows))
        return
    for row in rows:
        console.print(f"{row['chat_id']}#{row['message_id']}: {row['text'][:120]}")


# -------------------------------------------------------------------- outbox


@outbox_app.command("draft")
def outbox_draft(
    chat: str = typer.Argument(...),
    text: str = typer.Argument(...),
    reply_to: int = typer.Option(None),
    created_by: str = typer.Option("agent", help="Who wrote this draft"),
):
    """Create a draft message. Drafts are NOT sent until a human approves."""
    conn = _conn()
    chat_row = _resolve_chat(conn, chat)
    draft_id = dbmod.create_draft(
        conn, chat_row["chat_id"], text, created_by, reply_to_id=reply_to
    )
    console.print(
        f"Draft [bold]#{draft_id}[/bold] for [bold]{chat_row['title']}[/bold] created.\n"
        f"A human can review and send it with: tgvault outbox send {draft_id}"
    )


@outbox_app.command("list")
def outbox_list(
    status: str = typer.Option("draft"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List outbox entries."""
    conn = _conn()
    rows = conn.execute(
        """
        SELECT o.*, c.title AS chat_title FROM outbox o
        LEFT JOIN chats c ON c.chat_id = o.chat_id
        WHERE o.status = ? ORDER BY o.id
        """,
        (status,),
    ).fetchall()
    if as_json:
        _print_json(dbmod.rows_to_dicts(rows))
        return
    if not rows:
        console.print(f"No outbox entries with status '{status}'.")
        return
    for row in rows:
        console.print(
            f"[bold]#{row['id']}[/bold] → [cyan]{row['chat_title'] or row['chat_id']}[/cyan] "
            f"(by {row['created_by']}, {row['created_at']}):\n  {row['text']}"
        )


@outbox_app.command("send")
def outbox_send(draft_id: int):
    """Send a draft — interactive, human-only. Shows the message, asks to confirm."""
    if not sys.stdin.isatty():
        err_console.print(
            "[red]Refusing to send: this command requires an interactive terminal.[/red] "
            "Sending messages is a human action by design."
        )
        raise typer.Exit(2)
    conn = _conn()
    row = conn.execute(
        """
        SELECT o.*, c.title AS chat_title FROM outbox o
        LEFT JOIN chats c ON c.chat_id = o.chat_id WHERE o.id=?
        """,
        (draft_id,),
    ).fetchone()
    if row is None or row["status"] != "draft":
        err_console.print(f"[red]No pending draft #{draft_id}.[/red]")
        raise typer.Exit(1)

    console.print(f"\nTo: [bold cyan]{row['chat_title'] or row['chat_id']}[/bold cyan]")
    console.print(f"Message:\n[bold]{row['text']}[/bold]\n")
    confirmation = typer.prompt("Type SEND to send, anything else to cancel")
    if confirmation.strip() != "SEND":
        console.print("Not sent.")
        raise typer.Exit(0)

    chat = conn.execute(
        "SELECT * FROM chats WHERE chat_id=?", (row["chat_id"],)
    ).fetchone()
    if chat is None:
        err_console.print("[red]Chat not found in local registry.[/red]")
        raise typer.Exit(1)
    message_id = _run(
        _gateway().send_message(dict(chat), row["text"], reply_to_id=row["reply_to_id"])
    )
    dbmod.update_draft_status(conn, draft_id, "sent", sent_message_id=message_id)
    console.print(f"[green]Sent (message id {message_id}).[/green]")


@outbox_app.command("reject")
def outbox_reject(draft_id: int):
    """Reject a draft so it can never be sent."""
    conn = _conn()
    if dbmod.update_draft_status(conn, draft_id, "rejected"):
        console.print(f"Draft #{draft_id} rejected.")
    else:
        err_console.print(f"[red]No pending draft #{draft_id}.[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
