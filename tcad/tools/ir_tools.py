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
            description=(
                "Mutate the IR. ops is a list of {op, target_id?, payload, reason}. "
                "reason is mandatory (lands in the audit log). base_version must equal the "
                "current IR version (optimistic concurrency)."
            ),
            params_schema=IrPatch.model_json_schema(),
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
