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

      POSITIONING A PROFILE — prefer `offset` over absolute dimensions:
        Model the profile around the sketch's OWN origin (bind one curve to the
        sketch origin, then dimension the opposite endpoints — the recipe below),
        then place it with "offset": {"x": ..., "y": ..., "z": ...}.
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
      op: pad | pocket | revolution | groove | hole | fillet | chamfer | draft | thickness
        | linear_pattern | circular_pattern | mirrored | datum_plane | additive_* | subtractive_*
      pad params:    {"length": <mm>, "type": "Length"|"UpToLast"|"UpToFirst"|"UpToFace"|"UpToShape"}
      pocket params: {"length": <mm>, "type": "Length"|"ThroughAll"|"UpToFirst"|"UpToFace",
                      "reversed": true|false}
                     (a through-slot is {"type": "ThroughAll"})

      POCKET DIRECTION — the single most common silent failure:
      a pocket cuts in the direction OPPOSITE its profile's normal. A profile on
      the XY plane has normal +Z, so the cut goes DOWN (-Z). If the material sits
      on the +Z side of that plane — which it does when you padded the same XY
      profile — the cut goes into empty space and the feature does nothing at all
      while still reporting success. Set "reversed": true to cut upward through
      the plate. Cheap check: if a pocket "succeeded" but the volume did not
      change, this is why.

      `refs` lists the feature ids this one depends on; it must stay acyclic.

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
                     Only confirmed=true requirements may block the Gate.
                     (`constraints_append` adds instead of replacing; `raw_text` sets
                     the requirement text.)
  rename             target_id = entity id; payload = {"name": "<new stable name>"}
                     Names must be unique — an ambiguous name cannot be referenced later.

base_version must equal the current IR version (optimistic concurrency); a stale
value is rejected rather than silently rebased."""


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
