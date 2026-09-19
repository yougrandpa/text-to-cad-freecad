"""Interrupting a running turn from the conversation.

The feature is small; the ways to get it wrong are not:

1. **A stop must produce a verdict.** The defect this exists to fix is a turn
   that ends without anyone being able to tell, so a stopped turn has to arrive
   as an ordinary ``result`` frame with state ``aborted`` — from the engine, not
   as a stream that merely stops. That is the difference between "I stopped it"
   and "something went quiet".
2. **A stop must actually stop.** The step-boundary predicate alone would wait
   for the step in flight — up to a full model timeout — so the turn's task is
   cancelled too. The cancellation is only *reinterpreted* when the predicate
   says a stop was asked for; a cancellation from anywhere else (shutdown, the
   client hanging up) keeps its ordinary asyncio meaning.
3. **A stop must not be lost to a race.** The client mints the turn id, so a
   stop can arrive before the server has registered the turn. That stop is
   remembered and honoured at registration, instead of reporting success while
   the model carries on.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tcad.core.types import Thread, TurnKind, TurnState
from tcad.loop.engine import STOPPED_BY_USER, LoopEngine, UserMessage
from tcad.server.app import (
    ChatRequest,
    InterruptRequest,
    budget_limits_from_config,
    create_app,
)

TestClient = pytest.importorskip("fastapi.testclient").TestClient

from tests.unit.test_budget_no_ceiling import BlockingLlm  # noqa: E402
from tests.unit.test_server import make_services  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════


def _endpoint(app, path: str):
    """The endpoint function behind a route, so a test can drive it directly."""
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)


def _frame(raw: str) -> tuple[str, dict]:
    event, data = "message", "{}"
    for line in raw.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data = line[6:]
    return event, json.loads(data)


async def _drain(agen, frames: list[str], *, timeout: float = 10.0) -> None:
    """Consume the rest of a stream, collecting raw frames."""
    while True:
        try:
            frames.append(await asyncio.wait_for(agen.__anext__(), timeout=timeout))
        except StopAsyncIteration:
            return


async def _pump(agen, frames: list[str], predicate, *, timeout: float = 10.0) -> None:
    """Advance the SSE generator until *predicate* holds.

    Frames must be consumed for the endpoint's own `while not task.done()` loop
    to progress — `__anext__()` is what parks the generator and lets the turn
    task run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "回合未在预期时间内推进"
        frames.append(
            await asyncio.wait_for(agen.__anext__(), timeout=max(0.1, deadline - loop.time()))
        )


def _engine(services, **kwargs) -> LoopEngine:
    from tcad.tools.base import build_default_registry

    return LoopEngine(
        services,
        build_default_registry(services),
        budget_limits_from_config(services.config),
        services.loop_config,
        **kwargs,
    )


def _turn(engine: LoopEngine):
    return engine.run_turn(
        Thread(thread_id="t1", model_id="m1"),
        UserMessage(kind=TurnKind.CREATE, text="make a plate"),
    )


# ══════════════════════════════════════════════════════════════════════════
# the engine: what a stop means
# ══════════════════════════════════════════════════════════════════════════


def test_a_stop_between_steps_ends_the_turn_as_aborted(tmp_path):
    """The step-boundary half of the mechanism.

    A model that is only narrating would otherwise burn two more steps and then
    be reported as FAILED ("no progress") — which would attribute the ending to
    the model rather than to the person who pressed stop.
    """
    services = make_services(tmp_path, max_steps=5)
    llm = services.llm._client  # ScriptedLlm: text, no tool calls

    # The stop lands *after* the first reply, i.e. while the turn is genuinely
    # in flight — not before it starts, which is the easy case.
    engine = _engine(services, stop_requested=lambda: llm.calls >= 1)
    result = asyncio.run(_turn(engine))

    assert result.state is TurnState.ABORTED
    assert result.state is not TurnState.FAILED
    assert result.error == STOPPED_BY_USER
    assert result.steps == 1, "停止后不该再走一步"
    assert llm.calls == 1, "停止后不该再请求模型"


