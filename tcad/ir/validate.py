"""Semantic pre-checks for an :class:`IrDocument`.

This is the *read-side* validator. The model never writes the IR directly; every
mutation funnels through ``tcad.ir.patch.apply_patch``, which calls
:func:`validate_ir` on the *resulting* document and rejects (with a
``ToolError``) any ``error``-severity issue. So this module is the single place
that knows the cross-reference and DAG rules of the IR.

Severity contract
-----------------
* ``error`` — blocks a commit. The model must fix it.
* ``warn``  — does not block; surfaces a smell (e.g. an unverified op's param,
  or a param key not in the verified allow-list). The compiler is permissive
  (``.get(key, default)``), so an unknown key is at worst ignored — we warn,
  we do not fail the build.

NOTE on the param allow-list (design §7 / 附录 A-1 + B-3)
--------------------------------------------------------
The allow-list below is derived from the *verified* FreeCAD property tables.
It describes accepted IR vocabulary, not a promise that every installed kernel
has every property. Older builds lack some newer scalars: the worker checks the
live property list and refuses unavailable keys with the FreeCAD version.
FreeCAD property names are PascalCase (``Length``, ``UpToFace`` ...); the IR
uses snake_case param keys (``length``, ``up_to_face`` ...), and the worker
compiler is responsible for the translation. When a param key for a *verified*
op is not in the list we emit ``param_unknown`` (warn, not error) — failing the
build on a key the allow-list simply forgot would be worse than the smell. Ops
whose property tables were *not* verified at writing time (draft, thickness,
multi_transform, polar_pattern, datum_plane, additive_*/subtractive_*) are
treated as permissive: any params are allowed and a single ``op_unverified``
warn is emitted per feature.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from tcad.ir.capability import EXPERIMENTAL, capability
from tcad.ir.schema import (
    FeatureSpec,
    IrDocument,
    SketchSpec,
)


def _is_finite_number(v: Any) -> bool:
    """True when ``v`` is a real int/float (not bool) without NaN/inf.

    FreeCAD/OCC do not reject NaN or infinity at the API boundary — the values
    propagate into placements and solver inputs and surface later as either a
    segfault-adjacent native error or a silently empty shape. Rejecting
    non-finite numbers here is the only place the failure is locatable.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return True
    return math.isfinite(v)


def _finite_issue(where: str, target_id: str | None) -> "ValidationIssue":
    return ValidationIssue(
        code="non_finite_number", severity="error",
        message=f"{where} must be a finite number (NaN/±inf are not allowed)",
        target_id=target_id,
    )

