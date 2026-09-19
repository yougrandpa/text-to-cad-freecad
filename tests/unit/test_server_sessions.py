"""Tests for the session endpoints and the session/model binding.

A session is one conversation bound to one model. Two things are worth pinning:

* the list has to describe the *part* as well as the conversation, because the
  UI switches the viewport, artefacts and inspector along with the transcript;
* the binding is one-way. A conversation that could be repointed at another
  model mid-flight would let a turn edit one part while the screen showed
  another, which reads as corrupted output rather than as a caller mistake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcad.server.app import create_app

TestClient = pytest.importorskip("fastapi.testclient").TestClient

from tests.unit.test_server import (  # noqa: E402
    ScriptedLlm,
    _sse,
    make_services,
)


@pytest.fixture()
def client(tmp_path):
    services = make_services(tmp_path, max_steps=1)
    with TestClient(create_app(services)) as c:
        c.services = services
        yield c


def _new_session(client, **body):
    r = client.post("/sessions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ══════════════════════════════════════════════════════════════════════════
# listing and creating
# ══════════════════════════════════════════════════════════════════════════


def test_no_sessions_is_an_empty_list_not_an_error(client):
    assert client.get("/sessions").json() == {"sessions": []}


def test_creating_a_session_creates_its_model_too(client):
    """A session without a model is a usable-looking dead end: you could type
    into it and every turn would fail on a missing store."""
    created = _new_session(client, raw_requirement="一个 60x40 的底板")

    assert created["thread_id"]
    assert created["model_id"]
    assert created["ir_version"] == 0
    assert client.services.store.exists(created["model_id"]) is True

    # The requirement text is kept on the IR, so provenance survives.
    ir = client.get(f"/models/{created['model_id']}/ir").json()
    assert ir["requirements"]["raw_text"] == "一个 60x40 的底板"


def test_two_sessions_get_distinct_models(client):
    a = _new_session(client)
    b = _new_session(client)
    assert a["model_id"] != b["model_id"], "两个会话共用模型会让它们的零件互相污染"
    assert a["thread_id"] != b["thread_id"]


def test_reusing_an_existing_model_id_is_refused(client):
    """Seeding a model is not idempotent — it rewrites ``v0.json``.

    So accepting a ``model_id`` that already exists would let one request wipe a
    part the user had been building, and still answer ``ir_version: 0`` as if
    that were the new model. Refuse it and say what to do instead.
    """
    first = _new_session(client, model_id="part-shared")
    client.services.store._docs["part-shared"].version = 4     # a part with history

    r = client.post("/sessions", json={"model_id": "part-shared"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "part-shared" in detail
    assert "thread_id" in detail, "报错要给出可行做法，而不只是拒绝"

    assert client.services.store._docs["part-shared"].version == 4, "零件被重置为 v0 了"
    rows = client.get("/sessions").json()["sessions"]
    assert [row["thread_id"] for row in rows] == [first["thread_id"]], "多出了一个会话"


def test_an_explicit_model_id_is_still_honoured_when_it_is_new(client):
    """The documented "predictable handle" path must survive the refusal above."""
    created = _new_session(client, model_id="part-predictable")
    assert created["model_id"] == "part-predictable"
    assert created["ir_version"] == 0
    assert client.services.store.exists("part-predictable") is True


def test_the_list_reports_the_model_version_so_the_ui_can_switch(client):
    created = _new_session(client)
    row = client.get("/sessions").json()["sessions"][0]
    assert row["thread_id"] == created["thread_id"]
    assert row["model_id"] == created["model_id"]
    assert row["ir_version"] == 0
    assert row["messages"] == 0


def test_the_list_reports_none_when_the_model_is_gone(client, tmp_path):
    """`null` and `0` must stay distinguishable: a model at v0 exists."""
    created = _new_session(client)
    del client.services.store._docs[created["model_id"]]   # simulate a vanished model

    row = client.get("/sessions").json()["sessions"][0]
    assert row["ir_version"] is None, "缺模型必须报 null，不能与 v0 混淆"


def test_new_sessions_appear_at_the_top_of_the_list(client):
    first = _new_session(client)
    second = _new_session(client)
    order = [s["thread_id"] for s in client.get("/sessions").json()["sessions"]]
    assert order == [second["thread_id"], first["thread_id"]]


def test_a_session_that_has_been_used_moves_back_to_the_top(client):
    first = _new_session(client)
    second = _new_session(client)
    assert [s["thread_id"] for s in client.get("/sessions").json()["sessions"]][0] == second["thread_id"]

    # Talk in the first one; it should rise above the newer, empty session.
    client.post("/chat", json={
        "model_id": first["model_id"], "thread_id": first["thread_id"], "text": "开始吧",
    })
    order = [s["thread_id"] for s in client.get("/sessions").json()["sessions"]]
    assert order[0] == first["thread_id"]


def test_listing_sessions_does_not_require_a_live_stack(tmp_path):
    """Listing happens before the user has done anything, and building the stack
    starts FreeCADCmd. An empty data dir has nothing to list and must not pay
    that cost to say so."""
    from tcad.config.schema import Config

    cfg = Config()
    cfg.storage.data_dir = str(tmp_path / "fresh")
    app = create_app(config=cfg)
    with TestClient(app) as c:
        assert c.get("/sessions").json() == {"sessions": []}
        assert app.state.services is None, "列会话不该把 FreeCAD 拉起来"


# ══════════════════════════════════════════════════════════════════════════
# the binding: a session's model is fixed at creation
# ══════════════════════════════════════════════════════════════════════════


def test_chat_refuses_a_model_that_contradicts_the_session(client):
    session = _new_session(client)
    r = client.post("/chat", json={
        "model_id": "some-other-part", "text": "改一下", "thread_id": session["thread_id"],
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert session["model_id"] in detail and "some-other-part" in detail
    assert "repoint" in detail, "报错要说清为什么拒绝，而不只是拒绝"


def test_chat_accepts_the_session_own_model(client):
    session = _new_session(client)
    r = client.post("/chat", json={
        "model_id": session["model_id"], "text": "开始吧", "thread_id": session["thread_id"],
    })
    assert r.status_code == 200
    assert _sse(r.text), "应当是一个 SSE 流"


def test_chat_records_the_user_message_in_the_session(client):
    session = _new_session(client)
    client.post("/chat", json={
        "model_id": session["model_id"], "thread_id": session["thread_id"], "text": "做一个底板",
    })
    messages = client.get(f"/threads/{session['thread_id']}/messages").json()["messages"]
    assert [m["role"] for m in messages][0] == "user"
    assert messages[0]["content"] == "做一个底板"

    row = client.get("/sessions").json()["sessions"][0]
    assert row["title"] == "做一个底板", "会话标题取首条用户消息"


def test_chat_without_a_thread_creates_one_bound_to_that_model(client):
    """The no-session path: the first message starts the conversation."""
    session = _new_session(client)
    r = client.post("/chat", json={"model_id": session["model_id"], "text": "hi"})
    assert r.status_code == 200
    threads = client.get("/threads").json()["threads"]
    assert any(t["model_id"] == session["model_id"] for t in threads)


def test_the_stream_announces_the_bound_model(client):
    """The client renders whatever the server says it is building; if it echoed
    the request instead of the session's model, the two could disagree."""
    session = _new_session(client)
    r = client.post("/chat", json={
        "model_id": session["model_id"], "thread_id": session["thread_id"], "text": "x",
    })
    start = dict(_sse(r.text))["start"]
    assert start["model_id"] == session["model_id"]
    assert start["thread_id"] == session["thread_id"]
