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
import math

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
    # Formats the delivery contract requires for *this* build. Empty means
    # "grade only whatever is on disk" (the historical behaviour); non-empty
    # makes a missing or empty file a blocking FAIL instead of silence.
    required_exports: tuple[str, ...] = ()
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
    severity=None,
) -> CheckResult:
    return CheckResult(
        check_id=check.id,
        status=status,
        # A check whose severity depends on *what was asked* rather than on the
        # check's kind has to be able to say so per-result. `wall_thickness` is
        # the one such check: advisory when it is only reporting the shop
        # default, blocking when it is judging a confirmed requirement.
        severity=severity if severity is not None else check.severity,
        confidence=check.confidence,
        message=message,
        # Coerced, not passed through: ``measurements`` is typed as a scalar map,
        # and a check that reports a list used to die inside pydantic — the Gate
        # then recorded an ERROR whose message was a validation dump instead of
        # the finding the check was trying to state.
        measurements=_as_dict(measurements) or {},
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
        elif isinstance(item, (list, tuple)):
            # Several checks report a *set* of things (which formats are empty,
            # which features errored). ``measurements`` cannot hold a list, and
            # handing one to pydantic turned a clean FAIL into an opaque ERROR —
            # the check's own verdict was lost in a validation traceback.
            out[str(key)] = ", ".join(str(i) for i in item)
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
        if len(ctx.ir.bodies) > 1 and expect == 1:
            # Count every body's independently measured shape; a total alone can
            # hide one missing part and another disconnected, multi-solid part.
            invalid = [b.id for b in ctx.ir.bodies if d.body_solids.get(b.id) != 1]
            expect = len(ctx.ir.bodies)
            if invalid or actual != expect:
                return _r(self, "fail", "assembly requires one measured solid per body: "
                          + ", ".join(f'{id}={d.body_solids.get(id,"unmeasured")} solids (expected 1)' for id in invalid)
                          + ". The total below counts the whole assembly, not each failing body.", measurements={"solids": actual},
                          expected={"solids": expect}, feature_id=invalid[0] if invalid else fid)
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
            # SKIP, not PASS. "There was nothing to check" and "checked and
            # found correct" are different claims; reporting the first as the
            # second is how a report ends up green while proving nothing.
            # SKIP also lands in `skipped_checks`, where a reader can see it.
            return _r(self, "skip", "no confirmed bbox requirement")
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
            # SKIP, not PASS. "There was nothing to check" and "checked and
            # found correct" are different claims; reporting the first as the
            # second is how a report ends up green while proving nothing.
            # SKIP also lands in `skipped_checks`, where a reader can see it.
            return _r(self, "skip", "no confirmed volume requirement")
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
        required = [sketch for sketch in ctx.ir.all_sketches() if sketch.require_fully_constrained]
        if not required:
            return _r(self, "skip", "no sketches require full constraint verification")
        for sketch in required:
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
        if len(ctx.ir.bodies) > 1 and vol_ok and faces_ok and not edges_ok:
            # STEP sewing may merge coincident seam edges in an assembly.
            # Require independent geometric evidence before accepting this.
            area = summary.get("area")
            bbox = summary.get("bbox") or {}
            geometry_ok = (
                summary.get("is_valid") is True
                and summary.get("solids") == d.topology.solids
                and isinstance(area, (float, int)) and math.isfinite(area)
                and abs(area - d.area) / max(abs(d.area), 1e-12) <= tol
                and all(isinstance(bbox.get(k), (int, float)) and math.isfinite(bbox[k])
                        and abs(bbox[k] - v) <= self.config.bbox_tol_mm
                        for k, v in d.bbox.model_dump().items()))
            if geometry_ok:
                return _r(self, "pass", "STEP geometry consistent; assembly seam edge count changed",
                          measurements={"step_volume": summary["volume"], "step_area": area,
                                        "step_faces": summary["faces"], "step_edges": summary["edges"],
                                        "reference_edges": d.topology.edges, "topology_changed": True},
                          evidence=[step_path])
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
#    "Some geometry happened to be on disk" is not the same statement as
#    "the requested artifacts were delivered". With ``required_exports`` set
#    the check grades the contract, not the directory: every required format
#    must resolve to a non-empty file, or this blocks.
# ══════════════════════════════════════════════════════════════════════════


class ExportabilityCheck:
    id = "exportability"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        required = list(self.config.required_exports)

        def _usable(fmt: str) -> bool:
            path = ctx.exports.get(fmt)
            return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0

        empty: list[str] = [
            fmt for fmt, path in ctx.exports.items()
            if not os.path.exists(path) or os.path.getsize(path) == 0
        ]
        if required:
            missing = [fmt for fmt in required if not _usable(fmt)]
            if missing:
                return _r(self, "fail",
                          f"required export(s) {missing} were not delivered by this "
                          f"build (found on disk: {sorted(ctx.exports) or 'nothing'}"
                          + (f"; empty: {empty}" if empty else "") + ")",
                          measurements={"missing_formats": missing,
                                        "empty_formats": empty,
                                        "required_formats": required},
                          evidence=list(ctx.exports.values()))
            return _r(self, "pass",
                      f"all required exports {required} present and non-empty",
                      measurements={"required_formats": required},
                      evidence=[ctx.exports[f] for f in required])
        if not ctx.exports:
            return _r(self, "skip", "no exports present to verify")
        if empty:
            return _r(self, "fail",
                      f"export(s) {empty} are missing or empty",
                      measurements={"empty_formats": empty},
                      evidence=list(ctx.exports.values()))
        return _r(self, "pass", "all exports present and non-empty",
                  evidence=list(ctx.exports.values()))


# ══════════════════════════════════════════════════════════════════════════
# 8. wall_thickness  (design §4.6 item 3 & §12-7)
#    The supervisor has no FreeCAD, so the minimum wall thickness must be
#    measured by the worker and carried in digest.key_dimensions["min_wall_thickness"].
#    If that measurement is absent the check SKIPs — it never invents a pass.
#
#    TWO DIFFERENT QUESTIONS, and they used to be answered by one number:
#
#      * "is this part manufacturable?" — a shop default. That is the
#        config-level ``wall_thickness_min_mm``, advisory, and stays advisory.
#      * "did the wall the user asked for come out at that thickness?" — a
#        *requirement*. The user's own number, judged against the measurement.
#
#    Only the first was implemented, and because ``wall_thickness`` is owned by
#    this tier (``checks_spec.FIRST_TIER_OWNED``), the second was never built
#    anywhere: a confirmed "the wall is 3 mm" was accepted into the requirement
#    contract and then silently judged against nothing. That is exactly the
#    "required but unverified" case the objective forbids treating as a skip.
#    A confirmed requirement now drives this check, and it BLOCKS.
# ══════════════════════════════════════════════════════════════════════════


class WallThicknessCheck:
    id = "wall_thickness"
    severity = Severity.ADVISORY
    confidence = Confidence.APPROXIMATE

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def _confirmed_requirements(self, ctx: CheckContext) -> list:
        requirements = getattr(getattr(ctx, "ir", None), "requirements", None)
        return [c for c in (getattr(requirements, "constraints", None) or [])
                if getattr(c, "kind", None) == "wall_thickness"
                and getattr(c, "confirmed", False)]

    def run(self, ctx: CheckContext) -> CheckResult:
        required = self._confirmed_requirements(ctx)
        if required:
            return self._run_against_requirement(ctx, required)
        return self._run_advisory(ctx)

    def _run_against_requirement(self, ctx: CheckContext, required: list) -> CheckResult:
        """Judge the measured wall against what the user actually asked for."""
        d = ctx.digest
        src = "; ".join((c.source_text or "wall_thickness") for c in required)
        if d is None or not d.measurements_available:
            return _r(self, "error",
                      f"required_but_unverified: wall thickness "
                      f"'{src}' was required but there is no valid geometry "
                      f"measurement to judge it by",
                      severity=Severity.BLOCKING)
        mw = d.key_dimensions.get("min_wall_thickness")
        if mw is None:
            # Measured geometry, but the worker could not produce a wall
            # measurement for it (too many faces, or no opposed-face pair with
            # material between). Blocking, because the user's requirement is
            # unattested — NOT a skip.
            return _r(self, "error",
                      f"required_but_unverified: wall thickness '{src}' is a "
                      f"confirmed requirement and the worker did not measure a "
                      f"minimum wall for this shape (no opposed-face pair found, "
                      f"or the face count exceeds the wall-measurement cap)",
                      severity=Severity.BLOCKING)
        worst = min(required, key=lambda c: float(c.value or 0.0))
        want = float(worst.value)
        tol = float(getattr(worst, "tol", 0.0) or 0.0)
        measured = {"min_wall_thickness": float(mw)}
        expected = {"min_wall_thickness": want}
        if mw >= want - tol:
            return _r(self, "pass",
                      f"min wall {mw:.3f}mm >= required {want}mm "
                      f"(measured on the BRep, tol {tol})",
                      measurements=measured, expected=expected,
                      severity=Severity.BLOCKING)
        return _r(self, "fail",
                  f"min wall {mw:.3f}mm < required {want}mm "
                  f"(measured on the BRep, tol {tol})",
                  measurements=measured, expected=expected,
                  severity=Severity.BLOCKING)

    def _run_advisory(self, ctx: CheckContext) -> CheckResult:
        """No confirmed requirement: the shop default, advisory as before."""
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


# ══════════════════════════════════════════════════════════════════════════
# 9. provenance
#    Every number graded here has to belong to the build being graded. A
#    digest from another version, a STEP left behind by the attempt that just
#    failed, an export written before this build started — each is real data
#    about *something*, and none of it is evidence about this build. Because
#    artefacts live in a per-version directory that retries reuse, "the file
#    is there" is weaker than "this attempt put it there".
# ══════════════════════════════════════════════════════════════════════════


class ProvenanceCheck:
    id = "provenance"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def __init__(self, config: VerifyConfig | None = None):
        self.config = config or VerifyConfig()

    def run(self, ctx: CheckContext) -> CheckResult:
        problems: list[str] = []

        d = ctx.digest
        if d is not None:
            if d.model_id != ctx.model_id:
                problems.append(f"digest belongs to model {d.model_id!r}")
            if d.ir_version != ctx.ir_version:
                problems.append(
                    f"digest belongs to v{d.ir_version}, not the graded v{ctx.ir_version}")
        if ctx.ir.model_id != ctx.model_id:
            problems.append(f"IR snapshot belongs to model {ctx.ir.model_id!r}")
        if ctx.ir.version != ctx.ir_version:
            problems.append(
                f"IR snapshot is v{ctx.ir.version}, not the graded v{ctx.ir_version}")

        stamp = ctx.build_stamp
        if stamp is None:
            # "This Gate grades files of unknown origin" is not a condition to
            # shrug at. Every commit attempt stamps its artefact directory before
            # it calls the worker, so an unstamped directory was either written by
            # hand or left over from before stamping existed — either way nothing
            # here proves the exports belong to the build being graded. Reporting
            # that as SKIP would let the Gate pass on unverifiable provenance.
            return _r(self, "fail",
                      "artefacts are not all from this build: "
                      + "; ".join(problems + [
                          "the artefact directory carries no build stamp, so which "
                          "attempt wrote these files cannot be established"]),
                      measurements={"problems": problems + ["missing build stamp"]})

        if stamp.model_id != ctx.model_id or stamp.ir_version != ctx.ir_version:
            problems.append(
                f"build stamp names {stamp.model_id!r} v{stamp.ir_version}, "
                f"not {ctx.model_id!r} v{ctx.ir_version}")

        stale = []
        for fmt, path in sorted(ctx.exports.items()):
            try:
                if os.path.getmtime(path) < stamp.started_at:
                    stale.append(fmt)
            except OSError:  # vanished mid-grade: exportability reports that
                continue
        if stale:
            problems.append(
                f"export(s) {stale} predate attempt {stamp.attempt_id} — they are "
                "leftovers, not this build's delivery")

        if problems:
            return _r(self, "fail", "artefacts are not all from this build: "
                      + "; ".join(problems),
                      measurements={"attempt_id": stamp.attempt_id,
                                    "stale_formats": stale,
                                    "problems": problems})

        return _r(self, "pass",
                  f"digest, IR and {len(ctx.exports)} export(s) all trace to "
                  f"attempt {stamp.attempt_id}",
                  measurements={"attempt_id": stamp.attempt_id,
                                "checked_exports": float(len(ctx.exports))},
                  evidence=list(ctx.exports.values()))


ALL_SOLID_CHECKS = (
    SolidValidityCheck,
    SolidCountCheck,
    BBoxSpecCheck,
    MassSpecCheck,
    SketchFullyConstrainedCheck,
    RoundTripCheck,
    ExportabilityCheck,
    WallThicknessCheck,
    ProvenanceCheck,
)