# ── param allow-lists (IR-level snake_case keys) ──────────────────────────────
# A key belongs here only if the compiler can HONOUR it: `_assign_props` reaches a
# real FreeCAD property whose type it can set from a JSON value (length/angle/
# bool/enum/float/string/vector), or the compiler consumes it itself.
#
# Measured, not assumed. `tests/contract/test_param_capability.py` creates every
# PartDesign object on the real kernel and asserts no key in these tables resolves
# to a Link/LinkSub/LinkList/Shape/List property — the shapes `setattr` cannot
# take from a scalar. Keys that used to sit here and failed exactly that test are
# listed in `_PARAM_GUIDANCE` below, so a model that reaches for one is told why
# and what to use instead *before* anything is persisted.
_VERIFIED_OP_PARAMS: dict[str, frozenset[str]] = {
    "additive_box": frozenset({"length", "width", "height"}),
    "subtractive_box": frozenset({"length", "width", "height"}),
    "pad": frozenset({
        # NOTE: no "direction" — PartDesign::Pad.Direction is a Vector, and
        # `_assign_props` has no Vector branch, so a JSON {"x":…} would fail on
        # setattr. Custom directions are a capability to add, not a key to list.
        "midplane", "reversed", "length", "length2", "offset", "offset2",
        "taper_angle", "taper_angle2", "use_custom_vector", "along_sketch_normal",
        "type", "type2", "start_type", "side_type",
    }),
    "pocket": frozenset({
        "midplane", "reversed", "length", "length2", "offset", "offset2",
        "taper_angle", "taper_angle2", "use_custom_vector", "along_sketch_normal",
        "type", "type2", "start_type", "side_type",
    }),
    "revolution": frozenset({
        "axis", "angle", "angle2", "type", "type2", "side_type", "midplane",
        "reversed", "start_type", "start_offset", "allow_multi_face",
        "fuse_order", "operation",
    }),
    "groove": frozenset({
        # NOTE: no "fuse_order" — PartDesign::Groove has no such property (probe:
        # NOSUCH), while Revolution does. Same key, different object.
        "axis", "angle", "angle2", "type", "type2", "side_type", "midplane",
        "reversed", "start_type", "start_offset", "allow_multi_face",
        "operation",
    }),
    "additive_loft": frozenset({"ruled", "closed", "refine"}),
    "subtractive_loft": frozenset({"ruled", "closed", "refine"}),
    "datum_plane": frozenset(),
    "hole": frozenset({
        "depth", "diameter", "drill_point", "drill_point_angle", "midplane",
        "reversed", "tapered", "tapered_angle", "base_profile_type",
        "start_type", "start_offset", "threaded", "thread_type", "thread_size",
        "thread_class", "thread_direction", "thread_depth", "thread_depth_type",
        "thread_fit", "model_thread", "cosmetic_thread", "hole_cut_type",
        "hole_cut_diameter", "hole_cut_depth", "hole_cut_countersink_angle",
        "use_custom_thread_clearance", "custom_thread_clearance",
        "drill_for_depth", "depth_type", "allow_multi_face", "operation",
    }),
    "fillet": frozenset({
        "radius", "use_all_edges", "support_transform", "operation",
    }),
    "chamfer": frozenset({
        "size", "size2", "angle", "chamfer_type", "flip_direction",
        "use_all_edges", "support_transform", "operation",
    }),
    "draft": frozenset({
        # The faces to re-shape ride on base_feature + sub_elements and the
        # neutral plane on the typed "plane" field; these are the scalars left
        # over. Measured on the real kernel: 5 deg off the XY plane turns a
        # 40x40x20 box into a 29282.0083 mm^3 prismatoid (analytic value) —
        # tests/contract/test_draft_thickness.py.
        "angle", "reversed", "operation", "support_transform",
    }),
    "thickness": frozenset({
        # The face(s) to open ride on base_feature + sub_elements. Value is a
        # Length, so it takes a plain mm number. Measured: opening the top face
        # of the same box with 2 mm leaves 8672 mm^3 = the analytic open box.
        "value", "reversed", "operation", "support_transform",
    }),
    "mirrored": frozenset({
        "transform_mode",
    }),
    "linear_pattern": frozenset({
        "axis", "mode", "mode2", "length", "length2", "offset", "occurrences",
        "occurrences2", "reversed", "reversed2", "transform_mode",
    }),
    "polar_pattern": frozenset({
        "axis", "angle", "occurrences", "reversed", "offset", "mode",
        "transform_mode",
    }),
    "circular_pattern": frozenset({
        "number_circles", "radial_distance", "tangential_distance", "symmetry",
        "transform_mode",
    }),
}

#: Keys a model may reach for that the compiler cannot honour, and what to do
#: instead. Using one is an **error** (not the generic "unverified key" warning):
#: without the guidance it survives validation and dies inside FreeCAD with
#: ``TypeError: type must be 'DocumentObject' … not str``, after the patch has
#: already been persisted and a worker round-trip has been spent.
#:
#: The message has to be per **key**, because the same name is a different
#: property on different objects: ``Base`` is a ``LinkSub`` on Fillet/Chamfer but a
#: Vector (the base *point*) on Revolution/Groove, and ``DepthType`` exists on Hole
#: but not on Pad. ``_GUIDANCE_BASE`` carries the messages that are true wherever
#: the key is refused; ``_GUIDANCE_BY_OP`` overrides the ones that are not.
_GUIDANCE_BASE: dict[str, str] = {
    "profile": 'use "profile_sketch" (an IR sketch id) — "profile" is the FreeCAD property name',
    "base": ('this is the FreeCAD property name; use the typed fields instead: '
             'set "base_feature" (the feature) plus "sub_elements" (names from '
             'ir_digest) on the feature, e.g. {"op":"fillet","base_feature":"ft_plate",'
             '"sub_elements":["Edge1"],"params":{"radius":2.0}} — on draft/thickness '
             "the same two fields name FACES instead of edges"),
    "neutral_plane": ('use the typed "plane" field on the draft feature, e.g. '
                      '{"op":"draft","plane":{"kind":"origin_plane","plane":"XY"}} '
                      '(or a face / datum plane); "NeutralPlane" is the FreeCAD '
                      "property name, and without it the draft returns a null shape"),
    "facelist": ('the faces a draft re-shapes or a thickness opens are selected with '
                 '"base_feature" + "sub_elements" (face names from ir_digest)'),
    "face_list": ('the faces a draft re-shapes or a thickness opens are selected with '
                  '"base_feature" + "sub_elements" (face names from ir_digest)'),
    "up_to_face": ('the IR cannot carry a face reference; use "type": "Length" or '
                   '"ThroughAll" and express the extent with a second feature'),
    "up_to_shape": ('the IR cannot carry a shape reference; use "type": "Length" or '
                    '"ThroughAll"'),
    "mirror_plane": ('use the typed "plane" field on the mirrored feature, e.g. '
                     '{"op":"mirrored","refs":["ft_plate"],"plane":{"kind":'
                     '"origin_plane","plane":"XZ"}} (or a face / datum plane); '
                     '"MirrorPlane" is the FreeCAD property name'),
    "originals": 'use "refs" — the compiler reads the referenced feature ids from there',
    "axis": ('axis is a *name*, not the FreeCAD property: revolution/groove/'
             'linear_pattern/polar_pattern take params.axis = "X"/"Y"/"Z" (the '
             'body origin axes; revolution/groove also accept H_Axis/V_Axis/N_Axis '
             'of the profile sketch) and the compiler builds the reference itself'),
    "direction": ('on the pattern ops the direction comes from params.axis = '
                  '"X"/"Y"/"Z"; on Pad/Pocket the IR has no Vector param'),
    "direction2": 'the IR cannot carry a second direction reference',
    "reference_axis": ('use params.axis (revolution/groove: "X"/"Y"/"Z" or the '
                       'profile sketch\'s H_Axis/V_Axis/N_Axis); the compiler '
                       'resolves the name into the reference itself'),
    "start_reference": ('the IR cannot carry an edge/face reference for the start of the '
                        'feature; use "start_offset"'),
    "spacings": 'the IR cannot carry a float list; use "length" + "occurrences"',
    "spacings2": 'the IR cannot carry a float list; use "length2" + "occurrences2"',
    "spacing_pattern": 'the IR cannot carry a float list; use "length" + "occurrences"',
    "spacing_pattern2": 'the IR cannot carry a float list; use "length2" + "occurrences2"',
    "suppressed_positions": 'the IR cannot carry a list of index pairs',
    "add_sub_shape": 'internal PartDesign state (a Part::Shape), not an IR input',
    "fuse_order": 'PartDesign::Groove has no such property (Revolution does), so it is not an IR input here',
    "depth_type": 'PrimitivePad/Pocket have no DepthType property; use "type"',
}

