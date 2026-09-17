"""Hook-layer tests: the deterministic, fail-closed safety gate.

Written by the integrator after the original owner was cut off mid-task (it had
produced the implementation but zero tests). This is the layer that decides
whether a dangerous tool call proceeds, so the tests are deliberately hostile:
every failure mode you can reach (crash, timeout, garbage output, unknown
verdict, spawned process that does not exist) must end in DENY, never ALLOW.

The governing rule from the design (§4.5): **fail-closed, never fail-open** — a
hook that breaks must not be the same thing as a hook that approves.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from tcad.core.types import HookDecision, HookEvent, HookResult, HookSpec, ToolTier
from tcad.hooks.approval import JsonFileApprovalStore
from tcad.hooks.dispatcher import HookDispatcher
from tcad.hooks.policy import NetworkGuard, PathGuard, PrivilegedTripleGate

PRE = HookEvent.PRE_TOOL_USE


# ── helpers ───────────────────────────────────────────────────────────────


def script(tmp_path, name: str, body: str):
    """Write a throwaway executable-free python script and return an argv-splittable
    command string pointing at it."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {path}"


def spec(name: str, **kw) -> HookSpec:
    return HookSpec(name=name, events=[PRE], **kw)


# ══════════════════════════════════════════════════════════════════════════
# merge rules and ordering
# ══════════════════════════════════════════════════════════════════════════


def test_deny_beats_ask_beats_allow():
    def deny(e, p):
        return HookResult(decision=HookDecision.DENY, hook_name="d", reason="no")

    def ask(e, p):
        return HookResult(decision=HookDecision.ASK, hook_name="a", reason="maybe")

    def allow(e, p):
        return HookResult(decision=HookDecision.ALLOW, hook_name="o")

    merged = HookDispatcher(
        [spec("d"), spec("a"), spec("o")], {"d": deny, "a": ask, "o": allow}
    ).dispatch(PRE, {"tool_name": "x"})
    assert merged.decision is HookDecision.DENY
    assert merged.hook_name == "d" and merged.reason == "no"

    merged = HookDispatcher([spec("a"), spec("o")], {"a": ask, "o": allow}).dispatch(
        PRE, {"tool_name": "x"}
    )
    assert merged.decision is HookDecision.ASK

    merged = HookDispatcher([spec("o")], {"o": allow}).dispatch(PRE, {"tool_name": "x"})
    assert merged.decision is HookDecision.ALLOW


def test_hooks_run_in_deterministic_order():
    order: list[str] = []

    def make(tag):
        def hook(e, p):
            order.append(tag)
            return HookResult(decision=HookDecision.ALLOW, hook_name=tag)
        return hook

    HookDispatcher(
        [spec("first"), spec("second"), spec("third")],
        {"first": make("first"), "second": make("second"), "third": make("third")},
    ).dispatch(PRE, {})
    assert order == ["first", "second", "third"]


def test_only_hooks_for_the_event_fire():
    fired: list[str] = []

    def hook(e, p):
        fired.append(e.value)
        return HookResult(decision=HookDecision.ALLOW, hook_name="x")

    d = HookDispatcher(
        [HookSpec(name="x", events=[HookEvent.PRE_TURN]), spec("x2")], {"x": hook, "x2": hook}
    )
    d.dispatch(PRE, {})
    assert fired == ["pre_tool_use"], "a pre_turn hook must not fire on pre_tool_use"


def test_hook_set_is_frozen_at_construction():
    d = HookDispatcher([spec("a")], {"a": lambda e, p: HookResult(
        decision=HookDecision.ALLOW, hook_name="a")})
    assert isinstance(d.hooks, tuple)
    # the public surface offers no mutation API
    for forbidden in ("add", "register", "append", "remove", "extend", "clear"):
        assert not hasattr(d, forbidden), f"dispatcher exposes {forbidden}()"


# ══════════════════════════════════════════════════════════════════════════
# fail-closed: every breakage denies
# ══════════════════════════════════════════════════════════════════════════


