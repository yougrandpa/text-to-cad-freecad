"""IR -> FreeCAD document compiler (runs inside FreeCADCmd).

IMPORT BAN (see tcad/worker/protocol.py): only the standard library plus
FreeCAD / Part / Sketcher / MeshPart may be imported here. No pydantic, no
numpy, no ``import tcad.core`` / ``import tcad.ir``.

This module owns the shared ``_build`` core. The other worker modules
(introspect / mesh / exporters) import ``_build`` from here so that the
document is constructed exactly once per RPC call and identically for every
measurement/export path.

Design facts used here (verified, see docs/02-架构设计.md 附录 A/B):
  * ``App = FreeCAD``  (``from FreeCAD import App`` raises ImportError)
  * ``doc.addObject(type, name)`` + ``body.addObject(obj)`` (NOT body.newObject)
  * sketch attach: ``sk.AttachmentSupport = (target, [sub])`` + ``sk.MapMode = "FlatFace"``
  * origin planes via ``doc.XY_Plane`` / ``doc.XZ_Plane`` / ``doc.YZ_Plane``
  * geometry: ``Part.LineSegment`` / ``Part.Circle`` / ``Part.ArcOfCircle`` / ``Part.Point``
  * ``sk.addConstraint(Sketcher.Constraint(type, *refs))`` -> int index
  * ``sk.setDatum(idx, App.Units.Quantity("N mm"))``
  * ``setDatum`` raises ``ValueError: Invalid constraint index`` for BOTH a bad
    index AND a solver conflict — the message lies (SketchObjectPyImp.cpp:915).
    We classify every such ValueError as kind="solver".
"""

from __future__ import annotations

import os
import re
import tempfile
import traceback

import FreeCAD
import Part
import Sketcher

App = FreeCAD

# ── IR op -> FreeCAD type string (copy of tcad.ir.schema.FEATURE_TYPE_MAP) ──
# Kept in sync manually; schema.py is NOT importable here (pydantic).
FEATURE_TYPE_MAP = {
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "revolution": "PartDesign::Revolution",
    "groove": "PartDesign::Groove",
    "fillet": "PartDesign::Fillet",
    "chamfer": "PartDesign::Chamfer",
    "draft": "PartDesign::Draft",
    "thickness": "PartDesign::Thickness",
    "hole": "PartDesign::Hole",
    "mirrored": "PartDesign::Mirrored",
    "linear_pattern": "PartDesign::LinearPattern",
    "circular_pattern": "PartDesign::CircularPattern",
    "polar_pattern": "PartDesign::PolarPattern",
    "multi_transform": "PartDesign::MultiTransform",
    "datum_plane": "PartDesign::Plane",
    "additive_box": "PartDesign::AdditiveBox",
    "additive_cylinder": "PartDesign::AdditiveCylinder",
    "additive_sphere": "PartDesign::AdditiveSphere",
    "subtractive_box": "PartDesign::SubtractiveBox",
    "subtractive_cylinder": "PartDesign::SubtractiveCylinder",
    "subtractive_sphere": "PartDesign::SubtractiveSphere",
}

# Ops whose result accumulates into a body tip and that reference a profile sketch.
_PROFILE_OPS = {"pad", "pocket", "revolution", "groove", "hole"}
# Ops that pattern/transform other features (Originals = list of feature objects).
_PATTERN_OPS = {
    "linear_pattern", "circular_pattern", "polar_pattern",
    "multi_transform", "mirrored",
}


def _obj_name(ir_id: str, label: str | None) -> str:
    """A valid FreeCAD object Name (no spaces, unique-ish) derived from the IR id."""
    base = re.sub(r"\W", "_", ir_id) if ir_id else "obj"
    if not base:
        base = "obj"
    return base


def _close_doc(doc) -> None:
    if doc is None:
        return
    try:
        name = doc.Name
        if name in FreeCAD.listDocuments():
            FreeCAD.closeDocument(name)
    except Exception:  # noqa: BLE001
        pass


