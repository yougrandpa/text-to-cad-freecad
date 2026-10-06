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
  * geometry: ``Part.LineSegment`` / ``Part.Circle`` / ``Part.ArcOfCircle`` /
    ``Part.Point`` / ``Part.Ellipse`` / interpolated ``Part.BSplineCurve``
  * ``sk.addConstraint(Sketcher.Constraint(type, *refs))`` -> int index
  * ``sk.setDatum(idx, App.Units.Quantity("N mm"))``
  * ``setDatum`` raises ``ValueError: Invalid constraint index`` for BOTH a bad
    index AND a solver conflict — the message lies (SketchObjectPyImp.cpp:915).
    We classify every such ValueError as kind="solver".
"""

from __future__ import annotations

import math
import os
import re
import tempfile

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
    "additive_loft": "PartDesign::AdditiveLoft",
    "subtractive_loft": "PartDesign::SubtractiveLoft",
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
    "additive_cone": "PartDesign::AdditiveCone",
    "subtractive_box": "PartDesign::SubtractiveBox",
    "subtractive_cylinder": "PartDesign::SubtractiveCylinder",
    "subtractive_sphere": "PartDesign::SubtractiveSphere",
    "subtractive_cone": "PartDesign::SubtractiveCone",
}

# Ops whose result accumulates into a body tip and that reference a profile sketch.
# Params the compiler consumes structurally rather than by `setattr`. They are
# skipped by `_assign_props` because there is no property of that name, and a
# plain assignment would either fail ("unsupported property") or set something
# that is not what was meant.
_STRUCTURAL_PARAM_KEYS = frozenset({"Originals", "axis"})

#: Allowed values for a revolution/groove ``axis`` param, mapped to the reference
#: they resolve to. A scalar enum is the only axis spelling the IR can carry: the
#: real FreeCAD property (`ReferenceAxis`) is an ``App::PropertyLinkSub``, which no
#: JSON scalar can express. Translating a name into that LinkSub here is what makes
#: the feature usable at all.
_AXIS_NAMES = {
    "x": ("origin", 0), "y": ("origin", 1), "z": ("origin", 2),
    "h_axis": ("sketch", "H_Axis"),
    "v_axis": ("sketch", "V_Axis"),
    "n_axis": ("sketch", "N_Axis"),
}
_AXIS_DEFAULT = "v_axis"

#: Ops whose FreeCAD object takes its axis/direction from a LinkSub the IR cannot
#: spell as a scalar, and the property that carries it. ``ReferenceAxis``
#: (revolution/groove) and ``Direction``/``Axis`` (patterns) are the same kind of
#: reference under different names, so one resolver serves all four.
#:
#: Not setting the pattern reference is the dangerous case: FreeCAD answers a
#: LinearPattern with no ``Direction`` by producing **one** occurrence — a clean,
#: valid, wrong solid — so a missing axis is refused here rather than defaulted.
_AXIS_LINK_OPS: dict[str, str] = {
    "revolution": "ReferenceAxis",
    "groove": "ReferenceAxis",
    "linear_pattern": "Direction",
    "polar_pattern": "Axis",
}
_AXIS_OPS = frozenset(_AXIS_LINK_OPS)

#: Ops that default to the *profile sketch's* vertical axis. A pattern has no
#: profile, so it has no such default and must name the axis it repeats along.
_PROFILE_AXIS_OPS = frozenset({"revolution", "groove"})

#: Axis names a pattern accepts. The sketch-local names are meaningful only where
#: there is a profile sketch; a pattern repeats features, so the body's own axes
#: are the only thing the IR can name for it.
_PATTERN_AXIS_NAMES = frozenset({"x", "y", "z"})

#: Ops that select sub-elements (edges) of another feature via a LinkSub ``Base``.
_SUB_ELEMENT_OPS = frozenset({"fillet", "chamfer", "draft", "thickness"})

#: The sub-element ops whose ``Base`` is a FACE list, not an edge list. The two
#: kinds get different listings in their error messages, so a wrong name says
#: what was available in the vocabulary the caller actually used.
_FACE_SUB_OPS = frozenset({"draft", "thickness"})

#: Ops that read a plane from the typed ``plane`` field, and the FreeCAD property
#: that carries it. Both are ``App::PropertyLinkSub`` and both return a null shape
#: rather than an error when unset (measured: §33 in the review report).
_PLANE_OPS = {"mirrored": "MirrorPlane", "draft": "NeutralPlane"}


def _set_base_reference(obj, f: dict, ref_objects: dict, *,
                        op: str, kind: str = "edges") -> list[dict]:
    """Point a feature with a ``Base`` link at the edges/faces it works on.

    ``Base`` is an ``App::PropertyLinkSub`` — the same shape a scalar param cannot
    carry — so the IR gives it typed fields (``base_feature`` + ``sub_elements``)
    and the compiler builds the pair here. ``fillet``/``chamfer`` name *edges*;
    ``draft`` re-shapes and ``thickness`` opens *faces* — same field, different
    vocabulary, which is why the message lists the names this op can take.

    Each name is checked against the real shape first: a bad name else fails
    *later* with a confusing message, or in the worst case silently rounds
    nothing.
    """
    if "Base" not in obj.PropertiesList:
        return []

    singular = kind[:-1]
    base_id = f.get("base_feature")
    subs = list(f.get("sub_elements") or [])
    if not base_id:
        return [{
            "kind": "schema", "feature_id": f.get("id"),
            "message": (f"{op} needs 'base_feature' (the feature whose {kind} "
                        f"to use) and 'sub_elements' (its {singular} names, from "
                        "ir_digest)"),
        }]
    target = ref_objects.get(base_id)
    if target is None:
        return [{
            "kind": "not_found", "feature_id": f.get("id"),
            "message": (f"base_feature {base_id!r} is not a feature built before this one"),
        }]
    if not subs:
        listing = _edge_names(target) if kind == "edges" else _face_names(target)
        return [{
            "kind": "schema", "feature_id": f.get("id"),
            "message": (f"base_feature {base_id!r} was given no 'sub_elements'; pass at "
                        f"least one {singular} name from ir_digest "
                        f"(available {kind}: {listing})"),
        }]
    # A feature has no Shape until the document computes, and this reference has
    # to be resolved *now* — the fillet object is created in this same pass, and
    # the final recompute happens only after every feature is in the Body. Same
    # reason ``_add_sketch`` recomputes before adding geometry: without it, a
    # perfectly good Pad reports "produced no shape to take edges from".
    doc = getattr(target, "Document", None)
    if doc is not None:
        try:
            doc.recompute()
        except Exception as exc:  # noqa: BLE001
            return [{"kind": "compile", "feature_id": f.get("id"),
                     "message": (f"recompute before resolving {base_id!r}'s edges "
                                 f"failed: {type(exc).__name__}: {exc}")}]
    try:
        shape = target.Shape
    except Exception as exc:  # noqa: BLE001
        return [{"kind": "compile", "feature_id": f.get("id"),
                 "message": f"cannot read {base_id!r}'s shape: {type(exc).__name__}: {exc}"}]
    if shape is None or shape.isNull():
        return [{"kind": "compile", "feature_id": f.get("id"),
                 "message": f"base_feature {base_id!r} produced no shape to take edges from"}]

    for sub in subs:
        try:
            shape.getElement(sub)
        except Exception as exc:  # noqa: BLE001
            listing = _edge_names(target) if kind == "edges" else _face_names(target)
            where = ("each edge's length and direction" if kind == "edges"
                     else "each face's normal and centre")
            return [{
                "kind": "semantic", "feature_id": f.get("id"),
                "message": (f"sub-element {sub!r} does not exist on feature {base_id!r} "
                            f"({type(exc).__name__}: {exc}). Available {kind}: "
                            f"{listing}. Call ir_digest to see {where} and pick by "
                            "intent."),
            }]
    try:
        obj.Base = (target, subs)
    except Exception as exc:  # noqa: BLE001
        return [{"kind": "compile", "feature_id": f.get("id"),
                 "message": f"set Base={base_id!r}{subs} failed: {type(exc).__name__}: {exc}"}]
    return []


def _edge_names(obj, limit: int = 16) -> str:
    """The edge names this object actually has, for an error message."""
    try:
        shape = obj.Shape
    except Exception:  # noqa: BLE001
        return "(shape unavailable)"
    names = []
    for i, edge in enumerate(shape.Edges):
        names.append(f"Edge{i + 1}({edge.Length:.4g}mm)")
        if len(names) >= limit:
            names.append("…")
            break
    return ", ".join(names) or "(none)"


def _face_names(obj, limit: int = 12) -> str:
    """The planar face names this object actually has, for an error message."""
    try:
        shape = obj.Shape
    except Exception:  # noqa: BLE001
        return "(shape unavailable)"
    names = []
    for i, face in enumerate(shape.Faces):
        try:
            if face.Surface.__class__.__name__ != "Plane":
                continue
        except Exception:  # noqa: BLE001
            continue
        names.append(f"Face{i + 1}")
        if len(names) >= limit:
            names.append("…")
            break
    return ", ".join(names) or "(none)"


def _set_axis_reference(obj, body, f: dict, ref_objects: dict) -> list[dict]:
    """Point a revolution/groove (or pattern) at its axis/direction.

    ``params.axis`` is one of ``X``/``Y``/``Z`` (the body's origin axis) or
    ``H_Axis``/``V_Axis``/``N_Axis`` (the profile sketch's own axes; sketches
    only). Revolution/groove default to ``V_Axis``: a profile drawn to one side of
    the sketch's vertical axis is the ordinary way to revolve, and the sketch's
    local frame follows any attachment or offset, which a fixed global axis would
    not. A pattern has no profile, so it must name its axis — FreeCAD answers a
    missing ``Direction`` with a single occurrence rather than an error, and a
    valid-looking solid of the wrong size is exactly what this refuses to build.

    An unrecognised name is a structured error rather than a silent fallback —
    revolving, or repeating, about the wrong axis produces a plausible-looking
    solid of the wrong size, which is far worse than a build that refuses.
    """
    op = f.get("op")
    prop = _AXIS_LINK_OPS.get(op)
    if prop is None or prop not in obj.PropertiesList:
        return []

    params = f.get("params") or {}
    raw = params.get("axis")
    if raw is None:
        if op not in _PROFILE_AXIS_OPS:
            return [{
                "kind": "schema", "feature_id": f.get("id"),
                "message": (
                    f"{op} needs params.axis — 'X'/'Y'/'Z', the body axis to "
                    "repeat along/about. Without it FreeCAD produces a single "
                    "occurrence instead of the pattern, with no error."
                ),
            }]
        raw = _AXIS_DEFAULT
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if op not in _PROFILE_AXIS_OPS and key not in _PATTERN_AXIS_NAMES:
        return [{
            "kind": "semantic", "feature_id": f.get("id"),
            "message": (
                f"axis={raw!r} is not a supported {op} axis; use one of "
                f"{sorted(_PATTERN_AXIS_NAMES)} (the body's origin axes). "
                "Sketch-local axes (H_Axis/V_Axis/N_Axis) belong to ops with a "
                "profile sketch — a pattern repeats whole features, not a profile."
            ),
        }]
    resolved = _AXIS_NAMES.get(key)
    if resolved is None:
        return [{
            "kind": "semantic", "feature_id": f.get("id"),
            "message": (
                f"axis={raw!r} is not a supported revolution axis; use one of "
                f"{sorted(_AXIS_NAMES)} (X/Y/Z = body origin axis, "
                "H_Axis/V_Axis/N_Axis = the profile sketch's own axes)"
            ),
        }]

    kind, ref = resolved
    try:
        if kind == "origin":
            origin = getattr(body, "Origin", None)
            if origin is None or not origin.OriginFeatures:
                return [{"kind": "compile", "feature_id": f.get("id"),
                         "message": "body has no Origin to take an axis from"}]
            setattr(obj, prop, (origin.OriginFeatures[ref], [""]))
        else:
            profile_id = f.get("profile_sketch")
            profile = ref_objects.get(profile_id) if profile_id else None
            if profile is None:
                return [{"kind": "compile", "feature_id": f.get("id"),
                         "message": (
                             f"axis={raw!r} needs the profile sketch, but "
                             f"profile_sketch={profile_id!r} is not set")}]
            setattr(obj, prop, (profile, [ref]))
    except Exception as exc:  # noqa: BLE001
        return [{
            "kind": "compile", "feature_id": f.get("id"),
            "message": f"set {prop}={raw!r} failed: {type(exc).__name__}: {exc}",
        }]
    return []


def _set_plane_reference(obj, prop: str, f: dict, ref_objects: dict, *,
                         op: str) -> list[dict]:
    """Point a feature at the plane it reads (``MirrorPlane`` / ``NeutralPlane``).

    Both are ``App::PropertyLinkSub``, like the edge references, so the IR carries
    them in the typed ``plane`` field — the same shape a sketch uses for its
    attachment (origin plane / datum plane / a face of a feature), which means one
    mental model covers every place a plane is needed.

    Not setting it is not an option in either case: FreeCAD does not raise, it
    returns a NULL shape, and the body Tip then silently falls back to whatever
    was there before. Measured on the real kernel — a ``mirrored`` with no
    ``MirrorPlane`` and a ``draft`` with no ``NeutralPlane`` both produce a null
    Tip with a clean recompute.
    """
    if prop not in obj.PropertiesList:
        return []

    plane = f.get("plane") or {}
    kind = plane.get("kind")
    if not kind:
        return [{
            "kind": "schema", "feature_id": f.get("id"),
            "message": (f'{op} needs a "plane" ({prop} is a reference, and without '
                        'one FreeCAD returns a null shape): '
                        '{"kind":"origin_plane","plane":"XY"} | '
                        '{"kind":"face","feature_id":…,"sub":"Face6"} | '
                        '{"kind":"datum_plane","feature_id":…}'),
        }]

    doc = getattr(obj, "Document", None)
    try:
        if kind == "origin_plane":
            name = str(plane.get("plane") or "").upper()
            target = getattr(doc, f"{name}_Plane", None) if doc is not None else None
            if target is None:
                return [{
                    "kind": "semantic", "feature_id": f.get("id"),
                    "message": (f"plane {plane.get('plane')!r} is not one of the "
                                "origin planes XY / XZ / YZ"),
                }]
            setattr(obj, prop, (target, [""]))
        elif kind == "datum_plane":
            target = ref_objects.get(plane.get("feature_id"))
            if target is None:
                return [{
                    "kind": "not_found", "feature_id": f.get("id"),
                    "message": (f"plane target {plane.get('feature_id')!r} is not a "
                                "feature built before this one"),
                }]
            setattr(obj, prop, (target, [""]))
        elif kind == "face":
            target = ref_objects.get(plane.get("feature_id"))
            if target is None:
                return [{
                    "kind": "not_found", "feature_id": f.get("id"),
                    "message": (f"plane target {plane.get('feature_id')!r} is not a "
                                "feature built before this one"),
                }]
            sub = plane.get("sub") or ""
            if not sub:
                return [{"kind": "schema", "feature_id": f.get("id"),
                         "message": (f"plane target {plane.get('feature_id')!r} was "
                                     "given no 'sub' face name (from ir_digest)")}]
            # Same reason as ``_set_base_reference``: the face is looked up in a
            # shape that only exists after a recompute, and a wrong name would
            # otherwise become a silently wrong plane.
            if doc is not None:
                try:
                    doc.recompute()
                except Exception as exc:  # noqa: BLE001
                    return [{"kind": "compile", "feature_id": f.get("id"),
                             "message": (f"recompute before resolving "
                                         f"{plane.get('feature_id')!r}'s faces failed: "
                                         f"{type(exc).__name__}: {exc}")}]
            try:
                shape = target.Shape
            except Exception as exc:  # noqa: BLE001
                return [{"kind": "compile", "feature_id": f.get("id"),
                         "message": f"cannot read the plane target's shape: {exc}"}]
            if shape is None or shape.isNull():
                return [{"kind": "compile", "feature_id": f.get("id"),
                         "message": "the plane target produced no shape to take faces from"}]
            try:
                shape.getElement(sub)
            except Exception as exc:  # noqa: BLE001
                return [{
                    "kind": "semantic", "feature_id": f.get("id"),
                    "message": (f"plane face {sub!r} does not exist on "
                                f"{plane.get('feature_id')!r} "
                                f"({type(exc).__name__}). Available faces: "
                                f"{_face_names(target)}. Call ir_digest to see each "
                                "face's normal and centre and pick by intent."),
                }]
            setattr(obj, prop, (target, [sub]))
        else:
            return [{"kind": "semantic", "feature_id": f.get("id"),
                     "message": (f"plane kind {kind!r} is not supported for {op}; "
                                 "use origin_plane / face / datum_plane")}]
    except Exception as exc:  # noqa: BLE001
        return [{"kind": "compile", "feature_id": f.get("id"),
                 "message": f"set {prop} failed: {type(exc).__name__}: {exc}"}]
    return []


#: The documented WORLD COORDINATES convention: the direction each origin plane
#: is normal to (the draft's pull direction for that neutral plane).
_ORIGIN_PLANE_AXIS = {
    "XY": (0.0, 0.0, 1.0),
    "XZ": (0.0, 1.0, 0.0),
    "YZ": (1.0, 0.0, 0.0),
}


def _face_axis_in(shape, sub: str):
    """World normal of a named planar face, or ``None`` (curved face / bad name)."""
    try:
        face = shape.getElement(sub)
    except Exception:  # noqa: BLE001
        return None
    try:
        surface = face.Surface
        if surface.__class__.__name__ != "Plane":
            return None
        axis = surface.Axis
        return (float(axis.x), float(axis.y), float(axis.z))
    except Exception:  # noqa: BLE001
        return None


def _draft_degenerate_errors(obj, f: dict) -> list[dict]:
    """Faces parallel to the neutral plane cannot be drafted — refuse by name.

    Measured on this kernel: FreeCAD tapers a face by rotating it about the line
    where the face meets the neutral plane. A face *parallel* to that plane has no
    such line, so the whole feature comes back Invalid — with a message that names
    the feature but not the cause (probes: the four side faces about XZ, the +Y
    face alone about XZ, the top face about XY, the bottom face about XY). The
    references just set are checked here so the model is told which name is the
    problem instead of being sent to guess — the same reason the face/edge names
    are validated where they are resolved.

    Only exactly-parallel planar faces are refused; anything else (curved faces, a
    plane whose axis cannot be derived) is left to the kernel, whose failure for
    those is loud as well.
    """
    try:
        base_link = obj.Base or ()
        base = base_link[0] if len(base_link) > 0 else None
        base_subs = [s for s in (base_link[1] or []) if s] if len(base_link) > 1 else []
        if base is None or not base_subs:
            return []

        plane = f.get("plane") or {}
        kind = plane.get("kind")
        if kind == "origin_plane":
            axis = _ORIGIN_PLANE_AXIS.get(str(plane.get("plane") or "").upper())
        elif kind == "face":
            plane_link = obj.NeutralPlane or ()
            plane_target = plane_link[0] if len(plane_link) > 0 else None
            plane_sub = (plane_link[1] or [""])[0] if len(plane_link) > 1 else ""
            axis = _face_axis_in(plane_target.Shape, plane_sub) if plane_target else None
        else:
            axis = None  # datum plane: no axis derived, the kernel reports it
        if axis is None:
            return []

        doc = getattr(obj, "Document", None)
        if doc is not None:
            doc.recompute()
        shape = base.Shape
        if shape is None or shape.isNull():
            return []
        bad = []
        for sub in base_subs:
            face_axis = _face_axis_in(shape, sub)
            if face_axis is None:
                continue
            if abs(sum(a * b for a, b in zip(face_axis, axis))) > 1.0 - 1e-12:
                bad.append(sub)
    except Exception:  # noqa: BLE001
        return []
    if not bad:
        return []
    return [{
        "kind": "semantic", "feature_id": f.get("id"),
        "message": (
            f"draft cannot taper {'/'.join(bad)}: "
            f"{'those faces are' if len(bad) > 1 else 'that face is'} parallel to the "
            "neutral plane, so there is no line where they meet for FreeCAD to rotate "
            "about (measured: the feature comes back Invalid and the Body Tip keeps "
            "the un-drafted shape). Draft the faces that meet the neutral plane — "
            "e.g. the four side faces about the XY plane — or choose a plane they cross."
        ),
    }]

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


def _node_deps(kind: str, node: dict) -> set:
    """The IR ids a sketch or feature needs to exist before it can be built."""
    if kind == "sketch":
        plane = node.get("plane") or {}
        if plane.get("kind") in ("face", "datum_plane") and plane.get("feature_id"):
            # A sketch lying on a feature's face is meaningless until that
            # feature exists.
            return {plane["feature_id"]}
        return set()
    deps = set(node.get("refs") or [])
    if node.get("profile_sketch"):
        deps.add(node["profile_sketch"])
    deps.update(node.get("sections") or [])
    return deps


def _dependency_order(b: dict, available=()) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Order one body's sketches and features so every prerequisite is built first.

    The IR says "features: list order = build order", with sketches as implicit
    prerequisites — which is *not* the same as "all sketches, then all features".
    A sketch may be attached to a face (``plane.kind == "face"``) or a datum plane
    of a feature, and that feature has to exist first. Building every sketch up
    front made the documented face/datum attachment impossible: the lookup failed
    with "face target not found" before the feature had been created, and the
    cascade of follow-on errors buried that one line.

    Stable: nodes are emitted in the IR's declared order whenever dependencies
    allow, so the common "sketches, then features" body is built exactly as before.
    """
    body_id = b.get("id")
    nodes: list[tuple[str, dict]] = (
        [("sketch", s) for s in b.get("sketches") or []]
        + [("feature", f) for f in b.get("features") or []]
    )
    known = {node.get("id") for _kind, node in nodes}
    pending = list(nodes)
    built: set = set(available)
    order: list[tuple[str, dict]] = []
    errors: list[dict] = []

    while pending:
        ready = [
            (kind, node) for kind, node in pending
            if _node_deps(kind, node) <= built
        ]
        if not ready:
            blocked = [f"{kind}:{node.get('id')}" for kind, node in pending]
            errors.append({
                "kind": "semantic",
                "feature_id": body_id,
                "message": (
                    f"body {body_id!r} has a circular or unresolvable dependency "
                    f"between {blocked} ({len(known)} node(s) declared) — every "
                    f"refs/profile_sketch/plane target must name a node in this "
                    f"body or an already built supporting body."
                ),
            })
            # Emit them anyway so their own problems (if any) are reported too.
            order.extend(pending)
            break
        for kind, node in ready:
            pending.remove((kind, node))
            order.append((kind, node))
            built.add(node.get("id"))
    return order, errors


class _BadConstraint(ValueError):
    """A constraint shape this build refuses to hand to FreeCAD."""


def _datum_quantity(con_type: str, value: float):
    """The Quantity for a ``setDatum`` call. Angle -> deg, everything else -> mm."""
    if con_type == "Angle":
        return App.Units.Quantity(f"{float(value)} deg")
    return App.Units.Quantity(f"{float(value)} mm")


#: Verified construction recipes for ``Sketcher.Constraint``.
#:
#: ⚠️ **``Sketcher.Constraint(type, *refs)`` does not validate its arguments.**
#: An unrecognised shape does not raise — it **segfaults FreeCAD** (SIGSEGV 11,
#: measured on 26.3.0dev). A native crash cannot be caught by ``except``, takes
#: the whole geometry worker with it, and is how "在支架底面开一个直径 8mm 的通孔"
#: brought the harness down: our own recipe built ``Constraint("Radius", geoId)``,
#: which is exactly one such shape.
#:
#: So each type is checked here — arity, and whether it carries a value — before
#: FreeCAD is called at all. Measured, not guessed: every entry below was verified
#: against the real kernel, and the shapes that crash are recorded so nobody
#: reintroduces them.
#:
#: ``refs`` are passed positionally, exactly as before. Value-carrying types take
#: the value **in the constructor** — for Radius/Diameter the split
#: ``Constraint(type, geoId)`` + ``setDatum`` form is one of the crashing shapes.
_GEOMETRIC_CONSTRAINTS: dict[str, frozenset[int]] = {
    # type: the ref counts that are valid (verified)
    "Coincident": frozenset({4}),
    "Horizontal": frozenset({1}),
    "Vertical": frozenset({1}),
    "Parallel": frozenset({2}),
    "Perpendicular": frozenset({2}),
    "Equal": frozenset({2}),
    "Tangent": frozenset({2, 3}),
    "PointOnObject": frozenset({3}),
    "Symmetric": frozenset({5, 6}),
    "Block": frozenset({1}),
}

_VALUE_CONSTRAINTS: dict[str, frozenset[int]] = {
    # type: valid ref counts; the value is required
    "Distance": frozenset({1, 2}),
    "DistanceX": frozenset({1, 2}),
    "DistanceY": frozenset({1, 2}),
    "Angle": frozenset({2}),
    "Radius": frozenset({1}),
    "Diameter": frozenset({1}),
    "Weight": frozenset({1}),
}

#: ``(type, ref-count)`` pairs where the **refs-only** construction is safe.
#:
#: This matters beyond crash avoidance: a value handed to the constructor makes
#: the later ``setDatum`` a no-op, and ``setDatum`` is what makes FreeCAD
#: *validate* the constraint set. Measured on the over-determined rectangle that
#: `tests/contract/test_wired_pipeline.py` pins: with the value in the constructor
#: it reports ``solve()=0``, ``DoF=0`` and builds an 8000 mm³ solid — a green Gate
#: over a broken constraint set. With the refs-only form it raises the conflict.
#: So wherever the refs-only form is safe, it is the one used.
_REFS_ONLY_SAFE: frozenset[tuple[str, int]] = frozenset({
    ("Distance", 2), ("DistanceX", 2), ("DistanceY", 2), ("Angle", 2),
})


def _constraint_args(
    con_type: str, refs: list, value: float | None, geometry: list | None = None
) -> tuple[tuple, bool]:
    """``(Constraint args, apply the value with setDatum)``.

    Raises :class:`_BadConstraint` for any shape not verified against the real
    kernel — FreeCAD answers a malformed constraint with a segfault rather than an
    exception, so nothing unverified is passed through.
    """
    if not isinstance(con_type, str) or not con_type:
        raise _BadConstraint(f"constraint has no type: {con_type!r}")

    if con_type in _GEOMETRIC_CONSTRAINTS:
        allowed = _GEOMETRIC_CONSTRAINTS[con_type]
        if len(refs) not in allowed:
            raise _BadConstraint(
                f"{con_type} takes {sorted(allowed)} reference(s), got {len(refs)} "
                f"({refs}). FreeCAD segfaults on an unrecognised constraint shape, "
                f"so this is refused here."
            )
        if value is not None:
            raise _BadConstraint(f"{con_type} does not take a value (got {value})")
        return (con_type, *refs), False

    if con_type in _VALUE_CONSTRAINTS:
        allowed = _VALUE_CONSTRAINTS[con_type]
        if len(refs) not in allowed:
            raise _BadConstraint(
                f"{con_type} takes {sorted(allowed)} reference(s), got {len(refs)} "
                f"({refs}). FreeCAD segfaults on an unrecognised constraint shape, "
                f"so this is refused here."
            )
        if value is None:
            raise _BadConstraint(
                f'{con_type} needs a numeric value (e.g. "value": 4.0); it is a '
                f"dimension, and FreeCAD cannot express it without one."
            )
        # Two numeric arguments otherwise select FreeCAD's line-length form.
        # Circle/arc centres require an explicit PointPos::mid and value.
        centre_dimension = (con_type in {"DistanceX", "DistanceY"} and len(refs) == 2
                            and refs[1] == 3 and geometry is not None
                            and 0 <= refs[0] < len(geometry)
                            and geometry[refs[0]].get("kind") in {"circle", "arc"})
        if (con_type, len(refs)) in _REFS_ONLY_SAFE and not centre_dimension:
            return (con_type, *refs), True
        # Only where the refs-only form crashes: the value goes in the
        # constructor. FreeCAD then skips the redundancy validation for this
        # constraint — a real, documented weakening, and still far better than a
        # segfault that takes the worker with it.
        return (con_type, *refs, float(value)), False

    raise _BadConstraint(
        f"unsupported constraint type {con_type!r}. Verified types: "
        f"{', '.join(sorted(_GEOMETRIC_CONSTRAINTS | _VALUE_CONSTRAINTS))}. "
        f"(FreeCAD crashes on unrecognised constraint shapes, so nothing outside "
        f"this list is attempted.)"
    )


def _sketch_point(sk, p: dict, *, validate_plane: bool = False) -> "App.Vector":
    """Map one world-space IR point into the sketch's own (u, v) frame.

    **Sketcher takes geometry in the sketch's LOCAL 2-D frame.** The first two
    components of the vector handed to ``addGeometry`` are the in-plane u/v; the
    third is ignored for a planar sketch. Feeding it world coordinates therefore
    silently collapses every profile whose plane is not the sketch's own frame:
    a YZ profile written as ``(0, y, z)`` became ``(u=0, v=y)`` — all vertices on
    one line, no wire, no solid, and (before this) *no error either*.

    The sketch's ``Placement`` maps its local frame into the world, so its
    inverse maps a world point back. It is resolved once the attachment is set
    and the document has been recomputed, which is why :func:`_add_sketch`
    recomputes before adding geometry.

    Verified numerically on all three origin planes (world → local):
    ``XY: (x, y)``, ``XZ: (x, z)``, ``YZ: (y, z)``; a 40×20 rectangle padded 5
    gives 4000 mm³ on each, with the extrude direction +Z / −Y / +X respectively.
    """
    world = App.Vector(
        float(p.get("x", 0.0)), float(p.get("y", 0.0)), float(p.get("z", 0.0))
    )
    try:
        local = sk.Placement.inverse().multVec(world)
    except Exception:  # noqa: BLE001 — an unavailable placement must not abort the build
        return world
    if validate_plane and abs(local.z) > 1e-7:
        raise ValueError(
            f"world point ({world.x:g}, {world.y:g}, {world.z:g}) is "
            f"{abs(local.z):g} mm off the attached sketch plane. Sketcher would "
            "silently project it onto that plane, changing the requested position. "
            "Origin planes pass through world zero (XY: z=0; XZ: y=0; YZ: x=0). "
            "For an elevated profile, commit the supporting solid and use ir_digest "
            "to select an actual planar face with plane={kind:face, "
            "feature_id:existing_feature, sub:FaceN}; write points on that face. "
            "Do not use sketch.offset to raise an origin-plane profile."
        )
    return App.Vector(local.x, local.y, 0.0)


def _add_geometry(sk, g: dict):
    """Append one sketch geometry; return its index."""
    kind = g.get("kind")
    pts = g.get("points") or []
    construction = bool(g.get("construction", False))
    support = getattr(sk, "AttachmentSupport", None)
    def face_reference(link):
        # AttachmentSupport reads back as a LinkSubList in some FreeCAD builds,
        # and a LinkSub tuple in others; inspect the actual sub-element strings.
        if isinstance(link, str):
            return link.startswith("Face")
        return isinstance(link, (tuple, list)) and any(face_reference(part) for part in link)
    on_face = face_reference(support)
    # Preserve normal projection for legacy simple profiles attached to faces.
    # Origin/datum-plane points and native curves must actually lie on their
    # plane; changing a point's third coordinate cannot elevate an XY profile.
    if not on_face or kind in {"ellipse", "bspline"}:
        for point in pts:
            _sketch_point(sk, point, validate_plane=True)
    if kind == "line":
        p0 = _sketch_point(sk, pts[0])
        p1 = _sketch_point(sk, pts[1])
        geo = Part.LineSegment(p0, p1)
    elif kind == "circle":
        c = _sketch_point(sk, pts[0])
        geo = Part.Circle(c, App.Vector(0.0, 0.0, 1.0), float(g.get("radius", 1.0)))
    elif kind == "arc":
        c = _sketch_point(sk, pts[0])
        geo = Part.ArcOfCircle(
            Part.Circle(c, App.Vector(0.0, 0.0, 1.0), float(g.get("radius", 1.0))),
            float(g.get("theta1", 0.0)),
            float(g.get("theta2", 3.141592653589793)),
        )
    elif kind == "point":
        geo = Part.Point(_sketch_point(sk, pts[0]))
    elif kind == "ellipse":
        center = _sketch_point(sk, pts[0])
        angle = math.radians(float(g.get("rotation", 0.0)))
        major = float(g["major_radius"])
        minor = float(g["minor_radius"])
        geo = Part.Ellipse(
            center + App.Vector(major * math.cos(angle), major * math.sin(angle), 0),
            center + App.Vector(-minor * math.sin(angle), minor * math.cos(angle), 0),
            center,
        )
    elif kind == "bspline":
        local_points = [_sketch_point(sk, p) for p in pts]
        geo = Part.BSplineCurve()
        geo.interpolate(local_points, PeriodicFlag=bool(g.get("periodic", False)))
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
            # A bad sub-element does NOT fail the attach: FreeCAD leaves the sketch
            # unattached, the profile then collapses in its own frame, and the
            # build fails with "does not form a closed wire ... all N profile
            # points are at the same place ... check your world coordinates" —
            # which sends the model to fix the coordinates instead of the name.
            # Check the name against the real shape and say what is available.
            if sub:
                try:
                    doc.recompute()
                    shape = getattr(tgt, "Shape", None)
                    if shape is not None and not shape.isNull():
                        tgt.Shape.getElement(sub)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(
                        f"sub-element {sub!r} does not exist on feature "
                        f"{plane.get('feature_id')!r} ({type(exc).__name__}: {exc}). "
                        f"Available faces: {_face_names(tgt)}. Call ir_digest to see "
                        "each face's normal and centre and pick by intent."
                    ) from exc
            if tgt not in body.Group:
                # ProfileBased treats a supported Part::Feature as an implicit
                # additive base when this Body has no prior solid. A foreign
                # face is only a positioning dependency: import its attachment
                # frame through a non-solid datum, never its complete solid.
                support = doc.addObject("App::Plane", "_tcad_support_" + sk.Name)
                support.addExtension("Part::AttachExtensionPython")
                support.AttachmentSupport = (tgt, [sub])
                support.MapMode = "FlatFace"
                sk.AttachmentSupport = (support, [""])
            else:
                sk.AttachmentSupport = (tgt, [sub])
        else:
            raise ValueError(f"unknown plane kind: {kind!r}")
        sk.MapMode = s.get("map_mode") or "FlatFace"
        # SketchSpec.reversed lives on the SKETCH (schema field), not on the
        # plane ref; a sketch-level flag was silently dropped here.
        if plane.get("reversed") or s.get("reversed"):
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

    # Resolve the attachment BEFORE adding geometry.
    #
    # Sketcher takes geometry in the sketch's own (u, v) frame, and the only
    # thing that knows that frame is the sketch's Placement — which FreeCAD
    # computes during a recompute. Without this, an attached sketch still has an
    # identity placement, `_sketch_point` would map world → world, and every
    # profile on XZ/YZ would collapse to a line (see `_sketch_point`).
    try:
        doc.recompute()
    except Exception:  # noqa: BLE001 — geometry addition below reports its own errors
        pass

    # ── geometry ──
    for g in s.get("geometry") or []:
        try:
            _add_geometry(sk, g)
        except Exception as exc:  # noqa: BLE001
            state["errors"].append({
                "kind": "compile", "feature_id": s.get("id"),
                "message": (
                    f"geometry {g.get('kind')} failed: {type(exc).__name__}: {exc}"
                    + _profile_hint(sk, s)
                ),
            })

    # Generated fixed profiles need one solver update, rather than one per edge.
    constraints = s.get("constraints") or []
    if constraints and all(c.get("type") == "Block" and len(c.get("refs") or []) == 1 for c in constraints):
        try:
            sk.addConstraint([Sketcher.Constraint("Block", int(c["refs"][0])) for c in constraints])
            constraints = []
        except Exception as exc:
            state["errors"].append({"kind": "solver", "feature_id": s.get("id"),
                                    "message": f"fixed profile constraints failed: {exc}"})
            constraints = []
    # ── constraints ──
    for con in constraints:
        try:
            con_type = con.get("type")
            refs = list(con.get("refs") or [])
            value = con.get("value")
            args, via_setdatum = _constraint_args(con_type, refs, value, s.get("geometry"))
            idx = sk.addConstraint(Sketcher.Constraint(*args))
            if via_setdatum:
                # `setDatum` is not merely how the value gets applied — it is what
                # makes FreeCAD *validate* the constraint set. (The value-in-
                # constructor form above skips that check, which is why it is used
                # only where the refs-only form would crash.)
                sk.setDatum(idx, _datum_quantity(con_type, float(value)))
        except _BadConstraint as bad:
            # Our own guard: the shape was rejected before FreeCAD saw it.
            state["errors"].append({
                "kind": "semantic",
                "feature_id": s.get("id"),
                "message": str(bad),
            })
        except ValueError as ve:
            # FreeCAD maps solver conflicts onto a misleading ValueError whose
            # text talks about constraint indexes. Classify as solver, never index.
            state["errors"].append({
                "kind": "solver",
                "feature_id": s.get("id"),
                "message": (
                    f"constraint {con.get('type')}({refs}) value={value} "
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

    # SketchObject.solve() returns SketchSolveStatus (success=0), not the
    # lower-level GCS SolveStatus enum. FullyConstrained can be true on failure.
    if state.get("solve_status") not in (None, 0):
        state["errors"].append({"kind": "solver", "feature_id": s.get("id"),
                                "message": f"Sketch {s.get('id')} did not solve (solve()={state['solve_status']})."})
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


def _freecad_version() -> str:
    """Version for diagnostics; property probes, not version guesses, route API calls."""
    return ".".join(str(v) for v in App.Version()[:3])


def _enum_value(obj, pname: str, value):
    """Translate the two documented linear-pattern modes across FreeCAD builds.

    FreeCAD 1.0 calls these modes ``length`` / ``offset``; the newer pattern
    extension calls them ``Extent`` / ``Spacing``. Both operate on Length and
    Offset respectively. Query the live enum so newer releases keep their own
    behaviour, and leave unknown values untouched for FreeCAD to reject.
    """
    if obj.TypeId != "PartDesign::LinearPattern" or pname not in {"Mode", "Mode2"}:
        return value
    choices = obj.getEnumerationsOfProperty(pname)
    if value in choices:
        return value
    for aliases in (("Extent", "length"), ("Spacing", "offset")):
        if value in aliases:
            return next((alias for alias in aliases if alias in choices), value)
    return value


def _assign_props(obj, params: dict) -> list[dict]:
    """Set scalar params defensively (only keys that exist on the object).

    Length/Distance props -> mm Quantity; Angle props -> deg Quantity;
    Bool -> bool; Enumeration -> str; else raw. Returns a list of structured
    errors for keys that were rejected.
    """
    errors = []
    for k, v in (params or {}).items():
        if k in _STRUCTURAL_PARAM_KEYS:
            continue
        pname = _prop_name(obj, k)
        if pname is None:
            errors.append({
                "kind": "compile",
                "feature_id": getattr(obj, "Name", None),
                "message": (f"unsupported property {k!r} for {obj.TypeId} on "
                            f"FreeCAD {_freecad_version()}; this build does not "
                            "provide that property"),
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
                setattr(obj, pname, _enum_value(obj, pname, v))
            else:
                setattr(obj, pname, v)
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "kind": "compile",
                "feature_id": getattr(obj, "Name", None),
                "message": f"set {k}={v!r} failed: {type(exc).__name__}: {exc}",
            })
    return errors


def _apply_placement(obj, f: dict) -> list[dict]:
    """Put an origin-placed feature where the IR says, in world coordinates.

    Measured: ``AttachmentOffset`` does nothing to a PartDesign primitive while
    ``MapMode`` is Deactivated (the built shape stayed at x=[0,80] after setting
    it), while ``Placement`` moves the solid — so the typed IR placement lands on
    ``Placement`` and nowhere else. Rotation is about ``axis`` through
    ``position``, counter-clockwise, in degrees.
    """
    spec = f.get("placement") or {}
    pos = spec.get("position") or {}
    axis = spec.get("axis")
    angle = float(spec.get("angle") or 0.0)
    fid = f.get("id")
    if "Placement" not in obj.PropertiesList:
        return [{
            "kind": "semantic", "feature_id": fid,
            "message": (f"{f.get('op')!r} has a placement but {obj.TypeId} carries no "
                        "Placement property, so it cannot be positioned this way"),
        }]
    try:
        rotation = (App.Rotation(App.Vector(float(axis["x"]), float(axis["y"]),
                                            float(axis["z"])), angle)
                    if axis else App.Rotation())
        obj.Placement = App.Placement(
            App.Vector(float(pos.get("x", 0.0)), float(pos.get("y", 0.0)),
                       float(pos.get("z", 0.0))),
            rotation)
    except Exception as exc:  # noqa: BLE001
        return [{
            "kind": "compile", "feature_id": fid,
            "message": f"set placement failed: {type(exc).__name__}: {exc}",
        }]
    return []


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
    try:
        obj = doc.addObject(type_str, name)
    except Exception as exc:  # noqa: BLE001
        state["errors"].append({
            "kind": "compile", "feature_id": f.get("id"),
            "message": (f"cannot create feature {op!r} ({type_str}) on FreeCAD "
                        f"{_freecad_version()}: {type(exc).__name__}: {exc}"
                        + ("; circular_pattern is experimental and unavailable "
                           "in some builds; use polar_pattern for bolt circles"
                           if op == "circular_pattern" else "")),
        })
        return state
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
    if op in {"additive_loft", "subtractive_loft"}:
        section_ids = f.get("sections") or []
        try:
            if not profile_id or not section_ids:
                raise ValueError("loft needs profile_sketch and at least one additional sections sketch")
            obj.Sections = [ref_objects[sid] for sid in section_ids]
        except Exception as exc:  # noqa: BLE001
            state["errors"].append({"kind": "compile", "feature_id": f.get("id"),
                                    "message": f"set loft Sections failed: {exc}"})

    if op in _PATTERN_OPS and "Originals" in obj.PropertiesList:
        originals = [ref_objects[r] for r in (f.get("refs") or []) if r in ref_objects]
        if not originals:
            # A pattern with nothing to repeat builds an empty feature, and the
            # body Tip then falls back to the previous shape — a silent no-op.
            state["errors"].append({
                "kind": "semantic", "feature_id": f.get("id"),
                "message": (f"{op} needs at least one feature in 'refs' to repeat; "
                            "list the feature ids it should copy"),
            })
        else:
            try:
                obj.Originals = originals
            except Exception as exc:  # noqa: BLE001
                state["errors"].append({
                    "kind": "compile", "feature_id": f.get("id"),
                    "message": f"set Originals failed: {type(exc).__name__}: {exc}",
                })

    # revolution / groove / patterns -> the axis or direction LinkSub
    if op in _AXIS_OPS:
        state["errors"].extend(_set_axis_reference(obj, body, f, ref_objects))

    # mirrored -> MirrorPlane; draft -> NeutralPlane: one typed "plane" field,
    # two features that read it (both silently null without it).
    if op in _PLANE_OPS:
        state["errors"].extend(_set_plane_reference(
            obj, _PLANE_OPS[op], f, ref_objects, op=op))

    # fillet / chamfer -> Base = (feature, [edge names]);
    # draft re-shapes and thickness opens -> Base = (feature, [face names])
    if op in _SUB_ELEMENT_OPS:
        state["errors"].extend(_set_base_reference(
            obj, f, ref_objects, op=op,
            kind="faces" if op in _FACE_SUB_OPS else "edges"))

    # draft keeps records of faces it cannot taper at all (no intersection line
    # with the neutral plane) — the kernel only says "Invalid" for those.
    if op == "draft":
        state["errors"].extend(_draft_degenerate_errors(obj, f))

    # primitives -> Placement (there is no sketch to carry the position)
    if f.get("placement"):
        state["errors"].extend(_apply_placement(obj, f))

    state["errors"].extend(_assign_props(obj, params))
    body.addObject(obj)
    return state


def _copy_component(doc, body_spec, component, ref_objects, sketches):
    import hashlib
    path = component["path"]
    with open(path, "rb") as source:
        if hashlib.sha256(source.read()).hexdigest() != component["sha256"]:
            raise ValueError("component document failed its integrity check")
    source = FreeCAD.openDocument(path)
    try:
        original = source.getObject(_obj_name(component["body_id"], component["body_name"]))
        if original is None or original.TypeId != "PartDesign::Body" or original.Shape.isNull():
            raise ValueError("component contains no requested solid body")
        if component.get("reference"):
            body = doc.addObject("PartDesign::Body", _obj_name(body_spec["id"], body_spec["name"]))
            body.Label = body_spec["name"]
            feature = body.newObject("PartDesign::Feature", "ReferencedShape")
            feature.Shape = original.Shape.copy()
            body.Tip = feature
            if component.get("placement"):
                errors = _apply_placement(body, {"id": body_spec["id"], "placement": component["placement"]})
                if errors:
                    raise ValueError(errors[0]["message"])
        else:
            body = doc.copyObject(original, True)
            if body.Name != _obj_name(body_spec["id"], body_spec["name"]):
                raise ValueError("component body name collision")
            for node in body_spec.get("sketches", []) + body_spec.get("features", []):
                obj = doc.getObject(_obj_name(node["id"], node["name"]))
                if obj is None:
                    raise ValueError("component lost its parametric history")
                ref_objects[node["id"]] = obj
            for sk in body_spec.get("sketches", []):
                obj = ref_objects[sk["id"]]
                sketches.append({"id": sk["id"], "name": sk["name"], "errors": [],
                    "fully_constrained": bool(obj.FullyConstrained),
                    "dof": int(obj.DoF), "solve_status": int(obj.solve())})
    finally:
        _close_doc(source)


def _build(ir: dict, out_dir: str, components=None):
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
        component = (components or {}).get(b["id"])
        if b.get("part_ref") and not component:
            errors.append({"kind": "schema", "feature_id": b["id"],
                           "message": "PartRef must be resolved by the build runtime"})
            continue
        if component:
            _copy_component(doc, b, component, ref_objects, sketches)
        else:
            body = doc.addObject("PartDesign::Body", _obj_name(b.get("id"), b.get("name")))
            body.Label = b.get("name") or b.get("id")

            order, order_errors = _dependency_order(b, available=ref_objects)
            errors.extend(order_errors)
            for kind, node in order:
                if kind == "sketch":
                    sk_state = _add_sketch(doc, body, node, ref_objects)
                    sketches.append(sk_state)
                    # Surface per-sketch errors (e.g. solver conflicts) to the top level
                    # so the supervisor sees a structured, feature_id-tagged error.
                    errors.extend(sk_state.get("errors") or [])
                else:
                    fstate = _apply_feature(doc, body, node, ref_objects)
                    errors.extend(fstate["errors"])

        # The chain is reported in the IR's *declared* order (list order = build
        # order for features), which is what the model wrote and what a reader
        # expects — not in the interleaved order the scheduler happened to emit.
        for f in b.get("features") or []:
            feature_chain.append({
                "id": f.get("id"), "name": f.get("name") or f.get("id"),
                "op": f.get("op"), "params": f.get("params") or {},
                "suppressed": bool(f.get("suppress")),
            })

    try:
        doc.recompute()
    except Exception as exc:  # noqa: BLE001 — per-feature Invalid state below
        errors.append({"kind": "compile", "feature_id": None,
                       "message": f"recompute raised: {type(exc).__name__}: {exc}"})

    # ── why is there no solid? ────────────────────────────────────────────────
    #
    # Collected AFTER the recompute, because "this produced no geometry" is only
    # knowable then — and it is the one failure the build could previously report
    # with an empty error list, which the supervisor then rendered as the generic
    # "handler reported failure". Observed live: a phone stand whose side profile
    # was silently dropped by the (then missing) world → sketch-frame transform.
    # The model had nothing to repair and the user nothing to read.
    #
    # This runs FIRST among the post-recompute checks on purpose: the supervisor
    # forwards only the first error, and the reason geometry is missing (an
    # unclosed profile wire, a collapsed sketch) is the root cause — a generic
    # "feature is Invalid" line for the pad that consumed such a sketch would
    # bury it. A body whose solid genuinely built produces nothing here, so the
    # checks below still surface as the first error in their own cases.
    errors.extend(_no_geometry_errors(ir, doc, ref_objects))

    # A feature can fail to compute while EARLIER features still hold. The
    # Body's Tip then keeps the last good shape, the solid measures fine, and
    # the build looks like a success with a subtly wrong part — the "partial
    # Tip" failure mode. FreeCAD marks such features "Invalid" in obj.State
    # after the recompute; surface that as a compile error so the Gate never
    # grades a stale Tip. Suppressed features are intentionally absent.
    errors.extend(_invalid_feature_errors(ir, ref_objects))

    # FreeCAD treats a cut of nothing as SUCCESS: a Pocket whose profile misses
    # the material (or a Pad extruded into empty space) computes "fine", the Tip
    # keeps the previous solid, and the build would report ok with a valid
    # shape that is not the IR's declared result — a hole that never happened,
    # reported as built. A solid feature that leaves the tip unchanged changed
    # nothing, so it is a compile error naming the feature.
    errors.extend(_noop_feature_errors(ir, ref_objects))

    # Collect resulting solids from every body.
    body_shapes = []
    body_results = []
    body_ids = { _obj_name(b.get("id"), b.get("name")): b.get("id")
                 for b in ir.get("bodies") or [] }
    for obj in doc.Objects:
        if getattr(obj, "TypeId", "") == "PartDesign::Body":
            try:
                sh = obj.Shape
                if sh is not None and not sh.isNull():
                    body_shapes.append(sh)
                    body_results.append({"id": body_ids[obj.Name], "shape": sh})
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
        "body_results": body_results,
        "sketches": sketches,
        "feature_chain": feature_chain,
        "ref_objects": ref_objects,
        "errors": errors,
    }


def _obj_state(obj) -> list:
    try:
        return [str(s) for s in obj.State]
    except Exception:  # noqa: BLE001
        return []


def _linked(link):
    """The object behind a FreeCAD link property.

    A link property comes back as ``(object, [subelement names])`` — not as the
    object. Reading ``.Name`` off it silently yields the default, which is how a
    diagnosis ends up saying ``profile=?`` instead of naming the sketch.
    """
    if isinstance(link, tuple) and link:
        return link[0]
    return link


def _profile_hint(sk, s: dict) -> str:
    """A second sentence when the profile's points look mis-framed.

    A profile that projects onto a single line, or onto a single point, is not a
    profile — and there is exactly one common reason: the points were written in
    the sketch's *local* (u, v) frame (with a zero third component) while the
    compiler reads them as world coordinates. Naming that saves the reader a
    round of guessing, so it is worth the few lines.
    """
    pts = []
    for g in s.get("geometry") or []:
        if g.get("construction"):
            continue
        for p in g.get("points") or []:
            # Diagnostics describe the projected footprint even when the input
            # was refused for being off-plane. They must not throw a second
            # exception that hides the original, feature-scoped compile error.
            v = _sketch_point(sk, p, validate_plane=False)
            pts.append((round(v.x, 9), round(v.y, 9)))
    unique = sorted(set(pts))
    framing = (
        " That is what a profile looks like when its points were written in the "
        "sketch's local (u, v) frame (third component 0) instead of world "
        "coordinates: on the YZ plane the in-plane coordinates come from the "
        "point's y and z (x is the out-of-plane component), on XZ from x and z, "
        "on XY from x and y."
    )
    if len(unique) < 2:
        return (
            f" All {len(pts)} profile points are at the same place in the "
            f"sketch's own frame; a profile needs at least three distinct "
            f"points." + framing
        )
    if len(unique) < 3:
        return (
            f" The profile has only {len(unique)} distinct point(s) in the "
            f"sketch's own frame, so it encloses no area." + framing
        )
    area = 0.0
    (x0, y0) = unique[0]
    for i in range(1, len(unique) - 1):
        (x1, y1), (x2, y2) = unique[i], unique[i + 1]
        area = max(area, abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2.0)
    if area < 1e-9:
        return (
            f" All {len(unique)} distinct points lie on ONE LINE in the sketch's "
            f"own frame, so the profile encloses no area." + framing
        )
    return ""


def _invalid_feature_errors(ir: dict, ref_objects: dict) -> list[dict]:
    """Features FreeCAD left in "Invalid" state after the final recompute.

    A failed feature does not clear the Body — the Tip keeps the last shape
    that DID compute — so without this check a build where the final feature
    silently failed still measures and exports the earlier solid as if the
    whole IR had built. Suppressed features are skipped on purpose: they are
    declared absent, and their state is not evidence of anything.
    """
    out: list[dict] = []
    for b in ir.get("bodies") or []:
        for f in b.get("features") or []:
            if f.get("suppress"):
                continue
            fid = f.get("id")
            obj = ref_objects.get(fid)
            if obj is None:
                continue
            try:
                state = obj.State
            except Exception:  # noqa: BLE001
                continue
            if "Invalid" in state:
                out.append({
                    "kind": "compile", "feature_id": fid,
                    "message": (
                        f"feature {fid!r} (op={f.get('op')!r}) is Invalid after"
                        " recompute: it did not compute. The Body Tip still"
                        " holds an earlier shape, so the exported solid is"
                        " NOT the IR's declared result — repair this feature."
                    ),
                })
    return out


def _same_solid(a, b) -> bool:
    """True if two tip shapes are the same solid (a feature changed nothing).

    ``isSame``/``isEqual`` catch OCC returning the input shape unchanged from a
    boolean. The volume/area fallback catches the observed live behaviour of a
    *disjoint* boolean: OCC still rebuilds the solid, so identity fails, but the
    rebuild only perturbs the last digits (measured: |dV| ~ 3e-10 on a 4e5 mm3
    part) — orders of magnitude below any cut that removes real material, while
    a genuine no-op stays within a tight absolute epsilon.
    """
    try:
        if a.isSame(b) or a.isEqual(b):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        vol_eps = max(1e-6, 1e-12 * max(abs(a.Volume), abs(b.Volume)))
        area_eps = max(1e-6, 1e-12 * max(abs(a.Area), abs(b.Area)))
        return (abs(a.Volume - b.Volume) <= vol_eps
                and abs(a.Area - b.Area) <= area_eps)
    except Exception:  # noqa: BLE001
        return False


def _noop_feature_errors(ir: dict, ref_objects: dict) -> list[dict]:
    """Solid features that left the body's tip identical to the previous tip.

    FreeCAD reports a pocket whose profile misses the material (and a pad
    extruded away from the body) as a *successful* no-op: the feature is not
    Invalid, the previous solid still measures valid, and the build would say
    ok — while the declared hole/boss simply does not exist in the result.
    Whatever the IR declared, a feature that changed nothing did not implement
    it, so it is surfaced as an attributable compile error. Datum planes carry
    no solid and are excluded; suppressed/Invalid features are handled above.
    """
    out: list[dict] = []
    for b in ir.get("bodies") or []:
        prev_shape = None
        for f in b.get("features") or []:
            if f.get("suppress") or f.get("op") == "datum_plane":
                continue
            obj = ref_objects.get(f.get("id"))
            if obj is None:
                continue
            try:
                if "Invalid" in obj.State:
                    # Already reported by _invalid_feature_errors; its stale
                    # shape must not become the baseline for the next feature.
                    prev_shape = None
                    continue
                shape = obj.Shape
            except Exception:  # noqa: BLE001
                continue
            if shape is None or shape.isNull():
                continue
            if prev_shape is not None and _same_solid(shape, prev_shape):
                out.append({
                    "kind": "compile", "feature_id": f.get("id"),
                    "message": (
                        f"feature {f.get('id')!r} (op={f.get('op')!r}) did not "
                        "change the solid: the result is identical to the "
                        "previous feature. A pocket/groove whose profile does "
                        "not intersect the material, or a pad/revolution "
                        "extruded into empty space, produces this. Check the "
                        "sketch plane, attachment and direction against the "
                        "WORLD COORDINATES contract."
                    ),
                })
            prev_shape = shape
    return out


def _no_geometry_errors(ir: dict, doc, ref_objects: dict) -> list[dict]:
    """Structured reasons why a sketch or body yielded no geometry.

    Ordering matters: the supervisor forwards only the first error, so the most
    specific one (the sketch that could not form a wire) must come first. A body
    that is empty *because* one of its sketches failed is not reported again —
    that would bury the actionable line under a restatement of it.
    """
    out: list[dict] = []
    explained_bodies: set = set()

    bodies = ir.get("bodies") or []
    if not bodies:
        # Committing an empty document is a real thing to do (a model may commit
        # before patching anything). Say that, rather than letting it fall through
        # to a failure with no reason attached.
        return [{
            "kind": "schema",
            "feature_id": None,
            "message": (
                "the IR has no bodies, so there is nothing to build. Add a sketch "
                "and a feature (e.g. a pad) with ir_patch before calling ir_commit."
            ),
        }]

    for b in bodies:
        body_id = b.get("id")
        for s in b.get("sketches") or []:
            sk = ref_objects.get(s.get("id"))
            if sk is None:
                continue
            try:
                shape = sk.Shape
            except Exception as exc:  # noqa: BLE001
                out.append({
                    "kind": "compile", "feature_id": s.get("id"),
                    "message": f"sketch {s.get('id')!r} has no computable shape: "
                               f"{type(exc).__name__}: {exc}",
                })
                explained_bodies.add(body_id)
                continue
            bad = shape is None or shape.isNull()
            wires = 0
            if not bad:
                try:
                    wires = len(shape.Wires)
                except Exception:  # noqa: BLE001
                    wires = 0
                bad = wires == 0
            if not bad:
                continue
            geom = len([g for g in (s.get("geometry") or []) if not g.get("construction")])
            cons = len(s.get("constraints") or [])
            out.append({
                "kind": "compile",
                "feature_id": s.get("id"),
                "message": (
                    f"sketch {s.get('id')!r} does not form a closed wire "
                    f"({geom} profile curves, {cons} constraints, "
                    f"wires={wires}, state={_obj_state(sk)}). "
                    f"A pad/pocket profile must be a closed loop of connected "
                    f"curves; check for a gap or a duplicate point between "
                    f"consecutive curves." + _profile_hint(sk, s)
                ),
            })
            explained_bodies.add(body_id)

    for obj in doc.Objects:
        if getattr(obj, "TypeId", "") != "PartDesign::Body":
            continue
        name = getattr(obj, "Name", "?")
        if name in explained_bodies:
            continue
        try:
            shape = obj.Shape
            empty = shape is None or shape.isNull()
        except Exception:  # noqa: BLE001
            empty = True
        if not empty:
            continue

        details: list[str] = []
        open_profile = False
        for o in getattr(obj, "Group", []) or []:
            type_id = getattr(o, "TypeId", "")
            if not type_id.startswith("PartDesign::") or type_id == "PartDesign::Body":
                continue
            line = (
                f"{getattr(o, 'Name', '?')}({type_id.split('::')[-1]}) "
                f"state={_obj_state(o)}"
            )
            profile = _linked(getattr(o, "Profile", None))
            if profile is not None:
                line += f" profile={getattr(profile, 'Name', '?')}"
                try:
                    wires = list(profile.Shape.Wires)
                    closed = sum(1 for w in wires if w.isClosed())
                    line += f" wires={len(wires)} closed={closed}"
                    if wires and closed == 0:
                        open_profile = True
                except Exception:  # noqa: BLE001
                    pass
            details.append(line)
        if not details:
            details = [f"body state={_obj_state(obj)}"]

        reason = ""
        if open_profile:
            reason = (
                " The profile sketch has no closed wire: its curves do not join "
                "end-to-end (check for a gap or a missing coincidence between "
                "consecutive curves)."
            )
        else:
            reason = (
                " The features built, but the result is empty — a profile that is "
                "not closed, a feature whose computed shape failed, or a "
                "subtractive feature that removed everything."
            )
        out.append({
            "kind": "compile",
            "feature_id": name,
            "message": f"body {name!r} produced no solid: " + "; ".join(details) + "." + reason,
        })
    return out


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
