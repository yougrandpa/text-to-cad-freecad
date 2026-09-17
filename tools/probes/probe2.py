"""Probe #2: nail down BoundBox/tessellate semantics + unknown property names."""

import json
import sys
import tempfile

import FreeCAD
import Part
import Sketcher

App = FreeCAD
OUT = {"claims": {}, "props": {}}


def safe(fn, default="FAIL"):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"{default} {type(exc).__name__}: {exc}"


doc = FreeCAD.newDocument("Probe2")
body = doc.addObject("PartDesign::Body", "Body")
sk = doc.addObject("Sketcher::SketchObject", "Sk")
sk.AttachmentSupport = (doc.XY_Plane, [""])
sk.MapMode = "FlatFace"
body.addObject(sk)
sk.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(60, 0, 0)))
sk.addGeometry(Part.LineSegment(App.Vector(60, 0, 0), App.Vector(60, 40, 0)))
sk.addGeometry(Part.LineSegment(App.Vector(60, 40, 0), App.Vector(0, 40, 0)))
sk.addGeometry(Part.LineSegment(App.Vector(0, 40, 0), App.Vector(0, 0, 0)))
sk.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))
for i in range(4):
    sk.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
sk.addConstraint(Sketcher.Constraint("Horizontal", 0))
sk.addConstraint(Sketcher.Constraint("Horizontal", 2))
sk.addConstraint(Sketcher.Constraint("Vertical", 1))
sk.addConstraint(Sketcher.Constraint("Vertical", 3))
i = sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 2))
sk.setDatum(i, App.Units.Quantity("60 mm"))
i = sk.addConstraint(Sketcher.Constraint("DistanceY", 1, 2))
sk.setDatum(i, App.Units.Quantity("40 mm"))
doc.recompute()
pad = doc.addObject("PartDesign::Pad", "Pad")
pad.Profile = sk
pad.Length = 10.0
pad.Type = "Length"
body.addObject(pad)
doc.recompute()
shape = pad.Shape

# ── Q1: does TopoShape really expose BoundBox?
OUT["claims"]["hasattr_BoundBox"] = hasattr(shape, "BoundBox")
try:
    bb = shape.BoundBox
    OUT["claims"]["BoundBox_runtime"] = {
        "type": type(bb).__name__,
        "XLength": float(bb.XLength), "YLength": float(bb.YLength), "ZLength": float(bb.ZLength),
        "XMin": float(bb.XMin), "YMin": float(bb.YMin), "ZMin": float(bb.ZMin),
    }
except Exception as exc:  # noqa: BLE001
    OUT["claims"]["BoundBox_runtime"] = f"FAIL {type(exc).__name__}: {exc}"
OUT["claims"]["BoundBox_in_dir"] = "BoundBox" in dir(shape)

# ── Q2: tessellate signature
for arg_label, arg in (("no_arg", ()), ("float_0.5", (0.5,)), ("float_0.1", (0.1,))):
    try:
        v, f = shape.tessellate(*arg)
        OUT["claims"][f"tessellate_{arg_label}"] = {
            "vertices": len(v), "facets": len(f), "facet_repr": repr(f[0])[:90],
            "vertex_type": type(v[0]).__name__,
        }
    except Exception as exc:  # noqa: BLE001
        OUT["claims"][f"tessellate_{arg_label}"] = f"FAIL {type(exc).__name__}: {exc}"

# ── Q3: unknown property names (design doc issue #10)
for type_name, label in (
    ("PartDesign::Hole", "Hole"),
    ("PartDesign::LinearPattern", "LinearPattern"),
    ("PartDesign::CircularPattern", "CircularPattern"),
    ("PartDesign::Fillet", "Fillet"),
    ("PartDesign::Chamfer", "Chamfer"),
    ("PartDesign::Revolution", "Revolution"),
    ("PartDesign::Groove", "Groove"),
    ("PartDesign::Mirrored", "Mirrored"),
):
    try:
        obj = doc.addObject(type_name, f"P_{label}")
        props = [p for p in obj.PropertiesList if not p.startswith("_")]
        OUT["props"][label] = sorted(set(props))
        doc.removeObject(obj.Name)
        doc.recompute()
    except Exception as exc:  # noqa: BLE001
        OUT["props"][label] = f"FAIL {type(exc).__name__}: {exc}"

# ── Q4: Part view/observer classes available headlessly? (render feasibility)
OUT["claims"]["Part_has_viewProvider"] = hasattr(Part, "ViewProvider")
OUT["claims"]["GuiUp"] = bool(FreeCAD.GuiUp)
try:
    import FreeCADGui  # noqa: F401
    OUT["claims"]["FreeCADGui_importable"] = True
except Exception as exc:  # noqa: BLE001
    OUT["claims"]["FreeCADGui_importable"] = f"FAIL {type(exc).__name__}"

OUT["tmpdir"] = tempfile.mkdtemp(prefix="tcad_probe2_")
sys.stdout.write("###PROBE2###" + json.dumps(OUT, ensure_ascii=False, default=str) + "\n")
sys.stdout.flush()
