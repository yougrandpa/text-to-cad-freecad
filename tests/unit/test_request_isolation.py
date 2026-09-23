"""Per-request isolation of hooks and visual state (task book §5-E).

``services`` is a single bundle shared by every turn in the process. Two pieces
of per-turn state used to live on it:

  * the front end **replaced** ``services.hooks`` with an observer tap for the
    duration of a request and restored it in ``finally``. With two concurrent
    requests, each overwrote the other's tap, and whichever finished first
    restored the original dispatcher while the other was still mid-turn — that
    turn silently lost its lifecycle events and its hook decisions.
  * the engine wrote ``services._visual_ok`` before every tool call, so a commit
    that passed in one session opened the ``geo_view`` checkpoint for another
    session's step.

Both now travel on :class:`tcad.core.types.ToolContext`, which is created per
turn. These tests are deterministic and use no FreeCAD.
"""

from __future__ import annotations

from pathlib import Path

from tcad.core.types import HookDecision, HookEvent, HookResult, ToolResult, ToolSpec, ToolTier
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.core.types import Thread, TurnKind
from tcad.tools.base import ToolRegistry
from tcad.llm.client import LlmReply, ToolCall

from tests.unit.test_loop_engine import ScriptedLlm, make_ir, make_services


class RecordingTap:
    """Records the events it saw, then delegates unchanged (observation only)."""

    def __init__(self, inner):
        self.inner = inner
        self.events: list[HookEvent] = []

    def dispatch(self, event, payload):
        self.events.append(event)
        return self.inner.dispatch(event, payload)


def _probe_registry(seen: list[dict]) -> ToolRegistry:
    """A registry whose only tool records the ToolContext it was called with."""

    async def handler(args, ctx):
        seen.append({
            "visual_ok": ctx.visual_ok,
            "hooks": ctx.hooks,
            "thread_id": ctx.thread_id,
        })
        return ToolResult(ok=True, content="probed")

    reg = ToolRegistry()
    reg.register_tool(ToolSpec(
        name="probe", tier=ToolTier.READ, description="records its context",
        params_schema={"type": "object", "properties": {}}, handler=handler,
        concurrency_safe=True,
    ))
    return reg


def _engine(services, registry, *, hooks=None, kind=TurnKind.CREATE, reply_text="done"):
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="probe", args={})])])
    services.llm = llm
    return LoopEngine(
        services, registry, BudgetLimits(max_steps_per_turn=3),
        LoopConfig(data_dir="/tmp"), hooks=hooks,
    ), llm


# ══════════════════════════════════════════════════════════════════════════
# 1. hooks are per turn, not per process
# ══════════════════════════════════════════════════════════════════════════


async def test_each_turn_dispatches_through_its_own_tap():
    """Two turns on one services bundle must not share an observer.

    On the old code the engine read ``services.hooks`` at call time, so an
    ``hooks=`` argument was ignored and only a request that mutated the shared
    bundle could observe anything — which is exactly the cross-request pollution
    this pins shut.
    """
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    base = services.hooks
    tap_a, tap_b = RecordingTap(base), RecordingTap(base)

    engine_a, _ = _engine(services, _probe_registry([]), hooks=tap_a)
    engine_b, _ = _engine(services, _probe_registry([]), hooks=tap_b)

    await engine_a.run_turn(Thread(thread_id="th-a", model_id="m1"),
                            UserMessage(kind=TurnKind.CREATE, text="a"))
    assert tap_a.events, "the first turn's tap saw nothing"
    assert tap_b.events == [], "a second turn's tap saw the first turn's events"

    seen_by_a = list(tap_a.events)
    await engine_b.run_turn(Thread(thread_id="th-b", model_id="m1"),
                            UserMessage(kind=TurnKind.CREATE, text="b"))
    assert tap_b.events, "the second turn's tap saw nothing"
    assert tap_a.events == seen_by_a, "the second turn leaked into the first turn's tap"

    # ...and the shared bundle was never reassigned.
    assert services.hooks is base, "the shared services.hooks must not be replaced"


async def test_the_engine_still_works_without_a_per_turn_tap():
    """The default path (shared bundle) must keep working."""
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    engine, _ = _engine(services, _probe_registry([]), hooks=None)
    result = await engine.run_turn(Thread(thread_id="th", model_id="m1"),
                                   UserMessage(kind=TurnKind.CREATE, text="x"))
    assert result.state.value in {"running", "failed", "exhausted", "succeeded"}


