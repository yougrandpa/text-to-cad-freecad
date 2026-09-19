"""IR tools — the ONLY way the model may mutate the design (design §2, §4.2).

read tier : ir_get, ir_digest, ir_list_features
write tier: ir_patch (mutate IR), ir_commit (compile + gate)

The model never touches FreeCAD here. ``ir_patch`` validates through the
injected store validator, then persists via ``store.apply_patch``. ``ir_commit``
triggers the compile pipeline and returns the :class:`GateReport` — but it does
**not** declare success: the engine alone decides SUCCEEDED from
``GateReport.passed`` (design §4.1).
"""

from __future__ import annotations

import functools
import json

from tcad.core.types import (
    GateReport,
    ImageRef,
    IrPatch,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
)
from tcad.ir.schema import IrDocument


def _ok(content: str, **kw: object) -> ToolResult:
    return ToolResult(ok=True, content=content, **kw)  # type: ignore[arg-type]


def _err(
    kind: ToolErrorKind, message: str, feature_id: str | None = None, hint: str = ""
) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolError(kind=kind, message=message, feature_id=feature_id, hint=hint)
    )


# ─── handlers (services injected by closure) ───────────────────────────────


async def ir_get_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    ir = services.store.load(ctx.model_id)
    return _ok(ir.model_dump_json(indent=2))


async def ir_digest_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    version = services.store.current_version(ctx.model_id)
    digest = services.context.digest(ctx.model_id, version)
    text = getattr(digest, "text", "") or digest.model_dump_json()
    return _ok(text)


async def ir_list_features_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    ir = services.store.load(ctx.model_id)
    feats = [
        {"id": f.id, "name": f.name, "op": f.op, "params": f.params, "refs": f.refs}
        for b in ir.bodies
        for f in b.features
    ]
    return _ok(json.dumps(feats, indent=2))


async def ir_patch_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    """Mutate the IR: validate -> apply -> summarise.

    On rejection the error carries ``feature_id`` where attributable so the model
    knows what to change.
    """
    # ``base_version: "current"`` resolves to the latest version at the moment
    # the patch is applied.
    #
    # This is in the tool contract, not a test convenience, because it is the
    # natural thing to write — and rejecting it produces a pydantic message that
    # says nothing useful ("1 validation error for IrPatch / base_version"),
    # which is exactly the kind of dead end the model cannot recover from.
    # An explicit integer still works and remains the right choice for
    # multi-turn editing: it is the only form that can notice "the model moved
    # under me" and refuse instead of silently overwriting.
    if args.get("base_version") == "current":
        try:
            args = {**args, "base_version": services.store.current_version(ctx.model_id)}
        except Exception as e:  # noqa: BLE001 — e.g. the model does not exist yet
            return _err(
                ToolErrorKind.SEMANTIC,
                f"cannot resolve base_version='current': {e}",
                hint="该模型尚未创建，或 ir_get 之前先调用 POST /models。",
            )

    try:
        patch = IrPatch.model_validate(args)
    except Exception as e:  # malformed tool args
        return _err(ToolErrorKind.SCHEMA, f"invalid patch payload: {e}")

    ir = services.store.load(ctx.model_id)
    errors = services.store.validate_patch(ir, patch)
    if errors:
        e0 = errors[0]
        return _err(e0.kind, e0.message, feature_id=e0.feature_id, hint=e0.hint)

    try:
        new_doc, _event = services.store.apply_patch(ctx.model_id, patch)
    except Exception as e:  # optimistic-concurrency / IO failure
        return _err(ToolErrorKind.SEMANTIC, f"patch rejected: {e}")

    summary = patch.summary or f"applied {len(patch.ops)} op(s)"
    content = (
        f"Patch applied. New IR version: {new_doc.version}. {summary}. "
        f"Bodies: {len(new_doc.bodies)}, features: {len(new_doc.all_features())}."
    )
    return _ok(content, patch_applied=patch)