def _datum_quantity(con_type: str, value: float):
    """Build the Quantity for a setDatum call. Angle -> deg, everything else -> mm."""
    if con_type == "Angle":
        return App.Units.Quantity(f"{float(value)} deg")
    return App.Units.Quantity(f"{float(value)} mm")


def _add_geometry(sk, g: dict):
    """Append one sketch geometry; return its index."""
    kind = g.get("kind")
    pts = g.get("points") or []
    construction = bool(g.get("construction", False))
    if kind == "line":
        p0 = pts[0]
        p1 = pts[1]
        geo = Part.LineSegment(
            App.Vector(float(p0["x"]), float(p0["y"]), float(p0["z"])),
            App.Vector(float(p1["x"]), float(p1["y"]), float(p1["z"])),
        )
    elif kind == "circle":
        c = pts[0]
        geo = Part.Circle(
            App.Vector(float(c["x"]), float(c["y"]), float(c["z"])),
            App.Vector(0.0, 0.0, 1.0),
            float(g.get("radius", 1.0)),
        )
    elif kind == "arc":
        c = pts[0]
        geo = Part.ArcOfCircle(
            Part.Circle(
                App.Vector(float(c["x"]), float(c["y"]), float(c["z"])),
                App.Vector(0.0, 0.0, 1.0),
                float(g.get("radius", 1.0)),
            ),
            float(g.get("theta1", 0.0)),
            float(g.get("theta2", 3.141592653589793)),
        )
    elif kind == "point":
        p = pts[0]
        geo = Part.Point(App.Vector(float(p["x"]), float(p["y"]), float(p["z"])))
    else:
        raise ValueError(f"unknown geometry kind: {kind!r}")
    return sk.addGeometry(geo, construction)


def _add_sketch(doc, body, s: dict, ref_objects: dict) -> dict:
    """Create and constrain a sketch; attach it to the body.

    Returns a per-sketch state dict (used by introspect). Errors are collected
    into the returned "errors" list rather than raised, so one bad sketch does
    not abort the whole build.
    """
    state = {
        "id": s.get("id"),
        "name": s.get("name") or s.get("id"),
        "fully_constrained": None,
        "dof": None,
        "solve_status": None,
        "errors": [],
    }
    name = _obj_name(s.get("id"), s.get("name"))
    sk = doc.addObject("Sketcher::SketchObject", name)
    sk.Label = s.get("name") or s.get("id")
    ref_objects[s.get("id")] = sk

    # ── attachment ──
    plane = s.get("plane") or {}
    try:
        kind = plane.get("kind", "origin_plane")
        if kind == "origin_plane":
            p = (plane.get("plane") or "XY").upper()
            target = getattr(doc, f"{p}_Plane")
            sk.AttachmentSupport = (target, [""])
        elif kind == "datum_plane":
            tgt = doc.getObject(plane.get("feature_id"))
            if tgt is None:
                raise ValueError(f"datum_plane target not found: {plane.get('feature_id')!r}")
            sk.AttachmentSupport = (tgt, [""])
        elif kind == "face":
            tgt = doc.getObject(plane.get("feature_id"))
            if tgt is None:
                raise ValueError(f"face target not found: {plane.get('feature_id')!r}")
            sub = plane.get("sub") or ""
            sk.AttachmentSupport = (tgt, [sub])
        else:
            raise ValueError(f"unknown plane kind: {kind!r}")
        sk.MapMode = s.get("map_mode") or "FlatFace"
        if plane.get("reversed"):
            sk.MapReversed = True
        off = s.get("offset")
        if off:
            sk.AttachmentOffset = App.Placement(
                App.Vector(float(off.get("x", 0.0)), float(off.get("y", 0.0)),
                           float(off.get("z", 0.0))),
                App.Rotation(),
            )
    except Exception as exc:  # noqa: BLE001
        state["errors"].append({
            "kind": "compile", "feature_id": s.get("id"),
            "message": f"attachment failed: {type(exc).__name__}: {exc}",
        })
    body.addObject(sk)

    # ── geometry ──
    for g in s.get("geometry") or []:
        try:
            _add_geometry(sk, g)
        except Exception as exc:  # noqa: BLE001
            state["errors"].append({
                "kind": "compile", "feature_id": s.get("id"),
                "message": f"geometry {g.get('kind')} failed: {type(exc).__name__}: {exc}",
            })

    # ── constraints ──
    for con in s.get("constraints") or []:
        try:
            con_type = con.get("type")
            refs = con.get("refs") or []
            idx = sk.addConstraint(Sketcher.Constraint(con_type, *refs))
            if con.get("value") is not None:
                try:
                    sk.setDatum(idx, _datum_quantity(con_type, con["value"]))
                except ValueError as ve:
                    # The message is a lie — setDatum maps solver conflicts to
                    # "Invalid constraint index". Classify as solver, never index.
                    state["errors"].append({
                        "kind": "solver",
                        "feature_id": s.get("id"),
                        "message": (
                            f"constraint {con_type}({refs}) value={con.get('value')} "
                            f"failed to solve: {ve}"
                        ),
                    })
        except Exception as exc:  # noqa: BLE001
            state["errors"].append({
                "kind": "compile", "feature_id": s.get("id"),
                "message": f"constraint {con.get('type')} failed: {type(exc).__name__}: {exc}",
            })

    # ── solve + capture constraint state ──
    try:
        state["solve_status"] = int(sk.solve())
    except Exception:  # noqa: BLE001
        state["solve_status"] = None
    try:
        state["dof"] = int(sk.DoF)
    except Exception:  # noqa: BLE001
        state["dof"] = None
    try:
        state["fully_constrained"] = bool(sk.FullyConstrained)
    except Exception:  # noqa: BLE001
        state["fully_constrained"] = None

    return state


