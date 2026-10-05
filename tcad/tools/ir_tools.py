"""IR tools — the ONLY way the model may mutate the design (design §2, §4.2).

read tier : ir_get, ir_digest, ir_list_features
write tier: ir_patch (mutate IR), ir_commit (compile + gate)

The model never touches FreeCAD here. ``ir_patch`` validates through the
injected store validator, then persists via ``store.apply_patch``. ``ir_commit``
triggers the compile pipeline and returns the :class:`GateReport` — but it does
**not** declare success: the engine requires a current passed Gate and, in
production, a validated design_review before accepting recorded constraints.
"""

from __future__ import annotations

import functools
import json

from tcad.core.types import (
    IrPatch,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
)
from tcad.ir import capability


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
    ids = args.get("ids")
    if ids:
        objects = {obj.id: obj for body in ir.bodies for obj in (body, *body.sketches, *body.features)}
        missing = [id for id in ids if id not in objects]
        if missing:
            return _err(ToolErrorKind.NOT_FOUND, f"unknown IR IDs: {missing}")
        data = {"version": ir.version, "objects": [objects[id].model_dump(mode="json", exclude_defaults=not args.get("include_defaults", False)) for id in ids]}
    else:
        data = ir.model_dump(mode="json", exclude_defaults=not args.get("include_defaults", False))
    return _ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


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
    return _ok(json.dumps(feats, ensure_ascii=False, separators=(",", ":")))


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

    if ctx.request_text is not None:
        for op in patch.ops:
            if (op.op == "update_requirement" and "raw_text" in op.payload
                    and op.payload["raw_text"] != ctx.request_text):
                return _err(ToolErrorKind.SEMANTIC, "raw_text is the preserved user request; do not rewrite it",
                            hint="Add measured constraints with constraints_append; leave raw_text unchanged.")
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


async def ir_gear_profile_handler(services, args, ctx):
    from tcad.ir.gears import gear_sketch
    try:
        payload = gear_sketch(args)
    except (ValueError, KeyError, TypeError) as exc:
        return _err(ToolErrorKind.SCHEMA, str(exc))
    return await ir_patch_handler(services, {
        "base_version": args.get("base_version", "current"),
        "ops": [{"op": "add_sketch", "payload": payload, "reason": args["reason"]}],
        "summary": "Generated sampled involute profile; radial root transitions, not manufacturing-verified",
    }, ctx)


async def assembly_configure_handler(services, args, ctx):
    return await ir_patch_handler(services, {
        "base_version": args.get("base_version", "current"),
        "ops": [{"op": "set_assembly", "payload": {"assembly": args["assembly"]}, "reason": args["reason"]}],
        "summary": "Configured native FreeCAD Assembly joints and drivers",
    }, ctx)


async def ir_commit_handler(services: "Any", args: dict, ctx: ToolContext) -> "Any":
    """Trigger the commit pipeline (compile -> export -> gate).

    Returns a :class:`ToolOutcome` carrying the GateReport. The engine reads
    ``GateReport.passed`` to decide success — this handler never does.
    """
    from tcad.loop.commit import run_commit  # local import keeps layers clean
    from tcad.tools.base import ToolOutcome

    message = args.get("message", "") if isinstance(args, dict) else ""
    version = services.store.current_version(ctx.model_id)
    result, report = await run_commit(
        services, ctx.model_id, version, message, ctx.workdir, ctx.data_dir,
        hooks=getattr(ctx, "hooks", None),
    )
    return ToolOutcome(result=result, gate_report=report)


