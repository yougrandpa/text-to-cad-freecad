"""Unit tests for the first-tier deterministic checks (design §4.6 item 4)."""

from __future__ import annotations

from pathlib import Path

from tcad.core.types import CheckStatus, Severity
from tcad.ir.schema import ConstraintExpr, RequirementSpec
from tcad.verify.checks_solid import (
    MassSpecCheck, SolidCountCheck, SolidValidityCheck,
    SketchFullyConstrainedCheck, WallThicknessCheck, BBoxSpecCheck,
    VerifyConfig,
)
from tcad.verify.context import build_check_context
from tests.fixtures.gate_fixtures import FakeWorker, make_digest, make_ir, write_artefacts


def _ctx(tmp_path: Path, *, worker=None, **kw):
    """``worker`` belongs to the CheckContext, not to write_artefacts — pop it
    before forwarding so callers can override the worker without tripping over
    the fixture's signature."""
    ir_path, artifact_dir = write_artefacts(tmp_path, **kw)
    return build_check_context(
        model_id="m1", ir_version=1,
        artifact_dir=str(artifact_dir), ir_path=str(ir_path),
        worker=FakeWorker() if worker is None else worker,
    )


def test_solid_validity_and_count_pass(tmp_path):
    ctx = _ctx(tmp_path)
    assert SolidValidityCheck().run(ctx).status == CheckStatus.PASS
    assert SolidCountCheck().run(ctx).status == CheckStatus.PASS


def test_solid_count_fails_on_wrong_count(tmp_path):
    digest = make_digest(solids=2, faces=12, edges=24, vertexes=16)
    ctx = _ctx(tmp_path, digest=digest)
    r = SolidCountCheck(VerifyConfig(solid_count_expect=1)).run(ctx)
    assert r.status == CheckStatus.FAIL
    assert r.feature_id is not None


def test_solid_validity_skips_when_unmeasured(tmp_path):
    digest = make_digest(measurements_available=False)
    ctx = _ctx(tmp_path, digest=digest)
    assert SolidValidityCheck().run(ctx).status == CheckStatus.SKIP


def test_bbox_spec_skips_when_there_is_no_requirement(tmp_path):
    """SKIP, not PASS.

    "There was nothing to check" and "checked and found correct" are different
    claims, and reporting the first as the second is how a Gate ends up green
    while proving nothing. That is not hypothetical: it happened live, on a model
    that had recorded no requirement at all and still reported `bbox_spec pass`.
    """
    ctx = _ctx(tmp_path)
    assert BBoxSpecCheck().run(ctx).status == CheckStatus.SKIP


def test_mass_spec_fails_on_volume_deviation(tmp_path):
    ir = make_ir(requirements=RequirementSpec(constraints=[
        ConstraintExpr(kind="volume", value=1000.0, tol=0.01,
                       source_text="volume ~1000", confirmed=True),
    ]))
    ctx = _ctx(tmp_path, ir=ir)
    r = MassSpecCheck().run(ctx)
    assert r.status == CheckStatus.FAIL
    assert r.feature_id is not None


def test_sketch_underconstrained_fails(tmp_path):
    digest = make_digest()
    digest.key_dimensions["sk_base__fully_constrained"] = 0.0
    digest.key_dimensions["sk_base__dof"] = 3.0
    ctx = _ctx(tmp_path, digest=digest)
    r = SketchFullyConstrainedCheck().run(ctx)
    assert r.status == CheckStatus.FAIL
    assert r.feature_id == "sk_base"


def test_wall_thickness_advisory_and_fails_below_min(tmp_path):
    digest = make_digest(min_wall=0.5)
    ctx = _ctx(tmp_path, digest=digest)
    r = WallThicknessCheck(VerifyConfig(wall_thickness_min_mm=1.0)).run(ctx)
    assert r.severity == Severity.ADVISORY
    assert r.status == CheckStatus.FAIL


def test_round_trip_fails_on_disk_mismatch(tmp_path):
    from tests.fixtures.gate_fixtures import FakeWorkerWrong
    from tcad.verify.checks_solid import RoundTripCheck

    ctx = _ctx(tmp_path, worker=FakeWorkerWrong())
    r = RoundTripCheck().run(ctx)
    assert r.status == CheckStatus.FAIL


# ─── requirement coverage ──────────────────────────────────────────────────
# A green Gate with nothing to judge against reads as "your part is correct"
# while proving only that the geometry is self-consistent. Observed live: asked
# to add arms and legs to a cube, a model built two calibration probes, recorded
# no requirement, and the turn ended green.


def _ir_with(*, confirmed: int, unconfirmed: int = 0):
    from tcad.ir.schema import IrDocument, RequirementSpec

    return IrDocument(
        model_id="m1",
        requirements=RequirementSpec(
            raw_text="给这个立方体加两个手臂和两条腿",
            constraints=[
                *(ConstraintExpr(kind="count", value=2, confirmed=True) for _ in range(confirmed)),
                *(ConstraintExpr(kind="count", value=2, confirmed=False) for _ in range(unconfirmed)),
            ],
        ),
    )


def test_requirement_coverage_fails_when_nothing_was_recorded(tmp_path):
    from tcad.verify.checks_spec import RequirementCoverageCheck

    r = RequirementCoverageCheck().run(_ctx(tmp_path, ir=_ir_with(confirmed=0)))
    assert r.status == CheckStatus.FAIL
    assert r.severity == Severity.ADVISORY, "必须是 advisory —— 不能改变「全绿=完成」"
    assert "update_requirement" in r.message, "没有告诉模型该怎么补救"
    assert "NOT" in r.message, "没有说清它没证明什么"


def test_requirement_coverage_passes_once_a_requirement_exists(tmp_path):
    from tcad.verify.checks_spec import RequirementCoverageCheck

    r = RequirementCoverageCheck().run(_ctx(tmp_path, ir=_ir_with(confirmed=2)))
    assert r.status == CheckStatus.PASS
    assert "2 confirmed" in r.message


def test_unconfirmed_expressions_do_not_count_as_coverage(tmp_path):
    """`confirmed=False` may never block, so it cannot be evidence either."""
    from tcad.verify.checks_spec import RequirementCoverageCheck

    r = RequirementCoverageCheck().run(_ctx(tmp_path, ir=_ir_with(confirmed=0, unconfirmed=3)))
    assert r.status == CheckStatus.FAIL


def test_the_coverage_check_is_always_assembled(tmp_path):
    """It has to run even when there are no constraints at all — that is the
    case it exists for."""
    from tcad.verify.checks_spec import build_spec_checks

    checks = build_spec_checks(_ir_with(confirmed=0), None)
    assert any(c.id == "requirement_coverage" for c in checks)
