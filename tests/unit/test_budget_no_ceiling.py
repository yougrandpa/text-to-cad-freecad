"""No work ceiling on a turn: the semantics, and the consequences.

The shipped configuration sets every loop ceiling to ``None`` ("do not cap
this"). Three classes of thing need to hold, and none of them is about the
happy path:

1. ``None`` must survive the whole path config -> ``BudgetLimits`` -> ``Budget``
   without being coerced into a number. ``int(None)`` is a ``TypeError``, and it
   would be raised while assembling a turn rather than at start-up.
2. The unbounded state must be *visible*. "This turn has no ceiling" changes how
   long you should expect to wait, and inferring it from a run that never ends
   is not a UI.
3. An unbounded turn must not be able to outlive the client. With a step ceiling
   an orphaned turn died on its own; without one it would run forever.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tcad.config.schema import Config
from tcad.core.types import TurnKind
from tcad.loop.budget import LIMIT_NAMES, Budget, BudgetLimits
from tcad.server.app import ChatRequest, budget_limits_from_config, create_app

TestClient = pytest.importorskip("fastapi.testclient").TestClient

from tests.unit.test_server import (  # noqa: E402
    FakeGate,
    FakeHandle,
    FakeStore,
    FakeWorker,
    ScriptedLlm,
    make_services,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. None survives the config -> BudgetLimits path
# ══════════════════════════════════════════════════════════════════════════


def test_unset_ceilings_pass_through_without_coercion():
    """The regression: an unset ceiling is `None`, and `int(None)` raises.

    A `TypeError` here would surface while starting a turn, not at start-up, so
    it would look like "chat is broken" instead of "the config has no caps".
    """
    cfg = Config()
    assert all(getattr(cfg.loop, n) is None for n in LIMIT_NAMES), "默认应为无上限"

    limits = budget_limits_from_config(cfg)
    assert limits.bounded() == []
    assert sorted(limits.unlimited()) == sorted(LIMIT_NAMES)


def test_set_ceilings_are_carried_through_unchanged():
    cfg = Config()
    cfg.loop.max_steps_per_turn = 7
    cfg.loop.max_tokens_per_turn = 1234
    cfg.loop.step_timeout_s = 2.5
    cfg.loop.turn_wall_clock_s = 60.0
    cfg.loop.max_compile_retries = 0  # 0 is a real ceiling, not "off"

    limits = budget_limits_from_config(cfg)
    assert limits.max_steps_per_turn == 7
    assert limits.max_tokens_per_turn == 1234
    assert limits.step_timeout_s == 2.5
    assert limits.turn_wall_clock_s == 60.0
    assert limits.max_compile_retries == 0
    assert limits.unlimited() == []


def test_budget_built_from_an_unbounded_config_does_not_trip_under_load():
    b = Budget(budget_limits_from_config(Config()))
    for _ in range(500):
        b.check_step()
        b.add_tokens(100_000, 100_000)
        b.check_step_timeout(0.0)
    assert b.steps == 500
    assert b.total_tokens() == 100_000_000


# ══════════════════════════════════════════════════════════════════════════
# 2. the unbounded state is reported
# ══════════════════════════════════════════════════════════════════════════


def test_health_reports_the_ceilings_in_force(tmp_path):
    services = make_services(tmp_path)
    for name in LIMIT_NAMES:
        setattr(services.config.loop, name, None)
    with TestClient(create_app(services)) as c:
        body = c.get("/health").json()

    budget = body["budget"]
    assert budget is not None, "/health 必须报告预算，否则界面无从显示"
    assert budget["unbounded_all"] is True
    assert sorted(budget["unbounded"]) == sorted(LIMIT_NAMES)
    assert all(v is None for v in budget["limits"].values())


def test_health_reports_partial_and_bounded_configurations(tmp_path):
    services = make_services(tmp_path)  # make_services sets max_steps only
    with TestClient(create_app(services)) as c:
        body = c.get("/health").json()

    budget = body["budget"]
    assert budget["unbounded_all"] is False
    assert budget["unbounded"] == [n for n in LIMIT_NAMES if n != "max_steps_per_turn"]
    assert budget["limits"]["max_steps_per_turn"] == 2


def test_the_ui_shows_a_badge_when_a_turn_has_no_ceiling():
    from tests.unit.test_server_ui import UI_DIR

    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'id="budgetBadge"' in html
    assert "hidden" in html.split('id="budgetBadge"', 1)[1].split(">", 1)[0]
    assert "budgetBadge" in js and "health.budget" in js
    assert ".badge-unbounded" in css
    # The badge must not claim the loop is entirely unprotected: per-request
    # timeouts still apply, and saying otherwise would be a different lie.
    assert "超时" in js


# ══════════════════════════════════════════════════════════════════════════
# 3. an unbounded turn cannot outlive its client
# ══════════════════════════════════════════════════════════════════════════


class BlockingLlm:
    """A model call that never returns, so the turn is still in flight."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()

    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        self.started.set()
        try:
            await asyncio.Event().wait()  # forever
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


