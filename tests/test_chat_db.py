"""
Chat session ownership — repro tests for the leak where chat_sessions had no
owner column at all: list_sessions() returned everyone's sessions, and
get_messages/delete_session/rename_session/add_message/touch_session/
get_session operated on any session id regardless of who asked.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services.account_service import NotAuthorized, NotFound
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import chat_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _bob() -> RequestContext:
    return RequestContext(identity=_mint_identity_for_tests("bob"), source="test")


def test_new_session_is_owned_by_the_creator():
    ctx = local_context("test")
    sid = chat_db.new_session(ctx, "hello")
    assert chat_db.get_session(ctx, sid)["title"] == "hello"


def test_list_sessions_never_shows_another_owners_sessions():
    mine = chat_db.new_session(local_context("test"), "my chat")
    bobs = chat_db.new_session(_bob(), "bob's chat")

    my_ids = {s["id"] for s in chat_db.list_sessions(local_context("test"))}
    bob_ids = {s["id"] for s in chat_db.list_sessions(_bob())}

    assert my_ids == {mine}
    assert bob_ids == {bobs}


@pytest.mark.parametrize("op", ["get_messages", "add_message", "touch_session",
                                "rename_session", "delete_session", "get_session"])
def test_a_strangers_session_id_is_refused_not_silently_served(op):
    sid = chat_db.new_session(local_context("test"), "private conversation")
    chat_db.add_message(local_context("test"), sid, "user", "what should I buy?")

    stranger = _bob()
    with pytest.raises(NotAuthorized):
        if op == "get_messages":
            chat_db.get_messages(stranger, sid)
        elif op == "add_message":
            chat_db.add_message(stranger, sid, "user", "hijacked")
        elif op == "touch_session":
            chat_db.touch_session(stranger, sid, tickers=["AAPL"])
        elif op == "rename_session":
            chat_db.rename_session(stranger, sid, "hijacked title")
        elif op == "delete_session":
            chat_db.delete_session(stranger, sid)
        elif op == "get_session":
            chat_db.get_session(stranger, sid)

    # The legitimate owner's data is untouched by the failed cross-user attempt.
    assert chat_db.get_session(local_context("test"), sid)["title"] == "private conversation"
    assert len(chat_db.get_messages(local_context("test"), sid)) == 1


def test_get_session_returns_none_for_a_truly_missing_session_not_an_error():
    assert chat_db.get_session(local_context("test"), "does-not-exist") is None


def test_deleting_a_missing_session_raises_not_found():
    with pytest.raises(NotFound):
        chat_db.delete_session(local_context("test"), "does-not-exist")


def test_migrating_a_pre_existing_table_without_an_owner_column_does_not_crash(tmp_path, monkeypatch):
    """
    Regression test: a real deployment's chat_sessions table predates the
    owner column entirely. _create_schema() used to create the
    (owner, updated_at) index BEFORE migrate_columns() added the owner
    column to a pre-existing table, raising "no such column: owner" on the
    very first request. Build a pre-existing table in the OLD shape (no
    owner column, some rows already in it) and confirm opening it now
    migrates cleanly instead of crashing.
    """
    import sqlite3
    from portfolio_agent.domain import LOCAL_OWNER

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE chat_sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            tickers     TEXT DEFAULT '[]',
            last_rec    TEXT DEFAULT ''
        )
    """)
    conn.execute("INSERT INTO chat_sessions (id, title) VALUES ('old-session', 'pre-existing chat')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    ctx = local_context("test")
    sessions = chat_db.list_sessions(ctx)   # must not raise sqlite3.OperationalError
    assert [s["id"] for s in sessions] == ["old-session"]

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT owner FROM chat_sessions WHERE id = 'old-session'").fetchone()
    assert row["owner"] == LOCAL_OWNER