def test_a_requested_cancellation_becomes_an_aborted_result(tmp_path):
    """The mechanism half: the in-flight model call must actually be interrupted.

    Without this, "stop" would only take effect at the next step boundary — and
    a single model call can be the whole timeout.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    asked = {"stop": False}
    engine = _engine(services, stop_requested=lambda: asked["stop"])

    async def scenario():
        task = asyncio.create_task(_turn(engine))
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        asked["stop"] = True
        task.cancel()
        return await task

    result = asyncio.run(scenario())
    assert result.state is TurnState.ABORTED
    assert result.error == STOPPED_BY_USER
    assert blocking.cancelled.is_set(), "在飞的模型调用没有被真的取消"


def test_a_cancellation_nobody_asked_for_is_not_reinterpreted(tmp_path):
    """Cancellation keeps its asyncio meaning when no stop was requested.

    Server shutdown and a client that hangs up both cancel this task. Turning
    those into an "aborted" outcome would swallow the cancellation the caller
    is relying on — so the predicate, not the exception, decides.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    engine = _engine(services)  # no stop predicate at all

    async def scenario():
        task = asyncio.create_task(_turn(engine))
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════
# the server: stopping one exact turn
# ══════════════════════════════════════════════════════════════════════════


def test_interrupting_a_running_turn_ends_the_stream_with_an_aborted_verdict(tmp_path):
    """The whole feature, end to end over the real SSE generator.

    Three things are asserted together because any one of them alone would let
    the defect back in: the verdict arrives, it says `aborted` (not succeeded,
    not failed), and the model call was really interrupted.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    app = create_app(services)
    chat = _endpoint(app, "/chat")
    interrupt = _endpoint(app, "/chat/interrupt")

    async def scenario():
        resp = await chat(
            ChatRequest(model_id="m1", text="make a plate", request_id="turn-1")
        )
        agen = resp.body_iterator
        frames: list[str] = []
        frames.append(await agen.__anext__())
        event, data = _frame(frames[0])
        assert event == "start"
        assert data["request_id"] == "turn-1", "回合的名字必须回给客户端"

        await _pump(agen, frames, lambda: blocking.started.is_set())
        assert not blocking.cancelled.is_set(), "还没请求停止就不该取消"

        decision = await interrupt(InterruptRequest(request_id="turn-1"))
        assert decision["interrupted"] is True
        assert decision["stage"] == "running"
        assert decision["thread_id"]

        await _drain(agen, frames)

        results = [d for (e, d) in map(_frame, frames) if e == "result"]
        assert results, "被停止的回合必须以 result 帧结束，否则界面没有结论可显示"
        assert results[-1]["state"] == "aborted"
        assert results[-1]["state"] != "succeeded"
        assert "stopped by the user" in (results[-1]["error"] or "")
        assert blocking.cancelled.is_set(), "停止没有真的取消在飞的模型调用"
        assert app.state.turns == {}, "回合结束后注册表必须清空"

    asyncio.run(scenario())


def test_a_stop_that_races_registration_is_honoured_not_dropped(tmp_path):
    """The race the client-minted id exists for.

    A stop that arrives before the server has registered the turn used to be
    indistinguishable from a stop for a turn that does not exist — so the honest
    outcome was "nothing was stopped" while the model kept generating. The id
    arrives with the stop, so the intent can be held until the turn shows up.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    app = create_app(services)
    chat = _endpoint(app, "/chat")
    interrupt = _endpoint(app, "/chat/interrupt")

    async def scenario():
        decision = await interrupt(InterruptRequest(request_id="turn-2"))
        assert decision["stage"] == "pending", "尚未注册的回合不能被说成『已取消』"
        assert "turn-2" in app.state.pending_stops

        resp = await chat(ChatRequest(model_id="m1", text="x", request_id="turn-2"))
        agen = resp.body_iterator
        frames = [await agen.__anext__()]
        await _drain(agen, frames)

        results = [d for (e, d) in map(_frame, frames) if e == "result"]
        assert results and results[-1]["state"] == "aborted"
        assert not blocking.started.is_set(), "第一步之前就该停下，不该再去请求模型"
        assert "turn-2" not in app.state.pending_stops, "待停止标记必须被消费掉"

    asyncio.run(scenario())


