"""api_selftest — verify every FreeCAD API claim at worker start-up.

FreeCAD's API is a moving target. This rebuilds the documented 60x40x10 Pad
and probes each API touched by compiler/introspect/mesh/exporters. If anything
disappears or changes behaviour, ``ok`` is False and ``missing`` lists the names,
so the worker fails loudly instead of dying mid-build. CircularPattern is an
experimental, build-dependent type: its failed probe remains visible with
``ok=False`` in checks and optional_missing, but does not disable the core CAD
workflow. ``fully_supported`` distinguishes that case from a complete API pass.

Runs inside FreeCADCmd. Stdlib + FreeCAD/Part/Sketcher only.
"""

from __future__ import annotations

import os
import tempfile

import FreeCAD
import Part
import Sketcher

App = FreeCAD

# This is the one known build-dependent experimental type, not a blanket escape
# hatch for missing APIs. Keep all other probes required, including verified ops.
_OPTIONAL_FEATURE_OPS = frozenset({"circular_pattern"})


def api_selftest(**_extra) -> dict:
    checks: list = []
    missing: list = []
    optional_missing: list = []

    def record(name, ok, detail="", *, required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail),
                       "required": required})
        if not ok:
            (missing if required else optional_missing).append(name)

    # ── module-level constructors ──
    try:
        Part.LineSegment(App.Vector(0, 0, 0), App.Vector(1, 0, 0))
        record("Part.LineSegment", True)
    except Exception as exc:  # noqa: BLE001
        record("Part.LineSegment", False, f"{type(exc).__name__}: {exc}")
    try:
        Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 1.0)
        record("Part.Circle", True)
    except Exception as exc:  # noqa: BLE001
        record("Part.Circle", False, f"{type(exc).__name__}: {exc}")
    try:
        Part.ArcOfCircle(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 1.0), 0.0, 1.0)
        record("Part.ArcOfCircle", True)
    except Exception as exc:  # noqa: BLE001
        record("Part.ArcOfCircle", False, f"{type(exc).__name__}: {exc}")
    try:
        Part.Point(App.Vector(0, 0, 0))
        record("Part.Point", True)
    except Exception as exc:  # noqa: BLE001
        record("Part.Point", False, f"{type(exc).__name__}: {exc}")
    try:
        Sketcher.Constraint("Coincident", 0, 1, -1, 1)
        record("Sketcher.Constraint", True)
    except Exception as exc:  # noqa: BLE001
        record("Sketcher.Constraint", False, f"{type(exc).__name__}: {exc}")

    # ── document + body + origin plane attach ──
    name = "tcad_selftest"
    if name in FreeCAD.listDocuments():
        try:
            FreeCAD.closeDocument(name)
        except Exception:  # noqa: BLE001
            pass
    doc = FreeCAD.newDocument(name)
    # Origin planes (doc.XY_Plane/XZ_Plane/YZ_Plane) are only exposed once a
    # PartDesign Body (with its Origin) exists — matching the real compiler path.
    body = doc.addObject("PartDesign::Body", "Body")
    try:
        _ = doc.XY_Plane
        record("doc.XY_Plane", True)
    except Exception as exc:  # noqa: BLE001
        record("doc.XY_Plane", False, f"{type(exc).__name__}: {exc}")

    # ── sketch + attach + geometry + constraints + solve ──
    sk = doc.addObject("Sketcher::SketchObject", "Sk")
    try:
        sk.AttachmentSupport = (doc.XY_Plane, [""])
        sk.MapMode = "FlatFace"
        record("sketch.AttachmentSupport/MapMode", True)
    except Exception as exc:  # noqa: BLE001
        record("sketch.AttachmentSupport/MapMode", False, f"{type(exc).__name__}: {exc}")

    w, h = 60.0, 40.0
    pts = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    try:
        for i in range(4):
            p0, p1 = pts[i], pts[(i + 1) % 4]
            sk.addGeometry(Part.LineSegment(
                App.Vector(p0[0], p0[1], 0.0), App.Vector(p1[0], p1[1], 0.0)))
        record("sketch.addGeometry", True)
    except Exception as exc:  # noqa: BLE001
        record("sketch.addGeometry", False, f"{type(exc).__name__}: {exc}")

    try:
        sk.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))
        for i in range(4):
            sk.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
        sk.addConstraint(Sketcher.Constraint("Horizontal", 0))
        sk.addConstraint(Sketcher.Constraint("Vertical", 1))
        i = sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 2))
        sk.setDatum(i, App.Units.Quantity(f"{w} mm"))
        i = sk.addConstraint(Sketcher.Constraint("DistanceY", 1, 2))
        sk.setDatum(i, App.Units.Quantity(f"{h} mm"))
        record("sketch.addConstraint/setDatum", True)
    except Exception as exc:  # noqa: BLE001
        record("sketch.addConstraint/setDatum", False, f"{type(exc).__name__}: {exc}")

    try:
        st = sk.solve()
        _ = int(sk.DoF)
        _ = bool(sk.FullyConstrained)
        record("sketch.solve/DoF/FullyConstrained", True, f"solve={st}")
    except Exception as exc:  # noqa: BLE001
        record("sketch.solve/DoF/FullyConstrained", False, f"{type(exc).__name__}: {exc}")

    body.addObject(sk)
    doc.recompute()

    # ── pad ──
    pad = doc.addObject("PartDesign::Pad", "Pad")
    try:
        pad.Profile = sk
        pad.Length = 10.0
        pad.Type = "Length"
        body.addObject(pad)
        doc.recompute()
        record("PartDesign::Pad", True)
    except Exception as exc:  # noqa: BLE001
        record("PartDesign::Pad", False, f"{type(exc).__name__}: {exc}")

    shape = pad.Shape

    # ── shape measurements ──
    try:
        record("shape.isValid", bool(shape.isValid()), f"valid={shape.isValid()}")
    except Exception as exc:  # noqa: BLE001
        record("shape.isValid", False, f"{type(exc).__name__}: {exc}")

    try:
        rc = shape.check()
        record("shape.check returns None", rc is None, f"check()={rc!r}")
    except Exception as exc:  # noqa: BLE001
        record("shape.check returns None", False, f"check raised {type(exc).__name__}: {exc}")

    try:
        bb = shape.BoundBox
        _ = (bb.XLength, bb.YLength, bb.ZLength, bb.XMin, bb.YMin, bb.ZMin)
        record("shape.BoundBox", True,
               f"x={bb.XLength} y={bb.YLength} z={bb.ZLength}")
    except Exception as exc:  # noqa: BLE001
        record("shape.BoundBox", False, f"{type(exc).__name__}: {exc}")

    try:
        v, f = shape.tessellate(0.5)
        ok_t = len(v) >= 1 and len(f) >= 1
        record("shape.tessellate(0.5)", ok_t, f"vertices={len(v)} facets={len(f)}")
    except Exception as exc:  # noqa: BLE001
        record("shape.tessellate(0.5)", False, f"{type(exc).__name__}: {exc}")

    # ── exports ──
    tmp = tempfile.mkdtemp(prefix="tcad_selftest_")
    try:
        p = os.path.join(tmp, "m.step")
        shape.exportStep(p)
        record("shape.exportStep", os.path.getsize(p) > 0, f"{os.path.getsize(p)} bytes")
    except Exception as exc:  # noqa: BLE001
        record("shape.exportStep", False, f"{type(exc).__name__}: {exc}")
    try:
        p = os.path.join(tmp, "m.stl")
        shape.exportStl(p)
        record("shape.exportStl", os.path.getsize(p) > 0, f"{os.path.getsize(p)} bytes")
    except Exception as exc:  # noqa: BLE001
        record("shape.exportStl", False, f"{type(exc).__name__}: {exc}")
    try:
        p = os.path.join(tmp, "m.brep")
        shape.exportBrep(p)
        record("shape.exportBrep", os.path.getsize(p) > 0, f"{os.path.getsize(p)} bytes")
    except Exception as exc:  # noqa: BLE001
        record("shape.exportBrep", False, f"{type(exc).__name__}: {exc}")

    # ── feature type strings from FEATURE_TYPE_MAP exist ──
    from tcad.worker.compiler import FEATURE_TYPE_MAP
    for op, type_str in FEATURE_TYPE_MAP.items():
        required = op not in _OPTIONAL_FEATURE_OPS
        try:
            obj = doc.addObject(type_str, f"Probe_{op}")
            ok_f = obj is not None
            doc.removeObject(obj.Name)
            doc.recompute()
            record(f"feature:{op}", ok_f, required=required)
        except Exception as exc:  # noqa: BLE001
            record(f"feature:{op}", False, f"{type(exc).__name__}: {exc}",
                   required=required)

    try:
        FreeCAD.closeDocument(name)
    except Exception:  # noqa: BLE001
        pass

    errors = [{"kind": "compile", "feature_id": None,
               "message": f"FreeCAD API self-test failed: {c['name']}: {c['detail']}"}
              for c in checks if c["required"] and not c["ok"]]
    return {"ok": not missing, "fully_supported": not (missing or optional_missing),
            "freecad_version": ".".join(str(v) for v in App.Version()[:3]),
            "checks": checks, "missing": missing,
            "optional_missing": optional_missing, "errors": errors}
