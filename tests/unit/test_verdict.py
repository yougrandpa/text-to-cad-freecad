"""The current verdict must be durable and must not inherit a stale pass (R-3).

A persisted ``GateReport`` answers "was version N graded, and how?". The question
a person asks is "is what I am looking at verified?". Those differ the moment a
write follows a pass: the report for N still says passed (true — N *was*
verified) while the model has moved to N+1, which nothing has graded. Reading the
old report as "current" is how a stale success gets displayed as a live one.

Deterministic; no FreeCAD.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.config.schema import Config
from tcad.core.types import (
    GateReport,
    HookDecision,
    HookEvent,
    HookResult,
    Thread,
    ToolResult,
    ToolSpec,
    ToolTier,
    TurnKind,
)
from tcad.core.wiring import StoreAdapter
from tcad.hooks.dispatcher import HookDispatcher
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.server.app import create_app
from tcad.store.artifacts import build_verdict, write_gate_report

from tests.unit.test_loop_engine import ScriptedLlm, make_ir, make_services


# ══════════════════════════════════════════════════════════════════════════
# fixtures: what a real passing build leaves on disk
# ══════════════════════════════════════════════════════════════════════════


def _seed_model(store: StoreAdapter, model_id: str = "m") -> int:
    created = store.create(model_id, IrDocument(model_id=model_id, version=0))
    return int(created.version)


def _simulate_published_build(store: StoreAdapter, model_id: str, version: int, *,
                              passed: bool = True, attempt: str = "att-1") -> None:
    """Write exactly what run_commit writes for one attempt.

    A passing attempt publishes (stamp + manifest + a deliverable) and persists
    its report; a failing attempt persists only the report.
    """
    report = GateReport(
        model_id=model_id, ir_version=version, passed=passed,
        blocking_failures=[] if passed else ["solid_validity"],
        advisory_findings=[], attempt_id=attempt,
    )
    write_gate_report(store.data_dir, model_id, version, report)
    if not passed:
        return
    d = store.artifact_dir(model_id, version)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{model_id}.step").write_text("ISO-10303-21;", encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({
        "model_id": model_id, "ir_version": version, "attempt_id": attempt,
        "files": {f"{model_id}.step": {"sha256": "x", "bytes": 13}},
    }), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 1. build_verdict: verified means verified *now*
# ══════════════════════════════════════════════════════════════════════════


def test_never_graded_is_not_verified(tmp_path):
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    v = build_verdict(tmp_path, "m", 0)
    assert v["verified"] is False
    assert v["passed"] is None and v["graded_version"] is None
    assert "never been graded" in v["reason"]


def test_a_passing_report_with_published_artifacts_verifies(tmp_path):
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    _simulate_published_build(store, "m", 0)
    v = build_verdict(tmp_path, "m", 0)
    assert v["verified"] is True
    assert v["passed"] is True and v["verdict_is_current"] is True
    assert v["artifacts_published"] is True and v["artifact_manifest"] is True
    assert v["attempt_id"] == "att-1"
    assert "passed the Gate and its artifacts are published" in v["reason"]


def test_a_write_after_a_pass_leaves_the_new_version_unverified(tmp_path):
    """v1 passed, then a write moved the model to v2 with no grading of its own.

    The new version must read as ungraded — and the version that really was
    verified must still read as verified, because a stale verdict is not an
    erased one. (The wrong-version-report case, where currency itself is what
    saves us, is covered separately below.)
    """
    store = StoreAdapter(tmp_path)
    v1 = _seed_model(store)
    _simulate_published_build(store, "m", v1, attempt="att-v1")

    # The write: a new version with no report of its own.
    patched = store.load("m").model_copy(deep=True)
    patched.version = v1 + 1
    store.ir_store._write_snapshot("m", patched)

    stale = build_verdict(tmp_path, "m", v1 + 1)
    assert stale["verified"] is False
    assert stale["passed"] is None
    assert stale["graded_version"] is None
    assert "never been graded" in stale["reason"]
    assert build_verdict(tmp_path, "m", v1)["verified"] is True


def test_a_report_for_another_version_is_explicitly_stale(tmp_path):
    """If a caller asks about a version whose report is from elsewhere."""
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    _simulate_published_build(store, "m", 0)
    # Copy v0's artifacts+report under v1 with a report that still says v0.
    d1 = store.artifact_dir("m", 1)
    d1.mkdir(parents=True, exist_ok=True)
    (d1 / "m.step").write_text("ISO-10303-21;", encoding="utf-8")
    report = json.loads((store.data_dir / "gate_reports" / "m" / "v0.json").read_text())
    (store.data_dir / "gate_reports" / "m" / "v1.json").write_text(json.dumps(report))

    v = build_verdict(tmp_path, "m", 1)
    assert v["verified"] is False
    assert v["verdict_is_current"] is False
    assert "stale verdict" in v["reason"]


def test_a_passing_report_without_published_artifacts_is_not_verified(tmp_path):
    """A green report alone is not a delivery."""
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    write_gate_report(tmp_path, "m", 0,
                      GateReport(model_id="m", ir_version=0, passed=True, attempt_id="att-x"))
    v = build_verdict(tmp_path, "m", 0)
    assert v["verified"] is False
    assert v["passed"] is True and v["verdict_is_current"] is True
    assert v["artifacts_published"] is False
    assert "no artifacts are published" in v["reason"]


def test_a_failed_report_reports_the_blocking_failures(tmp_path):
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    _simulate_published_build(store, "m", 0, passed=False, attempt="att-bad")
    v = build_verdict(tmp_path, "m", 0)
    assert v["verified"] is False
    assert v["passed"] is False
    assert v["blocking_failures"] == ["solid_validity"]
    assert "did NOT pass" in v["reason"]


def test_store_adapter_verdict_defaults_to_the_current_version(tmp_path):
    store = StoreAdapter(tmp_path)
    _seed_model(store)
    _simulate_published_build(store, "m", 0)
    v = store.verdict("m")
    assert v["ir_version"] == 0 and v["verified"] is True
    with pytest.raises(FileNotFoundError):
        store.verdict("nope")


# ══════════════════════════════════════════════════════════════════════════
# 2. the API shows it (the front end must not infer success from artifacts)
# ══════════════════════════════════════════════════════════════════════════


def _client(tmp_path, *, model_id="m", prepared=False):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from tests.unit import test_server as ts

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    services = ts.make_services(tmp_path)
    services.store = StoreAdapter(data_dir)
    if prepared:
        _seed_model(services.store, model_id)
        _simulate_published_build(services.store, model_id, 0, attempt="att-api")
    return TestClient(create_app(services)), services


def test_verdict_endpoint_reports_verified_and_unverified(tmp_path):
    client, services = _client(tmp_path, prepared=True)
    with client:
        body = client.get("/models/m/verdict").json()
    assert body["verified"] is True
    assert body["attempt_id"] == "att-api"
    assert body["reason"]


def test_verdict_endpoint_404s_for_an_unknown_model(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        assert client.get("/models/nope/verdict").status_code == 404


def test_artifact_listing_carries_the_verdict(tmp_path):
    client, _ = _client(tmp_path, prepared=True)
    with client:
        body = client.get("/models/m/artifacts").json()
    assert any(f.endswith(".step") for f in body["files"])
    assert body["verdict"]["verified"] is True


def test_sessions_list_says_whether_the_latest_version_is_verified(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/sessions", json={"raw_requirement": "a plate"}).json()
        model_id = created["model_id"]
        rows = client.get("/sessions").json()["sessions"]
    row = next(r for r in rows if r["model_id"] == model_id)
    assert row["ir_version"] == 0
    assert row["verified"] is False, (
        "a freshly seeded model has never been graded; the list must not imply it was")


# ══════════════════════════════════════════════════════════════════════════
# 3. the invalidation rule is structural, not a hardcoded tool list
# ══════════════════════════════════════════════════════════════════════════


async def test_any_write_tier_tool_invalidates_a_pass():
    """A *new* write tool must invalidate without anyone remembering to add it.

    The rule used to name ``("ir_patch", "raw_python")``; the first write tool
    added afterwards would have silently stopped invalidating, and "a pass
    survives a write" is the exact failure this exists to prevent.
    """
    from tcad.core.types import ToolSpec
    from tcad.tools.base import ToolRegistry
    from tests.unit.test_loop_engine import FakeGate, make_services

    ir = make_ir()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[
            ToolCall(id="c1", name="ir_commit", args={"message": "go"}),
            ToolCall(id="c2", name="brand_new_writer", args={}),
        ]),
    ])
    svc = make_services(ir, llm, gate_passed=True)

    reg = ToolRegistry()

    async def writer(args, ctx):
        return ToolResult(ok=True, content="mutated")

    reg.register_tool(ToolSpec(name="brand_new_writer", tier=ToolTier.WRITE,
                               description="d", params_schema={"type": "object"},
                               handler=writer))
    reg.register_tool(ToolSpec(name="ir_commit", tier=ToolTier.WRITE,
                               description="d",
                               params_schema={"type": "object",
                                              "properties": {"message": {"type": "string"}},
                                              "required": ["message"]},
                               handler=_commit_handler(svc)))

    engine = LoopEngine(svc, reg, BudgetLimits(max_steps_per_turn=10),
                        LoopConfig(data_dir="/tmp"))
    await engine.run_turn(Thread(thread_id="th1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="x"))

    assert engine._last_commit_passed is False, (
        "a write-tier tool ran after a pass and the pass survived")


def _commit_handler(services):
    from tcad.loop.commit import run_commit

    async def handler(args, ctx):
        _res, report = await run_commit(
            services, ctx.model_id, services.store.current_version(ctx.model_id),
            args.get("message", ""), ctx.workdir, ctx.data_dir)
        from tcad.tools.base import ToolOutcome

        return ToolOutcome(result=ToolResult(ok=True, content="committed"),
                           gate_report=report)

    return handler


async def test_ir_commit_itself_does_not_invalidate_its_own_pass():
    """The act of grading is not a write to the graded document."""
    ir = make_ir()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="ir_commit", args={"message": "go"})]),
    ])
    svc = make_services(ir, llm, gate_passed=True)
    engine = LoopEngine(svc, _real_registry(svc), BudgetLimits(max_steps_per_turn=5),
                        LoopConfig(data_dir="/tmp"))
    await engine.run_turn(Thread(thread_id="th1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="x"))
    assert engine._last_commit_passed is True


def _real_registry(services):
    from tcad.tools.base import build_default_registry

    return build_default_registry(services)