_IR_PATCH_DESCRIPTION = """\
Mutate the IR. Returns the new IR version and a summary.

ops is a list of {"op": <name>, "target_id"?: <id>, "payload": {...}, "reason": <str>}.
`reason` is mandatory (it lands in the audit log and is what makes multi-turn
"move that hole" requests resolvable later).

op names and their payload shapes:

  add_body     payload = {"id": "crank", "name": "crank_assembly"}.
               Creates an EMPTY independent solid body. Add sketches/features to it
               with body_id. Use separate bodies for housing, shafts, gears, grip
               and cutter; disconnected moving parts must not be fused into one body.
               Each body must finish as exactly one valid solid before ir_commit.
               For a toothed wheel, draw ONE closed profile sketch and pad it;
               dozens of additive tooth primitives repeatedly fuse the BRep and
               can time out. A polygon/toothed outline is only an approximate
               wheel unless its tooth form and contact are independently verified.
  add_body / update_body may use part_ref={model_id, artifact_id, body_id, placement?}
               to reuse a verified immutable component. No local sketches/features
               on a PartRef body; commit to resolve and assemble it.
  update_body  target_id = body id; payload may set name and/or motion:
               {"motion": {"pivot": {"x":30,"y":0,"z":40},
                           "axis": {"x":0,"y":1,"z":0}, "ratio": 1.0}}.
               The viewport rotates this body about the world pivot/axis by
               input crank angle * ratio. Housing has motion=null. Bodies on the
               same shaft share pivot/axis/ratio. An external gear pair uses
               ratio = -driver_teeth / driven_teeth. Geometry is the zero-angle
               pose. This is prescribed rigid kinematics only: it does NOT prove
               tooth contact, collision clearance, torque or material removal.
               Do not call star polygons verified gears or claim cutting simulation.

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

      POSITIONING A PROFILE — the coordinates ARE the position:
        Write each point where you want it in world coordinates. Do NOT reach for
        a sketch `offset`: it is refused, because it does not place the profile
        and — together with a curve bound to the sketch origin — actively
        deforms it. Measured on the kernel: a 40x20 rectangle padded 5 gives
        volume 4000 at offset (0,0,0), 2500 at (10,10,0) and 4050 with a sheared
        bbox at (5,-3,0). Same profile, same bbox, different solid.
        To anchor a profile that does not start at the origin, dimension its
        points ABSOLUTELY — one DistanceX and one DistanceY per edge, referencing
        that edge's own points — and leave the origin binding out. What DOES
        over-determine a sketch is dimensioning both endpoints of the same line
        in absolute coordinates *while also* binding one of them to the origin:
        those two say different things, and the solver satisfies both by moving
        the geometry. Pick one anchor per edge, not both.

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
      Coordinates and constraints are the SAME thing to the solver — it satisfies the
      constraints and moves the geometry to do it. So a bind that contradicts the
      coordinates you wrote (profile at (10,10)…(30,30) plus a Coincident to the
      origin) does NOT fail: the solver drags the profile over to the origin, the
      build passes, and the cut lands somewhere you did not write. Bind the origin
      only when the profile really starts there; a profile that lives elsewhere
      must be anchored with absolute dimensions instead (and never with a sketch
      offset — that is refused). Whenever a feature's measured result is not what
      you intended, check this before anything else.

  add_feature  payload is a feature:
      {"id": "ft_pad", "name": "base_pad", "op": "pad", "profile_sketch": "sk_base",
       "params": {"length": 10.0, "type": "Length"}}
{OP_CAPABILITY}

      pad params:    {"length": <mm>, "type": "Length"|"UpToLast"|"UpToFirst"|"UpToFace"}
      pocket params: {"length": <mm>, "type": "Length"|"ThroughAll"|"UpToFirst"|"UpToFace",
                      "reversed": true|false}
                     (a through-slot is {"type": "ThroughAll"})
      revolution params: {"angle": <deg, default 360>, "type": "Angle",
                          "axis": "V_Axis"|"H_Axis"|"N_Axis"|"X"|"Y"|"Z"}
                     A Revolve needs an AXIS, and it must lie in the profile's plane:
                       V_Axis / H_Axis / N_Axis -> the PROFILE SKETCH's own vertical /
                         horizontal / normal axis (default V_Axis). This follows the
                         sketch, so it stays correct when the sketch is offset.
                       X / Y / Z -> that axis of the BODY's origin. Use this when the
                         profile is drawn away from the sketch origin and the revolve
                         axis must stay on the global axis.
                     RECIPE (measured): draw the closed profile at its real coordinates
                     and close the loop with Coincident constraints; put one edge ON the
                     axis you revolve about. Adding DistanceX/DistanceY on top of an
                     already-closed chain can silently MOVE a vertex — a shaft that
                     should be 2720*pi came out at 38453 mm^3 with no error — so size a
                     revolved profile by its coordinates, then check ir_digest.
                     Example (stepped shaft about Z, profile on XZ):
                       points (0,0) (10,0) (10,20) (6,20) (6,40) (0,40), edge (0,40)-(0,0)
                       lies on the axis; revolution with axis "V_Axis" -> V = 2720*pi.
      mirrored / linear_pattern / polar_pattern — the plane and the axis are
      REFERENCES, and the kernel's failure mode when one is missing is SILENT:
        mirrored:       "plane": {"kind":"origin_plane","plane":"XY"|"XZ"|"YZ"}
                        or {"kind":"face","feature_id":"ft_plate","sub":"Face6"}.
                        With no plane FreeCAD returns a NULL shape.
        linear_pattern: params {"axis":"X"|"Y"|"Z", "mode":"Extent"|"Spacing",
                        "length" (Extent) | "offset" (Spacing) in mm,
                        "occurrences": <n>}. Extent spreads the copies across the
                        length; Spacing steps by offset. With no axis FreeCAD
                        returns ONE occurrence and reports no error.
        polar_pattern:  params {"axis":"X"|"Y"|"Z", "angle": <deg>,
                        "occurrences": <n>}. Spacing is angle/(occurrences-1), so
                        angle 270 with 4 copies lands at 0/90/180/270 degrees.
                     X / Y / Z are the BODY's origin axes. H_Axis / V_Axis /
                     N_Axis are the PROFILE sketch's axes (revolution/groove only)
                     and are refused here: a pattern repeats features, not a
                     profile. `refs` names the feature(s) to repeat, and they must
                     already exist — a pattern of nothing is refused.
      draft / thickness — the FACES are references, carried in the same two fields
      a fillet uses for edges ("base_feature" + "sub_elements" = the face names
      ir_digest publishes), and draft takes its neutral plane from the same typed
      "plane" field a mirror uses:
        draft:      the named faces are tapered about the neutral plane; params
                    {"angle": <deg>, "reversed": true|false}. With no "plane" the
                    kernel returns a NULL shape; a face PARALLEL to the neutral
                    plane has no intersection line to rotate about and fails —
                    both are refused by name and tell you the fix. Measured: the
                    four side faces of a 40x40x20 box drafted 5 deg off the XY
                    plane → 29282.0083 mm^3 (reversed grows to 34881.2827).
                    Prefer the XY/XZ/YZ plane that the part stands on.
        thickness:  the named face is OPENED (it stays where it is; every other
                    face is offset inwards by the wall); params {"value": <mm
                    wall thickness>, "reversed": true|false}. It reads no plane.
                    Measured: opening the top face of the same box with value=2
                    leaves 8672 mm^3 — a cup, not a shell around the outside.
      additive_*/subtractive_* are PRIMITIVES: no sketch needed, they carry their own
      size. Pass lowercase keys matching the object's properties, e.g.
      {"length": 30, "width": 10, "height": 10}. An unknown key comes back as
      "unsupported property" — read the object with ir_get rather than guessing.
      Cone primitives (additive_cone/subtractive_cone): params
      {"radius1": <base mm>, "radius2": <tip mm, may be 0>, "height": <mm>}.
      Local +Z is the cone axis; use placement to rotate and position it.

      WHERE a primitive goes is the typed "placement" field, in WORLD mm:
        "placement": {"position": {"x": 20, "y": 30, "z": 0}}
        "placement": {"position": {"x": 40, "y": 25, "z": 8},
                      "axis": {"x": 0, "y": 1, "z": 0}, "angle": 90}
      A primitive with no placement is built AT THE ORIGIN, and a part that does
      not contain the origin gets a lump floating beside it — a Compound, which
      the Gate rejects. `position` is the feature's own origin: a box grows into
      +X/+Y/+Z from there, a cylinder/sphere is centred on it. `axis` + `angle`
      (degrees, counter-clockwise about `axis` through `position`) turn it; a
      cylinder defaults to its axis along +Z, so axis Y with angle 90 lays it
      along +X. Measured on the real kernel: cylinder r6 h20 at (10,10,0) on an
      80x50x8 plate merges into ONE solid of 32000 + 432*pi mm^3; a subtractive
      box 10x10x4 at (40,20,4) removes exactly 400. Only the primitives take a
      placement — every other op is positioned by its sketch, its `refs` or its
      `plane`, and offering one there is refused. To move a primitive that is
      already there ("move that pin to the other corner"), send the new
      `placement` in an `update_feature` on it; a null placement sends it back
      to the origin. Do not rebuild the part to move one feature.

      ── growing material onto an existing solid (an arm, a boss, a rib) ──
      Features in one body DO merge into a single solid — but only where they
      actually overlap in space. Two lumps that do not touch produce a Compound,
      and the Gate's solid_count check then fails. That is the number one reason
      "add an arm" fails, and it is a POSITION problem, not a feature-type one.

      POCKET DIRECTION — the single most common silent failure:
      a pocket cuts in the direction OPPOSITE its profile's normal. A profile on
      the XY plane has normal +Z, so the cut goes DOWN (-Z). If the material sits
      on the +Z side of that plane — which it does when you padded the same XY
      profile — the cut goes into empty space. Set "reversed": true INSIDE the
      pocket's `params` — i.e. {"op": "pocket", "profile_sketch": "sk",
      "params": {"length": 8, "reversed": true}} — to cut upward through the
      plate. `reversed` is a `params` key on every profile op; writing it at the
      top level of the payload is refused by name rather than ignored, because a
      silently dropped direction is a cut that goes into air. The compiler also
      refuses a cut that leaves the solid unchanged and names the feature ("did
      not change the solid"), so a pocket that misses is an error rather than a
      quiet no-op — but measure after any cut anyway (ir_digest), because "the
      volume dropped" is not the same as "the volume dropped by what I asked
      for".

      `refs` declares build ORDER only — it creates no geometric relationship.
      Pointing it at the torso will NOT make the arm grow out of the torso. To
      attach a feature to an existing solid, do one of:
        (a) draw its sketch ON one of that solid's faces:
            "plane": {"kind":"face","feature_id":"ft_body","sub":"Face6"}
            `ir_digest` LISTS the planar faces with their name, area, outward
            normal and centre — pick by intent ("the +Z face of area 4000") rather
            than guessing a number, which shifts when the model changes. The name
            is FreeCAD's 1-based `Face<N>`.
            Face-attached geometry still uses WORLD x/y/z coordinates on that
            face. The compiler maps these into the face's local frame; do not
            supply local (u,v) in x/y. A Y-normal face at y=15 needs profile points
            {"x":..,"y":15,"z":..}, not {"x":u,"y":v,"z":0}.
        (b) keep the sketch on an origin plane and write the profile's WORLD
            coordinates so it sits inside the torso's extent, overlapping it.
            Use coordinates for this, NOT `offset`: sketch coordinates are world
            coordinates and `offset` is part of the same placement, so a
            non-zero offset CANCELLED OUT against them and moved nothing
            (measured on the real kernel — a 40x20 XY rectangle with
            offset=(10,10,0) pads to a body still at the origin). A non-zero
            offset now draws a warning saying exactly that.
      Face attachment inherits the face normal and support. Write the profile
      on its measured world plane; do not guess the face's local axes.

      `refs` must stay acyclic.

  update_sketch      target_id = sketch id; payload = partial sketch fields.
                     `geometry`/`constraints` REPLACE the whole list; use
                     `geometry_append`/`constraints_append` to add instead.
                     These are ARRAYS of objects, and the values are TYPED:
                     `reversed`/`require_fully_constrained` are booleans, `offset`
                     is an object, `plane` is an object. A string where a list or
                     a boolean belongs is rejected with the field named — it is
                     not coerced ("false" would otherwise be a truthy string and
                     silently reverse the sketch).
  update_feature     target_id = feature id; payload = partial feature fields.
                     `params` merges field-wise; `refs` replaces (`refs_append` adds).
                     `refs` is an ARRAY OF ID STRINGS — passing the bare id
                     ("refs": "ft_pad") is rejected rather than read as a list of
                     characters.
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
                     (`constraints_append` adds instead of replacing.) In review-enabled
                     mode the engine preserves raw_text as the user's original request;
                     leave it unchanged. Guessed or derived dimensions are not user-confirmed.
                     Use source_text verbatim.
  rename             target_id = entity id; payload = {"name": "<new stable name>"}
                     Names must be unique — an ambiguous name cannot be referenced later.

base_version: pass the integer IR version you last read, or the string "current" to
mean "whatever is latest when this patch is applied".
  - "current" is the convenient default and saves an ir_get round trip.
  - an explicit integer is the safe form for multi-turn editing: it is the only
    one that can detect that the model moved under you, and it is refused rather
    than silently rebased. A stale integer is rejected, never rebased."""

