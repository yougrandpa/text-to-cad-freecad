"""Tessellate the built shape -> mesh dict (runs inside FreeCADCmd).

Returns the Mesh dict shape from tcad/core/types.py: vertices as [x,y,z] lists,
facets as [i,j,k] lists, bbox, volume, tolerance. Plus vertex/facet counts so the
supervisor can detect an over-large mesh.

CRITICAL: TopoShape.tessellate(tolerance) REQUIRES an argument (verified).
shape.tessellate() with no args raises TypeError — the .pyi is wrong.
"""

from __future__ import annotations

import os
import tempfile

from tcad.worker.compiler import _build, _close_doc, _measure
from tcad.worker.protocol import DEFAULT_TESSELLATE_TOLERANCE
from tcad.ir.motion import pose_vertices


def tessellate(ir: dict | None = None, out_dir: str = "", tolerance: float = None,
               views=None, driver_angle_deg: float = 0, **_extra) -> dict:
    """Tessellate the IR's resulting solid. ``views`` is accepted for protocol
    symmetry but the worker returns a single mesh of the whole model."""
    if not ir:
        return {"ok": False, "error": "missing ir", "mesh": None}

    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="tcad_mesh_")
    os.makedirs(out_dir, exist_ok=True)

    if tolerance is None:
        tolerance = DEFAULT_TESSELLATE_TOLERANCE
    tolerance = float(tolerance)

    built = _build(ir, out_dir)
    # Tessellating a shape the build failed to produce would hand the
    # renderer a stale/partial Tip as if it were the declared result.
    if built["errors"]:
        _close_doc(built["doc"])
        return {"ok": False, "error": "; ".join(
            f'{e.get("feature_id") or "?"}: {e.get("message")}' for e in built["errors"]
        ), "mesh": None}
    shape = built["result_shape"]

    mesh = None
    motion = []
    if shape is not None and not shape.isNull():
        try:
            verts, facets = shape.tessellate(tolerance)
            vertices = [[float(v.x), float(v.y), float(v.z)] for v in verts]
            facet_list = [[int(a), int(b), int(c)] for (a, b, c) in facets]
            motions = {b["id"]: b["motion"] for b in ir.get("bodies", []) if b.get("motion")}
            if motions:
                # Per-body ranges preserve rigid parts in the combined preview.
                # Tessellate the actual BRep, never substitute synthetic shapes.
                vertices, facet_list = [], []
                for body in built["body_results"]:
                    verts, facets = body["shape"].tessellate(tolerance)
                    start = len(vertices)
                    vertices.extend([[float(v.x), float(v.y), float(v.z)] for v in verts])
                    facet_list.extend([[int(a)+start, int(b)+start, int(c)+start] for a, b, c in facets])
                    if body["id"] in motions:
                        motion.append({"body_id": body["id"], "vertex_start": start,
                                       "vertex_count": len(verts), **motions[body["id"]]})
            if driver_angle_deg:
                vertices = pose_vertices(vertices, motion, driver_angle_deg)
            measure = _measure(shape)
            mesh = {
                "vertices": vertices,
                "facets": facet_list,
                "bbox": measure["bbox"],
                "volume": measure["volume"],
                "tolerance": tolerance,
            }
            if driver_angle_deg:
                lows = [min(p[j] for p in vertices) for j in range(3)]
                highs = [max(p[j] for p in vertices) for j in range(3)]
                mesh["bbox"] = dict(zip(("x", "y", "z", "x_min", "y_min", "z_min"),
                                        [highs[j]-lows[j] for j in range(3)] + lows))
        except Exception as exc:  # noqa: BLE001
            _close_doc(built["doc"])
            return {"ok": False, "error": f"tessellate failed: {type(exc).__name__}: {exc}",
                    "mesh": None}

    _close_doc(built["doc"])

    if mesh is None:
        return {"ok": False, "error": "no solid to tessellate", "mesh": None}

    mesh["vertex_count"] = len(mesh["vertices"])
    mesh["facet_count"] = len(mesh["facets"])
    return {"ok": True, "mesh": mesh, "motion": motion}