# ══════════════════════════════════════════════════════════════════════════
# 2. the visual checkpoint is per step, not per process
# ══════════════════════════════════════════════════════════════════════════


async def test_visual_permission_lives_on_the_turn_context_only():
    seen: list[dict] = []
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    services.llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="probe", args={})])])
    engine = LoopEngine(services, _probe_registry(seen), BudgetLimits(max_steps_per_turn=3),
                        LoopConfig(data_dir="/tmp"))

    await engine.run_turn(Thread(thread_id="th", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="build"))
    assert seen, "the probe tool never ran"
    assert seen[0]["visual_ok"] is False, (
        "a create turn with no passed commit must not be a visual checkpoint")
    assert not hasattr(services, "_visual_ok"), (
        "the shared services bundle must not carry per-turn visual state")


async def test_an_inspect_turn_is_a_visual_checkpoint():
    seen: list[dict] = []
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    engine, _ = _engine(services, _probe_registry(seen))
    await engine.run_turn(Thread(thread_id="th", model_id="m1"),
                          UserMessage(kind=TurnKind.INSPECT, text="show me"))
    assert seen and seen[0]["visual_ok"] is True
    assert not hasattr(services, "_visual_ok")


async def test_geo_view_reads_the_turn_context_not_the_shared_bundle(tmp_path):
    """The gate must consult the per-step flag, and the bundle only as fallback."""
    from types import SimpleNamespace

    from tcad.tools.geo_tools import geo_view_handler

    def ctx_with(visual_ok: bool):
        class _Ctx:
            model_id = "m"
            data_dir = str(tmp_path)
            thread_id = "th"
            turn_id = "tn"
        _Ctx.visual_ok = visual_ok
        return _Ctx()

    class _Ir:
        def model_dump(self):
            return {}

    def services_with(bundle_visual: bool | None = None):
        ns = SimpleNamespace(
            store=SimpleNamespace(current_version=lambda m: 0, load=lambda *a, **k: _Ir()),
            worker=SimpleNamespace(request=lambda *a, **k: {
                "ok": False, "error": {"kind": "runtime", "message": "reached the worker"}}),
        )
        if bundle_visual is not None:
            ns._visual_ok = bundle_visual
        return ns

    # 1. Neither says yes -> denied at the checkpoint gate.
    denied = await geo_view_handler(services_with(False), {}, ctx_with(False))
    assert denied.ok is False
    assert "visual checkpoint" in (denied.error.message or "")

    # 2. THIS STEP says yes, the shared bundle says nothing -> must be allowed.
    #    This is the case that catches an implementation still reading only the
    #    bundle: one session's commit opening another session's checkpoint.
    stepped = await geo_view_handler(services_with(None), {}, ctx_with(True))
    assert "reached the worker" in (stepped.error.message or ""), (
        "a per-step visual checkpoint was ignored in favour of the shared bundle")
    assert "visual checkpoint" not in (stepped.error.message or "")

    # 3. Back-compat: an embedder that drives the tool directly may still use the
    #    old shared spelling.
    legacy = await geo_view_handler(services_with(True), {}, ctx_with(False))
    assert "reached the worker" in (legacy.error.message or "")
    assert "visual checkpoint" not in (legacy.error.message or "")


# ══════════════════════════════════════════════════════════════════════════
# 3. structural guard: the server must not reassign the shared dispatcher
# ══════════════════════════════════════════════════════════════════════════


def test_the_server_never_assigns_services_hooks():
    """A source-level guard, because the runtime symptom is a race.

    Reintroducing ``s.hooks = tap`` compiles and passes every single-request
    test; it only breaks under concurrency. Asserting the pattern is absent is
    the deterministic way to keep it out (the same technique
    ``tests/unit/test_server_ui.py`` uses for the front end).
    """
    source = Path(__file__).resolve().parents[2].joinpath("tcad/server/app.py").read_text(
        encoding="utf-8")
    assert "s.hooks = tap" not in source
    assert "s.hooks = original_hooks" not in source
    # The tap must still be created and handed to the engine.
    assert "HookEventTap(s.hooks)" in source
    assert "hooks=tap" in source
