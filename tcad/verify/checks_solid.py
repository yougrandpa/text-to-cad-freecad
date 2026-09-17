"""First-tier deterministic checks (design §4.6 item 4).

Every check is a pure ``(CheckContext) -> CheckResult``. Checks consume the
*independently produced* :class:`GeometryDigest` (read from disk by
``tcad.verify.context.build_check_context``) — they never touch a raw TopoShape
and never read the loop's in-memory IR for geometry facts.

Honesty rules baked in here:
  * If ``digest.measurements_available is False`` (worker was unreachable and the
    digest is structure-only) the geometry checks return SKIP rather than a
    dishonest PASS. Flying blind is reported, never hidden.
  * ``round_trip`` is the only check that *must* leave the supervisor: it reads
    the exported STEP back through the worker's independent import path. The
    supervisor has no FreeCAD, so without a worker handle it SKIPs. See the
    module-level note in ``RoundTripCheck`` for exactly how independent that is.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict

from tcad.core.types import (
    CheckContext,
    CheckResult,
    Confidence,
    GeometryDigest,
    Severity,
)
from tcad.verify.context import WorkerReadbackHandle
from tcad.verify.specexpr import evaluate


class VerifyConfig(BaseModel):
    """Tolerances for the deterministic checks. Driven by config (design §8
    ``verify.checks``); defaults match the design's stated numbers."""

    model_config = ConfigDict(extra="ignore")

    bbox_tol_mm: float = 0.05
    mass_tol_ratio: float = 0.01
    solid_count_expect: int = 1
    round_trip_tol_ratio: float = 1e-6
    wall_thickness_min_mm: float = 1.0
    # kinds owned by the first tier (so checks_spec.py does not double-count them)
    spec_kinds_owned_by_first_tier: tuple[str, ...] = ("bbox", "volume")


def _r(
    check,
    status,
    message="",
    *,
    measurements=None,
    expected=None,
    evidence=None,
    feature_id=None,
) -> CheckResult:
    return CheckResult(
        check_id=check.id,
        status=status,
        severity=check.severity,
        confidence=check.confidence,
        message=message,
        measurements=measurements or {},
        expected=expected,
        evidence=evidence or [],
        feature_id=feature_id,
    )


