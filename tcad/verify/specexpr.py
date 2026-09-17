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
  * hole_diameter   — absolute mm per matched hole
  * hole_position   — absolute mm per axis of the hole's placement
  * symmetric       — structural: a mirrored/pattern counterpart must exist
  * wall_thickness  — absolute mm minimum (read from digest if the worker
                       measured it; otherwise None -> SKIP)
  * feature_count   — absolute integer (number of features in the IR)
"""

from __future__ import annotations

from tcad.core.types import GeometryDigest, IrDocument
from tcad.ir.schema import ConstraintExpr


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
        holes = _matched_holes(expr, ir)
        if not holes:
            return None, {"measured": None, "expected": expected, "tol": tol}
        measured = {h.id: float(h.params.get("Diameter", 0.0)) for h in holes}
        exp = float(expected)
        ok = all(abs(v - exp) <= tol for v in measured.values())
        return ok, {"measured": measured, "expected": exp, "tol": tol}

    if kind == "hole_position":
        holes = _matched_holes(expr, ir)
        if not holes:
            return None, {"measured": None, "expected": expected, "tol": tol}
        measured = {h.id: _hole_position(h) for h in holes}
        if any(v is None for v in measured.values()):
            return None, {"measured": measured, "expected": expected, "tol": tol}
        exp = expected if isinstance(expected, dict) else {}
        ok = all(
            abs(float(measured[h.id].get(ax, 0.0)) - float(exp.get(ax, 0.0))) <= tol
            for h in holes for ax in ("x", "y", "z")
        )
        return ok, {"measured": measured, "expected": exp, "tol": tol}

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
    holes = [f for f in ir.all_features() if f.op == "hole"]
    if expr.target:
        holes = [h for h in holes if h.id == expr.target or h.name == expr.target]
    return holes


def _hole_position(feature):
    p = feature.params
    if "x" in p and "y" in p and "z" in p:
        return {"x": float(p["x"]), "y": float(p["y"]), "z": float(p["z"])}
    # fall back to the profile sketch's attachment offset (best-effort)
    sk = ir.find_sketch(feature.profile_sketch) if feature.profile_sketch else None
    if sk and sk.offset is not None:
        return {"x": sk.offset.x, "y": sk.offset.y, "z": sk.offset.z}
    return None


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
