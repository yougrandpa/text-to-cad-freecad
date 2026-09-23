"""Unit tests for the first-tier deterministic checks (design §4.6 item 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_a_list_measurement_still_produces_a_verdict():
    """``CheckResult.measurements`` is a scalar map, but checks report *sets* of
    things (which formats are empty, which features errored). Handing a list to
    pydantic raised inside the check, so the Gate recorded an opaque ERROR and
    the finding itself was lost — objective §5-C's type mismatch."""
    from tcad.verify.checks_solid import SolidValidityCheck, _r

    r = _r(SolidValidityCheck(), "fail", "n/a",
           measurements={"empty_formats": ["step", "stl"], "count": 2, "ok": False})
    assert r.status == CheckStatus.FAIL
    assert r.measurements == {"empty_formats": "step, stl", "count": 2.0, "ok": False}


# ══════════════════════════════════════════════════════════════════════════
# second tier — a confirmed requirement must always produce a verdict
# ══════════════════════════════════════════════════════════════════════════
#
# The first tier was fixed for the list-measurement case above; the second tier
# (`checks_spec.SpecCheck`) built `CheckResult` directly and never went through
# the coercion, so it still had the same class of defect in a worse form: for
# `count`, `feature_count`, `symmetric` and `wall_thickness` the measurement is
# a *bare scalar*, and pydantic raised on construction. Under the Gate that
# became an ERROR whose message was a validation traceback — so recording "one
# solid, please" or "the wall is 3 mm" as the user's confirmed requirement made
# the build unpassable and hid why. These tests pin the whole set.

_SPEC_DIGEST_HOLE = {
    "index": 1, "diameter": 6.0, "radius": 3.0, "center": [10.0, 10.0, 0.0],
    "axis": [0.0, 0.0, 1.0], "depth": 8.0, "through": True,
}


def _spec_ctx(tmp_path, expr, *, solids=1, min_wall=None):
    from tcad.core.types import CheckContext

    digest = make_digest(solids=solids, holes=[_SPEC_DIGEST_HOLE])
    if min_wall is not None:
        digest.key_dimensions["min_wall_thickness"] = min_wall
    return CheckContext(
        model_id="m1", ir_version=1,
        ir=make_ir(), artifact_dir=str(tmp_path), digest=digest,
    )


@pytest.mark.parametrize(
    "expr, expected_status",
    [
        (ConstraintExpr(kind="count", value=1, tol=0, confirmed=True), CheckStatus.PASS),
        (ConstraintExpr(kind="count", value=5, tol=0, confirmed=True), CheckStatus.FAIL),
        (ConstraintExpr(kind="feature_count", value=1, tol=0, confirmed=True), CheckStatus.PASS),
        (ConstraintExpr(kind="wall_thickness", value=2.0, confirmed=True), CheckStatus.PASS),
    ],
)
def test_a_confirmed_scalar_requirement_produces_a_verdict(tmp_path, expr, expected_status):
    """Every kind whose measurement is a scalar — none may raise."""
    from tcad.verify.checks_spec import SpecCheck

    r = SpecCheck(expr, 0).run(_spec_ctx(tmp_path, expr, min_wall=3.0))
    assert r.status == expected_status, r.message
    assert r.measurements, "the measurement must survive into the report"


def test_a_confirmed_structural_requirement_produces_a_verdict(tmp_path):
    """`symmetric` reports a *dict with a non-float value* — also not a scalar map
    until it is coerced, and it was one of the kinds that raised."""
    from tcad.verify.checks_spec import SpecCheck

    expr = ConstraintExpr(kind="symmetric", target="pad1", confirmed=True)
    r = SpecCheck(expr, 0).run(_spec_ctx(tmp_path, expr))
    assert r.status in (CheckStatus.PASS, CheckStatus.FAIL), r.message