def test_policy_hook_that_raises_denies():
    def boom(e, p):
        raise RuntimeError("kaboom")

    res = HookDispatcher([spec("boom")], {"boom": boom}).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY
    assert "hook_failed" in res.reason and "kaboom" in res.reason


def test_unresolvable_policy_hook_fails_loudly_at_construction():
    """A hook whose module cannot be imported is a *configuration* error.

    Failing at construction (i.e. at start-up) is deliberate and is the honest
    behaviour: silently denying every call would look like a working harness that
    mysteriously blocks everything. Runtime breakage inside a resolved hook still
    fails closed — see the tests below.
    """
    with pytest.raises(Exception):
        HookDispatcher([spec("ghost", module="definitely.not.a.module:Nope")])


@pytest.mark.parametrize(
    "body,expect_fragment",
    [
        ("import sys,json; sys.exit(3)", "exited 3"),
        ("print('not json at all')", "unparseable"),
        ('print(\'{"decision": "maybe"}\')', "unknown decision"),
        ('print(\'{"nope": 1}\')', "unknown decision"),
        ('print(\'"a string, not an object"\')', "expected an object"),
        ('print(\'[1,2,3]\')', "expected an object"),
    ],
)
def test_broken_command_hooks_all_deny(tmp_path, body, expect_fragment):
    cmd = script(tmp_path, "h.py", body)
    res = HookDispatcher([spec("c", kind="command", command=cmd)]).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY, body
    assert expect_fragment in res.reason, (expect_fragment, res.reason)


def test_command_hook_timeout_denies(tmp_path):
    cmd = script(tmp_path, "slow.py", "import time; time.sleep(30)")
    res = HookDispatcher(
        [spec("c", kind="command", command=cmd, timeout_s=0.4)]
    ).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY
    assert "timed out" in res.reason


def test_nonexistent_command_denies(tmp_path):
    res = HookDispatcher(
        [spec("c", kind="command", command=str(tmp_path / "does-not-exist-xyz"))]
    ).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY


def test_command_hook_with_no_command_denies():
    res = HookDispatcher([spec("c", kind="command", command=None)]).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY


def test_hook_returning_garbage_denies():
    res = HookDispatcher([spec("g")], {"g": lambda e, p: "allow? sure"}).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY


def test_hook_returning_none_denies():
    res = HookDispatcher([spec("n")], {"n": lambda e, p: None}).dispatch(PRE, {})
    assert res.decision is HookDecision.DENY


# ── command hooks that work ───────────────────────────────────────────────


def test_working_command_hook_can_allow_ask_and_deny(tmp_path):
    body = (
        "import json,sys\n"
        "req = json.load(sys.stdin)\n"
        "tool = req['payload'].get('tool_name')\n"
        "verdict = {'ok': 'allow', 'hmm': 'ask', 'no': 'deny'}.get(tool, 'deny')\n"
        "json.dump({'decision': verdict, 'reason': f'tool={tool}'}, sys.stdout)\n"
    )
    cmd = script(tmp_path, "h.py", body)
    d = HookDispatcher([spec("c", kind="command", command=cmd)])
    assert d.dispatch(PRE, {"tool_name": "ok"}).decision is HookDecision.ALLOW
    got = d.dispatch(PRE, {"tool_name": "hmm"})
    assert got.decision is HookDecision.ASK and got.reason == "tool=hmm"
    assert d.dispatch(PRE, {"tool_name": "no"}).decision is HookDecision.DENY


def test_command_hook_receives_event_and_payload(tmp_path):
    body = (
        "import json,sys\n"
        "req = json.load(sys.stdin)\n"
        "json.dump({'decision':'allow','reason':req['event']+'|'+str(req['payload'].get('n'))},"
        " sys.stdout)\n"
    )
    cmd = script(tmp_path, "echo.py", body)
    res = HookDispatcher([spec("c", kind="command", command=cmd)]).dispatch(PRE, {"n": 7})
    assert res.reason == "pre_tool_use|7"