def _prop_name(obj, key: str):
    """Map an IR param key to a real FreeCAD property name.

    The IR spec uses lowercase/snake_case keys (e.g. ``length``, ``type``,
    ``up_to_face``) while FreeCAD exposes Capitalized/CamelCase properties
    (``Length``, ``Type``, ``UpToFace``). Try, in order: exact match,
    first-letter-capitalized, then CamelCase-of-snake_case. Returns None if no
    candidate exists in PropertiesList.
    """
    if key in obj.PropertiesList:
        return key
    cap = key[0].upper() + key[1:] if key else key
    if cap in obj.PropertiesList:
        return cap
    if "_" in key:
        camel = "".join(seg[:1].upper() + seg[1:] for seg in key.split("_"))
        if camel in obj.PropertiesList:
            return camel
    return None


def _assign_props(obj, params: dict) -> list[dict]:
    """Set scalar params defensively (only keys that exist on the object).

    Length/Distance props -> mm Quantity; Angle props -> deg Quantity;
    Bool -> bool; Enumeration -> str; else raw. Returns a list of structured
    errors for keys that were rejected.
    """
    errors = []
    for k, v in (params or {}).items():
        if k in ("Originals",):
            continue
        pname = _prop_name(obj, k)
        if pname is None:
            errors.append({
                "kind": "compile",
                "feature_id": getattr(obj, "Name", None),
                "message": f"unsupported property {k!r} for {obj.TypeId}",
            })
            continue
        tid = obj.getTypeIdOfProperty(pname)
        try:
            if tid in ("App::PropertyLength", "App::PropertyDistance"):
                setattr(obj, pname, App.Units.Quantity(f"{float(v)} mm"))
            elif tid == "App::PropertyAngle":
                setattr(obj, pname, App.Units.Quantity(f"{float(v)} deg"))
            elif tid == "App::PropertyBool":
                setattr(obj, pname, bool(v))
            elif tid == "App::PropertyEnumeration":
                setattr(obj, pname, v)
            else:
                setattr(obj, pname, v)
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "kind": "compile",
                "feature_id": getattr(obj, "Name", None),
                "message": f"set {k}={v!r} failed: {type(exc).__name__}: {exc}",
            })
    return errors


