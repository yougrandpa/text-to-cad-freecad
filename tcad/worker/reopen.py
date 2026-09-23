"""Reopen a saved FCStd, apply parametric edits, recompute, and measure.

This is what makes the delivered .FCStd a *live parametric document* rather
than a mesh in a box: after a worker (or service) restart, the file reopens
with its Body/Sketch/Feature history intact, sketch constraints and feature
properties can be edited by name, and the model recomputes to the new
dimensions. That is the acceptance evidence for "重启服务后继续修改" and for
single-part parametric modification at the real-kernel layer.

Edit forms (all names are the FreeCAD object Names the compiler derives from
IR ids — ``_obj_name`` sanitises the id, so an IR sketch ``sk_holes`` is the
object ``sk_holes`` in the file):

    {"object": "ft_pad",   "property": "Length", "value": 50.0}
    {"object": "sk_holes", "constraint": "Radius", "value": 4.0}

A *constraint* edit applies to every constraint of that type on the sketch
(the natural reading of "把这四个孔的直径改成 8": one edit, four holes).
"""

from __future__ import annotations

import math
import os

import FreeCAD
import Part

from tcad.worker.compiler import _measure


def _close_doc(doc) -> None:
    try:
        FreeCAD.closeDocument(doc.Name)
    except Exception:  # noqa: BLE001
        pass


def reopen_edit_measure(fcstd_path=None, edits=None, out_dir=None, **_extra) -> dict:
    errors: list[dict] = []
    doc = None
    try:
        if not fcstd_path or not os.path.exists(fcstd_path):
            return {"ok": False, "error": f"FCStd not found: {fcstd_path!r}",
                    "measurements": None, "feature_states": {}}

        base = os.path.splitext(os.path.basename(fcstd_path))[0]
        if base in FreeCAD.listDocuments():
            try:
                FreeCAD.closeDocument(base)
            except Exception:  # noqa: BLE001
                pass

        doc = FreeCAD.openDocument(fcstd_path)

        for i, edit in enumerate(edits or []):
            _apply_edit(doc, i, edit, errors)

        try:
            doc.recompute()
        except Exception as exc:  # noqa: BLE001 — per-feature Invalid state below
            errors.append({"kind": "compile", "feature_id": None,
                           "message": f"recompute raised: {type(exc).__name__}: {exc}"})

        feature_states: dict = {}
        for obj in doc.Objects:
            try:
                state = [str(s) for s in obj.State]
            except Exception:  # noqa: BLE001
                state = []
            feature_states[obj.Name] = {"type_id": getattr(obj, "TypeId", ""), "state": state}
            if "Invalid" in state and getattr(obj, "TypeId", "").startswith("PartDesign::"):
                errors.append({
                    "kind": "compile", "feature_id": obj.Name,
                    "message": (
                        f"object {obj.Name!r} is Invalid after the edit + recompute: "
                        "the new value is not buildable (solver conflict, or a cut that "
                        "no longer intersects the material). Revert or pick a valid value."
                    ),
                })

        body_shapes = []
        for obj in doc.Objects:
            if getattr(obj, "TypeId", "") == "PartDesign::Body":
                try:
                    sh = obj.Shape
                    if sh is not None and not sh.isNull():
                        body_shapes.append(sh)
                except Exception:  # noqa: BLE001
                    pass

        shape = None
        if len(body_shapes) == 1:
            shape = body_shapes[0]
        elif len(body_shapes) > 1:
            shape = Part.makeCompound(body_shapes)

        measurements = _measure(shape)
        if shape is None or shape.isNull():
            errors.append({"kind": "compile", "feature_id": base,
                           "message": f"reopened {base!r} produced no solid after the edits"})

        ok = (not errors) and shape is not None and measurements["is_valid"]
        return {"ok": ok, "measurements": measurements,
                "feature_states": feature_states, "errors": errors,
                "sketches": _sketch_summaries(doc)}
    finally:
        if doc is not None:
            _close_doc(doc)