def test_command_hook_can_rewrite_arguments(tmp_path):
    body = (
        "import json,sys\n"
        "json.dump({'decision':'allow','mutated_args':{'n': 99}}, sys.stdout)\n"
    )
    cmd = script(tmp_path, "rw.py", body)
    res = HookDispatcher([spec("c", kind="command", command=cmd)]).dispatch(PRE, {})
    assert res.decision is HookDecision.ALLOW
    assert res.mutated_args == {"n": 99}


# ══════════════════════════════════════════════════════════════════════════
# PathGuard
# ══════════════════════════════════════════════════════════════════════════


def test_path_guard_denies_a_matching_path():
    g = PathGuard(["/etc/**"], extractor=lambda p: [p.get("path", "")])
    assert g(PRE, {"path": "/etc/passwd"}).decision is HookDecision.DENY
    assert g(PRE, {"path": "/tmp/safe.txt"}).decision is HookDecision.ALLOW


def test_path_guard_resolves_traversal_and_symlinks(tmp_path):
    """A glob that only string-matches is a bug: '../../etc/passwd' would slip
    through. The guard must resolve first."""
    g = PathGuard(["/etc/**"], extractor=lambda p: [p.get("path", "")])
    assert g(PRE, {"path": "/tmp/../etc/passwd"}).decision is HookDecision.DENY

    link = tmp_path / "sneaky"
    link.symlink_to("/etc")
    assert g(PRE, {"path": str(link / "passwd")}).decision is HookDecision.DENY


def test_path_guard_expands_tilde_in_globs(tmp_path):
    """Regression: `~/.ssh/**` — a glob the default policy actually ships — used
    to compile into a pattern that matched nothing, ever, because `~` was never
    expanded. A deny-rule that silently never fires is worse than no rule."""
    import os

    home = os.path.expanduser("~")
    g = PathGuard(["~/.ssh/**"], extractor=lambda p: [p.get("path", "")])
    assert g(PRE, {"path": f"{home}/.ssh/id_rsa"}).decision is HookDecision.DENY
    assert g(PRE, {"path": f"{home}/.ssh"}).decision is HookDecision.DENY
    assert g(PRE, {"path": f"{home}/notes.txt"}).decision is HookDecision.ALLOW


def test_path_guard_ignores_non_path_arguments():
    g = PathGuard(["/etc/**"], extractor=lambda p: [p.get("path", "")])
    assert g(PRE, {"path": ""}).decision is HookDecision.ALLOW
    assert g(PRE, {}).decision is HookDecision.ALLOW


# ══════════════════════════════════════════════════════════════════════════
# NetworkGuard
# ══════════════════════════════════════════════════════════════════════════


def test_network_guard_allows_when_permitted_and_denies_when_not():
    tool = {"tool_name": "net_fetch", "tier": ToolTier.PRIVILEGED.value,
            "network_capable": True}
    assert NetworkGuard(True)(PRE, tool).decision is HookDecision.ALLOW
    got = NetworkGuard(False)(PRE, tool)
    assert got.decision is HookDecision.DENY
    assert "net_fetch" in got.reason


def test_network_guard_lets_non_network_tools_through():
    """The guard must not become a blanket deny on the read tier."""
    res = NetworkGuard(False)(PRE, {"tool_name": "ir_digest", "tier": ToolTier.READ.value})
    assert res.decision is HookDecision.ALLOW


def test_network_guard_can_name_network_tools_explicitly():
    g = NetworkGuard(False, network_tools=["net_fetch"])
    assert g(PRE, {"tool_name": "net_fetch"}).decision is HookDecision.DENY
    assert g(PRE, {"tool_name": "ir_digest"}).decision is HookDecision.ALLOW


# ══════════════════════════════════════════════════════════════════════════
# PrivilegedTripleGate — defence in depth
# ══════════════════════════════════════════════════════════════════════════


