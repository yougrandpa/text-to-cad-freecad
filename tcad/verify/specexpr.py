"""``ConstraintExpr`` evaluator (design §4.6 item 5).

``evaluate(expr, digest, ir) -> (passed, info)`` where ``passed`` is:

  * ``True``  — measured value satisfies the expression within ``expr.tol``
  * ``False`` — it does not
  * ``None``  — the value cannot be measured from the available artefacts
               (caller should SKIP, never fake a pass)

``info`` always carries the measured value and the expected value so the Gate
report can show the model *exactly* what missed. ``info`` has the shape
``{"measured": <json>, "expected": <json>, "tol": <float>}``.

Tolerance semantics per kind (documented, not guessed):
  * bbox            — absolute mm per axis (axis-aligned, matches design §4.6:
                     "轴对齐尺寸校验用 BoundBox")
  * volume          — relative ratio (design §4.6: ±1%)
  * count           — absolute integer (number of solids)
  * hole_diameter   — absolute mm, against the DIAMETER MEASURED OFF THE BREP
                     (``digest.holes``), never the IR's declared parameter
  * hole_position   — absolute mm, as the perpendicular distance from the
                     expected point to a measured hole axis
  * symmetric       — structural: a mirrored/pattern counterpart must exist
  * wall_thickness  — absolute mm minimum (read from digest if the worker
                       measured it; otherwise None -> SKIP)
  * feature_count   — absolute integer (number of features in the IR)

A hole requirement with no BRep hole evidence returns ``None`` (unverified)
rather than falling back to the IR: the declared number is the thing under
test, not a witness.
"""

from __future__ import annotations

from tcad.core.types import GeometryDigest, IrDocument
from tcad.ir.schema import ConstraintExpr

# mm. Decides only WHICH measured hole a targeted expression refers to; it is
# never a pass criterion (the expression's own ``tol`` is).
_HOLE_MATCH_TOL = 0.5


def _bbox_measured(d: GeometryDigest) -> dict[str, float]:
    return {"x": d.bbox.x, "y": d.bbox.y, "z": d.bbox.z}


def evaluate(
    expr: ConstraintExpr, digest: GeometryDigest, ir: IrDocument
) -> tuple[bool | None, dict]:
    kind = expr.kind
    tol = expr.tol
    expected = expr.value

    if kind == "bbox":
        if digest is None or not digest.measurements_available:
            return None, {"measured": None, "expected": expected, "tol": tol}
        m = _bbox_measured(digest)
        exp = expected if isinstance(expected, dict) else {"x": float(expected),
                                                           "y": float(expected),
                                                           "z": float(expected)}
        ok = all(abs(m.get(ax, 0.0) - float(exp.get(ax, 0.0))) <= tol
                 for ax in ("x", "y", "z"))
        return ok, {"measured": m, "expected": exp, "tol": tol}

    if kind == "volume":
        if digest is None or not digest.measurements_available:
            return None, {"measured": None, "expected": expected, "tol": tol}
        m = digest.volume
        exp = float(expected)
        rel = abs(m - exp) / max(abs(exp), 1e-12)
        return rel <= tol, {"measured": m, "expected": exp, "tol": tol,
                            "rel_error": rel}

    if kind == "count":
        if digest is None:
            return None, {"measured": None, "expected": expected, "tol": tol}
        m = digest.topology.solids
        exp = int(expected)
        return abs(m - exp) <= max(int(tol), 0), {"measured": m, "expected": exp,
                                                  "tol": tol}

    if kind == "feature_count":
        m = len(ir.all_features())
        exp = int(expected)
        return abs(m - exp) <= max(int(tol), 0), {"measured": m, "expected": exp,
                                                  "tol": tol}

    if kind == "hole_diameter":
        return _hole_measure(expr, digest, ir, "diameter")

    if kind == "hole_position":
        return _hole_measure(expr, digest, ir, "position")

    if kind == "symmetric":
        # Best-effort structural check: a mirrored / linear / circular pattern
        # feature must reference the target (or a feature named "<target>_mirror"
        # / "<target>_pattern" must exist). Approximate by nature.
        target = expr.target
        if not target:
            return None, {"measured": None, "expected": target, "tol": tol}
        counterpart = _find_symmetric_counterpart(target, ir)
        ok = counterpart is not None
        return ok, {"measured": {"counterpart": counterpart}, "expected": target,
                    "tol": tol}

    if kind == "wall_thickness":
        if digest is None or not digest.measurements_available:
            return None, {"measured": None, "expected": expected, "tol": tol}
        mw = digest.key_dimensions.get("min_wall_thickness")
        if mw is None:
            return None, {"measured": None, "expected": expected, "tol": tol}
        m = float(mw)
        exp = float(expected)
        return m >= (exp - tol), {"measured": m, "expected": exp, "tol": tol}

    # Unknown kind — never pretend.
    return None, {"measured": None, "expected": expected, "tol": tol}


# ── helpers ────────────────────────────────────────────────────────────────


def _matched_holes(expr: ConstraintExpr, ir: IrDocument):
    """Holes the expression can be judged against.

    Two supported constructions, by design:
      * ``op == "hole"`` features (parametric hole op).
      * ``op == "pocket"`` features whose profile sketch is exactly one
        non-construction circle — a circular pocket IS a hole.

    Anything else (rectangular pocket, multi-circle sketch, no profile) is
    not matched rather than guessed at: an expression judged against a
    guessed hole would report a fabricated measurement.
    """
    holes = [f for f in ir.all_features()
             if f.op == "hole" or (f.op == "pocket" and _circle_of(f, ir) is not None)]
    if expr.target:
        holes = [h for h in holes if h.id == expr.target or h.name == expr.target]
    return holes


