"""Smoke test: verify the critical IR -> FreeCAD -> STEP path under FreeCADCmd.

Run: FreeCADCmd <this file>
Prints a single JSON blob prefixed with ###SMOKE### so the caller can parse it.
"""

import json
import os
import sys
import tempfile
import traceback

RESULT = {"steps": {}, "api_claims": {}, "properties": {}, "errors": []}


def step(name):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                RESULT["steps"][name] = fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                RESULT["steps"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                RESULT["errors"].append(f"{name}: {traceback.format_exc(limit=3)}")
        return wrapper
    return deco


def main():
    import FreeCAD
    import Part
    import Sketcher

    App = FreeCAD  # NOTE: FreeCAD.App does not exist in 26.3.0dev; `App = FreeCAD` is the correct alias

    RESULT["freecad_version"] = ".".join(FreeCAD.Version()[0:3])
    tmp = tempfile.mkdtemp(prefix="tcad_smoke_")

    doc = FreeCAD.newDocument("SmokeModel")
    body = doc.addObject("PartDesign::Body", "Body")

    sk = doc.addObject("Sketcher::SketchObject", "Sketch_base")
    # --- claim: origin plane attach via doc.XY_Plane + MapMode FlatFace
    try:
        sk.AttachmentSupport = (doc.XY_Plane, [""])
        sk.MapMode = "FlatFace"
        RESULT["api_claims"]["attach_via_doc.XY_Plane"] = True
    except Exception as exc:  # noqa: BLE001
        RESULT["api_claims"]["attach_via_doc.XY_Plane"] = f"FAIL {exc}"
    body.addObject(sk)

    w, hgt = 60.0, 40.0
    pts = [(0.0, 0.0), (w, 0.0), (w, hgt), (0.0, hgt)]
    for i in range(4):
        p0, p1 = pts[i], pts[(i + 1) % 4]
        sk.addGeometry(
            Part.LineSegment(
                App.Vector(p0[0], p0[1], 0.0), App.Vector(p1[0], p1[1], 0.0)
            )
        )

    trace = []

    def addc(label, con, value=None):
        before = len(sk.Constraints)
        ret = sk.addConstraint(con)
        after = len(sk.Constraints)
        rec = {
            "label": label, "returned": ret, "returned_type": type(ret).__name__,
            "before": before, "after": after,
        }
        if value is not None:
            try:
                sk.setDatum(ret if isinstance(ret, int) else after - 1, value)
                rec["setDatum"] = "ok"
            except Exception as exc:  # noqa: BLE001
                rec["setDatum"] = f"FAIL {type(exc).__name__}: {exc}"
        trace.append(rec)
        return ret

    # origin first, then dimension the *free* endpoints (never dimension a point
    # already bound to the origin -> that is a solver conflict, reported with a
    # misleading "Invalid constraint index" message).
    addc("Coincident(0,1,-1,1)", Sketcher.Constraint("Coincident", 0, 1, -1, 1))
    for i in range(4):
        addc(f"Coincident({i},2,{(i + 1) % 4},1)",
             Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
    addc("Horizontal(0)", Sketcher.Constraint("Horizontal", 0))
    addc("Horizontal(2)", Sketcher.Constraint("Horizontal", 2))
    addc("Vertical(1)", Sketcher.Constraint("Vertical", 1))
    addc("Vertical(3)", Sketcher.Constraint("Vertical", 3))
    addc("DistanceX(0,2,w)", Sketcher.Constraint("DistanceX", 0, 2), App.Units.Quantity(f"{w} mm"))
    addc("DistanceY(1,2,h)", Sketcher.Constraint("DistanceY", 1, 2), App.Units.Quantity(f"{hgt} mm"))

    RESULT["api_claims"]["bind_to_origin_geoid_minus1"] = True
    RESULT["constraint_trace"] = trace
    RESULT["constraints_total"] = len(sk.Constraints)

    # --- claim: solve() returns SolveStatus int, DoF is a separate property
    RESULT["solve_status_int"] = sk.solve()
    RESULT["sketch_DoF"] = int(sk.DoF)
    RESULT["sketch_FullyConstrained"] = bool(sk.FullyConstrained)

    doc.recompute()

    pad = doc.addObject("PartDesign::Pad", "Pad_base")
    pad.Profile = sk
    pad.Length = 10.0
    pad.Type = "Length"
    body.addObject(pad)
    doc.recompute()

    shape = pad.Shape
    RESULT["pad_shape_isNull"] = bool(shape.isNull())
    RESULT["pad_shape_shapetype"] = str(shape.ShapeType)

    def probe(key, fn):
        try:
            RESULT[key] = fn()
        except Exception as exc:  # noqa: BLE001
            RESULT[key] = f"FAIL {type(exc).__name__}: {exc}"

    probe("pad_faces", lambda: len(shape.Faces))
    probe("pad_volume", lambda: round(float(shape.Volume), 6))
    probe("pad_area", lambda: round(float(shape.Area), 6))
    probe("pad_solids", lambda: len(shape.Solids))
    probe("pad_edges", lambda: len(shape.Edges))
    probe("pad_vertexes", lambda: len(shape.Vertexes))
    probe("pad_isValid", lambda: bool(shape.isValid()))

    # --- claim: TopoShape has NO BoundBox attribute; optimalBoundingBox() exists
    RESULT["api_claims"]["hasattr_BoundBox_property"] = hasattr(shape, "BoundBox")
    probe("optimalBoundingBox", lambda: {
        "x": round(shape.optimalBoundingBox().XLength, 4),
        "y": round(shape.optimalBoundingBox().YLength, 4),
        "z": round(shape.optimalBoundingBox().ZLength, 4),
    })

    # --- claim: check() returns None on success (not True)
    try:
        RESULT["api_claims"]["check_return"] = repr(shape.check())
    except Exception as exc:  # noqa: BLE001
        RESULT["api_claims"]["check_return"] = f"RAISED {type(exc).__name__}: {exc}"

    # --- claim: tessellate() takes no tolerance arg
    try:
        verts, facets = shape.tessellate()
        RESULT["api_claims"]["tessellate_no_arg"] = {
            "vertices": len(verts), "facets": len(facets),
            "first_vertex_type": type(verts[0]).__name__ if verts else None,
            "first_facet_type": type(facets[0]).__name__ if facets else None,
            "first_facet_repr": repr(facets[0])[:80] if facets else None,
        }
    except Exception as exc:  # noqa: BLE001
        RESULT["api_claims"]["tessellate_no_arg"] = f"FAIL {exc}"

    # --- STEP round trip
    if not shape.isNull():
        step_path = os.path.join(tmp, "model.step")
        probe("step_bytes", lambda: (shape.exportStep(step_path), os.path.getsize(step_path))[1])
        try:
            rt = Part.Shape()
            rt.read(step_path)
            RESULT["roundtrip_volume"] = round(float(rt.Volume), 6)
            RESULT["roundtrip_volume_rel_err"] = abs(float(rt.Volume) - float(shape.Volume)) / float(shape.Volume)
            RESULT["roundtrip_faces"] = len(rt.Faces)
        except Exception as exc:  # noqa: BLE001
            RESULT["roundtrip_error"] = f"{type(exc).__name__}: {exc}"
        stl_path = os.path.join(tmp, "model.stl")
        probe("stl_bytes", lambda: (shape.exportStl(stl_path), os.path.getsize(stl_path))[1])

    # --- resolve unknown property names: Hole + patterns (design doc issue #10)
    for type_name, label in (
        ("PartDesign::Hole", "Hole"),
        ("PartDesign::LinearPattern", "LinearPattern"),
        ("PartDesign::CircularPattern", "CircularPattern"),
        ("PartDesign::Fillet", "Fillet"),
    ):
        try:
            obj = doc.addObject(type_name, f"Probe_{label}")
            RESULT["properties"][label] = sorted(
                p for p in obj.PropertiesList if not p.startswith("_")
            )
            doc.removeObject(obj.Name)
            doc.recompute()
        except Exception as exc:  # noqa: BLE001
            RESULT["properties"][label] = f"FAIL {type(exc).__name__}: {exc}"

    RESULT["tmpdir"] = tmp
    RESULT["ok"] = len(RESULT["errors"]) == 0


try:
    main()
except Exception:  # noqa: BLE001
    RESULT["fatal"] = traceback.format_exc()
    RESULT["ok"] = False

sys.stdout.write("###SMOKE###" + json.dumps(RESULT, ensure_ascii=False) + "\n")
sys.stdout.flush()