#: Corrections where the key-level message would describe the wrong property.
_GUIDANCE_BY_OP: dict[tuple[str, str], str] = {
    ("additive_box", "depth"): 'native boxes use length/width/height, not beam recipe width/depth; remove a stored depth with update_feature params_remove=["depth"]',
    ("subtractive_box", "depth"): 'native boxes use length/width/height; remove a stored depth with update_feature params_remove=["depth"]',
    ("revolution", "base"): ('Revolution.Base is the base POINT (a Vector), and the IR '
                             'has no Vector param; the profile already places the shape'),
    ("groove", "base"): ('Groove.Base is the base POINT (a Vector), and the IR has no '
                         'Vector param; the profile already places the shape'),
    ("hole", "base"): 'PartDesign::Hole has no "base" property',
}


def _guidance_for(op: str, key: str) -> str | None:
    """Why ``key`` cannot be honoured by ``op``, or ``None`` if it is a real key."""
    return _GUIDANCE_BY_OP.get((op, key)) or _GUIDANCE_BASE.get(key)


# Treated as permissive (any params allowed) + one `op_unverified` warn.
#: Ops that select sub-elements of another feature via base_feature +
#: sub_elements. ``fillet``/``chamfer`` take *edges*; ``draft`` re-shapes and
#: ``thickness`` opens *faces*. Mirrors the compiler's ``_SUB_ELEMENT_OPS``.
_SUB_ELEMENT_OPS: frozenset[str] = frozenset({"fillet", "chamfer", "draft", "thickness"})

#: The subset of the above whose sub-elements are FACES, so the message can ask
#: for face names (and the compiler can list faces) instead of edges.
_FACE_SUB_OPS: frozenset[str] = frozenset({"draft", "thickness"})

#: Ops that read a plane from the typed ``plane`` field, mapped to the FreeCAD
#: property that carries it. Both are ``App::PropertyLinkSub``: unset, FreeCAD
#: returns a NULL shape rather than an error, and the body Tip silently falls back
#: to the previous shape. Measured on the real kernel in
#: tests/contract/test_patterns_mirror.py (mirrored) and
#: tests/contract/test_draft_thickness.py (draft).
_PLANE_OPS: dict[str, str] = {"mirrored": "MirrorPlane", "draft": "NeutralPlane"}

