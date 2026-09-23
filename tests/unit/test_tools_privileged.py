"""Privileged-tool tests (design §4.2 / §4.5).

The headline property: ``raw_python`` is NOT registered by default. It only
enters the registry when explicitly enabled. When it does run, it goes through
the hook dispatch and refuses unless the privileged gate ALLOWs, then executes
in a subprocess with a hard timeout — never in the supervisor, never touching
the live IR.

The second headline, added after a real defect: **whether that subprocess is
sandboxed is a server decision**. It used to be ``args["sandbox"]``, defaulting
to ``False`` — so by default the model got an unsandboxed subprocess, and asking
for a sandbox was the only way to get one. A payload can no longer influence it
in either direction.
"""

from __future__ import annotations

from pathlib import Path
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
from tcad.tools.privileged import (
    _sandbox_decision,
    _sandbox_profile,
    build_privileged_tools,
)


class FakeHooks:
    def __init__(self, decision=HookDecision.ALLOW):
        self.decision = decision
        self.calls = 0

    def dispatch(self, event, payload):
        self.calls += 1
        return HookResult(decision=self.decision, hook_name="test", reason="nope" if self.decision != HookDecision.ALLOW else "")


def _services(hooks, *, sandbox_config=None):
    """``sandbox_config=None`` means "server has not configured a sandbox".

    The handler treats that as sandboxed (the stricter default). Tests that want
    to exercise the subprocess plumbing without a platform sandbox pass an
    explicit ``enabled=False`` — an operator's recorded choice, which is exactly
    the distinction the tool exists to preserve.
    """
    ns = SimpleNamespace(hooks=hooks)
    if sandbox_config is not None:
        ns.config = SimpleNamespace(sandbox=sandbox_config)
    return ns


def _unsandboxed():
    return SimpleNamespace(enabled=False, backend="none")


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


async def test_handler_runs_subprocess_when_allowed_and_the_server_says_unsandboxed():
    hooks = FakeHooks(HookDecision.ALLOW)
    services = _services(hooks, sandbox_config=_unsandboxed())
    spec = build_privileged_tools(services)["raw_python"]
    outcome = await execute_tool(
        spec, {"code": "print(2*3)"}, _ctx(), allowed_tiers={ToolTier.PRIVILEGED}
    )
    assert outcome.result.ok is True
    assert "6" in outcome.result.content


async def test_handler_timeout_is_structured():
    hooks = FakeHooks(HookDecision.ALLOW)
    services = _services(hooks, sandbox_config=_unsandboxed())
    spec = build_privileged_tools(services)["raw_python"]
    outcome = await execute_tool(
        spec, {"code": "import time; time.sleep(2)", "timeout_s": 0.2},
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


# ══════════════════════════════════════════════════════════════════════════
# the sandbox is the server's decision, not the model's
# ══════════════════════════════════════════════════════════════════════════


async def test_a_model_cannot_turn_the_sandbox_off():
    """The old code read ``args["sandbox"]`` with a default of ``False``. So a
    model that wanted no sandbox simply omitted the key and got one — the
    sandbox was opt-IN for a tool whose entire justification is containment.

    Two independent guards refuse it, and this asserts the pair: the declared
    schema is closed (so the arg never reaches a handler) and the handler checks
    again (so a caller that bypasses ``execute_tool`` gets the same answer).
    """
    hooks = FakeHooks(HookDecision.ALLOW)
    services = _services(hooks)  # unconfigured => sandboxed
    spec = build_privileged_tools(services)["raw_python"]

    through_registry = await execute_tool(
        spec, {"code": "print(1)", "sandbox": False}, _ctx(),
        allowed_tiers={ToolTier.PRIVILEGED},
    )
    assert through_registry.result.ok is False
    assert through_registry.result.error.kind == ToolErrorKind.SCHEMA
    assert "sandbox" in through_registry.result.error.message

    # …and the handler itself, with the schema check skipped entirely.
    direct = await spec.handler({"code": "print(1)", "sandbox": False}, _ctx())
    assert direct.ok is False
    assert direct.error.kind == ToolErrorKind.SCHEMA
    assert "server-side policy" in direct.error.message


async def test_the_declared_schema_does_not_offer_a_sandbox_switch():
    """A parameter the model must not set should not be advertised. The schema is
    also closed, so the server-side arg check and the declaration agree."""
    spec = build_privileged_tools(_services(FakeHooks()))["raw_python"]
    props = spec.params_schema["properties"]
    assert "sandbox" not in props
    assert spec.params_schema.get("additionalProperties") is False
    assert "not a parameter" in spec.description or "not a parameter" in (
        spec.description.replace("deliberately ", ""))


def test_the_profile_declares_its_version():
    """``sandbox-exec`` rejects a profile with no ``(version n)`` — exit 65,
    "no version specified" — so every sandboxed run failed *before* the model's
    code was reached. This is a syntax fact, checkable without running code."""
    profile = _sandbox_profile(writable_root="./data/sandbox", no_network=True)
    assert profile.splitlines()[0] == "(version 1)", profile
    assert "(deny default)" in profile


def test_the_profile_confines_writes_to_the_configured_root_and_honours_no_network():
    profile = _sandbox_profile(writable_root="/tmp/only-here", no_network=True)
    assert '(subpath "/tmp/only-here")' in profile
    assert "(deny network*)" in profile

    open_profile = _sandbox_profile(writable_root="/tmp/only-here", no_network=False)
    assert "(deny network*)" not in open_profile


def test_an_unconfigured_server_is_sandboxed_not_permissive():
    """ "Nobody configured a sandbox" must not read as "no sandbox"."""
    assert _sandbox_decision(_services(FakeHooks())).sandbox is True


def test_an_explicit_server_choice_is_honoured():
    d = _sandbox_decision(_services(FakeHooks(), sandbox_config=_unsandboxed()))
    assert d.sandbox is False
    assert d.refuse == ""


def test_an_unimplemented_backend_fails_closed():
    """``bwrap`` is not implemented. Running unsandboxed because the configured
    backend is missing is the one thing this must not do."""
    cfg = SimpleNamespace(enabled=True, backend="bwrap")
    d = _sandbox_decision(_services(FakeHooks(), sandbox_config=cfg))
    assert d.sandbox is False
    assert "not implemented" in d.refuse
