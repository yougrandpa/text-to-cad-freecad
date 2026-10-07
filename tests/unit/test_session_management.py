"""Persistent history organisation, safe deletion and existing DB migration."""

import asyncio
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tcad.server.app import ChatRequest, SessionUpdateRequest, create_app
from tcad.store.session_db import SessionDB
from tests.unit.test_server import make_services
from tests.unit.test_server_interrupt import _endpoint, _frame


def session_endpoint(app, method):
    return next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/sessions/{thread_id}" and method in route.methods)


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(make_services(tmp_path))) as client:
        yield client


def test_existing_database_migrates_without_changing_history(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, "
                     "created_at TEXT NOT NULL, context_state TEXT NOT NULL DEFAULT 'full')")
        conn.execute("INSERT INTO threads VALUES ('old', 'part', '2026-01-01T00:00:00+00:00', 'full')")
    db = SessionDB(path)
    db.add_message("old", "user", "原有设计")
    folder = db.save_folder("机械设计")
    db.update_thread("old", {"folder_id": folder["folder_id"], "archived": True})
    db.close()
    db = SessionDB(path)
    row = db.list_threads()[0]
    assert row["title"] == "原有设计"
    assert row["model_id"] == "part"
    assert row["folder_id"] == folder["folder_id"] and row["archived"] is True
    assert db.list_threads(archived=False) == []
    assert db.list_folders() == [folder]
    db.close()


def test_archive_restore_and_move_preserve_model_and_messages(client):
    folder = client.post("/session-folders", json={"name": "  机构  "}).json()
    assert folder["name"] == "机构"
    session = client.post("/sessions", json={}).json()
    db = client.app.state.session_db
    db.add_message(session["thread_id"], "user", "四足机构")
    url = f"/sessions/{session['thread_id']}"
    assert client.patch(url, json={"folder_id": folder["folder_id"], "archived": True}).status_code == 200
    assert client.get("/sessions").json() == {"sessions": []}
    row = client.get("/sessions?include_archived=true").json()["sessions"][0]
    assert row["folder_id"] == folder["folder_id"] and row["archived"] is True
    assert row["model_id"] == session["model_id"] and row["messages"] == 1
    assert client.patch(url, json={"archived": False}).status_code == 200
    assert client.get("/sessions").json()["sessions"][0]["folder_id"] == folder["folder_id"]
    assert client.patch(url, json={"folder_id": None}).status_code == 200
    assert client.get("/sessions").json()["sessions"][0]["folder_id"] is None
    assert db.list_messages(session["thread_id"])[0]["content"] == "四足机构"


def test_folder_creation_rename_and_delete_keep_contained_conversations(client):
    folder = client.post("/session-folders", json={"name": "初稿"}).json()
    session = client.post("/sessions", json={"folder_id": folder["folder_id"]}).json()
    archived = client.post("/sessions", json={"folder_id": folder["folder_id"]}).json()
    client.patch(f"/sessions/{archived['thread_id']}", json={"archived": True})
    url = f"/session-folders/{folder['folder_id']}"
    assert client.patch(url, json={"name": "成品"}).json()["name"] == "成品"
    assert client.delete(url).status_code == 200
    assert client.get("/session-folders").json() == {"folders": []}
    rows = client.get("/sessions?include_archived=true").json()["sessions"]
    assert {row["thread_id"] for row in rows} == {session["thread_id"], archived["thread_id"]}
    assert all(row["folder_id"] is None for row in rows)
    assert next(row for row in rows if row["thread_id"] == archived["thread_id"])["archived"] is True


@pytest.mark.parametrize("name", ["", "  ", "x" * 61])
def test_invalid_folder_names_are_rejected(client, name):
    assert client.post("/session-folders", json={"name": name}).status_code == 422
    assert client.get("/session-folders").json()["folders"] == []


def test_duplicate_names_and_missing_folder_do_not_change_session(client):
    client.post("/session-folders", json={"name": "设计"})
    assert client.post("/session-folders", json={"name": "  设计 "}).status_code == 409
    session = client.post("/sessions", json={}).json()
    assert client.patch(f"/sessions/{session['thread_id']}", json={"folder_id": "missing", "archived": True}).status_code == 404
    row = client.get("/sessions").json()["sessions"][0]
    assert row["folder_id"] is None and row["archived"] is False
    assert client.post("/sessions", json={"folder_id": "missing"}).status_code == 404
    assert len(client.get("/sessions").json()["sessions"]) == 1


@pytest.mark.parametrize("body", [{}, {"archived": None}, {"archived": "true"}, {"model_id": "other"}])
def test_invalid_session_changes_are_rejected(client, body):
    session = client.post("/sessions", json={}).json()
    assert client.patch(f"/sessions/{session['thread_id']}", json=body).status_code == 422


def test_delete_cleans_related_records_and_blocks_resurrection(client):
    session = client.post("/sessions", json={}).json()
    other = client.post("/sessions", json={}).json()
    db = client.app.state.session_db
    turn = db.start_turn(session["thread_id"], "create", 0)
    db.record_step(turn.turn_id, "ir_patch")
    db.add_message(session["thread_id"], "user", "删除测试", turn.turn_id)
    db.add_token_usage(turn.turn_id, "in", 10)
    db.request_approval(session["thread_id"], turn.turn_id, "raw_python")
    db.finish_turn(turn.turn_id, "succeeded")
    db.add_message(other["thread_id"], "user", "保留测试")
    assert client.delete(f"/sessions/{session['thread_id']}").status_code == 200
    assert db.get_thread(session["thread_id"]) is None
    assert db.list_messages(session["thread_id"]) == []
    assert db.get_pending_approvals(session["thread_id"]) == []
    for table in ("turns", "steps", "token_usage", "approvals"):
        assert db._fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    assert db.list_messages(other["thread_id"])[0]["content"] == "保留测试"
    assert client.get(f"/models/{session['model_id']}/ir").status_code == 200
    assert client.post("/chat", json={**session, "text": "重试"}).status_code == 404
    assert client.delete(f"/sessions/{session['thread_id']}").status_code == 404
    with pytest.raises(ValueError, match="deleted"):
        db.create_thread(session["model_id"], thread_id=session["thread_id"])


def test_active_turn_blocks_archive_move_and_delete(tmp_path):
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")
    delete = session_endpoint(app, "DELETE")
    update = session_endpoint(app, "PATCH")

    async def scenario():
        response = await chat(ChatRequest(model_id="part", thread_id="running", text="work"))
        assert _frame(await response.body_iterator.__anext__())[0] == "start"
        try:
            for action in [lambda: delete("running"), lambda: update("running", SessionUpdateRequest(archived=True)),
                           lambda: update("running", SessionUpdateRequest(folder_id=None))]:
                with pytest.raises(HTTPException) as exc:
                    await action()
                assert exc.value.status_code == 409
            assert app.state.session_db.get_thread("running") is not None
        finally:
            await response.body_iterator.aclose()
        assert await delete("running") == {"deleted": True}

    asyncio.run(scenario())


def test_delete_between_chat_acceptance_and_registration_cannot_recreate_thread(tmp_path):
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")

    async def scenario():
        response = await chat(ChatRequest(model_id="part", thread_id="existing", text="work"))
        db = app.state.session_db
        db.create_thread("part", "existing")
        await session_endpoint(app, "DELETE")("existing")
        event, data = _frame(await response.body_iterator.__anext__())
        assert event == "error" and data["type"] == "DeletedSession"
        await response.body_iterator.aclose()
        assert db.get_thread("existing") is None and db.list_messages("existing") == []
        assert app.state.turns == {}

    asyncio.run(scenario())