class _Approval:
    """An approval object. ``expires_at`` MUST be a real datetime: the gate
    rejects anything whose expiry it cannot compare (a missing/None expiry counts
    as invalid, which is the right fail-closed default)."""

    def __init__(self, granted=True, tool_name="raw_python", expired=False):
        from datetime import datetime, timedelta, timezone

        self.granted = granted
        self.tool_name = tool_name
        now = datetime.now(timezone.utc)
        self.expires_at = now - timedelta(hours=1) if expired else now + timedelta(hours=1)


PRIV_PAYLOAD = {"tool_name": "raw_python", "tier": ToolTier.PRIVILEGED.value}


def _gate(allow=True, approval=_Approval(), sandbox=True):
    return PrivilegedTripleGate(
        allow_privileged=allow,
        approval_lookup=lambda tn: approval,
        sandbox_ok=lambda: sandbox,
    )


def test_triple_gate_allows_only_when_everything_holds():
    res = _gate()(PRE, PRIV_PAYLOAD)
    assert res.decision is HookDecision.ALLOW


@pytest.mark.parametrize(
    "kwargs,expected_fragment",
    [
        ({"allow": False}, "static_config"),
        ({"approval": None}, "approval"),
        ({"approval": _Approval(granted=False)}, "approval"),
        ({"sandbox": False}, "sandbox"),
    ],
)
def test_triple_gate_denies_when_any_single_condition_fails(kwargs, expected_fragment):
    res = _gate(**kwargs)(PRE, PRIV_PAYLOAD)
    assert res.decision is HookDecision.DENY
    assert expected_fragment in res.reason, (expected_fragment, res.reason)


def test_triple_gate_ignores_non_privileged_calls():
    res = _gate(allow=False, approval=None, sandbox=False)(
        PRE, {"tool_name": "geo_view", "tier": ToolTier.READ.value}
    )
    assert res.decision is HookDecision.ALLOW, "the gate must not block normal tools"


def test_triple_gate_does_not_trust_the_approval_object():
    """Even if the lookup hands back something expired, the gate re-checks."""
    res = _gate(approval=_Approval(expired=True))(PRE, PRIV_PAYLOAD)
    assert res.decision is HookDecision.DENY
    assert "approval" in res.reason


# ══════════════════════════════════════════════════════════════════════════
# approval store
# ══════════════════════════════════════════════════════════════════════════


def test_approval_must_be_granted_before_it_is_valid(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=60)
    rec = store.request("raw_python", args_hash="h1")
    assert store.lookup_valid("raw_python", "h1") is None, "ungranted must not be valid"
    store.resolve(rec.id, granted=True)
    found = store.lookup_valid("raw_python", "h1")
    assert found is not None and found.granted is True


def test_approval_is_scoped_to_the_exact_arguments(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=60)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=True)
    assert store.lookup_valid("raw_python", "h2") is None, "a different payload must not inherit the grant"
    assert store.lookup_valid("raw_python", "h1") is not None


def test_approval_is_scoped_to_the_tool(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=60)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=True)
    assert store.lookup_valid("net_fetch", "h1") is None


def test_approval_expires(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=0.2)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=True)
    assert store.lookup_valid("raw_python", "h1") is not None
    time.sleep(0.35)
    assert store.lookup_valid("raw_python", "h1") is None, "an expired grant must not be honoured"


def test_approval_denial_is_final(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=60)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=False)
    assert store.lookup_valid("raw_python", "h1") is None


def test_approval_store_persists_across_instances(tmp_path):
    path = str(tmp_path / "a.json")
    store = JsonFileApprovalStore(path, ttl_s=60)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=True)
    # a fresh instance reads the same file
    other = JsonFileApprovalStore(path, ttl_s=60)
    assert other.lookup_valid("raw_python", "h1") is not None


def test_expire_stale_reports_how_many_it_dropped(tmp_path):
    store = JsonFileApprovalStore(str(tmp_path / "a.json"), ttl_s=0.2)
    rec = store.request("raw_python", args_hash="h1")
    store.resolve(rec.id, granted=True)
    time.sleep(0.35)
    assert store.expire_stale() >= 1
    assert store.lookup_valid("raw_python", "h1") is None
