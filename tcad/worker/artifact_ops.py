"""Export and BRep inspection of pinned documents, with no source compilation."""

import hashlib
import math
import os
from contextlib import contextmanager
from itertools import combinations

import FreeCAD as App
import Part

from tcad.worker.compiler import _close_doc, _obj_name
from tcad.worker.exporters import _safe_component


@contextmanager
def artifact_shapes(path, sha256, bodies):
    with open(path, "rb") as source:
        if hashlib.sha256(source.read()).hexdigest() != sha256:
            raise ValueError("artifact document failed its integrity check")
    doc = App.openDocument(path)
    try:
        shapes = {}
        for body in bodies:
            obj = doc.getObject(_obj_name(body["id"], body["name"]))
            if obj is None or obj.Shape.isNull():
                raise ValueError("artifact contains no requested body")
            shapes[body["id"]] = obj.Shape.copy()
        yield shapes
    finally:
        _close_doc(doc)


def export_saved(path, sha256, bodies, out_dir, fmt, name, **_extra):
    if fmt not in {"step", "stl", "brep"}:
        raise ValueError("saved geometry converter supports step/stl/brep")
    name = _safe_component(name, kind="export name")
    with artifact_shapes(path, sha256, bodies) as shapes:
        shape = Part.makeCompound(list(shapes.values())) if len(shapes) > 1 else next(iter(shapes.values()))
        destination = os.path.join(out_dir, name + "." + fmt)
        os.makedirs(out_dir, exist_ok=True)
        {"step": shape.exportStep, "stl": shape.exportStl, "brep": shape.exportBrep}[fmt](destination)
    return {"ok": True, "path": destination}


def check_saved_motion(path, sha256, bodies, angles=None, motion=None, frames=None,
                       pairs=None, volume_tolerance=1e-6, check_stride=1, measure_distance=False, **_extra):
    motion = {p["body_id"]: p for p in motion or []}
    if frames is None:
        angles = angles if angles is not None else [0, 90, 180, 270, 360]
        if not 1 <= len(angles) <= 73 or any(not math.isfinite(a) or abs(a) > 720 for a in angles):
            raise ValueError("invalid sampled angles")
    elif not 1 <= len(frames) <= 600:
        raise ValueError("invalid saved animation frames")
    if not math.isfinite(volume_tolerance) or volume_tolerance < 0 or not 1 <= check_stride <= 30:
        raise ValueError("invalid interference tolerance/stride")
    with artifact_shapes(path, sha256, bodies) as shapes:
        pairs = list(combinations(shapes, 2)) if pairs is None else pairs
        if len(pairs) > 100 or any(len(p) != 2 or p[0] == p[1] or any(x not in shapes for x in p) for p in pairs):
            raise ValueError("invalid body pairs")
        findings, distances = [], []
        samples = list(range(0, len(frames), check_stride)) if frames is not None else angles
        for sample in samples:
            posed = {}
            for id, original in shapes.items():
                shape = original.copy()
                if frames is not None:
                    transform = App.Placement(App.Matrix(*frames[sample][id]))
                    shape.Placement = transform.multiply(shape.Placement)
                elif id in motion:
                    spec = motion[id]
                    shape.rotate(App.Vector(*(spec["pivot"][k] for k in ("x", "y", "z"))),
                                 App.Vector(*(spec["axis"][k] for k in ("x", "y", "z"))), sample * spec["ratio"])
                posed[id] = shape
            for a, b in pairs:
                common = posed[a].common(posed[b])
                volume = float(common.Volume)
                if not common.isValid() or not math.isfinite(volume):
                    raise ValueError("invalid artifact intersection result")
                if measure_distance:
                    distance = float(posed[a].distToShape(posed[b])[0])
                    if not math.isfinite(distance) or distance < 0:
                        raise ValueError('invalid artifact minimum distance')
                    distances.append({'frame' if frames is not None else 'angle_deg': sample,
                                      'bodies': [a,b],
                                      'min_distance_mm': 0.0 if volume > volume_tolerance else distance,
                                      'min_surface_distance_mm': distance, 'overlap_mm3': volume})
                if volume > volume_tolerance:
                    findings.append({"frame" if frames is not None else "angle_deg": sample,
                                     "bodies": [a, b], "overlap_mm3": volume})
    return {"ok": True, "interferences": findings, "sampled_clear": not findings,
            **({"pair_measurements": distances} if measure_distance else {}),
            "frames_checked": len(samples), "pairs_checked": len(pairs),
            "angles_deg": None if frames is not None else angles,
            "volume_tolerance_mm3": volume_tolerance,
            "scope": "Sampled BRep overlap only; no continuous clearance, contact forces or material removal."}