def _as_dict(value, *, floats_only: bool = False) -> dict | None:
    """Coerce a ``specexpr`` measurement blob into a ``CheckResult`` field type.

    ``specexpr.evaluate`` returns ``info["measured"]`` as ``None | scalar | dict``
    and ``info["expected"]`` likewise. ``CheckResult.measurements`` is typed
    ``dict[str, float | str | bool]`` while ``CheckResult.expected`` is
    ``dict[str, float] | None`` — so a bare scalar has to be wrapped, and for
    ``expected`` anything non-numeric must be dropped rather than allowed to fail
    pydantic validation (a validation error here would turn a clean FAIL into an
    opaque ERROR and lose the measurement).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        try:
            return {"value": float(value)}
        except (TypeError, ValueError):
            return {} if floats_only else {"value": str(value)}
    out: dict[str, float | str | bool] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            if not floats_only:
                out[str(key)] = item
        elif isinstance(item, (int, float)):
            out[str(key)] = float(item)
        elif not floats_only:
            out[str(key)] = str(item)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 1. solid_validity
# ══════════════════════════════════════════════════════════════════════════


class SolidValidityCheck:
    id = "solid_validity"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d: GeometryDigest | None = ctx.digest
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        if d.is_valid:
            return _r(self, "pass", "all solids pass OCC BRepCheck")
        return _r(self, "fail", "geometry is not a valid solid",
                  measurements={"is_valid": d.is_valid})


# ══════════════════════════════════════════════════════════════════════════
# 2. solid_count
# ══════════════════════════════════════════════════════════════════════════


class SolidCountCheck:
    id = "solid_count"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.digest
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        actual = d.topology.solids
        expect = self.config.solid_count_expect
        fid = ctx.ir.bodies[0].id if ctx.ir.bodies else None
        if actual == expect:
            return _r(self, "pass", f"solid count == {expect}",
                      measurements={"solids": actual}, expected={"solids": expect},
                      feature_id=fid)
        return _r(self, "fail", f"expected {expect} solid(s), found {actual}",
                  measurements={"solids": actual}, expected={"solids": expect},
                  feature_id=fid)


# ══════════════════════════════════════════════════════════════════════════
# 3. bbox_spec  (geometry vs confirmed `bbox` ConstraintExpr)
# 4. mass_spec  (geometry vs confirmed `volume` ConstraintExpr)
# ══════════════════════════════════════════════════════════════════════════


class BBoxSpecCheck:
    id = "bbox_spec"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.digest
        exprs = [e for e in ctx.ir.requirements.constraints
                 if e.kind == "bbox" and e.confirmed]
        if not exprs:
            return _r(self, "pass", "no confirmed bbox requirement")
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        fid = ctx.ir.bodies[0].id if ctx.ir.bodies else None
        for expr in exprs:
            ok, info = evaluate(expr, d, ctx.ir)
            if ok is None:
                return _r(self, "skip", "bbox requirement could not be measured")
            if not ok:
                return _r(self, "fail",
                          f"bounding box deviates from requirement "
                          f"({expr.source_text or 'bbox'})",
                          measurements=info.get("measured", {}),
                          expected=_as_dict(info.get("expected")),
                          feature_id=fid)
        return _r(self, "pass", "bounding box within tolerance")


class MassSpecCheck:
    id = "mass_spec"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.digest
        exprs = [e for e in ctx.ir.requirements.constraints
                 if e.kind == "volume" and e.confirmed]
        if not exprs:
            return _r(self, "pass", "no confirmed volume requirement")
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        fid = ctx.ir.bodies[0].id if ctx.ir.bodies else None
        for expr in exprs:
            ok, info = evaluate(expr, d, ctx.ir)
            if ok is None:
                return _r(self, "skip", "volume requirement could not be measured")
            if not ok:
                return _r(self, "fail",
                          f"volume deviates from requirement "
                          f"({expr.source_text or 'volume'})",
                          measurements=_as_dict(info.get("measured")),
                          expected=_as_dict(info.get("expected")),
                          feature_id=fid)
        return _r(self, "pass", "volume within tolerance")


# ══════════════════════════════════════════════════════════════════════════
# 5. sketch_fully_constrained
#    Per-sketch constraint state is carried in digest.key_dimensions under the
#    reserved keys "<sketch_id>__fully_constrained" (1.0/0.0) and
#    "<sketch_id>__dof" (float). The worker populates these from
#    sketch.FullyConstrained / sketch.DoF (design §4.3 / 附录 A-2). This is the
#    only free-form numeric carrier the frozen GeometryDigest offers, so it is
#    the agreed convention between worker and gate.
# ══════════════════════════════════════════════════════════════════════════


class SketchFullyConstrainedCheck:
    id = "sketch_fully_constrained"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.digest
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        for sketch in ctx.ir.all_sketches():
            if not sketch.require_fully_constrained:
                continue
            fc = d.key_dimensions.get(f"{sketch.id}__fully_constrained")
            dof = d.key_dimensions.get(f"{sketch.id}__dof")
            if fc is None:
                return _r(self, "skip",
                          f"constraint state for sketch {sketch.id} not measured")
            if fc != 1.0:
                return _r(self, "fail",
                          f"sketch '{sketch.name}' ({sketch.id}) is not fully "
                          f"constrained (DoF={dof})",
                          measurements={"fully_constrained": bool(fc), "dof": dof or 0},
                          feature_id=sketch.id)
        return _r(self, "pass", "all required sketches fully constrained")


# ══════════════════════════════════════════════════════════════════════════
# 6. round_trip
#    Reads the exported STEP back from DISK through the worker's independent
#    import path and compares volume + face/edge counts with the in-process
#    digest.
#
#    HOW INDEPENDENT IS THIS, HONESTLY?
#      * Input is the on-disk *.step file path — never an in-memory shape. Good.
#      * The re-measurement is performed by the worker process (the only process
#        that has FreeCAD/OCC), via import_asset -> a fresh OCC Part.Shape.read
#        of the file. That path is independent of the in-memory compiled shape,
#        so it genuinely catches export/serialisation loss.
#      * COMPROMISE: it is the *same* worker process that compiled and exported
#        the model. It is not a second, separately-built kernel acting as an
#        oracle. True oracle independence (a second toolchain) is out of scope
#        for this harness; the read path is independent in data (disk), not in
#        process. Documented here and reported to team-lead.
#    When the STEP file or the worker handle is absent the check cannot run and
#    returns SKIP (it never fakes a pass).
# ══════════════════════════════════════════════════════════════════════════


class RoundTripCheck:
    id = "round_trip"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        step_path = ctx.exports.get("step")
        if not step_path:
            return _r(self, "skip", "no STEP export on disk to read back")
        if ctx.worker is None:
            return _r(self, "skip",
                      "worker handle unavailable; supervisor cannot read STEP "
                      "without FreeCAD")
        d = ctx.digest
        if d is None or not d.measurements_available:
            return _r(self, "skip", "in-process digest has no measurements to compare")
        handle = WorkerReadbackHandle(worker=ctx.worker)
        summary = handle.read_step_summary(step_path)  # may raise -> Gate -> ERROR
        tol = self.config.round_trip_tol_ratio
        ref = max(abs(d.volume), 1e-12)
        rel = abs(summary["volume"] - d.volume) / ref
        vol_ok = rel <= tol
        faces_ok = int(summary["faces"]) == d.topology.faces
        edges_ok = int(summary["edges"]) == d.topology.edges
        if vol_ok and faces_ok and edges_ok:
            return _r(self, "pass", "STEP round-trip consistent",
                      measurements={"step_volume": summary["volume"],
                                    "step_faces": summary["faces"],
                                    "step_edges": summary["edges"]},
                      evidence=[step_path])
        return _r(self, "fail",
                  f"STEP round-trip mismatch (vol rel err={rel:.2e}, "
                  f"faces {int(summary['faces'])}!={d.topology.faces}, "
                  f"edges {int(summary['edges'])}!={d.topology.edges})",
                  measurements={"step_volume": summary["volume"],
                                "step_faces": summary["faces"],
                                "step_edges": summary["edges"],
                                "rel_volume_error": rel},
                  evidence=[step_path])


# ══════════════════════════════════════════════════════════════════════════
# 7. exportability
# ══════════════════════════════════════════════════════════════════════════


class ExportabilityCheck:
    id = "exportability"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        if not ctx.exports:
            return _r(self, "skip", "no exports present to verify")
        empty: list[str] = []
        for fmt, path in ctx.exports.items():
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                empty.append(fmt)
        if empty:
            return _r(self, "fail",
                      f"export(s) {empty} are missing or empty",
                      measurements={"empty_formats": empty},
                      evidence=list(ctx.exports.values()))
        return _r(self, "pass", "all exports present and non-empty",
                  evidence=list(ctx.exports.values()))


# ══════════════════════════════════════════════════════════════════════════
# 8. wall_thickness  (advisory / approximate — design §4.6 item 3 & §12-7)
#    The supervisor has no FreeCAD, so the minimum wall thickness must be
#    measured by the worker and carried in digest.key_dimensions["min_wall_thickness"].
#    If that measurement is absent the check SKIPs — it never invents a pass.
# ══════════════════════════════════════════════════════════════════════════


class WallThicknessCheck:
    id = "wall_thickness"
    severity = Severity.ADVISORY
    confidence = Confidence.APPROXIMATE

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        d = ctx.digest
        if d is None or not d.measurements_available:
            return _r(self, "skip", "no valid geometry measurements available")
        mw = d.key_dimensions.get("min_wall_thickness")
        if mw is None:
            return _r(self, "skip",
                      "min wall thickness not measured by worker (approximate "
                      "check cannot run)")
        min_mm = self.config.wall_thickness_min_mm
        if mw >= min_mm:
            return _r(self, "pass", f"min wall {mw:.3f}mm >= {min_mm}mm",
                      measurements={"min_wall_thickness": mw},
                      expected={"min_wall_thickness": min_mm})
        return _r(self, "fail", f"min wall {mw:.3f}mm < {min_mm}mm (approximate)",
                  measurements={"min_wall_thickness": mw},
                  expected={"min_wall_thickness": min_mm})


ALL_SOLID_CHECKS = (
    SolidValidityCheck,
    SolidCountCheck,
    BBoxSpecCheck,
    MassSpecCheck,
    SketchFullyConstrainedCheck,
    RoundTripCheck,
    ExportabilityCheck,
    WallThicknessCheck,
)
