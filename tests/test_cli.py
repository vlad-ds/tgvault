from typer.testing import CliRunner

from tgvault import db
from tgvault.cli import app
from tgvault.paths import db_path

runner = CliRunner()


def _vault(tmp_path, monkeypatch):
    monkeypatch.setenv("TGVAULT_HOME", str(tmp_path))
    conn = db.init_db(db_path())
    db.upsert_chat(
        conn,
        {
            "chat_id": 1,
            "entity_type": "user",
            "kind": "user",
            "access_hash": 42,
            "title": "Alice",
            "username": "alice",
            "phone": None,
        },
    )
    return conn


def test_outbox_send_refuses_non_interactive(tmp_path, monkeypatch):
    conn = _vault(tmp_path, monkeypatch)
    draft_id = db.create_draft(conn, 1, "hi", "agent")
    result = runner.invoke(app, ["outbox", "send", str(draft_id)])
    assert result.exit_code == 2
    assert "interactive" in result.output
    row = conn.execute("SELECT status FROM outbox WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "draft"  # untouched


def test_draft_and_reject_via_cli(tmp_path, monkeypatch):
    conn = _vault(tmp_path, monkeypatch)
    result = runner.invoke(app, ["outbox", "draft", "alice", "hello there"])
    assert result.exit_code == 0
    assert "Draft" in result.output

    result = runner.invoke(app, ["outbox", "reject", "1"])
    assert result.exit_code == 0
    row = conn.execute("SELECT status FROM outbox WHERE id=1").fetchone()
    assert row[0] == "rejected"


def test_read_and_search_cli(tmp_path, monkeypatch):
    conn = _vault(tmp_path, monkeypatch)
    db.ingest_messages(
        conn,
        1,
        [
            {
                "message_id": 1,
                "sent_at": "2026-07-01T10:00:00+00:00",
                "sender_id": 111,
                "sender_name": "Alice",
                "is_outgoing": False,
                "text": "want to grab dinner?",
                "media_type": None,
                "reply_to_id": None,
            }
        ],
    )
    result = runner.invoke(app, ["read", "alice"])
    assert result.exit_code == 0
    assert "dinner" in result.output

    result = runner.invoke(app, ["search", "dinner", "--json"])
    assert result.exit_code == 0
    assert '"message_id": 1' in result.output


def test_processed_default_namespace_and_all(tmp_path, monkeypatch):
    conn = _vault(tmp_path, monkeypatch)
    db.ingest_messages(
        conn,
        1,
        [
            {
                "message_id": i,
                "sent_at": f"2026-07-0{i}T10:00:00+00:00",
                "sender_id": 111,
                "sender_name": "Alice",
                "is_outgoing": False,
                "text": f"msg {i}",
                "media_type": None,
                "reply_to_id": None,
            }
            for i in (1, 2, 3)
        ],
    )
    result = runner.invoke(app, ["processed", "pending"])
    assert result.exit_code == 0
    assert result.output.count('"message_id"') == 3

    result = runner.invoke(app, ["processed", "mark", "--chat", "alice", "--ids", "1"])
    assert result.exit_code == 0 and "Marked 1" in result.output

    result = runner.invoke(app, ["processed", "mark", "--all"])
    assert result.exit_code == 0 and "Marked 2" in result.output

    result = runner.invoke(app, ["processed", "mark"])  # neither --ids nor --all
    assert result.exit_code == 1

    result = runner.invoke(app, ["processed", "pending"])
    assert '"message_id"' not in result.output


def test_ambiguous_and_missing_chat_refs(tmp_path, monkeypatch):
    conn = _vault(tmp_path, monkeypatch)
    db.upsert_chat(
        conn,
        {
            "chat_id": 2,
            "entity_type": "user",
            "kind": "user",
            "access_hash": 43,
            "title": "Alice Cooper",
            "username": None,
            "phone": None,
        },
    )
    # "alice" matches the @alice username exactly -> unambiguous
    result = runner.invoke(app, ["read", "alice"])
    assert result.exit_code == 0
    # "Ali" is a substring of both titles -> ambiguous
    result = runner.invoke(app, ["read", "Ali"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["read", "nonexistent"])
    assert result.exit_code == 1