def _chat_endpoint(app):
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/chat")


async def _pump_until(agen, predicate, *, timeout: float = 10.0) -> None:
    """Advance the SSE generator until *predicate* holds or it finishes.

    Frames must be consumed for the endpoint's own `while not task.done()` loop
    to progress — `await agen.__anext__()` is what parks the generator and lets
    the turn task run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "回合未在预期时间内推进"
        try:
            await asyncio.wait_for(agen.__anext__(), timeout=max(0.1, deadline - loop.time()))
        except StopAsyncIteration:
            return


def test_client_disconnect_cancels_the_running_turn(tmp_path):
    """The consequence that removing the ceiling created.

    Previously an orphaned turn hit `max_steps_per_turn` and died. With no
    ceiling it would keep spending tokens and holding the worker for as long as
    the process lives, and nothing else would stop it.
    """
    services = make_services(tmp_path)
    blocking = BlockingLlm()
    services.llm._client = blocking
    app = create_app(services)
    endpoint = _chat_endpoint(app)

    async def scenario():
        resp = await endpoint(
            ChatRequest(model_id="m1", text="do something", kind=TurnKind.CREATE)
        )
        agen = resp.body_iterator
        first = await agen.__anext__()
        assert "start" in first
        await _pump_until(agen, lambda: blocking.started.is_set())
        assert not blocking.cancelled.is_set(), "还没断开就不该取消"

        await agen.aclose()  # the client went away
        await asyncio.wait_for(blocking.cancelled.wait(), timeout=5)
        assert blocking.cancelled.is_set()
        assert blocking.finished.is_set()

    asyncio.run(scenario())


def test_the_hook_tap_is_restored_when_the_client_leaves_early(tmp_path):
    """Cleanup has to cover the `start` frame too.

    The turn task is created *after* `start` is yielded, so a disconnect at that
    point closes the generator before its `try` is entered — and a `finally` the
    generator never reaches restores nothing. That is exactly the case a user
    hits by closing the tab while the model is still thinking.
    """
    services = make_services(tmp_path)
    original = services.hooks
    app = create_app(services)
    endpoint = _chat_endpoint(app)

    async def one_request(*, close_after_start_only: bool):
        resp = await endpoint(ChatRequest(model_id="m1", text="x", kind=TurnKind.CREATE))
        agen = resp.body_iterator
        await agen.__anext__()
        if not close_after_start_only:
            # Let the turn itself finish (make_services caps steps at 2), then
            # close the already-exhausted generator.
            await _pump_until(agen, lambda: False, timeout=10)
        await agen.aclose()

    async def scenario():
        # Disconnect immediately after `start` — the earliest possible moment.
        await one_request(close_after_start_only=True)
        assert services.hooks is original, "在 start 帧之后就断开时，tap 未被还原"

        # And the ordinary case: three full requests must not nest taps.
        for _ in range(3):
            await one_request(close_after_start_only=False)
        assert services.hooks is original, "tap 未被还原，会逐请求累积"

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════
# 4. the bounded profile is still reachable
# ══════════════════════════════════════════════════════════════════════════


def test_the_strict_overlay_restores_every_ceiling():
    """The base config is unbounded, so the overlay is the only place ceilings
    come from — it must therefore set all of them."""
    from tcad.config.loader import REPO_ROOT, load_config

    strict = load_config(
        f"{REPO_ROOT}/configs/default.yaml",
        overlays=[f"{REPO_ROOT}/configs/policies/strict.yaml"],
    )
    limits = budget_limits_from_config(strict)
    assert limits.bounded() == list(LIMIT_NAMES), "strict 必须把每一项都限住"
    assert limits.max_steps_per_turn == 12
    assert limits.step_timeout_s is not None, "漏掉它会留下一项无上限"


def test_exhausted_is_still_reachable_when_a_ceiling_is_set(tmp_path):
    """The EXHAUSTED terminal state must not become dead code: it is the
    distinctive outcome of the bounded profile, and the UI reports it as
    explicitly-not-success."""
    from tcad.core.types import TurnState
    from tcad.loop.engine import LoopEngine

    services = make_services(tmp_path, max_steps=2)
    from tcad.tools.base import build_default_registry

    registry = build_default_registry(services)
    engine = LoopEngine(
        services, registry, budget_limits_from_config(services.config), services.loop_config
    )

    async def scenario():
        from tcad.core.types import Thread
        from tcad.loop.engine import UserMessage

        return await engine.run_turn(
            Thread(thread_id="t1", model_id="m1"),
            UserMessage(kind=TurnKind.CREATE, text="x"),
        )

    result = asyncio.run(scenario())
    assert result.state is TurnState.EXHAUSTED
    assert result.state is not TurnState.SUCCEEDED
    assert "max_steps_per_turn" in (result.error or "")