def _apply_feature(doc, body, f: dict, ref_objects: dict) -> dict:
    """Create a PartDesign feature from an IR FeatureSpec. Returns state dict."""
    state = {"id": f.get("id"), "name": f.get("name") or f.get("id"),
             "op": f.get("op"), "errors": []}
    op = f.get("op")
    params = f.get("params") or {}
    type_str = FEATURE_TYPE_MAP.get(op)
    if type_str is None:
        state["errors"].append({
            "kind": "compile", "feature_id": f.get("id"),
            "message": f"unsupported feature op: {op!r}",
        })
        return state

    name = _obj_name(f.get("id"), f.get("name"))
    obj = doc.addObject(type_str, name)
    obj.Label = f.get("name") or f.get("id")
    ref_objects[f.get("id")] = obj

    # suppress
    if f.get("suppress") and "Suppressed" in obj.PropertiesList:
        try:
            obj.Suppressed = True
        except Exception:  # noqa: BLE001
            pass

    # profile sketch
    profile_id = f.get("profile_sketch")
    if profile_id and profile_id in ref_objects and "Profile" in obj.PropertiesList:
        try:
            obj.Profile = ref_objects[profile_id]
        except Exception as exc:  # noqa: BLE001
            state["errors"].append({
                "kind": "compile", "feature_id": f.get("id"),
                "message": f"set Profile failed: {type(exc).__name__}: {exc}",
            })

    # pattern / mirror -> Originals (list of referenced feature objects)
    if op in _PATTERN_OPS and "Originals" in obj.PropertiesList:
        originals = [ref_objects[r] for r in (f.get("refs") or []) if r in ref_objects]
        if originals:
            try:
                obj.Originals = originals
            except Exception as exc:  # noqa: BLE001
                state["errors"].append({
                    "kind": "compile", "feature_id": f.get("id"),
                    "message": f"set Originals failed: {type(exc).__name__}: {exc}",
                })

    state["errors"].extend(_assign_props(obj, params))
    body.addObject(obj)
    return state


def _build(ir: dict, out_dir: str):
    """Build the FreeCAD document from an IR dict.

    Returns a dict with keys:
      doc            live FreeCAD document (caller must close it)
      result_shape   combined TopoShape of all bodies (or None)
      sketches       list of per-sketch state dicts
      feature_chain  list of feature descriptors (in build order)
      errors         list of structured compile errors
    The caller is responsible for closing ``doc`` after extracting what it needs.
    """
    model_id = ir.get("model_id") or "tcad_model"
    # Avoid accumulating documents across calls in a long-lived worker.
    if model_id in FreeCAD.listDocuments():
        try:
            FreeCAD.closeDocument(model_id)
        except Exception:  # noqa: BLE001
            pass

    doc = FreeCAD.newDocument(model_id)

    ref_objects: dict = {}  # ir id -> FreeCAD object
    sketches: list = []
    feature_chain: list = []
    errors: list = []

    for b in ir.get("bodies") or []:
        body = doc.addObject("PartDesign::Body", _obj_name(b.get("id"), b.get("name")))
        body.Label = b.get("name") or b.get("id")
        for s in b.get("sketches") or []:
            sk_state = _add_sketch(doc, body, s, ref_objects)
            sketches.append(sk_state)
            # Surface per-sketch errors (e.g. solver conflicts) to the top level
            # so the supervisor sees a structured, feature_id-tagged error.
            errors.extend(sk_state.get("errors") or [])
        for f in b.get("features") or []:
            fstate = _apply_feature(doc, body, f, ref_objects)
            errors.extend(fstate["errors"])
            feature_chain.append({
                "id": f.get("id"), "name": f.get("name") or f.get("id"),
                "op": f.get("op"), "params": f.get("params") or {},
                "suppressed": bool(f.get("suppress")),
            })

    doc.recompute()

    # Collect resulting solids from every body.
    body_shapes = []
    for obj in doc.Objects:
        if getattr(obj, "TypeId", "") == "PartDesign::Body":
            try:
                sh = obj.Shape
                if sh is not None and not sh.isNull():
                    body_shapes.append(sh)
            except Exception:  # noqa: BLE001
                pass

    result_shape = None
    if body_shapes:
        if len(body_shapes) == 1:
            result_shape = body_shapes[0]
        else:
            result_shape = Part.makeCompound(body_shapes)

    return {
        "doc": doc,
        "result_shape": result_shape,
        "sketches": sketches,
        "feature_chain": feature_chain,
        "errors": errors,
    }


