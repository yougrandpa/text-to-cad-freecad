"""Geometry/read tools — read tier, model never reaches FreeCAD.

geo_view      : render declared visual-checkpoint views from saved scenes
geo_measure   : measurements (volume/bbox/faces/edges/solids) from introspect
asset_export  : read or convert saved step/stl/brep/fcstd artifacts
asset_import  : import an external asset (step/iges/stl/dxf) via the worker

Token cost is the reason geo_view is gated: the engine sets a per-step
``ToolContext.visual_ok`` (True only at declared visual checkpoints, or for
inspect turns). The handler refuses otherwise so screenshots are never burned
every step (design §4.3 / §4.6 cross-layer check C←E). The ``services._visual_ok``
spelling is still honoured as a fallback for embedders that drive the handler
directly; it is no longer written by the engine, because a bundle shared by every
turn is the wrong place for per-turn state.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os

from tcad.core.types import (
    ToolContext,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
)
from tcad.worker.protocol import (
    M_EXPORT,
    M_IMPORT_ASSET,
)

from tcad.tools.ir_tools import _err, _ok  # shared helpers


def _artifact_dir(ctx: ToolContext, version: int) -> str:
    return os.path.join(ctx.data_dir, "artifacts", ctx.model_id, f"v{version}")


async def _ask_worker(services: "Any", method: str, params: dict, *, timeout_s: float) -> dict:
    """Call the worker protocol without blocking the event loop.

    ``Worker.request`` is synchronous by contract (``tcad/tools/base.py``), and
    these four handlers are ``async def``. Calling it inline parked the entire
    asyncio loop for the length of the call — a tessellation or a STEP import can
    take seconds, and during that window *other* sessions' SSE streams stall,
    health checks stop answering and a cancelled turn never reaches its next
    checkpoint. The commit path was moved onto a thread for exactly this reason;
    the geo tools had the same shape and were missed.
    """
    from tcad.build.execution import run_blocking
    return await run_blocking(services, services.worker.request, method, params,
                              timeout_s=timeout_s, label=method)


async def geo_view_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    # Per-turn first, shared bundle only as a fallback for embedders that drive
    # the tool directly. Reading the bundle unconditionally is what let one
    # request's commit open the visual checkpoint for another request's step.
    allowed_now = bool(getattr(ctx, "visual_ok", False)) or bool(
        getattr(services, "_visual_ok", False)
    )
    if not allowed_now:
        return _err(
            ToolErrorKind.RUNTIME,
            "geo_view is only permitted at declared visual checkpoints "
            "(first_compile / major_change / final) or during an inspect turn. "
            "This step is not a checkpoint — call ir_commit (or use ir_digest) instead.",
        )
    views = args.get("views") or ["iso"]
    style = args.get("style", "flat_edges")
    from tcad.inspect.artifact import ArtifactReader
    from tcad.render.snapshot import render_snapshot

    def snapshot():
        reader = ArtifactReader(ctx.data_dir)
        artifact_id = args.get("artifact_id")
        version = None if artifact_id else services.store.current_version(ctx.model_id)
        manifest, root = reader.resolve(ctx.model_id, version, artifact_id)
        images = render_snapshot(reader, manifest, root, services.renderer,
            views=list(views), style=style, angle=args.get("driver_angle_deg", 0),
            frame=args.get("frame_index", 0))
        return manifest, images

    try:
        manifest, images = await asyncio.to_thread(snapshot)
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.RUNTIME, str(exc), hint="Commit this version to create its scene first.")
    rendered = ", ".join(i.view for i in images) or "(no views)"
    return _ok(f"Rendered {len(images)} view(s): {rendered}. "
               f"artifact_id={manifest.artifact_id}, v{manifest.ir_version}, status={manifest.status.value}. "
               "Saved kinematic poses do not verify contact, collisions or cutting.", images=images)


async def geo_measure_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    what = args.get("what") or ["volume", "bbox", "faces", "edges", "solids"]
    from tcad.inspect.artifact import ArtifactReader

    def read_measurements():
        reader = ArtifactReader(ctx.data_dir)
        artifact_id = args.get("artifact_id")
        version = None if artifact_id else services.store.current_version(ctx.model_id)
        manifest, root = reader.resolve(ctx.model_id, version, artifact_id)
        return reader.digest(manifest, root).model_dump(mode="json")

    try:
        digest = await asyncio.to_thread(read_measurements)
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.RUNTIME, str(exc),
                    hint="Commit this version first, or supply the artifact_id of an existing build.")
    # introspect_document returns a GeometryDigest-shaped dict — there is no
    # "measurements" key. Map each requested item onto the digest's real
    # fields; an unmeasured build must error, not report "{}".
    if not digest.get("measurements_available"):
        return _err(
            ToolErrorKind.RUNTIME,
            "no solid to measure (build produced an empty shape)",
            hint="Fix the build so a solid exists, then call geo_measure again.",
        )
    topology = digest.get("topology") or {}
    bbox = digest.get("bbox") or {}
    catalog = {
        "volume": digest.get("volume"),
        "area": digest.get("area"),
        "bbox": {k: bbox.get(k) for k in ("x", "y", "z", "x_min", "y_min", "z_min")},
        "faces": topology.get("faces"),
        "edges": topology.get("edges"),
        "solids": topology.get("solids"),
        "vertexes": topology.get("vertexes"),
        "shells": topology.get("shells"),
        "is_valid": digest.get("is_valid"),
        "shape_type": digest.get("shape_type"),
        # The BRep-measured holes, not the IR's declared ones: without this the
        # model can only ever see its own numbers echoed back and keeps asserting
        # a diameter the kernel never cut.
        "holes": digest.get("holes") or [],
    }
    unknown = [w for w in what if w not in catalog]
    if unknown:
        return _err(
            ToolErrorKind.SEMANTIC,
            f"unknown measurement(s): {', '.join(unknown)}",
            hint="supported: " + ", ".join(sorted(catalog)),
        )
    return _ok(json.dumps({w: catalog[w] for w in what}, indent=2))


async def asset_export_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    from tcad.core.ids import InvalidIdentifier, ensure_safe_id

    fmt = args.get("fmt", "step")
    name = args.get("name", ctx.model_id)
    # `name` is model-controlled and becomes a filename in the worker
    # (`os.path.join(out_dir, f"{name}.{fmt}")`). An id-shaped name cannot name
    # anything but a child of `out_dir`, so validate it here rather than trusting
    # the model not to write "../../x.step".
    try:
        name = ensure_safe_id(name, kind="export name")
    except InvalidIdentifier as exc:
        return _err(ToolErrorKind.SCHEMA, str(exc))
    from tcad.inspect.operations import export_artifact
    try:
        from tcad.build.execution import run_blocking
        result = await run_blocking(services, export_artifact, services, ctx,
                                   {**args, "name": name}, label="artifact export")
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.RUNTIME, str(exc))
    return _ok(json.dumps(result, indent=2))


async def asset_import_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    from tcad.core.ids import InvalidIdentifier, ensure_contained

    path = args.get("path")
    fmt = args.get("fmt")
    if not path:
        return _err(ToolErrorKind.SCHEMA, "asset_import requires 'path'")

    # A model-supplied path is a read primitive. Confine it to the two roots this
    # project legitimately exchanges files through — the working tree and the data
    # directory — checked on the RESOLVED path so `..` and symlinks are collapsed
    # first. Anything else is refused with the roots named.
    roots = [r for r in (ctx.workdir, ctx.data_dir) if r]
    resolved = os.path.realpath(path) if ctx.access_mode == "full" else None
    tried: list[str] = []
    for root in roots:
        if resolved is not None:
            break
        try:
            resolved = ensure_contained(path, root, kind="import path")
            break
        except InvalidIdentifier as exc:
            tried.append(str(exc))
    if resolved is None:
        return _err(
            ToolErrorKind.DENIED,
            f"import path {path!r} is outside the permitted roots "
            f"({', '.join(str(r) for r in roots)}); refusing to read it. "
            + (tried[0] if tried else ""),
        )
    path = str(resolved)
    res = await _ask_worker(
        services, M_IMPORT_ASSET, {"path": path, "fmt": fmt}, timeout_s=60.0
    )
    if not res.get("ok"):
        e = res.get("error", {}) or {}
        return _err(
            ToolErrorKind(e.get("kind", "runtime")),  # type: ignore[arg-type]
            e.get("message", "import failed"),
        )
    summary = (res.get("result") or {}).get("shape_summary", {})
    return _ok(json.dumps(summary, indent=2))


async def _assembly_result(services, ctx, checks=None):
    from tcad.inspect.operations import saved_assembly
    from tcad.build.execution import run_blocking
    return await run_blocking(services, saved_assembly, services, ctx, checks or {}, label="saved assembly")


async def assembly_simulate_handler(services, args, ctx):
    try:
        result, path = await _assembly_result(services, ctx, args or None)
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.SOLVER, str(exc))
    return _ok(json.dumps({"solver": result["solver"], "frames": len(result["frames"]),
        "bodies": [p["body_id"] for p in result["parts"]], "start": result["start"], "step": result["step"],
        "animation": path, "fcstd": result["export"], "artifact_id": result["artifact_id"], "scope": result["scope"], "max_swing_deg": result.get('max_swing_deg'), "interferences": (result["interferences"][:10] if result.get("interferences") is not None else None), "interference_count": len(result.get("interferences") or []), "frames_checked": result.get("frames_checked", 0)}, ensure_ascii=False, separators=(",", ":")))


async def assembly_solve_handler(services, args, ctx):
    return await assembly_simulate_handler(services, {"solve_only": True}, ctx)


async def assembly_export_handler(services, args, ctx):
    from tcad.inspect.operations import export_saved_animation
    try:
        summary = await asyncio.to_thread(export_saved_animation, services, ctx, args)
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.SEMANTIC, str(exc))
    return _ok(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


async def geo_check_motion_handler(services, args, ctx):
    from tcad.inspect.operations import check_motion
    try:
        from tcad.build.execution import run_blocking
        result = await run_blocking(services, check_motion, services, ctx, args, label="motion check")
    except (OSError, ValueError) as exc:
        return _err(ToolErrorKind.RUNTIME, str(exc))
    return _ok(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def build_geo_tools(services: "Any") -> dict[str, ToolSpec]:
    return {
        "assembly_solve": ToolSpec(
            name="assembly_solve", tier=ToolTier.READ,
            description="Read the committed native static Assembly solution, including passive Fixed/Ball/Distance/Parallel/Perpendicular/Angle joints. Requires grounding and configured joints but no motion driver. Returns solved placement/export summary or native constraint errors. Use assembly_simulate for time-dependent motion.",
            params_schema={"type":"object", "additionalProperties":False, "properties":{"artifact_id":{"type":"string"},}},
            handler=functools.partial(assembly_solve_handler,services),timeout_s=190.0,
        ),
        "assembly_export": ToolSpec(
            name="assembly_export", tier=ToolTier.READ,
            description="Export saved artifact animation as GIF, MP4, AVI or WebM; commit this version first or pin artifact_id. mode=motion exports solved motion (default); mode=assemble shows separated parts converging to their saved solved pose, mode=explode reverses it. Assembly presentation keeps grounded parts fixed and uses straight eased paths; it does not solve joints or validate assembly-path collisions. duration_s (default 3), frames (default 41) and explode_distance_mm (automatic by model size) apply to assemble/explode. Fixed camera bounds avoid frame-to-frame zoom; stride samples frames and includes the final pose. GIF needs Pillow, video optional PyAV and matching encoder.",
            params_schema={"type":"object", "additionalProperties":False, "properties":{"artifact_id":{"type":"string"},
                "mode":{"type":"string","enum":["motion","assemble","explode"]},
                "duration_s":{"type":"number","minimum":0.1,"maximum":60},
                "frames":{"type":"integer","minimum":2,"maximum":600},
                "explode_distance_mm":{"type":"number","exclusiveMinimum":0},
                "format":{"type":"string","enum":["gif","mp4","avi","webm"]},
                "view":{"type":"string","enum":["iso","front","top","right"]},
                "width":{"type":"integer","minimum":128,"maximum":1024},
                "height":{"type":"integer","minimum":128,"maximum":1024},
                "stride":{"type":"integer","minimum":1,"maximum":30}}},
            handler=functools.partial(assembly_export_handler,services),timeout_s=300.0,
        ),
        "assembly_simulate": ToolSpec(
            name="assembly_simulate", tier=ToolTier.READ,
            description="Read saved native Assembly or gravity-pendulum animation frames. Returns compact summary, solver scope and measured max_swing_deg, not large frame arrays. Optional check_pairs performs sampled BRep overlap checks on saved poses, check_stride selects frames. Commit after edits. A clear sample set does not prove continuous clearance or contact forces; this is separate from the geometry Gate.",
            params_schema={"type":"object", "additionalProperties":False, "properties":{"artifact_id":{"type":"string"},
                "check_pairs":{"type":"array","minItems":1,"maxItems":100,"items":{"type":"array","minItems":2,"maxItems":2,"items":{"type":"string"}}},
                "check_stride":{"type":"integer","minimum":1,"maximum":30}}},
            handler=functools.partial(assembly_simulate_handler, services), timeout_s=190.0,
        ),
        "geo_check_motion": ToolSpec(
            name="geo_check_motion", tier=ToolTier.READ,
            description="Read the committed FCStd and check real solid overlap at sampled crank angles. Optional pairs selects body ID pairs; default all. Reports interference volume; sampled_clear never proves continuous clearance, gear contact or cutting. Check mounting parts separately from expected intentional fits.",
            params_schema={"type": "object", "additionalProperties": False, "properties": {
                "artifact_id": {"type": "string"},
                "angles": {"type": "array", "minItems": 1, "maxItems": 73, "items": {"type": "number", "minimum": -720, "maximum": 720}},
                "pairs": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}}},
                "volume_tolerance": {"type": "number", "minimum": 0}}},
            handler=functools.partial(geo_check_motion_handler, services), concurrency_safe=True, timeout_s=130.0,
        ),
        "geo_view": ToolSpec(
            name="geo_view",
            tier=ToolTier.READ,
            description=("Render saved artifact views (iso/front/top/right) at a visual checkpoint. "
                         "Optional artifact_id pins a build; otherwise commit this version first. "
                         "Create/modify turns require a successful ir_commit in THIS turn to open the checkpoint, "
                         "even for an already published or pinned artifact. Inspect turns can render saved artifacts directly. "
                         "Optional driver_angle_deg poses bodies using their motion pivot/axis/ratio "
                         "or frame_index selects a saved native animation frame. This previews rigid kinematics, not collision, "
                         "contact or material removal. Default angle 0 preserves the static pose; native frame 0 shows the first solved frame."),
            params_schema={
                "type": "object",
                "properties": {
                    "views": {"type": "array", "items": {"type": "string"}},
                    "style": {"type": "string"},
                    "driver_angle_deg": {"type": "number", "minimum": -720, "maximum": 720},
                    "artifact_id": {"type": "string"},
                    "frame_index": {"type": "integer", "minimum": 0, "maximum": 599},
                },
            },
            handler=functools.partial(geo_view_handler, services),
            concurrency_safe=True,
        ),
        "geo_measure": ToolSpec(
            name="geo_measure",
            tier=ToolTier.READ,
            description="Read geometric measurements (volume/bbox/faces/edges/solids/holes) from a committed artifact. Optional artifact_id pins an earlier build; otherwise this IR version must already have an artifact. Never rebuilds the current IR. 'holes' are measured on the BRep and can disagree with the IR.",
            params_schema={
                "type": "object",
                "properties": {
                    "what": {"type": "array", "items": {"type": "string", "enum":["volume","area","bbox","faces","edges","solids","vertexes","shells","is_valid","shape_type","holes"]}},
                    "artifact_id": {"type": "string", "description": "sha256:<64 hex digits>"},
                },
            },
            handler=functools.partial(geo_measure_handler, services),
            concurrency_safe=True,
        ),
        "asset_export": ToolSpec(
            name="asset_export",
            tier=ToolTier.READ,
            description="Export committed step/stl/brep/fcstd artifacts and return path, size and artifact_id; commit first or pin artifact_id. fcstd exports the native assembly document when saved, retaining editable bodies, joints and drivers; otherwise the editable part document.",
            params_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "fmt": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
            handler=functools.partial(asset_export_handler, services),
            concurrency_safe=True,
        ),
        "asset_import": ToolSpec(
            name="asset_import",
            tier=ToolTier.READ,
            description="Import an external asset (step/iges/stl/dxf) and return a shape summary.",
            params_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "fmt": {"type": "string"},
                },
            },
            handler=functools.partial(asset_import_handler, services),
            concurrency_safe=True,
        ),
    }