def _sketch_summaries(doc) -> dict:
    """Per-sketch evidence after the edits: circles in WORLD coordinates
    (placement applied) and datum constraint values.

    This is what lets an acceptance test prove "the four holes kept their
    centres and only grew to r=4" straight from the reopened document, instead
    of inferring it from a volume delta. Defensive throughout: a property that
    happens to be missing in some FreeCAD build must not fail the reopen.
    """
    summaries: dict = {}
    datum_types = ("Radius", "Diameter", "Distance", "DistanceX", "DistanceY", "Angle")
    for obj in doc.Objects:
        if not str(getattr(obj, "TypeId", "")).startswith("Sketcher::"):
            continue
        entry: dict = {"circles": [], "constraints": []}
        placement = getattr(obj, "Placement", None)
        try:
            for geo in obj.Geometry or []:
                center = getattr(geo, "Center", None)
                radius = getattr(geo, "Radius", None)
                if center is None or radius is None:
                    continue
                if placement is not None:
                    center = placement.multVec(center)
                entry["circles"].append({
                    "x": round(float(center.x), 6),
                    "y": round(float(center.y), 6),
                    "z": round(float(center.z), 6),
                    "radius": round(float(radius), 6),
                })
        except Exception:  # noqa: BLE001
            pass
        try:
            for i, con in enumerate(obj.Constraints or []):
                ctype = str(getattr(con, "Type", ""))
                if ctype not in datum_types:
                    continue
                try:
                    val = obj.getDatum(i)
                    entry["constraints"].append(
                        {"type": ctype, "value": round(float(getattr(val, "Value", val)), 6)})
                except Exception:  # noqa: BLE001 — skip unreadable datums
                    continue
        except Exception:  # noqa: BLE001
            pass
        summaries[obj.Name] = entry
    return summaries


def _prop_value(obj, prop: str, value):
    """Coerce an edit value to the type the FreeCAD property actually accepts.

    FreeCAD is strict here: ``Occurrences = 3.0`` (a float from JSON) raises
    "type must be int", and ``Suppressed = 1.0`` raises "type must be bool".
    Setting every edit to ``float(value)`` therefore made *integer* and
    *boolean* properties uneditable — which is exactly the "change the pattern
    from 3 to 5 occurrences" and "suppress this feature" edits a user asks for.
    """
    try:
        tid = obj.getTypeIdOfProperty(prop)
    except Exception:  # noqa: BLE001
        tid = ""
    if tid == "App::PropertyBool":
        if not isinstance(value, bool):
            raise ValueError(f"{prop} is a boolean property; pass true/false, not {value!r}")
        return value
    if tid in ("App::PropertyInteger", "App::PropertyIntegerConstraint"):
        ival = int(round(float(value)))
        if abs(float(value) - ival) > 1e-9:
            raise ValueError(f"{prop} is an integer property; {value!r} is not a whole number")
        return ival
    if tid in ("App::PropertyLength", "App::PropertyDistance"):
        return FreeCAD.Units.Quantity(f"{float(value)} mm")
    if tid == "App::PropertyAngle":
        return FreeCAD.Units.Quantity(f"{float(value)} deg")
    return float(value)


def _apply_edit(doc, index: int, edit: dict, errors: list) -> None:
    if not isinstance(edit, dict):
        errors.append({"kind": "schema", "feature_id": None,
                       "message": f"edit #{index} is not an object: {edit!r}"})
        return
    name = edit.get("object")
    value = edit.get("value")
    obj = doc.getObject(name) if name else None
    if obj is None:
        errors.append({"kind": "semantic", "feature_id": str(name),
                       "message": f"edit #{index}: no object named {name!r} in the document"})
        return
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        errors.append({"kind": "schema", "feature_id": str(name),
                       "message": f"edit #{index}: value must be a finite number, got {value!r}"})
        return

    if "property" in edit:
        prop = edit["property"]
        if not hasattr(obj, prop):
            errors.append({"kind": "semantic", "feature_id": str(name),
                           "message": f"edit #{index}: object {name!r} has no property {prop!r}"})
            return
        try:
            setattr(obj, prop, _prop_value(obj, prop, value))
        except Exception as exc:  # noqa: BLE001
            errors.append({"kind": "compile", "feature_id": str(name),
                           "message": f"edit #{index}: setting {prop} failed: {exc}"})
        return

    if "constraint" in edit:
        con_type = edit["constraint"]
        cons = obj.Constraints
        matched = [i for i, c in enumerate(cons) if c.Type == con_type]
        if not matched:
            errors.append({"kind": "semantic", "feature_id": str(name),
                           "message": f"edit #{index}: sketch {name!r} has no "
                                      f"{con_type!r} constraint"})
            return
        for ci in matched:
            try:
                obj.setDatum(ci, FreeCAD.Units.Quantity(f"{float(value)} mm"))
            except ValueError as exc:
                # setDatum raises ValueError "Invalid constraint index" for BOTH a
                # bad index AND a solver conflict — the message lies. Either way
                # the edit did not take, so classify it as a solver failure.
                errors.append({"kind": "solver", "feature_id": str(name),
                               "message": f"edit #{index}: setDatum({con_type}) on "
                                          f"{name!r} failed: {exc}"})
        return

    errors.append({"kind": "schema", "feature_id": str(name),
                   "message": f"edit #{index} must carry a 'property' or 'constraint' key"})