#: Ops that carry their own size and nothing else, so a ``placement`` is the only
#: way to say *where* they go. Every other op is positioned by its sketch (with the
#: sketch's own plane/offset), by its ``refs`` or by its ``plane``; giving one of
#: them a placement would be a second, contradictory answer to the same question.
#: Measured on the real kernel in ``tests/contract/test_primitive_placement.py``.
_PLACEMENT_OPS: frozenset[str] = frozenset({
    "additive_box", "additive_cylinder", "additive_sphere", "additive_cone",
    "subtractive_box", "subtractive_cylinder", "subtractive_sphere", "subtractive_cone",
    "datum_plane",
})

#: Ops that repeat features and therefore need ``params.axis`` to say along/about
#: which body axis. Mirrors the compiler's ``_AXIS_LINK_OPS`` pattern half.
_PATTERN_AXIS_OPS: frozenset[str] = frozenset({"linear_pattern", "polar_pattern"})
_PATTERN_AXIS_VALUES: frozenset[str] = frozenset({"x", "y", "z"})
_ORIGIN_PLANE_NAMES: frozenset[str] = frozenset({"XY", "XZ", "YZ"})

_UNVERIFIED_OPS: frozenset[str] = frozenset({
    "multi_transform",
    "additive_cylinder", "additive_sphere", "additive_cone",
    "subtractive_cylinder", "subtractive_sphere", "subtractive_cone",
})


class ValidationIssue(BaseModel):
    """One finding from :func:`validate_ir`.

    ``severity`` is ``"error"`` (blocks a commit) or ``"warn"`` (does not).
    ``code`` is a stable machine key; ``target_id`` is the offending entity id
    when known (so the model can be told *what* to change).
    """

    code: str
    severity: str  # "error" | "warn"
    message: str
    target_id: str | None = None