def _measure(shape) -> dict:
    """Topology / bbox / volume from a TopoShape (defensive)."""
    m = {
        "solids": 0, "faces": 0, "edges": 0, "vertexes": 0, "shells": 0,
        "volume": 0.0, "area": 0.0, "shape_type": "", "is_valid": False,
        "bbox": {"x": 0.0, "y": 0.0, "z": 0.0, "x_min": 0.0, "y_min": 0.0, "z_min": 0.0},
    }
    if shape is None or shape.isNull():
        return m
    try:
        m["solids"] = len(shape.Solids)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["faces"] = len(shape.Faces)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["edges"] = len(shape.Edges)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["vertexes"] = len(shape.Vertexes)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["shells"] = len(shape.Shells)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["volume"] = float(shape.Volume)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["area"] = float(shape.Area)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["shape_type"] = str(shape.ShapeType)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["is_valid"] = bool(shape.isValid())
    except Exception:  # noqa: BLE001
        pass
    try:
        bb = shape.BoundBox
        m["bbox"] = {
            "x": float(bb.XLength), "y": float(bb.YLength), "z": float(bb.ZLength),
            "x_min": float(bb.XMin), "y_min": float(bb.YMin), "z_min": float(bb.ZMin),
        }
    except Exception:  # noqa: BLE001
        pass
    return m


def _round_trip(shape, step_path: str) -> dict:
    """STEP export -> re-import -> relative volume error. Deterministic gate."""
    rt = {"ok": False, "step_path": step_path, "volume": None, "rel_err": None}
    if shape is None or shape.isNull():
        return rt
    try:
        shape.exportStep(step_path)
        reimport = Part.Shape()
        reimport.read(step_path)
        v0 = float(shape.Volume)
        v1 = float(reimport.Volume)
        rel = abs(v1 - v0) / v0 if v0 != 0 else 0.0
        rt["ok"] = True
        rt["volume"] = v1
        rt["rel_err"] = rel
    except Exception as exc:  # noqa: BLE001
        rt["error"] = f"{type(exc).__name__}: {exc}"
    return rt


def compile_ir(ir: dict | None = None, out_dir: str = "", **_extra) -> dict:
    """Build the document, persist a .FCStd, and return measurements + round-trip.

    Result envelope (nested under the rpc ``result`` key):
      ok, fcstd, errors, measurements, round_trip
    """
    if not ir:
        return {"ok": False, "errors": [{"kind": "schema",
                "feature_id": None, "message": "missing ir"}], "fcstd": None,
                "measurements": None, "round_trip": None}

    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="tcad_out_")
    os.makedirs(out_dir, exist_ok=True)

    built = _build(ir, out_dir)
    doc = built["doc"]
    shape = built["result_shape"]
    measurements = _measure(shape)

    fcstd = None
    try:
        fcstd = os.path.join(out_dir, f"{ir.get('model_id') or 'model'}.FCStd")
        doc.saveAs(fcstd)
    except Exception as exc:  # noqa: BLE001
        built["errors"].append({"kind": "runtime", "feature_id": None,
                                "message": f"saveAs failed: {type(exc).__name__}: {exc}"})

    step_path = os.path.join(out_dir, "roundtrip.step")
    round_trip = _round_trip(shape, step_path)

    _close_doc(doc)

    ok = (len(built["errors"]) == 0) and (shape is not None) and (not shape.isNull())
    return {
        "ok": ok,
        "fcstd": fcstd,
        "errors": built["errors"],
        "measurements": measurements,
        "round_trip": round_trip,
    }
