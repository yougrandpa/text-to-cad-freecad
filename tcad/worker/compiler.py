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
    return deps


def _dependency_order(b: dict) -> tuple[list[tuple[str, dict]], list[dict]]:
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
    built: set = set()
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
                    f"refs/profile_sketch/plane target must name something in the "
                    f"same body."
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
    con_type: str, refs: list, value: float | None
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
        if (con_type, len(refs)) in _REFS_ONLY_SAFE:
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


def _sketch_point(sk, p: dict) -> "App.Vector":
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
    return App.Vector(local.x, local.y, 0.0)


def _add_geometry(sk, g: dict):
    """Append one sketch geometry; return its index."""
    kind = g.get("kind")
    pts = g.get("points") or []
    construction = bool(g.get("construction", False))
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

    # ── constraints ──
    for con in s.get("constraints") or []:
        try:
            con_type = con.get("type")
            refs = list(con.get("refs") or [])
            value = con.get("value")
            args, via_setdatum = _constraint_args(con_type, refs, value)
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

        order, order_errors = _dependency_order(b)
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

    doc.recompute()

    # ── why is there no solid? ────────────────────────────────────────────────
    #
    # Collected AFTER the recompute, because "this produced no geometry" is only
    # knowable then — and it is the one failure the build could previously report
    # with an empty error list, which the supervisor then rendered as the generic
    # "handler reported failure". Observed live: a phone stand whose side profile
    # was silently dropped by the (then missing) world → sketch-frame transform.
    # The model had nothing to repair and the user nothing to read.
    errors.extend(_no_geometry_errors(ir, doc, ref_objects))

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
            v = _sketch_point(sk, p)
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