def validate_ir(ir: IrDocument) -> list[ValidationIssue]:
    """Run all semantic pre-checks. Returns issues; empty list == valid."""
    issues: list[ValidationIssue] = []

    sketches: list[SketchSpec] = ir.all_sketches()
    features: list[FeatureSpec] = ir.all_features()

    sketch_ids: set[str] = {s.id for s in sketches}
    feature_ids: set[str] = {f.id for f in features}

    if ir.assembly is not None:
        try:
            ir.assembly.validate_bodies(b.id for b in ir.bodies)
            if any(b.motion is not None for b in ir.bodies):
                raise ValueError("native assembly and prescribed body.motion cannot be combined; clear body.motion first")
        except ValueError as exc:
            issues.append(ValidationIssue(code="assembly_invalid", severity="error", message=str(exc)))

    # 1) Unique ids within the document (sketch<->feature clash included).
    seen: set[str] = set()
    for body in ir.bodies:
        if body.id in seen:
            issues.append(ValidationIssue(code="dup_body_id", severity="error",
                          message=f"duplicate body id '{body.id}'", target_id=body.id))
        seen.add(body.id)
        if body.suspension_pivot is not None and not all(math.isfinite(v) for v in body.suspension_pivot.as_tuple()):
            issues.append(_finite_issue('suspension pivot',body.id))
        if body.motion is not None:
            motion = body.motion
            values = (*motion.pivot.as_tuple(), *motion.axis.as_tuple(), motion.ratio)
            if not all(math.isfinite(v) for v in values):
                issues.append(_finite_issue("body motion", body.id))
            elif math.hypot(*motion.axis.as_tuple()) <= 1e-12:
                issues.append(ValidationIssue(code="motion_axis_zero", severity="error",
                              message="motion axis must be non-zero", target_id=body.id))
    for s in sketches:
        if s.id in seen:
            issues.append(ValidationIssue(
                code="dup_sketch_id", severity="error",
                message=f"duplicate sketch id '{s.id}'", target_id=s.id))
        seen.add(s.id)
    for f in features:
        if f.id in seen:
            issues.append(ValidationIssue(
                code="dup_feature_id", severity="error",
                message=f"duplicate feature id '{f.id}'", target_id=f.id))
        seen.add(f.id)

    # 1b) A non-zero sketch `offset` is refused.
    #
    # Geometry coordinates are WORLD coordinates (`_sketch_point` maps each point
    # through the sketch Placement's inverse) and the offset is part of that same
    # Placement, so the offset does not position anything on its own. Worse,
    # combined with the origin-binding recipe this codebase's own tool
    # description recommended ("model the profile around the sketch's OWN origin
    # ... then place it with offset"), it does not merely fail to move the
    # profile — it *deforms* it, silently. Measured on the real kernel, a 40x20
    # XY rectangle, pad 5:
    #
    #   offset (0, 0, 0)   -> bbox 40x20x5, volume 4000   (correct)
    #   offset (10, 10, 0) -> bbox 40x20x5, volume 2500   (same box, wrong shape)
    #   offset (5, -3, 0)  -> bbox 40x23x5, volume 4050   (sheared)
    #
    # An `error`, not a warning: the outcome is a solid that is not the shape the
    # coordinates describe, and a Gate can only catch that if the user happened
    # to state a volume or bbox requirement. "The part is quietly not what the
    # numbers say" is the failure this project treats as unacceptable, and the
    # fix is entirely within the model's control — write the coordinates.
    for s in sketches:
        off = s.offset
        if off is None:
            continue
        if any(abs(float(v)) > 1e-9 for v in (off.x, off.y, off.z)):
            issues.append(ValidationIssue(
                code="sketch_offset_unsupported", severity="error",
                message=(f"sketch '{s.id}' sets offset=({off.x}, {off.y}, {off.z}). "
                         f"Sketch coordinates ARE world coordinates, so an offset "
                         f"does not place the profile and — combined with a curve "
                         f"bound to the sketch origin — deforms it (measured: a "
                         f"40x20 rectangle, pad 5, gives volume 2500 with "
                         f"offset (10,10,0) instead of 4000). Remove the offset and "
                         f"write the coordinates where you want the profile; anchor "
                         f"a profile that does not start at the origin by "
                         f"dimensioning its points absolutely (DistanceX/DistanceY "
                         f"per edge) instead of binding the origin."),
                target_id=s.id))

    # 2) feature.profile_sketch resolves to an existing SketchSpec.id
    for f in features:
        if f.profile_sketch is not None and f.profile_sketch not in sketch_ids:
            issues.append(ValidationIssue(
                code="profile_sketch_missing", severity="error",
                message=(f"feature '{f.id}' references non-existent profile "
                         f"sketch '{f.profile_sketch}'"),
                target_id=f.id))

    # 3) feature.refs[] resolve to existing feature ids
    for body in ir.bodies:
        local_sketches = {s.id for s in body.sketches}
        for f in body.features:
            if f.op in {"additive_loft", "subtractive_loft"}:
                profiles = [f.profile_sketch, *f.sections]
                if not f.profile_sketch or not f.sections:
                    issues.append(ValidationIssue(code="loft_sections_missing", severity="error",
                        message=f"loft '{f.id}' needs profile_sketch and at least one additional sections sketch", target_id=f.id))
                elif len(set(profiles)) != len(profiles) or any(s not in local_sketches for s in profiles):
                    issues.append(ValidationIssue(code="loft_sections_invalid", severity="error",
                        message=f"loft '{f.id}' profiles must be distinct sketch IDs in the same body, ordered along the loft", target_id=f.id))
            elif f.sections:
                issues.append(ValidationIssue(code="loft_sections_unused", severity="error",
                    message=f"feature '{f.id}' has sections but only additive_loft/subtractive_loft use them", target_id=f.id))

    for f in features:
        for r in f.refs:
            if r not in feature_ids:
                issues.append(ValidationIssue(
                    code="ref_missing", severity="error",
                    message=(f"feature '{f.id}' references non-existent "
                             f"feature '{r}'"),
                    target_id=f.id))

    # 4) refs form an acyclic DAG (edges: feature -> its refs)
    issues.extend(_check_acyclic(features, feature_ids))

    # 4b) face-attached sketches point at an existing feature, with a sub-element
    for s in sketches:
        if s.plane.kind != "face":
            continue
        fid = s.plane.feature_id
        target = ir.find_feature(fid) if fid else None
        if target is None:
            issues.append(ValidationIssue(
                code="face_target", severity="error",
                message=(f"sketch '{s.id}' attaches to a face but "
                         f"feature_id={fid!r} is not a feature in this document"),
                target_id=s.id))
        elif not (s.plane.sub or "").strip():
            # Without a sub-element there is nothing to attach to; FreeCAD leaves
            # the sketch unattached and the profile then collapses in its own
            # frame, which surfaces as a bogus "check your world coordinates".
            issues.append(ValidationIssue(
                code="face_sub_missing", severity="error",
                message=(f"sketch '{s.id}' attaches to a face of '{fid}' but has no "
                         'sub-element name; call ir_digest and pass e.g. '
                         '"sub": "Face6"'),
                target_id=s.id))

    # 5) datum_plane sketches point at a datum_plane feature
    for s in sketches:
        if s.plane.kind == "datum_plane":
            fid = s.plane.feature_id
            target = ir.find_feature(fid) if fid else None
            if target is None or target.op != "datum_plane":
                issues.append(ValidationIssue(
                    code="datum_plane_target", severity="error",
                    message=(f"sketch '{s.id}' attaches to a datum_plane but "
                             f"'{fid}' is not a datum_plane feature"),
                    target_id=s.id))

    # 5b) sub-element references (fillet/chamfer: edges; draft/thickness: faces)
    # must be complete and ordered
    for f in features:
        uses_subs = f.op in _SUB_ELEMENT_OPS
        face_op = f.op in _FACE_SUB_OPS
        sub_word = "face" if face_op else "edge"
        example = '"Face6"' if face_op else '"Edge1"'
        if f.sub_elements or f.base_feature:
            if not uses_subs:
                issues.append(ValidationIssue(
                    code="sub_elements_unused", severity="error",
                    message=(f"feature '{f.id}' (op '{f.op}') has base_feature/"
                             "sub_elements, but only "
                             f"{sorted(_SUB_ELEMENT_OPS)} select sub-elements; "
                             "they would be ignored"),
                    target_id=f.id))
                continue
            target = ir.find_feature(f.base_feature) if f.base_feature else None
            if target is None:
                issues.append(ValidationIssue(
                    code="base_feature", severity="error",
                    message=(f"feature '{f.id}' (op '{f.op}') references base_feature="
                             f"{f.base_feature!r}, which is not a feature in this "
                             "document"),
                    target_id=f.id))
            elif target.id == f.id:
                issues.append(ValidationIssue(
                    code="base_feature", severity="error",
                    message=f"feature '{f.id}' cannot take its edges from itself",
                    target_id=f.id))
            if not f.sub_elements:
                issues.append(ValidationIssue(
                    code="sub_elements_missing", severity="error",
                    message=(f"feature '{f.id}' (op '{f.op}') needs 'sub_elements' "
                             f"({sub_word} names from ir_digest), e.g. [{example}]"),
                    target_id=f.id))
        elif uses_subs:
            issues.append(ValidationIssue(
                code="sub_elements_missing", severity="error",
                message=(f"feature '{f.id}' (op '{f.op}') selects nothing: set "
                         "'base_feature' and 'sub_elements' "
                         f"({sub_word} names from ir_digest)"),
                target_id=f.id))

    # 5c) repeating ops must name the axis they repeat along/about. FreeCAD does
    # not error on a missing reference: a LinearPattern with no ``Direction``
    # yields ONE occurrence — a clean, valid, wrong solid — so this is an error
    # here (and again in the compiler, which is where the reference is built).
    for f in features:
        if f.op not in _PATTERN_AXIS_OPS:
            continue
        raw = (f.params or {}).get("axis")
        if raw is None:
            issues.append(ValidationIssue(
                code="pattern_axis_missing", severity="error",
                message=(f"feature '{f.id}' (op '{f.op}') has no params.axis; "
                         'set "axis": "X"/"Y"/"Z" (the body axis to repeat '
                         "along/about). FreeCAD produces a single occurrence "
                         "without it, with no error."),
                target_id=f.id))
        elif str(raw).strip().lower() not in _PATTERN_AXIS_VALUES:
            issues.append(ValidationIssue(
                code="pattern_axis_unknown", severity="error",
                message=(f"feature '{f.id}' (op '{f.op}') has axis={raw!r}; use one "
                         f"of {sorted(_PATTERN_AXIS_VALUES)} (the body origin axes). "
                         "H_Axis/V_Axis/N_Axis are the *profile sketch's* axes and "
                         "exist only on ops that have a profile."),
                target_id=f.id))

    # 5d) ops that read a plane from the typed field must be given one (mirrored:
    # MirrorPlane; draft: NeutralPlane). Both are LinkSubs, and FreeCAD does not
    # error when one is missing — it returns a NULL shape, and the body Tip then
    # silently falls back to the shape it already had. Measured on the real
    # kernel (§25 / §33 of the review report), which is why this is an error here
    # and not a warning.
    for f in features:
        uses_plane = f.op in _PLANE_OPS
        if f.plane is None:
            if uses_plane:
                prop = _PLANE_OPS[f.op]
                issues.append(ValidationIssue(
                    code="plane_missing", severity="error",
                    message=(f"feature '{f.id}' (op '{f.op}') has no plane; set "
                             '"plane": {"kind":"origin_plane","plane":"XY"} | '
                             '{"kind":"face","feature_id":…,"sub":"Face6"} | '
                             '{"kind":"datum_plane","feature_id":…}. '
                             f"{prop} is a reference, and without one FreeCAD "
                             "returns a null shape instead of an error."),
                    target_id=f.id))
            continue
        if not uses_plane:
            issues.append(ValidationIssue(
                code="plane_unused", severity="error",
                message=(f"feature '{f.id}' (op '{f.op}') has a plane, but only "
                         f"{sorted(_PLANE_OPS)} read one "
                         f"({', '.join(sorted(_PLANE_OPS.values()))}); it would be "
                         "ignored"),
                target_id=f.id))
            continue
        if f.plane.kind == "origin_plane":
            name = str(f.plane.plane or "").upper()
            if name not in _ORIGIN_PLANE_NAMES:
                issues.append(ValidationIssue(
                    code="plane_origin_unknown", severity="error",
                    message=(f"feature '{f.id}' uses origin plane "
                             f"{f.plane.plane!r}; the origin planes are "
                             f"{sorted(_ORIGIN_PLANE_NAMES)}"),
                    target_id=f.id))
        elif f.plane.kind == "face":
            fid = f.plane.feature_id
            target = ir.find_feature(fid) if fid else None
            if target is None:
                issues.append(ValidationIssue(
                    code="face_target", severity="error",
                    message=(f"feature '{f.id}' takes its plane from a face but "
                             f"feature_id={fid!r} is not a feature in this document"),
                    target_id=f.id))
            elif not (f.plane.sub or "").strip():
                issues.append(ValidationIssue(
                    code="face_sub_missing", severity="error",
                    message=(f"feature '{f.id}' takes its plane from a face of "
                             f"'{fid}' but has no sub-element name; call ir_digest "
                             'and pass e.g. "sub": "Face6"'),
                    target_id=f.id))
        elif f.plane.kind == "datum_plane":
            fid = f.plane.feature_id
            target = ir.find_feature(fid) if fid else None
            if target is None or target.op != "datum_plane":
                issues.append(ValidationIssue(
                    code="datum_plane_target", severity="error",
                    message=(f"feature '{f.id}' takes its plane from a datum plane "
                             f"but '{fid}' is not a datum_plane feature"),
                    target_id=f.id))

    # 5e) placement: only for the ops that carry their own position, and complete
    # (a rotation with no axis silently means "no rotation" in FreeCAD, and a
    # half-given placement is the kind of thing that builds the wrong solid
    # quietly — so it is refused here, before anything is persisted).
    for f in features:
        if f.placement is None:
            continue
        if f.op not in _PLACEMENT_OPS:
            issues.append(ValidationIssue(
                code="placement_unused", severity="error",
                message=(f"feature '{f.id}' (op '{f.op}') has a placement, but only "
                         f"{sorted(_PLACEMENT_OPS)} are positioned by one; every "
                         "other op takes its position from its sketch, its refs or "
                         "its plane reference"),
                target_id=f.id))
            continue
        axis = f.placement.axis
        if f.placement.angle and axis is None:
            issues.append(ValidationIssue(
                code="placement_axis_missing", severity="error",
                message=(f"feature '{f.id}' rotates angle={f.placement.angle} but "
                         "has no placement.axis; give the axis to rotate about, "
                         'e.g. {"position": {...}, "axis": {"x":0,"y":1,"z":0}, '
                         '"angle": 90}'),
                target_id=f.id))
        if axis is not None and not (axis.x or axis.y or axis.z):
            issues.append(ValidationIssue(
                code="placement_axis_zero", severity="error",
                message=(f"feature '{f.id}' has placement.axis (0,0,0); a zero axis "
                         "has no direction to rotate about"),
                target_id=f.id))

    # 6) params keys against per-op allow-list
    for f in features:
        if f.op in _UNVERIFIED_OPS:
            if f.params:
                issues.append(ValidationIssue(
                    code="op_unverified", severity="warn",
                    message=(f"op '{f.op}' has no verified property table; "
                             f"params are accepted without checking"),
                    target_id=f.id))
            continue
        allowed = _VERIFIED_OP_PARAMS.get(f.op)
        if allowed is None:
            # op known by enum but not in our tables — treat permissively.
            continue
        for key in f.params:
            if key in allowed:
                continue
            guidance = _guidance_for(f.op, key)
            if guidance:
                # A key we know the compiler cannot honour: reject it now, with
                # the alternative named, instead of letting it die inside FreeCAD
                # after the patch is persisted.
                issues.append(ValidationIssue(
                    code="param_unsupported", severity="error",
                    message=(f"feature '{f.id}' (op '{f.op}'): param '{key}' cannot be "
                             f"honoured — {guidance}"),
                    target_id=f.id))
                continue
            issues.append(ValidationIssue(
                code="param_unknown", severity="warn",
                message=(f"feature '{f.id}' (op '{f.op}') uses unverified "
                         f"param key '{key}'"),
                target_id=f.id))

    # 6b) capability tier. Every op below instantiates a FreeCAD object, so a
    # successful build is not evidence the feature works — say so per feature.
    for f in features:
        cap = capability(f.op)
        if cap and cap.tier == EXPERIMENTAL:
            issues.append(ValidationIssue(
                code="op_experimental", severity="warn",
                message=(f"op '{f.op}' is experimental: {cap.proof}"
                         + (f"; {cap.gap}" if cap.gap else "")),
                target_id=f.id))

    # 7) every numeric input must be finite (NaN/inf are not dimensions).
    issues.extend(_check_finite_numbers(sketches, features,
                                       ir.requirements.constraints))

    return issues