def test_the_gate_never_sees_a_validation_error_from_a_spec_check(tmp_path):
    """The end the user cares about: a confirmed requirement is judged, not
    turned into an ERROR carrying a pydantic dump."""
    from tcad.verify.checks_spec import build_spec_checks

    ir = _ir_with(confirmed=0)
    ir.requirements.constraints = [
        ConstraintExpr(kind="count", value=1, tol=0, confirmed=True,
                       source_text="expect one solid"),
        ConstraintExpr(kind="feature_count", value=0, tol=0, confirmed=True,
                       source_text="no features"),
    ]
    ctx = _spec_ctx(tmp_path, None)
    ctx.ir = ir
    for check in build_spec_checks(ir, None):
        r = check.run(ctx)  # must not raise
        assert r.status != CheckStatus.ERROR, (check.id, r.message)
        assert "validation error" not in r.message.lower(), r.message


# ══════════════════════════════════════════════════════════════════════════
# wall thickness — the user's requirement must be the judge, not the default
# ══════════════════════════════════════════════════════════════════════════
#
# `wall_thickness` is owned by this tier, so the second tier never built a check
# for it. Combined with the first tier judging only the *config* default, that
# meant a confirmed "the wall is 3 mm" entered the requirement contract and was
# then graded against nothing — the objective's "a confirmed requirement must
# participate in acceptance, and a wall requirement may not be replaced by the
# default advisory minimum".


def _wall_ctx(tmp_path, *, wall, required=None):
    from tcad.core.types import CheckContext

    # `make_digest` seeds a 2.0 mm wall by default; "not measured" means the key
    # is *absent*, which is a different state from a measured zero.
    digest = make_digest(solids=1, min_wall=wall if wall is not None else 2.0)
    if wall is None:
        digest.key_dimensions.pop("min_wall_thickness", None)
    ir = make_ir()
    if required is not None:
        ir.requirements.constraints = [
            ConstraintExpr(kind="wall_thickness", value=required, confirmed=True,
                           tol=0.01, source_text=f"壁厚 {required}mm"),
        ]
    return CheckContext(model_id="m1", ir_version=1, ir=ir,
                        artifact_dir=str(tmp_path), digest=digest)


def test_a_confirmed_wall_requirement_is_judged_against_the_users_number(tmp_path):
    """3 mm measured, 3 mm asked for: PASS — and it blocks, so it can fail."""
    r = WallThicknessCheck().run(_wall_ctx(tmp_path, wall=3.0, required=3.0))
    assert r.status == CheckStatus.PASS, r.message
    assert r.severity == Severity.BLOCKING
    assert r.measurements["min_wall_thickness"] == 3.0


def test_a_confirmed_wall_requirement_that_is_not_met_fails(tmp_path):
    r = WallThicknessCheck().run(_wall_ctx(tmp_path, wall=2.0, required=3.0))
    assert r.status == CheckStatus.FAIL, r.message
    assert r.severity == Severity.BLOCKING
    assert "required" in r.message


def test_a_confirmed_wall_requirement_without_a_measurement_is_not_a_skip(tmp_path):
    """Measured geometry, no wall measurement: "cannot verify" is not
    "not applicable" — it must block and say required_but_unverified."""
    r = WallThicknessCheck().run(_wall_ctx(tmp_path, wall=None, required=3.0))
    assert r.status == CheckStatus.ERROR, r.message
    assert r.severity == Severity.BLOCKING
    assert "required_but_unverified" in r.message


def test_without_a_requirement_the_shop_default_stays_advisory(tmp_path):
    """The other half of the contract: no requirement recorded means the old
    advisory behaviour, unchanged — it must not start blocking builds."""
    r = WallThicknessCheck().run(_wall_ctx(tmp_path, wall=0.5, required=None))
    assert r.status == CheckStatus.FAIL
    assert r.severity == Severity.ADVISORY

    skipped = WallThicknessCheck().run(_wall_ctx(tmp_path, wall=None, required=None))
    assert skipped.status == CheckStatus.SKIP