def test_a_request_id_cannot_name_two_running_turns(tmp_path):
    """A reused id would overwrite a running turn's registry entry.

    The overwritten turn would become impossible to stop — the exact failure
    this endpoint exists to prevent — so the collision is refused rather than
    allowed to be silent.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    app = create_app(services)
    chat = _endpoint(app, "/chat")

    async def scenario():
        resp = await chat(ChatRequest(model_id="m1", text="x", request_id="turn-3"))
        agen = resp.body_iterator
        frames = [await agen.__anext__()]
        await _pump(agen, frames, lambda: blocking.started.is_set())

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await chat(ChatRequest(model_id="m1", text="again", request_id="turn-3"))
        assert exc.value.status_code == 409
        await agen.aclose()

    asyncio.run(scenario())


def test_a_stop_for_a_turn_that_never_appears_expires(tmp_path):
    """A remembered stop must not arm an unrelated later turn.

    Found the hard way: the live probe reuses its request id between runs, and a
    stop recorded for a turn that had already finished was still sitting in the
    registry — so the *next* run was stopped before its first step, with nothing
    on screen to explain why. A stop is aimed at one specific turn; if that turn
    never shows up, the request has to lapse.
    """
    import time as _time

    from tcad.server.app import (
        _PENDING_STOP_LIMIT,
        _PENDING_STOP_TTL_S,
        _remember_pending_stop,
        _take_pending_stop,
    )

    now = _time.monotonic()
    pending: dict[str, float] = {}
    _remember_pending_stop(pending, "turn-x")
    assert _take_pending_stop(pending, "turn-x", now=now + _PENDING_STOP_TTL_S - 1) is True

    _remember_pending_stop(pending, "turn-y")
    assert _take_pending_stop(
        pending, "turn-y", now=now + _PENDING_STOP_TTL_S + 1
    ) is False, "过期的待停止标记不该还能停掉一个后来的回合"
    assert _take_pending_stop(
        pending, "turn-y", now=now + _PENDING_STOP_TTL_S + 1
    ) is False, "过期条目也必须被消费掉，否则会一直堆着"

    assert _take_pending_stop(pending, "never-seen") is False

    # Bounded: the ids come from callers, so the map cannot grow without limit.
    for i in range(_PENDING_STOP_LIMIT + 10):
        _remember_pending_stop(pending, f"flood-{i}")
    assert len(pending) <= _PENDING_STOP_LIMIT


def test_interrupting_an_unknown_turn_is_an_answer_not_an_error(tmp_path):
    """The UI has to be able to tell "nothing was running" from "stop failed"."""
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as c:
        r = c.post("/chat/interrupt", json={"request_id": "never-started"})
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "pending"
    assert body["note"], "没有说清楚这一次停止落到了哪里"


def test_registration_refuses_an_id_that_was_taken_in_between(tmp_path):
    """The 409 in `chat()` is a fast path, not the authority.

    Two requests with the same id can both pass that check before either
    generator has run. The second must not overwrite the first's registry entry:
    the overwritten turn would become impossible to stop — the exact failure
    this feature exists to remove — and it would do so silently.
    """
    from tcad.server.app import RunningTurn

    services = make_services(tmp_path)
    app = create_app(services)
    chat = _endpoint(app, "/chat")

    async def scenario():
        resp = await chat(ChatRequest(model_id="m1", text="x", request_id="dup"))
        # The race, reproduced: another request took this id between `chat()`
        # returning and this generator starting.
        rival = RunningTurn("dup", "th-rival")
        app.state.turns["dup"] = rival

        frames: list[str] = []
        await _drain(resp.body_iterator, frames)
        events = [e for (e, _) in map(_frame, frames)]
        assert "error" in events, "被占用时必须以错误帧结束，而不是偷偷覆盖别人的回合"
        assert "result" not in events
        assert app.state.turns["dup"] is rival, "不能把别人的注册表条目删掉"

    asyncio.run(scenario())


def test_a_finished_turn_is_deregistered(tmp_path):
    """A normal turn must not leave a registry entry behind.

    Otherwise the registry grows without bound and a later turn reusing an id
    would be refused for no reason.
    """
    services = make_services(tmp_path)
    app = create_app(services)
    chat = _endpoint(app, "/chat")

    async def scenario():
        resp = await chat(ChatRequest(model_id="m1", text="x", request_id="turn-4"))
        agen = resp.body_iterator
        frames = [await agen.__anext__()]
        await _drain(agen, frames)
        results = [d for (e, d) in map(_frame, frames) if e == "result"]
        assert results, "普通回合也必须以 result 帧结束"
        assert app.state.turns == {}

    asyncio.run(scenario())
