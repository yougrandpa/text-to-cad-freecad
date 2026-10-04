"""One active chat turn per conversation/model, even with distinct request ids."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from tcad.server.app import ChatRequest, create_app
from tests.unit.test_server import make_services
from tests.unit.test_server_interrupt import _drain, _endpoint, _frame


@pytest.mark.parametrize("second_thread", ["first", "another-thread"])
def test_active_thread_or_model_rejects_second_turn_before_streaming(tmp_path, second_thread):
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")

    async def scenario():
        first = await chat(ChatRequest(model_id="part", thread_id="first", text="create", request_id="a"))
        assert _frame(await first.body_iterator.__anext__())[0] == "start"
        try:
            with pytest.raises(HTTPException) as exc:
                await chat(ChatRequest(model_id="part", thread_id=second_thread, text="edit", request_id="b"))
            assert exc.value.status_code == 409
            assert "active turn" in exc.value.detail
            assert list(app.state.turns) == ["a"]
        finally:
            await first.body_iterator.aclose()
        assert app.state.turns == {}
        # Closing/canceling a turn releases the model, it is not permanently busy.
        next_response = await chat(ChatRequest(model_id="part", thread_id=second_thread, text="retry", request_id="c"))
        assert _frame(await next_response.body_iterator.__anext__())[0] == "start"
        await next_response.body_iterator.aclose()

    asyncio.run(scenario())


def test_requests_accepted_before_registration_cannot_race_model_edits(tmp_path):
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")

    async def scenario():
        first = await chat(ChatRequest(model_id="part", text="first", request_id="a"))
        second = await chat(ChatRequest(model_id="part", text="second", request_id="b"))
        assert _frame(await first.body_iterator.__anext__())[0] == "start"
        frames = []
        await _drain(second.body_iterator, frames)
        assert [_frame(frame)[0] for frame in frames] == ["error"]
        assert _frame(frames[0])[1]["type"] == "ActiveTurnConflict"
        assert list(app.state.turns) == ["a"]
        # Refused work must not enter the transcript as if it had been run.
        rows = app.state.session_db.list_messages("th-part")
        assert [row["content"] for row in rows] == ["first"]
        await first.body_iterator.aclose()

    asyncio.run(scenario())


def test_different_models_and_threads_can_stream_concurrently(tmp_path):
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")

    async def scenario():
        first = await chat(ChatRequest(model_id="one", text="first", request_id="a"))
        second = await chat(ChatRequest(model_id="two", text="second", request_id="b"))
        assert _frame(await first.body_iterator.__anext__())[0] == "start"
        assert _frame(await second.body_iterator.__anext__())[0] == "start"
        assert set(app.state.turns) == {"a", "b"}
        await first.body_iterator.aclose()
        assert set(app.state.turns) == {"b"}
        await second.body_iterator.aclose()

    asyncio.run(scenario())


def test_disconnect_keeps_model_busy_until_cancellation_cleanup_finishes(tmp_path, monkeypatch):
    started, cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_turn(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()

    monkeypatch.setattr("tcad.server.app.run_turn_request", slow_turn)
    app = create_app(make_services(tmp_path))
    chat = _endpoint(app, "/chat")

    async def scenario():
        response = await chat(ChatRequest(model_id="part", text="work", request_id="a"))
        assert _frame(await response.body_iterator.__anext__())[0] == "start"
        pump = asyncio.create_task(response.body_iterator.__anext__())
        await asyncio.wait_for(started.wait(), 2)
        running = app.state.turns["a"]
        pump.cancel()  # Client disconnects while the engine is still running.
        with pytest.raises(asyncio.CancelledError):
            await pump
        await asyncio.wait_for(cleanup_started.wait(), 2)
        with pytest.raises(HTTPException) as exc:
            await chat(ChatRequest(model_id="part", text="retry", request_id="b"))
        assert exc.value.status_code == 409
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await running.task
        await asyncio.sleep(0)  # The registered done callback releases ownership.
        assert app.state.turns == {}

    asyncio.run(scenario())