async def ir_commit_handler(services: "Any", args: dict, ctx: ToolContext) -> "Any":
    """Trigger the commit pipeline (compile -> export -> gate).

    Returns a :class:`ToolOutcome` carrying the GateReport. The engine reads
    ``GateReport.passed`` to decide success — this handler never does.
    """
    from tcad.loop.commit import ToolOutcome, run_commit  # local import keeps layers clean

    message = args.get("message", "") if isinstance(args, dict) else ""
    version = services.store.current_version(ctx.model_id)
    result, report = await run_commit(
        services, ctx.model_id, version, message, ctx.workdir, ctx.data_dir
    )
    return ToolOutcome(result=result, gate_report=report)


_IR_PATCH_DESCRIPTION = """\
Mutate the IR. Returns the new IR version and a summary.

ops is a list of {"op": <name>, "target_id"?: <id>, "payload": {...}, "reason": <str>}.
`reason` is mandatory (it lands in the audit log and is what makes multi-turn
"move that hole" requests resolvable later).

op names and their payload shapes:

  add_sketch   payload is a sketch:
      {"id": "sk_base", "name": "base_outline",
       "plane": {"kind": "origin_plane", "plane": "XY"|"XZ"|"YZ"}
              | {"kind": "face", "feature_id": "<feature id>", "sub": "Face5"}
              | {"kind": "datum_plane", "feature_id": "<datum feature id>"},
       "geometry": [
          {"id": "g0", "kind": "line",   "points": [{"x":0,"y":0,"z":0},{"x":60,"y":0,"z":0}]},
          {"id": "g1", "kind": "circle", "points": [{"x":30,"y":20,"z":0}], "radius": 5.0},
          {"id": "g2", "kind": "arc",    "points": [{"x":0,"y":0,"z":0}], "radius": 10.0,
                                         "theta1": 0.0, "theta2": 3.14}
       ],
       "constraints": [{"type": "Coincident", "refs": [0,1,-1,1]}, ...],
       "require_fully_constrained": true}

      `id`/`name` may be omitted and will be minted for you. `body_id` targets a
      specific body; otherwise the first body is used (a fresh one is created when
      the document has none).

      COORDINATES ARE WORLD COORDINATES, AND THEY MUST LIE IN THE SKETCH'S PLANE.
        A point is placed by the plane the sketch is attached to; the component
        along that plane's normal is ignored. So:
          XY plane -> profile points use x and y (leave z = 0)
          XZ plane -> profile points use x and z (leave y = 0)
          YZ plane -> profile points use y and z (leave x = 0)
        A side profile — a wedge, a bracket's upright, a phone stand's incline —
        belongs on YZ or XZ and is written with real coordinates, e.g.
        {"x":0,"y":0,"z":0} -> {"x":0,"y":90,"z":0} -> {"x":0,"y":90,"z":60}.
        Do NOT write local 2-D (u, v) values into x and y for a non-XY plane: on
        YZ that puts every vertex at x=0, the profile collapses onto a line, and
        the compile fails with "does not form a closed wire".

      EXTRUSION DIRECTION follows the plane's normal, and a `pad` extrudes along
      it:
          XY -> +Z        XZ -> -Y        YZ -> +X
        Set "reversed": true, or "midplane": true, in the pad's params to extrude
        the other way / symmetrically.

      POSITIONING A PROFILE — prefer `offset` over absolute dimensions:
        Model the profile around the sketch's OWN origin (bind one curve to the
        sketch origin, then dimension the opposite endpoints — the recipe below),
        then place it with "offset": {"x": ..., "y": ..., "z": ...} (applied in
        world axes, after the sketch is attached).
        Dimensioning several points in absolute coordinates instead tends to
        over-determine the sketch: mixing an absolute dimension on a line's start
        point with a horizontal/vertical constraint and a dimension on its end
        point is a solver conflict, reported as "the sketch contains conflicting
        constraints".

      Constraint refs are positional, exactly as Sketcher.Constraint(type, *refs):
        {"type":"Coincident","refs":[0,2,1,1]}   line0 end  == line1 start
        {"type":"Coincident","refs":[0,1,-1,1]}  line0 start == the sketch origin
        {"type":"Horizontal","refs":[0]} / {"type":"Vertical","refs":[1]}
        {"type":"DistanceX","refs":[0,2],"value":60.0}   absolute X of line0's end point
        {"type":"DistanceY","refs":[1,2],"value":40.0}   absolute Y of line1's end point
        {"type":"Radius","refs":[2],"value":5.0}
      ORDER MATTERS: bind a curve to the origin FIRST (refs [i, pointIdx, -1, 1]), then
      dimension its FREE endpoints. Dimensioning a point already tied to the origin is a
      solver conflict, and FreeCAD reports that with a misleading "Invalid constraint index".

  add_feature  payload is a feature:
      {"id": "ft_pad", "name": "base_pad", "op": "pad", "profile_sketch": "sk_base",
       "params": {"length": 10.0, "type": "Length"}}
      op 必须精确，不存在通配写法：
        pad | pocket | revolution | groove | hole | fillet | chamfer | draft
        | thickness | mirrored | linear_pattern | circular_pattern | polar_pattern
        | multi_transform | datum_plane
        | additive_box | additive_cylinder | additive_sphere
        | subtractive_box | subtractive_cylinder | subtractive_sphere

      pad params:    {"length": <mm>, "type": "Length"|"UpToLast"|"UpToFirst"|"UpToFace"}
      pocket params: {"length": <mm>, "type": "Length"|"ThroughAll"|"UpToFirst"|"UpToFace",
                      "reversed": true|false}
                     (a through-slot is {"type": "ThroughAll"})
      additive_*/subtractive_* are PRIMITIVES: no sketch needed, they carry their own
      size. Pass lowercase keys matching the object's properties, e.g.
      {"length": 30, "width": 10, "height": 10}. An unknown key comes back as
      "unsupported property" — read the object with ir_get rather than guessing.

      ── growing material onto an existing solid (an arm, a boss, a rib) ──
      Features in one body DO merge into a single solid — but only where they
      actually overlap in space. Two lumps that do not touch produce a Compound,
      and the Gate's solid_count check then fails. That is the number one reason
      "add an arm" fails, and it is a POSITION problem, not a feature-type one.

      POCKET DIRECTION — the single most common silent failure:
      a pocket cuts in the direction OPPOSITE its profile's normal. A profile on
      the XY plane has normal +Z, so the cut goes DOWN (-Z). If the material sits
      on the +Z side of that plane — which it does when you padded the same XY
      profile — the cut goes into empty space and the feature does nothing at all
      while still reporting success. Set "reversed": true to cut upward through
      the plate. Cheap check: if a pocket "succeeded" but the volume did not
      change, this is why.

      `refs` declares build ORDER only — it creates no geometric relationship.
      Pointing it at the torso will NOT make the arm grow out of the torso. To
      attach a feature to an existing solid, do one of:
        (a) draw its sketch ON one of that solid's faces:
            "plane": {"kind":"face","feature_id":"ft_body","sub":"Face6"}
            (look the face number up with ir_get / ir_digest — do not guess it)
        (b) keep the sketch on an origin plane and use offset / coordinates to
            place it inside the torso's extent, overlapping it
      (a) is usually less work: face attachment inherits that face's coordinate
      system, so there is no mapping to work out by hand.

      `refs` must stay acyclic.

  update_sketch      target_id = sketch id; payload = partial sketch fields.
                     `geometry`/`constraints` REPLACE the whole list; use
                     `geometry_append`/`constraints_append` to add instead.
  update_feature     target_id = feature id; payload = partial feature fields.
                     `params` merges field-wise; `refs` replaces (`refs_append` adds).
  remove_feature     target_id = feature id; refused if another feature references it,
                     and the error names the dependents. Pass "cascade": true only to
                     deliberately rewire dependents.
  update_requirement payload = {"constraints": [{"kind": "bbox"|"volume"|"count"|
                     "hole_diameter"|"hole_position"|"symmetric"|"wall_thickness"|
                     "feature_count", "value": ..., "tol": 0.05,
                     "source_text": "<verbatim user words>", "confirmed": true}]}
                     ⚠ THIS IS THE ONLY THING THE GATE JUDGES AGAINST. Every number
                     the user gives you — a size, a thickness, a hole diameter, "two
                     of these" — should be recorded here as a confirmed constraint at
                     the same time you build the geometry that satisfies it. With none
                     recorded, a green Gate means only "the geometry is
                     self-consistent"; it does NOT mean the part is what was asked
                     for, and ir_commit will tell you so.
                     Only confirmed=true requirements may block the Gate.
                     (`constraints_append` adds instead of replacing; `raw_text` sets
                     the requirement text.)
  rename             target_id = entity id; payload = {"name": "<new stable name>"}
                     Names must be unique — an ambiguous name cannot be referenced later.

base_version: pass the integer IR version you last read, or the string "current" to
mean "whatever is latest when this patch is applied".
  - "current" is the convenient default and saves an ir_get round trip.
  - an explicit integer is the safe form for multi-turn editing: it is the only
    one that can detect that the model moved under you, and it is refused rather
    than silently rebased. A stale integer is rejected, never rebased."""


