"""Geometry/read tools — read tier, model never reaches FreeCAD.

geo_view      : render declared visual-checkpoint views from a worker mesh
geo_measure   : measurements (volume/bbox/faces/edges/solids) from introspect
asset_export  : export step/stl/brep/fcstd via the worker
asset_import  : import an external asset (step/iges/stl/dxf) via the worker

Token cost is the reason geo_view is gated: the engine supplies a per-step
``services._visual_ok`` hint (True only at declared visual checkpoints, or for
inspect turns). The handler refuses otherwise so screenshots are never burned
every step (design §4.3 / §4.6 cross-layer check C←E).
"""

from __future__ import annotations

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


async def geo_view_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    if not getattr(services, "_visual_ok", False):
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

    mesh_res = services.worker.request(
        M_TESSELLATE,
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
    res = services.worker.request(
        M_INTROSPECT,
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
    measurements = (res.get("result") or {}).get("measurements", {})
    return _ok(json.dumps(measurements, indent=2))


async def asset_export_handler(services: "Any", args: dict, ctx: ToolContext) -> ToolResult:
    fmt = args.get("fmt", "step")
    name = args.get("name", ctx.model_id)
    version = services.store.current_version(ctx.model_id)
    ir = services.store.load(ctx.model_id, version)
    out_dir = _artifact_dir(ctx, version)
    os.makedirs(out_dir, exist_ok=True)
    res = services.worker.request(
        M_EXPORT,
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
    path = args.get("path")
    fmt = args.get("fmt")
    if not path:
        return _err(ToolErrorKind.SCHEMA, "asset_import requires 'path'")
    res = services.worker.request(M_IMPORT_ASSET, {"path": path, "fmt": fmt}, timeout_s=60.0)
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
            description="Return geometric measurements (volume/bbox/faces/edges/solids) of the current model.",
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
