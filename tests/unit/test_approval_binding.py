"""Approvals are bound to the exact call, not just the tool name (task §5-E).

``ApprovalStore.lookup_valid`` always accepted an ``args_hash``, but the
production wiring called it as ``lambda tool_name: lookup_valid(tool_name)``, and
the gate only ever handed it a tool name. So a user granting one ``raw_python``
call — for one specific piece of code, that they actually read — authorised
**any** ``raw_python`` payload for the whole TTL. "Approve this" silently meant
"approve this tool until the record expires".

These tests are deterministic and need no FreeCAD.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tcad.config.loader import load_default_config
from tcad.core.types import HookDecision, HookEvent
from tcad.hooks.approval import JsonFileApprovalStore, args_fingerprint
from tcad.hooks.policy import PrivilegedTripleGate
from tcad.core.wiring import build_hooks
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine

from tests.unit.test_loop_engine import ScriptedLlm, make_ir, make_services


CODE_A = {"code": "print('a')", "timeout_s": 5}
CODE_B = {"code": "print('b')", "timeout_s": 5}


def _granted():
    """A minimal ApprovalLike that satisfies granted + unexpired."""
    return type("R", (), {
        "granted": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    })()


# ══════════════════════════════════════════════════════════════════════════
# 1. the fingerprint itself
# ══════════════════════════════════════════════════════════════════════════


def test_fingerprint_is_order_independent_but_value_sensitive():
    assert args_fingerprint({"a": 1, "b": 2}) == args_fingerprint({"b": 2, "a": 1})
    assert args_fingerprint({"a": 1}) != args_fingerprint({"a": 2})
    assert args_fingerprint({"code": "x"}) != args_fingerprint({"code": "x "})
    assert args_fingerprint(None) == args_fingerprint({})
    assert args_fingerprint({"a": 1}).startswith("sha256:")


def test_fingerprint_handles_non_json_values():
    """Pydantic may hand through Path/datetime; the digest must not blow up."""
    fp = args_fingerprint({"path": Path("/tmp/x"), "n": 3})
    assert isinstance(fp, str) and len(fp) > len("sha256:")


# ══════════════════════════════════════════════════════════════════════════
# 2. the gate binds to the payload it was handed
# ══════════════════════════════════════════════════════════════════════════


def _gate(lookup, *, allow=True, sandbox=True):
    return PrivilegedTripleGate(
        allow_privileged=allow, approval_lookup=lookup, sandbox_ok=lambda: sandbox
    )


def _payload(args):
    return {
        "tool_name": "raw_python", "tier": "privileged", "args": args,
        "thread_id": "th", "turn_id": "tn", "model_id": "m",
    }


def test_an_approval_for_one_payload_does_not_authorise_another():
    granted = args_fingerprint(CODE_A)
    seen: list[tuple] = []

    def lookup(tool_name, args_hash=None):
        seen.append((tool_name, args_hash))
        if tool_name == "raw_python" and args_hash == granted:
            return _granted()
        return None

    gate = _gate(lookup)
    allow = gate(HookEvent.PRE_TOOL_USE, _payload(CODE_A))
    deny = gate(HookEvent.PRE_TOOL_USE, _payload(CODE_B))

    assert allow.decision == HookDecision.ALLOW, allow.reason
    assert "tool+args" in allow.reason
    assert deny.decision == HookDecision.DENY, deny.reason
    assert "no_valid_approval" in deny.reason
    # The gate computed the fingerprint itself — the caller is not trusted to.
    assert seen[0] == ("raw_python", granted)


def test_a_one_argument_lookup_still_works_and_admits_the_weakening():
    gate = _gate(lambda tool_name: _granted())
    res = gate(HookEvent.PRE_TOOL_USE, _payload(CODE_A))
    assert res.decision == HookDecision.ALLOW
    assert "tool only" in res.reason


# ══════════════════════════════════════════════════════════════════════════
# 3. the PRODUCTION wiring binds (this is the regression)
# ══════════════════════════════════════════════════════════════════════════


def _privileged_cfg(tmp_path):
    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path / "data")
    cfg.policy.allow_privileged = True
    # A sandbox probe that really runs and really succeeds, so condition 3 holds
    # and the only thing under test is the approval.
    cfg.policy.sandbox_probe = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    return cfg


def test_wired_gate_denies_a_different_payload_under_a_granted_approval(tmp_path):
    cfg = _privileged_cfg(tmp_path)
    hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)

    rec = approvals.request("raw_python", args_hash=args_fingerprint(CODE_A))
    approvals.resolve(rec.id, granted=True)

    allowed = hooks.dispatch(HookEvent.PRE_TOOL_USE, _payload(CODE_A))
    blocked = hooks.dispatch(HookEvent.PRE_TOOL_USE, _payload(CODE_B))

    assert allowed.decision == HookDecision.ALLOW, allowed.reason
    assert blocked.decision == HookDecision.DENY, (
        "an approval for one payload authorised a different one — the wiring is "
        "matching by tool name only again")
    assert "no_valid_approval" in blocked.reason


def test_wired_gate_denies_an_approval_that_was_never_granted(tmp_path):
    cfg = _privileged_cfg(tmp_path)
    hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)
    approvals.request("raw_python", args_hash=args_fingerprint(CODE_A))

    res = hooks.dispatch(HookEvent.PRE_TOOL_USE, _payload(CODE_A))
    assert res.decision == HookDecision.DENY
    assert "no_valid_approval" in res.reason


# ══════════════════════════════════════════════════════════════════════════
# 4. the engine and the gate must compute the SAME fingerprint
# ══════════════════════════════════════════════════════════════════════════


def test_a_request_created_by_the_engine_is_the_one_the_gate_accepts(tmp_path):
    """Round-trips through the real store: engine writes, gate checks.

    If the two ever computed the digest differently, the gate would deny a call
    the user just approved — and the failure would look like "approvals are
    broken", not "the two hashes disagree".
    """
    cfg = _privileged_cfg(tmp_path)
    hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)

    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    services.approvals = approvals
    engine = LoopEngine(services, None, BudgetLimits(), LoopConfig(data_dir=str(tmp_path)))

    approval_id = engine._request_approval("raw_python", CODE_A)
    assert approval_id, "the engine did not create an approval record"
    approvals.resolve(approval_id, granted=True)

    assert hooks.dispatch(HookEvent.PRE_TOOL_USE, _payload(CODE_A)).decision == HookDecision.ALLOW
    assert hooks.dispatch(HookEvent.PRE_TOOL_USE, _payload(CODE_B)).decision == HookDecision.DENY


# ══════════════════════════════════════════════════════════════════════════
# 5. session binding + a payload a human can actually read
# ══════════════════════════════════════════════════════════════════════════


def _payload_in(thread_id: str, args):
    p = _payload(args)
    p["thread_id"] = thread_id
    return p


def test_an_approval_does_not_carry_into_another_session(tmp_path):
    cfg = _privileged_cfg(tmp_path)
    hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)

    rec = approvals.request("raw_python", args_hash=args_fingerprint(CODE_A),
                            thread_id="th-1", turn_id="tn-1")
    approvals.resolve(rec.id, granted=True)

    assert hooks.dispatch(
        HookEvent.PRE_TOOL_USE, _payload_in("th-1", CODE_A)).decision == HookDecision.ALLOW
    assert hooks.dispatch(
        HookEvent.PRE_TOOL_USE, _payload_in("th-2", CODE_A)).decision == HookDecision.DENY


def test_a_legacy_unscoped_approval_still_matches(tmp_path):
    """A file written before session binding existed must keep working."""
    cfg = _privileged_cfg(tmp_path)
    hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)

    rec = approvals.request("raw_python", args_hash=args_fingerprint(CODE_A))  # no thread
    approvals.resolve(rec.id, granted=True)

    assert hooks.dispatch(
        HookEvent.PRE_TOOL_USE, _payload_in("any-session", CODE_A)).decision == HookDecision.ALLOW


def test_the_operator_can_see_what_they_are_approving(tmp_path):
    """An approval that only shows a tool name is a signature on a blank page."""
    cfg = _privileged_cfg(tmp_path)
    _hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)

    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    services.approvals = approvals
    engine = LoopEngine(services, None, BudgetLimits(), LoopConfig(data_dir=str(tmp_path)))

    approval_id = engine._request_approval(
        "raw_python", CODE_A, thread_id="th-42", turn_id="tn-7")
    assert approval_id

    rec = next(r for r in approvals._load() if r.id == approval_id)
    assert rec.args_summary and "print('a')" in rec.args_summary
    assert rec.thread_id == "th-42"
    assert rec.turn_id == "tn-7"
    assert rec.args_hash == args_fingerprint(CODE_A)
    # What the operator sees must be the payload, from the API dump too.
    dumped = rec.model_dump(mode="json")
    assert "print('a')" in dumped["args_summary"]


def test_the_stored_summary_is_bounded(tmp_path):
    cfg = _privileged_cfg(tmp_path)
    _hooks, approvals = build_hooks(cfg, cfg.storage.data_dir)
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    services.approvals = approvals
    engine = LoopEngine(services, None, BudgetLimits(), LoopConfig(data_dir=str(tmp_path)))

    huge = {"code": "x" * 20000}
    aid = engine._request_approval("raw_python", huge)
    rec = next(r for r in approvals._load() if r.id == aid)
    assert len(rec.args_summary) < 5000
    # ...and the fingerprint still covers the whole payload, not the truncation.
    assert rec.args_hash == args_fingerprint(huge)
