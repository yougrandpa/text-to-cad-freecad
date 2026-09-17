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

from pydantic import BaseModel

from tcad.ir.schema import (
    FeatureSpec,
    IrDocument,
    SketchSpec,
)

# ── param allow-lists (IR-level snake_case keys) ──────────────────────────────
# Verified against 附录 A-1 (Pad/Pocket/Revolution/Groove/Hole/Fillet/Chamfer/
# Mirrored/LinearPattern/CircularPattern property tables) and 附录 B-3.
_VERIFIED_OP_PARAMS: dict[str, frozenset[str]] = {
    "pad": frozenset({
        "profile", "midplane", "reversed", "up_to_face", "up_to_shape",
        "length", "length2", "start_offset", "offset", "offset2",
        "taper_angle", "taper_angle2", "direction", "use_custom_vector",
        "along_sketch_normal", "start_reference", "reference_axis",
        "type", "type2", "start_type", "side_type", "depth_type",
    }),
    "pocket": frozenset({
        "profile", "midplane", "reversed", "up_to_face", "up_to_shape",
        "length", "length2", "start_offset", "offset", "offset2",
        "taper_angle", "taper_angle2", "direction", "use_custom_vector",
        "along_sketch_normal", "start_reference", "reference_axis",
        "type", "type2", "start_type", "side_type", "depth_type",
    }),
    "revolution": frozenset({
        "profile", "base", "axis", "reference_axis", "angle", "angle2",
        "type", "type2", "side_type", "midplane", "reversed", "up_to_face",
        "up_to_shape", "start_type", "start_offset", "start_reference",
        "allow_multi_face", "fuse_order", "operation",
    }),
    "groove": frozenset({
        "profile", "base", "axis", "reference_axis", "angle", "angle2",
        "type", "type2", "side_type", "midplane", "reversed", "up_to_face",
        "up_to_shape", "start_type", "start_offset", "start_reference",
        "allow_multi_face", "fuse_order", "operation",
    }),
    "hole": frozenset({
        "profile", "depth", "depth_type", "diameter", "drill_point",
        "drill_point_angle", "midplane", "reversed", "tapered",
        "tapered_angle", "base_profile_type", "up_to_face", "up_to_shape",
        "start_type", "start_offset", "start_reference", "threaded",
        "thread_type", "thread_size", "thread_class", "thread_direction",
        "thread_depth", "thread_depth_type", "thread_fit", "model_thread",
        "cosmetic_thread", "hole_cut_type", "hole_cut_diameter",
        "hole_cut_depth", "hole_cut_countersink_angle",
        "use_custom_thread_clearance", "custom_thread_clearance",
        "drill_for_depth", "allow_multi_face", "operation", "add_sub_shape",
    }),
    "fillet": frozenset({
        "base", "radius", "use_all_edges", "support_transform", "operation",
        "add_sub_shape",
    }),
    "chamfer": frozenset({
        "base", "size", "size2", "angle", "chamfer_type", "flip_direction",
        "use_all_edges", "support_transform", "operation",
    }),
    "mirrored": frozenset({
        "originals", "mirror_plane", "transform_mode",
    }),
    "linear_pattern": frozenset({
        "originals", "direction", "direction2", "mode", "mode2", "length",
        "length2", "occurrences", "occurrences2", "spacings", "spacings2",
        "spacing_pattern", "spacing_pattern2", "reversed", "reversed2",
        "suppressed_positions", "transform_mode",
    }),
    "circular_pattern": frozenset({
        "originals", "axis", "number_circles", "radial_distance",
        "tangential_distance", "symmetry", "transform_mode",
    }),
}

# Ops whose FreeCAD property tables were NOT verified at writing time.
# Treated as permissive (any params allowed) + one `op_unverified` warn.
_UNVERIFIED_OPS: frozenset[str] = frozenset({
    "draft", "thickness", "multi_transform", "polar_pattern", "datum_plane",
    "additive_box", "additive_cylinder", "additive_sphere",
    "subtractive_box", "subtractive_cylinder", "subtractive_sphere",
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

    # 1) Unique ids within the document (sketch<->feature clash included).
    seen: set[str] = set()
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

    # 2) feature.profile_sketch resolves to an existing SketchSpec.id
    for f in features:
        if f.profile_sketch is not None and f.profile_sketch not in sketch_ids:
            issues.append(ValidationIssue(
                code="profile_sketch_missing", severity="error",
                message=(f"feature '{f.id}' references non-existent profile "
                         f"sketch '{f.profile_sketch}'"),
                target_id=f.id))

    # 3) feature.refs[] resolve to existing feature ids
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
            if key not in allowed:
                issues.append(ValidationIssue(
                    code="param_unknown", severity="warn",
                    message=(f"feature '{f.id}' (op '{f.op}') uses unverified "
                             f"param key '{key}'"),
                    target_id=f.id))

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