# Substituted, not formatted: the text above is full of literal JSON braces.
# The op list and its tiers come from tcad/ir/capability.py — the same table the
# validator reads — so the promise in the prompt cannot drift from the proof.
_IR_PATCH_DESCRIPTION = _IR_PATCH_DESCRIPTION.replace(
    "{OP_CAPABILITY}", capability.describe_for_model()
)


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
    # The declared type must match what the handler actually accepts. `ir_patch`
    # resolves the literal string "current" to the latest version before building
    # the IrPatch, so declaring `base_version: integer` would make the enforced
    # schema reject a documented, supported call.
    schema["properties"]["base_version"] = {
        "anyOf": [{"type": "integer"}, {"type": "string", "enum": ["current"]}],
        "title": "Base Version",
        "description": "The integer IR version you last read, or \"current\".",
    }
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


async def design_review_handler(services, args, ctx):
    # The engine, which owns the current Gate, validates the evidence and decides
    # the terminal state after this schema-checked call. This handler grants no writes.
    from tcad.loop.completion import DesignReview
    try:
        DesignReview.model_validate(args)
    except ValueError as exc:
        return _err(ToolErrorKind.SCHEMA, f"invalid design review: {exc}")
    return _ok("Review received; the engine will validate current measured evidence.")


def build_ir_tools(services: "Any") -> dict[str, ToolSpec]:
    from tcad.loop.completion import DesignReview
    from tcad.ir.assembly import AssemblySpec
    return {
        "assembly_configure": ToolSpec(
            name="assembly_configure", tier=ToolTier.WRITE,
            description="Configure native FreeCAD Assembly: grounded body IDs, all 13 joint types, world connector positions/axes/roll, limits and time drivers. Replaces assembly declaration; null clears it. Clear prescribed body.motion first. Angular drivers target Revolute/Cylindrical; Linear target Slider/Cylindrical. Formula is native math in time (seconds); Angular uses radians (e.g. pi/2*time for 90 degrees/s), Linear mm, initialValue is supported. Gears/Belt distance and distance2 are positive pitch radii; RackPinion distance=pitch radius; Screw distance=native pitch. Native constraints must make the mechanism solvable; grounding graph alone does not prove solvability. Then ir_commit to build and save actual solver frames; assembly_simulate reads them. ir_commit still grades zero-pose part geometry separately.",
            params_schema={"type": "object", "additionalProperties": False, "required": ["assembly", "reason"],
                "$defs": AssemblySpec.model_json_schema().get("$defs", {}),
                "properties": {"assembly": {"anyOf": [AssemblySpec.model_json_schema(), {"type": "null"}]},
                    "reason": {"type": "string"}, "base_version": {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["current"]}]}}},
            handler=functools.partial(assembly_configure_handler, services),
        ),
        "ir_gear_profile": ToolSpec(
            name="ir_gear_profile", tier=ToolTier.WRITE,
            description="Create one fully constrained spur-gear sketch from compact parameters; no coordinate output needed. Then pad it with ir_patch and batch other features before one ir_commit. Uses sampled involute flanks, radial roots, no profile shift; rejects undercut-risk counts. Units mm/degrees; backlash is tooth thickness reduction per gear. plane sets world coordinate axes; offset planes require support_feature/support_face and world center on that face. Compatible pairs need same module/pressure angle, center distance module*(z1+z2)/2 and correctly phased teeth. Not certified tooth contact or manufacturing geometry.",
            params_schema={"type": "object", "additionalProperties": False, "required": ["id", "body_id", "teeth", "module", "reason"], "properties": {
                "id": {"type": "string"}, "body_id": {"type": "string"}, "reason": {"type": "string"},
                "base_version": {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["current"]}]},
                "teeth": {"type": "integer", "minimum": 18, "maximum": 120}, "module": {"type": "number", "minimum": 1e-6},
                "pressure_angle": {"type": "number", "minimum": 15, "maximum": 30}, "backlash": {"type": "number", "minimum": 0},
                "phase_deg": {"type": "number"}, "samples": {"type": "integer", "minimum": 3, "maximum": 12},
                "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
                "center": {"type": "object", "additionalProperties": False, "required": ["x", "y", "z"], "properties": {k: {"type": "number"} for k in ("x", "y", "z")}},
                "support_feature": {"type": "string"}, "support_face": {"type": "string"}}},
            handler=functools.partial(ir_gear_profile_handler, services),
        ),
        "design_review": ToolSpec(
            name="design_review", tier=ToolTier.WRITE,
            description=("After completing ALL requested geometry and committing it, review EVERY user objective. "
                         "A passed ir_commit is only a build checkpoint. Supply a checklist with verbatim user "
                         "source_text and actual Gate check_ids that measure each objective. Generic solid/export "
                         "checks are not functional evidence. Use empty check_ids for unmeasurable objectives. "
                         "List all missing work, ambiguities and physical tests in remaining_work. Missing evidence "
                         "ends as a draft pending acceptance, never as verified functionality. Never invent user "
                         "dimensions or confirmed requirements. The engine validates the evidence."),
            params_schema=DesignReview.model_json_schema(),
            handler=functools.partial(design_review_handler, services), concurrency_safe=True,
        ),
        "ir_get": ToolSpec(
            name="ir_get",
            tier=ToolTier.READ,
            description="Read current IR, omitting reconstructible defaults. Prefer ids to fetch only needed bodies/sketches/features; ir_digest for measured geometry. include_defaults=true returns all fields.",
            params_schema={"type": "object", "additionalProperties": False, "properties": {"ids": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "include_defaults": {"type": "boolean"}}},
            handler=functools.partial(ir_get_handler, services),
            concurrency_safe=True,
        ),
        "ir_digest": ToolSpec(
            name="ir_digest",
            tier=ToolTier.READ,
            description=("Return a compact program-generated geometry digest (feature chain, "
                         "topology, bbox, volume, key dimensions, BRep-measured holes and the "
                         "planar faces you can attach a sketch to). Cheaper than ir_get."),
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
                "passed=true verifies the build, not completion of the full user request. "
                "Continue missing features after intermediate commits, then call design_review. "
                "If it fails, repair the named feature(s) and call ir_patch + ir_commit again."
            ),
            params_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            handler=functools.partial(ir_commit_handler, services), timeout_s=480.0,
        ),
    }