def _check_value_finite(
    v: Any, where: str, target_id: str | None, issues: list[ValidationIssue]
) -> None:
    """Recursively reject NaN/inf inside a (possibly nested) numeric value."""
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        if not _is_finite_number(v):
            issues.append(_finite_issue(where, target_id))
        return
    if isinstance(v, dict):
        for k, sub in v.items():
            _check_value_finite(sub, f"{where}.{k}", target_id, issues)
        return
    if isinstance(v, (list, tuple)):
        for i, sub in enumerate(v):
            _check_value_finite(sub, f"{where}[{i}]", target_id, issues)


def _check_finite_numbers(
    sketches: list[SketchSpec], features: list[FeatureSpec],
    req_constraints: list | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def vec(where: str, v, target_id: str | None) -> None:
        for axis in ("x", "y", "z"):
            if not _is_finite_number(getattr(v, axis, None)):
                issues.append(_finite_issue(f"{where}.{axis}", target_id))

    for s in sketches:
        if s.offset is not None:
            vec(f"sketch '{s.id}' offset", s.offset, s.id)
        for g in s.geometry:
            for i, p in enumerate(g.points or []):
                vec(f"sketch '{s.id}' geometry '{g.id}' point[{i}]", p, s.id)
            for fld in ("radius", "theta1", "theta2", "major_radius", "minor_radius", "rotation"):
                if not _is_finite_number(getattr(g, fld, None)):
                    issues.append(_finite_issue(
                        f"sketch '{s.id}' geometry '{g.id}' {fld}", s.id))
        for c in s.constraints:
            if not _is_finite_number(getattr(c, "value", None)):
                issues.append(_finite_issue(
                    f"sketch '{s.id}' constraint '{getattr(c, 'type', '?')}' value",
                    s.id))

    for f in features:
        for key, v in (f.params or {}).items():
            if not _is_finite_number(v):
                issues.append(_finite_issue(
                    f"feature '{f.id}' (op '{f.op}') param '{key}'", f.id))
        if f.placement is not None:
            vec(f"feature '{f.id}' placement.position", f.placement.position, f.id)
            if f.placement.axis is not None:
                vec(f"feature '{f.id}' placement.axis", f.placement.axis, f.id)
            if not _is_finite_number(f.placement.angle):
                issues.append(_finite_issue(
                    f"feature '{f.id}' placement.angle", f.id))

    # Requirement-contract expressions are inputs to the Gate, not geometry,
    # but a NaN expectation there poisons every downstream comparison the same
    # way — reject them at the same boundary as every other dimension.
    for c in req_constraints or []:
        kind = getattr(c, "kind", "?")
        target = getattr(c, "target", None)
        if not _is_finite_number(getattr(c, "tol", None)):
            issues.append(_finite_issue(
                f"requirement constraint '{kind}' tol", target))
        _check_value_finite(getattr(c, "value", None),
                            f"requirement constraint '{kind}' value",
                            target, issues)

    return issues


def _check_acyclic(
    features: list[FeatureSpec], feature_ids: set[str]
) -> list[ValidationIssue]:
    """Detect cycles in the feature refs DAG (edges feature -> ref)."""
    issues: list[ValidationIssue] = []
    adj: dict[str, list[str]] = {f.id: list(f.refs) for f in features}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {fid: WHITE for fid in feature_ids}
    # Edges may point at ids we already validated as "missing"; skip those.
    for fid in list(color.keys()):
        if fid not in adj:
            color[fid] = BLACK

    cycle_path: list[str] = []

    def dfs(node: str, stack: list[str]) -> bool:
        color[node] = GRAY
        stack.append(node)
        for nxt in adj.get(node, []):
            if nxt not in color:
                continue  # dangling ref already reported elsewhere
            if color[nxt] == GRAY:
                # found a back-edge -> cycle
                idx = stack.index(nxt)
                cycle_path.extend(stack[idx:] + [nxt])
                return True
            if color[nxt] == WHITE and dfs(nxt, stack):
                return True
        stack.pop()
        color[node] = BLACK
        return False

    for fid in feature_ids:
        if color[fid] == WHITE:
            if dfs(fid, []):
                break

    if cycle_path:
        issues.append(ValidationIssue(
            code="ref_cycle", severity="error",
            message=("feature refs contain a cycle: " + " -> ".join(cycle_path)),
            target_id=cycle_path[0] if cycle_path else None))
    return issues