def _circle_of(feature, ir: IrDocument):
    """The single non-construction circle of the feature's profile sketch,
    or None when the sketch has zero or several (not an unambiguous hole)."""
    if not feature.profile_sketch:
        return None
    sk = ir.find_sketch(feature.profile_sketch)
    if sk is None:
        return None
    circles = [g for g in sk.geometry
               if g.kind == "circle" and not g.construction and g.points]
    if len(circles) != 1:
        return None
    return circles[0]


def _nominal_axis(feature, ir: IrDocument):
    """Where the IR *asked* for the hole, as a point on its axis.

    Used only to decide which measured hole a targeted expression refers to —
    never as the value that is compared. Reporting a declared position as a
    measured one is exactly the self-grading this module exists to prevent.
    """
    p = feature.params
    if all(k in p for k in ("x", "y", "z")):
        return [float(p["x"]), float(p["y"]), float(p["z"])]
    circle = _circle_of(feature, ir)
    if circle is not None and circle.points:
        c = circle.points[0]
        return [float(c.x), float(c.y), float(c.z)]
    return None


def _axis_distance(point, hole) -> float:
    """Perpendicular distance from *point* to a measured hole's axis."""
    foot = hole.get("center") or []
    axis = hole.get("axis") or []
    if len(foot) != 3 or len(axis) != 3:
        return float("inf")
    w = [point[i] - foot[i] for i in range(3)]
    cross = [
        w[1] * axis[2] - w[2] * axis[1],
        w[2] * axis[0] - w[0] * axis[2],
        w[0] * axis[1] - w[1] * axis[0],
    ]
    return (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5


def _measured_holes(digest) -> list[dict]:
    out = []
    for h in (getattr(digest, "holes", None) or []):
        out.append(h if isinstance(h, dict) else h.model_dump())
    return out


def _hole_measure(expr: ConstraintExpr, digest, ir: IrDocument, what: str):
    """Judge a hole requirement against BRep measurements, never IR numbers.

    The IR contributes only the nominal axis that identifies WHICH measured
    hole a ``target``-bound expression is about; the compared diameter, depth
    and position all come from ``digest.holes``, which the worker measured off
    the built shape. A declared hole the kernel never produced therefore shows
    up as "no measured hole at this axis" (FAIL), not as a pass.
    """
    expected, tol = expr.value, expr.tol
    measured = _measured_holes(digest)
    declared = _matched_holes(expr, ir)

    if not measured:
        reason = ("no hole declared and none measured" if not declared else
                  "the worker digest carries no BRep hole measurement — "
                  "hole size/position cannot be verified")
        return None, {"measured": None, "expected": expected, "tol": tol,
                      "reason": reason}

    if expr.target:
        target = ir.find_feature(expr.target)
        nominal = _nominal_axis(target, ir) if target is not None else None
        if nominal is None:
            # Which measured hole does this expression mean? Without a declared
            # axis there is no honest answer, and judging against every hole
            # would silently grade the wrong one.
            return None, {"measured": None, "expected": expected, "tol": tol,
                          "reason": (f"cannot identify which measured hole "
                                     f"{expr.target!r} refers to")}
        window = max(_HOLE_MATCH_TOL, tol)
        near = [h for h in measured if _axis_distance(nominal, h) <= window]
        if not near:
            return False, {
                "measured": {f"hole{h['index']}": h["center"] for h in measured},
                "expected": nominal, "tol": tol,
                "reason": (f"no measured hole axis passes near the declared "
                           f"position {nominal} of {expr.target!r} "
                           f"(within {window} mm)"),
            }
        measured = near

    if what == "diameter":
        values = {f"hole{h['index']}": float(h["diameter"]) for h in measured}
        exp = float(expected)
        ok = all(abs(v - exp) <= tol for v in values.values())
        return ok, {"measured": values, "expected": exp, "tol": tol,
                    "through": {f"hole{h['index']}": bool(h["through"]) for h in measured},
                    "depth": {f"hole{h['index']}": float(h["depth"]) for h in measured}}

    point = expected if isinstance(expected, dict) else {}
    axes = [
        float(point.get(ax, 0.0)) for ax in ("x", "y", "z")
    ]
    nearest = min(measured, key=lambda h: _axis_distance(axes, h), default=None)
    if nearest is None:
        return None, {"measured": None, "expected": point, "tol": tol}
    distance = _axis_distance(axes, nearest)
    values = {f"hole{h['index']}": h["center"] for h in measured}
    return distance <= tol, {"measured": values, "expected": point, "tol": tol,
                             "axis_distance_mm": round(distance, 9)}


def _find_symmetric_counterpart(target: str, ir: IrDocument):
    feat = ir.find_feature(target)
    if feat is None:
        # maybe target is a sketch
        sk = ir.find_sketch(target)
        if sk is None:
            return None
    mirrored_ops = {"mirrored", "linear_pattern", "circular_pattern", "polar_pattern"}
    for f in ir.all_features():
        if f.op in mirrored_ops and target in f.refs:
            return f.id
        if f.id == f"{target}_mirror" or f.id == f"{target}_pattern":
            return f.id
    return None
