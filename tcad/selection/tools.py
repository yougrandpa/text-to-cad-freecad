"""Deterministic circle editing through the existing declarative patch path."""

from functools import partial
import math

from tcad.core.types import ToolErrorKind, ToolSpec, ToolTier
from tcad.selection.resolve import circular_profile
from tcad.selection.types import SelectionError


async def set_hole_diameter(services, args, ctx):
    from tcad.tools.ir_tools import _err, ir_patch_handler

    selection = ctx.edit_precondition
    if selection is None:
        return _err(ToolErrorKind.DENIED, "Select a proven circular through-hole first.")
    try:
        selection.check(services.store, ctx.model_id)
        # Every selected sketch/feature must denote this same hole. A second,
        # unsupported hole is ambiguity, not permission to edit only the first.
        targets = [t for t in selection.targets if t.ref.entity_kind != "body"]
        ids = {t.hole_sketch_id for t in targets}
        if not targets or None in ids or len(ids) != 1:
            raise SelectionError("ambiguous_target", "Select exactly one supported hole.", 422)
        diameter = args["diameter_mm"]
        if isinstance(diameter, bool) or not isinstance(diameter, (float, int)) or not math.isfinite(diameter) or diameter <= 0:
            raise SelectionError("invalid_dimension", "Diameter must be finite and positive.", 422)
        target = targets[0]
        ir = services.store.load(ctx.model_id)
        body = next(b for b in ir.bodies if b.id == target.ref.body_id)
        sketch = next((s for s in body.sketches if s.id == target.hole_sketch_id), None)
        if sketch is None or circular_profile(body, sketch) is None:
            raise SelectionError("unmapped_entity", "Selected circle/Pocket relationship changed.", 422)
        geometry = [g.model_dump(mode="json") for g in sketch.geometry]
        constraints = [c.model_dump(mode="json") for c in sketch.constraints]
        geometry[0]["radius"] = diameter / 2
        for constraint in constraints:
            if constraint["type"] in {"Radius", "Diameter"}:
                constraint["value"] = diameter / 2 if constraint["type"] == "Radius" else diameter
        return await ir_patch_handler(services, {
            "base_version": selection.version,
            "ops": [{"op": "update_sketch", "target_id": sketch.id,
                     "payload": {"geometry": geometry, "constraints": constraints}, "reason": args["reason"]}],
            "summary": f"Selected hole diameter {diameter:g} mm",
        }, ctx)
    except SelectionError as exc:
        return _err(ToolErrorKind.SEMANTIC, f"{exc.code}: {exc}", hint="Reselect or clarify the target.")


def build_selection_tools(services):
    config = getattr(services, "config", None)
    if config is not None and not config.selection.enabled:
        return {}
    return {"cad_set_hole_diameter": ToolSpec(
        name="cad_set_hole_diameter", tier=ToolTier.WRITE,
        selection_capability="set_hole_diameter",
        description="Resize one referenced, verified circular ThroughAll Pocket. Updates the circle radius and its Radius/Diameter constraint together using ir_patch. Refuses stale or ambiguous targets. Then update changed requirements and run ir_commit/design_review.",
        params_schema={"type": "object", "additionalProperties": False,
                       "properties": {"diameter_mm": {"type": "number", "exclusiveMinimum": 0},
                                      "reason": {"type": "string", "minLength": 1}},
                       "required": ["diameter_mm", "reason"]},
        handler=partial(set_hole_diameter, services),
    )}
