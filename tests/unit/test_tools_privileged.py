"""Privileged-tool tests (design §4.2 / §4.5).

The headline property: ``raw_python`` is NOT registered by default. It only
enters the registry when explicitly enabled. When it does run, it goes through
the hook dispatch and refuses unless the privileged gate ALLOWs, then executes
in a subprocess with a hard timeout — never in the supervisor, never touching
the live IR.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tcad.core.types import (
    HookDecision,
    HookEvent,
    HookResult,
    ToolContext,
    ToolErrorKind,
    ToolTier,
    TurnKind,
)
from tcad.tools.base import build_default_registry, execute_tool
from tcad.tools.privileged import build_privileged_tools


class FakeHooks:
    def __init__(self, decision=HookDecision.ALLOW):
        self.decision = decision
        self.calls = 0

    def dispatch(self, event, payload):
        self.calls += 1
        return HookResult(decision=self.decision, hook_name="test", reason="nope" if self.decision != HookDecision.ALLOW else "")


def _services(hooks):
    return SimpleNamespace(hooks=hooks)


def _ctx() -> ToolContext:
    return ToolContext(thread_id="t", turn_id="t", model_id="m")


def test_raw_python_not_registered_by_default():
    reg = build_default_registry(_services(FakeHooks()))
    assert reg.get("raw_python") is None
    assert "raw_python" not in set(reg.names_for(TurnKind.CREATE))


def test_raw_python_present_when_enabled():
    reg = build_default_registry(_services(FakeHooks()), enable_privileged=True)
    spec = reg.get("raw_python")
    assert spec is not None
    assert spec.tier == ToolTier.PRIVILEGED


async def test_handler_refuses_when_gate_denies():
    hooks = FakeHooks(HookDecision.DENY)
    services = _services(hooks)
    spec = build_privileged_tools(services)["raw_python"]
    outcome = await execute_tool(
        spec, {"code": "print(1)"}, _ctx(), allowed_tiers={ToolTier.PRIVILEGED}
    )
    assert outcome.result.ok is False
    assert outcome.result.error.kind == ToolErrorKind.DENIED
    assert hooks.calls >= 1  # it went through the hook dispatch


async def test_handler_runs_subprocess_when_allowed():
    hooks = FakeHooks(HookDecision.ALLOW)
    services = _services(hooks)
    spec = build_privileged_tools(services)["raw_python"]
    outcome = await execute_tool(
        spec, {"code": "print(2*3)", "sandbox": False}, _ctx(), allowed_tiers={ToolTier.PRIVILEGED}
    )
    assert outcome.result.ok is True
    assert "6" in outcome.result.content


async def test_handler_timeout_is_structured():
    hooks = FakeHooks(HookDecision.ALLOW)
    services = _services(hooks)
    spec = build_privileged_tools(services)["raw_python"]
    outcome = await execute_tool(
        spec, {"code": "import time; time.sleep(2)", "sandbox": False, "timeout_s": 0.2},
        _ctx(), allowed_tiers={ToolTier.PRIVILEGED},
    )
    assert outcome.result.ok is False
    assert outcome.result.error.kind == ToolErrorKind.TIMEOUT


async def test_privileged_refused_when_not_in_kind_subset():
    # Even if registered, the engine only passes PRIVILEGED in `allowed_tiers`
    # when explicitly enabled — otherwise execute_tool refuses it outright.
    hooks = FakeHooks(HookDecision.ALLOW)
    spec = build_privileged_tools(_services(hooks))["raw_python"]
    outcome = await execute_tool(spec, {"code": "print(1)"}, _ctx(), allowed_tiers={ToolTier.READ})
    assert outcome.result.ok is False
    assert outcome.result.error.kind == ToolErrorKind.DENIED
