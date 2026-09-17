"""Document -> GeometryDigest (runs inside FreeCADCmd).

Produces the GeometryDigest dict shape documented in tcad/core/types.py
(GeometryDigest / Topology / BBox / FeatureDigest). Read-only measurements only;
the digest is program-generated, never model-summarised.

Measurement truth: shape.BoundBox (axis-aligned) for the box, shape.isValid()
as the primary validity signal, shape.check() inside try/except (it returns None
on success and RAISES on failure — never write ``if shape.check():``).
"""

from __future__ import annotations

import os
import tempfile

from tcad.worker.compiler import _build, _measure, _close_doc


def _render_text(model_id: str, ir_version: int, feature_chain, measure: dict,
                 sketches) -> str:
    """Deterministic, compact, <=~2000-token text rendering of the digest."""
    lines = []
    lines.append(f"model: {model_id} (v{ir_version})")
    lines.append(f"feature chain ({len(feature_chain)}):")
    for i, fc in enumerate(feature_chain, 1):
        params = fc.get("params") or {}
        ps = " ".join(f"{k}={v}" for k, v in params.items())
        flag = " [suppressed]" if fc.get("suppressed") else ""
        lines.append(f"  {i}. {fc.get('name')} [{fc.get('op')}]{flag} {ps}".rstrip())
    topo = measure
    lines.append(
        f"topology: solids={topo['solids']} faces={topo['faces']} "
        f"edges={topo['edges']} vertexes={topo['vertexes']} shells={topo['shells']}"
    )
    bb = topo["bbox"]
    lines.append(
        f"bbox: x={bb['x']:.4f} y={bb['y']:.4f} z={bb['z']:.4f} "
        f"(min x={bb['x_min']:.4f} y={bb['y_min']:.4f} z={bb['z_min']:.4f})"
    )
    lines.append(f"volume: {topo['volume']:.6f}  area: {topo['area']:.6f}")
    lines.append(f"shape_type: {topo['shape_type']}  valid: {topo['is_valid']}")
    if sketches:
        lines.append("sketches:")
        for sk in sketches:
            fc_state = "?" if sk["fully_constrained"] is None else (
                "yes" if sk["fully_constrained"] else "no")
            lines.append(
                f"  {sk['name']}: fully_constrained={fc_state} "
                f"dof={sk['dof']} solve_status={sk['solve_status']}"
            )
    return "\n".join(lines)


def _key_dimensions(measure: dict, sketches: list) -> dict:
    """Build the digest's ``key_dimensions`` blob.

    Besides the axis-aligned extents, this carries the **reserved per-sketch
    keys** that are the agreed carrier for constraint state between the worker
    and the Gate:

        "<sketch_id>__fully_constrained"  -> 1.0 / 0.0
        "<sketch_id>__dof"                -> float

    ``tcad/verify/checks_solid.py::SketchFullyConstrainedCheck`` reads exactly
    these. If they are missing the check can only SKIP — which would silently
    drop the "every required sketch must be fully constrained" invariant out of
    the Gate. Keys are emitted ONLY when the state was actually measured, so an
    unmeasurable sketch produces an honest SKIP rather than a fake zero.

    Keyed by sketch **id** (not name): the id is the stable handle the IR and
    the Gate both refer to.
    """
    out: dict = {
        "x": measure["bbox"]["x"], "y": measure["bbox"]["y"],
        "z": measure["bbox"]["z"],
    }
    for sk in sketches or []:
        sid = sk.get("id")
        if not sid:
            continue
        fc = sk.get("fully_constrained")
        if fc is not None:
            out[f"{sid}__fully_constrained"] = 1.0 if fc else 0.0
        dof = sk.get("dof")
        if dof is not None:
            out[f"{sid}__dof"] = float(dof)
    return out


def introspect_document(ir: dict | None = None, out_dir: str = "", **_extra) -> dict:
    """Return a GeometryDigest-shaped dict for the IR (rebuilds the document)."""
    if not ir:
        return {"ok": False, "error": "missing ir"}

    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="tcad_introspect_")
    os.makedirs(out_dir, exist_ok=True)

    built = _build(ir, out_dir)
    shape = built["result_shape"]
    measure = _measure(shape)

    digest = {
        "model_id": ir.get("model_id") or "",
        "ir_version": int(ir.get("version", 0)),
        "feature_chain": [
            {"id": fc["id"], "name": fc["name"], "op": fc["op"],
             "params": fc["params"], "suppressed": fc["suppressed"]}
            for fc in built["feature_chain"]
        ],
        "topology": {
            "solids": measure["solids"], "faces": measure["faces"],
            "edges": measure["edges"], "vertexes": measure["vertexes"],
            "shells": measure["shells"],
        },
        "bbox": measure["bbox"],
        "volume": measure["volume"],
        "area": measure["area"],
        "shape_type": measure["shape_type"],
        "is_valid": measure["is_valid"],
        "key_dimensions": _key_dimensions(measure, built["sketches"]),
        "spec_deviation": {},
        "measurements_available": shape is not None and not shape.isNull(),
        "text": "",
    }
    digest["text"] = _render_text(
        digest["model_id"], digest["ir_version"], built["feature_chain"], measure,
        built["sketches"],
    )

    _close_doc(built["doc"])
    digest["ok"] = True
    return digest
