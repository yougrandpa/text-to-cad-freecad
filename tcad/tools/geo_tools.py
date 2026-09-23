"""Geometry/read tools — read tier, model never reaches FreeCAD.

geo_view      : render declared visual-checkpoint views from a worker mesh
geo_measure   : measurements (volume/bbox/faces/edges/solids) from introspect
asset_export  : export step/stl/brep/fcstd via the worker
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
    ImageRef,
    Mesh,
    ToolContext,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
    ToolTier,
)
from tcad.worker.protocol import (
    M_EXPORT,
    M_IMPORT_ASSET,
    M_INTROSPECT,
    M_TESSELLATE,
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
    return await asyncio.to_thread(
        functools.partial(services.worker.request, method, params, timeout_s=timeout_s)
    )


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
    version = services.store.current_version(ctx.model_id)
    ir = services.store.load(ctx.model_id, version)
    out_dir = _artifact_dir(ctx, version)
    os.makedirs(out_dir, exist_ok=True)

    mesh_res = await _ask_worker(
        services, M_TESSELLATE,
        {"ir": ir.model_dump(), "views": views, "out_dir": out_dir},
        timeout_s=60.0,
    )
    if not mesh_res.get("ok"):
        e = mesh_res.get("error", {}) or {}
        return _err(
            ToolErrorKind(e.get("kind", "runtime")),  # type: ignore[arg-type]
            e.get("message", "tessellation failed"),
            feature_id=e.get("feature_id"),
        )
    mesh = Mesh.model_validate(mesh_res["result"]["mesh"])
    images: list[ImageRef] = services.renderer.render(
        mesh, out_dir=out_dir, views=list(views), style=style, width=768, height=576
    )
    rendered = ", ".join(i.view for i in images) or "(no views)"
    return _ok(f"Rendered {len(images)} view(s): {rendered}.", images=images)


async def geo_measure_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    what = args.get("what") or ["volume", "bbox", "faces", "edges", "solids"]
    version = services.store.current_version(ctx.model_id)
    ir = services.store.load(ctx.model_id, version)
    res = await _ask_worker(
        services, M_INTROSPECT,
        {"ir": ir.model_dump(), "out_dir": _artifact_dir(ctx, version), "what": list(what)},
        timeout_s=60.0,
    )
    if not res.get("ok"):
        e = res.get("error", {}) or {}
        return _err(
            ToolErrorKind(e.get("kind", "runtime")),  # type: ignore[arg-type]
            e.get("message", "introspect failed"),
            feature_id=e.get("feature_id"),
        )
    # introspect_document returns a GeometryDigest-shaped dict — there is no
    # "measurements" key. Map each requested item onto the digest's real
    # fields; an unmeasured build must error, not report "{}".
    digest = res.get("result") or {}
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
    version = services.store.current_version(ctx.model_id)
    ir = services.store.load(ctx.model_id, version)
    out_dir = _artifact_dir(ctx, version)
    os.makedirs(out_dir, exist_ok=True)
    res = await _ask_worker(
        services, M_EXPORT,
        {"ir": ir.model_dump(), "exports": [fmt], "name": name, "out_dir": out_dir},
        timeout_s=60.0,
    )
    if not res.get("ok"):
        e = res.get("error", {}) or {}
        return _err(
            ToolErrorKind(e.get("kind", "runtime")),  # type: ignore[arg-type]
            e.get("message", "export failed"),
            feature_id=e.get("feature_id"),
        )
    files = (res.get("result") or {}).get("files", {})
    path = files.get(fmt)
    size = os.path.getsize(path) if path and os.path.exists(path) else 0
    return _ok(json.dumps({"path": path, "size_bytes": size}, indent=2))


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
    resolved = None
    tried: list[str] = []
    for root in roots:
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


def build_geo_tools(services: "Any") -> dict[str, ToolSpec]:
    return {
        "geo_view": ToolSpec(
            name="geo_view",
            tier=ToolTier.READ,
            description="Render orthographic views (iso/front/top/right) of the current model as images. Only call at a declared visual checkpoint.",
            params_schema={
                "type": "object",
                "properties": {
                    "views": {"type": "array", "items": {"type": "string"}},
                    "style": {"type": "string"},
                },
            },
            handler=functools.partial(geo_view_handler, services),
            concurrency_safe=True,
        ),
        "geo_measure": ToolSpec(
            name="geo_measure",
            tier=ToolTier.READ,
            description="Return geometric measurements (volume/bbox/faces/edges/solids/holes) of the current model. 'holes' are measured on the BRep (diameter, axis, centre, depth, through-or-blind) and are the only hole evidence that counts; they can disagree with the IR.",
            params_schema={
                "type": "object",
                "properties": {"what": {"type": "array", "items": {"type": "string"}}},
            },
            handler=functools.partial(geo_measure_handler, services),
            concurrency_safe=True,
        ),
        "asset_export": ToolSpec(
            name="asset_export",
            tier=ToolTier.READ,
            description="Export the current model to step/stl/brep/fcstd and return the file path + size.",
            params_schema={
                "type": "object",
                "properties": {
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
