"""Gate acceptance tests (design §4.6 / §4.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tcad.core.types import CheckStatus, Confidence, Severity
from tcad.ir.schema import ConstraintExpr, RequirementSpec
from tcad.verify.context import build_check_context
from tcad.verify.gate import Gate
from tcad.verify.checks_solid import ALL_SOLID_CHECKS, VerifyConfig
from tests.fixtures.gate_fixtures import (
    FakeWorker, make_digest, make_ir, write_artefacts,
)


def _ctx(tmp_path: Path, *, ir=None, digest=None, with_exports=True, worker=None):
    ir_path, artifact_dir = write_artefacts(
        tmp_path, ir=ir, digest=digest, with_exports=with_exports
    )
    return build_check_context(
        model_id=(ir or make_ir()).model_id,
        ir_version=(ir or make_ir()).version,
        artifact_dir=str(artifact_dir), ir_path=str(ir_path), worker=worker,
    )


def _gate(ctx):
    return Gate(lambda m, v: ctx)


def test_passing_gate_on_good_digest(tmp_path):
    ctx = _ctx(tmp_path, worker=FakeWorker())
    rep = _gate(ctx).evaluate("m1", 1)
    assert rep.passed is True
    assert rep.blocking_failures == []
    assert rep.skipped_checks == []


def test_failing_bbox_blocks_and_attaches_feature_id(tmp_path):
    ir = make_ir(requirements=RequirementSpec(constraints=[
        ConstraintExpr(kind="bbox", value={"x": 100.0, "y": 40.0, "z": 10.0},
                       tol=0.05, source_text="width must be 100mm", confirmed=True),
    ]))
    ctx = _ctx(tmp_path, ir=ir, worker=FakeWorker())
    rep = _gate(ctx).evaluate("m1", 1)
    assert rep.passed is False
    assert "bbox_spec" in rep.blocking_failures
    r = next(r for r in rep.results if r.check_id == "bbox_spec")
    assert r.feature_id is not None          # attributable to a feature/body
    assert r.status == CheckStatus.FAIL


def test_unconfirmed_expr_does_not_block(tmp_path):
    ir = make_ir(requirements=RequirementSpec(constraints=[
        # would FAIL (digest has 1 solid) but is unconfirmed -> must NOT block
        ConstraintExpr(kind="count", value=5, tol=0,
                       source_text="should have 5 solids", confirmed=False),
    ]))
    ctx = _ctx(tmp_path, ir=ir, worker=FakeWorker())
    rep = _gate(ctx).evaluate("m1", 1)
    assert rep.passed is True
    assert rep.blocking_failures == []
    assert any("spec_count" in fid for fid in rep.advisory_findings)


def test_check_that_raises_is_error_fail_closed(tmp_path):
    class RaisingCheck:
        id = "raise_check"
        severity = Severity.BLOCKING
        confidence = Confidence.DETERMINISTIC

        def run(self, ctx):
            raise RuntimeError("boom")

    solid = [c(VerifyConfig()) for c in ALL_SOLID_CHECKS] + [RaisingCheck()]
    ctx = _ctx(tmp_path, worker=FakeWorker())
    rep = Gate(lambda m, v: ctx, solid_checks=solid).evaluate("m1", 1)
    assert rep.passed is False
    r = next(r for r in rep.results if r.check_id == "raise_check")
    assert r.status == CheckStatus.ERROR
    assert "raise_check" in rep.blocking_failures


def test_check_that_cannot_run_is_listed_in_skipped(tmp_path):
    # no STEP export and no worker -> round_trip cannot run -> SKIP (not silent)
    ctx = _ctx(tmp_path, with_exports=False, worker=None)
    rep = _gate(ctx).evaluate("m1", 1)
    assert "round_trip" in rep.skipped_checks
    # other blocking checks still PASS, so not *all* blocking skipped -> passed True
    assert rep.passed is True


def test_gateway_cannot_verify_is_not_success(tmp_path):
    # digest without measurements -> every geometry check SKIPs -> must NOT pass
    digest = make_digest(measurements_available=False)
    ctx = _ctx(tmp_path, digest=digest, with_exports=False, worker=None)
    rep = _gate(ctx).evaluate("m1", 1)
    assert rep.passed is False
    assert rep.skipped_checks  # proves it could not verify anything