def _ir_patch_schema() -> dict:
    """Model-facing schema for ir_patch.

    ``IrPatch.model_json_schema()`` alone is misleading: ``ops`` has a default, so
    pydantic does not mark it required and a model may reasonably call
    ``ir_patch(base_version=0)`` with no operations at all. The generator also
    emits ``payload`` as an untyped object, which for the one tool that does the
    actual modelling is a black box — the model cannot guess what an add_sketch
    payload should contain. So we take the generated schema and pin down the two
    things the model actually needs: `ops` is required, and `payload` is described
    in the tool description above.
    """
    schema = IrPatch.model_json_schema()
    schema["required"] = ["base_version", "ops"]
    op_def = schema.get("$defs", {}).get("IrPatchOp", {})
    if isinstance(op_def, dict):
        op_def["required"] = ["op", "payload", "reason"]
        props = op_def.setdefault("properties", {})
        if "payload" in props:
            props["payload"]["description"] = (
                "Op-specific. See the tool description for the exact shape of each op."
            )
        if "reason" in props:
            props["reason"]["description"] = (
                "Why this change is being made. Mandatory; it is recorded in the audit "
                "log and is how earlier intents stay referable in later turns."
            )
    return schema


# ─── spec factory ──────────────────────────────────────────────────────────


def build_ir_tools(services: "Any") -> dict[str, ToolSpec]:
    return {
        "ir_get": ToolSpec(
            name="ir_get",
            tier=ToolTier.READ,
            description="Return the full current IR document as JSON. Use when you need the complete, exact current state.",
            params_schema={"type": "object", "properties": {}},
            handler=functools.partial(ir_get_handler, services),
            concurrency_safe=True,
        ),
        "ir_digest": ToolSpec(
            name="ir_digest",
            tier=ToolTier.READ,
            description="Return a compact program-generated geometry digest (feature chain, topology, bbox, volume, key dimensions). Cheaper than ir_get.",
            params_schema={"type": "object", "properties": {}},
            handler=functools.partial(ir_digest_handler, services),
            concurrency_safe=True,
        ),
        "ir_list_features": ToolSpec(
            name="ir_list_features",
            tier=ToolTier.READ,
            description="List every feature as {id, name, op, params, refs}. Use to discover stable ids before referencing them in a patch.",
            params_schema={"type": "object", "properties": {}},
            handler=functools.partial(ir_list_features_handler, services),
            concurrency_safe=True,
        ),
        "ir_patch": ToolSpec(
            name="ir_patch",
            tier=ToolTier.WRITE,
            description=_IR_PATCH_DESCRIPTION,
            params_schema=_ir_patch_schema(),
            handler=functools.partial(ir_patch_handler, services),
        ),
        "ir_commit": ToolSpec(
            name="ir_commit",
            tier=ToolTier.WRITE,
            description=(
                "Compile the current IR and run the Gate. Returns the GateReport. "
                "You may ONLY consider the build done when the report says passed=true. "
                "If it fails, repair the named feature(s) and call ir_patch + ir_commit again."
            ),
            params_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            handler=functools.partial(ir_commit_handler, services),
        ),
    }
