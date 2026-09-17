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


def tessellate(ir: dict | None = None, out_dir: str = "", tolerance: float = None,
               views=None, **_extra) -> dict:
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
    shape = built["result_shape"]

    mesh = None
    if shape is not None and not shape.isNull():
        try:
            verts, facets = shape.tessellate(tolerance)
            vertices = [[float(v.x), float(v.y), float(v.z)] for v in verts]
            facet_list = [[int(a), int(b), int(c)] for (a, b, c) in facets]
            measure = _measure(shape)
            mesh = {
                "vertices": vertices,
                "facets": facet_list,
                "bbox": measure["bbox"],
                "volume": measure["volume"],
                "tolerance": tolerance,
            }
        except Exception as exc:  # noqa: BLE001
            _close_doc(built["doc"])
            return {"ok": False, "error": f"tessellate failed: {type(exc).__name__}: {exc}",
                    "mesh": None}

    _close_doc(built["doc"])

    if mesh is None:
        return {"ok": False, "error": "no solid to tessellate", "mesh": None}

    mesh["vertex_count"] = len(mesh["vertices"])
    mesh["facet_count"] = len(mesh["facets"])
    return {"ok": True, "mesh": mesh}
