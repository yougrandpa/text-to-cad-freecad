"""Artifact query operations; authoring IR is never an execution input."""

import json
import shutil
import tempfile
from pathlib import Path

from tcad.build.digest import content_hash
from tcad.core.ids import contained_path, ensure_safe_id
from tcad.inspect.artifact import ArtifactReader
from tcad.build.pool import check_cancelled


def resolve_context(services, ctx, args):
    reader = ArtifactReader(ctx.data_dir)
    identity = args.get("artifact_id")
    version = None if identity else services.store.current_version(ctx.model_id)
    manifest, root = reader.resolve(ctx.model_id, version, identity)
    return reader, manifest, root


def document_params(reader, manifest, root):
    source = json.loads(reader.read_file(manifest, root, "ir.json"))
    name = manifest.model_id + ".FCStd"
    reader.read_file(manifest, root, name)
    return {"path": str(root / name), "sha256": manifest.files[name].sha256,
            "bodies": [{"id": b["id"], "name": b["name"]} for b in source["bodies"]]}


def export_artifact(services, ctx, args):
    reader, manifest, root = resolve_context(services, ctx, args)
    fmt = args.get("fmt", "step").lower()
    if fmt not in {"fcstd", "step", "stl", "brep"}:
        raise ValueError("unsupported export format")
    name = ensure_safe_id(args.get("name", ctx.model_id), kind="export name")
    extension = ".FCStd" if fmt == "fcstd" else "." + fmt
    if name.lower().endswith(extension.lower()):
        name = ensure_safe_id(name[:-len(extension)], kind="export name")
    original = manifest.model_id + (".FCStd" if fmt == "fcstd" else "." + fmt)
    if fmt == 'fcstd' and 'assembly.FCStd' in manifest.files:
        # The native document retains editable source bodies plus joints and
        # drivers. Exporting only the source model silently loses the assembly.
        original = 'assembly.FCStd'
    if original in manifest.files and name == manifest.model_id:
        reader.read_file(manifest, root, original)
        return {"path": str(root / original), "size_bytes": (root / original).stat().st_size,
                "artifact_id": manifest.artifact_id}
    params = None if original in manifest.files else document_params(reader, manifest, root)
    identity = manifest.files[original].sha256 if params is None else params["sha256"]
    destination = contained_path(ctx.data_dir, "derived", "exports", identity, name + extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=destination.parent) as scratch:
        if original in manifest.files:
            candidate = Path(scratch) / destination.name
            candidate.write_bytes(reader.read_file(manifest, root, original))
        else:
            response = services.worker.request("export_saved", {**params, "fmt": fmt,
                "name": name, "out_dir": scratch}, timeout_s=120)
            if not response.get("ok"):
                raise ValueError(response.get("error", {}).get("message", "artifact export failed"))
            candidate = Path(scratch) / destination.name
        check_cancelled()
        candidate.replace(destination)
    return {"path": str(destination), "size_bytes": destination.stat().st_size,
            "artifact_id": manifest.artifact_id}


def saved_assembly(services, ctx, args):
    reader, manifest, root = resolve_context(services, ctx, args)
    scene = reader.scene(manifest, root)
    if not scene.animation:
        raise ValueError("commit a configured native assembly first")
    render_mesh = scene.mesh_for_render()
    result = {**scene.animation, "mesh": {**render_mesh.model_dump(mode="json"),
        'facet_groups': render_mesh.facet_groups},
        "artifact_id": manifest.artifact_id, "scope": scene.animation.get('scope', "Saved native kinematics; not contact or cutting verification."),
        "export": str(root / ("assembly.FCStd" if (root / "assembly.FCStd").exists() else manifest.model_id + '.FCStd')), "interferences": None, "frames_checked": 0}
    if args.get("check_pairs"):
        response = services.worker.request("check_saved_motion", {
            **document_params(reader, manifest, root), "frames": scene.animation["frames"],
            "pairs": args["check_pairs"], "check_stride": args.get("check_stride", 1)}, timeout_s=180)
        if not response.get("ok"):
            raise ValueError(response.get("error", {}).get("message", "artifact frame check failed"))
        result.update(response["result"])
    return result, str(root / "scene.json")


def check_motion(services, ctx, args):
    reader, manifest, root = resolve_context(services, ctx, args)
    scene = reader.scene(manifest, root)
    if scene.animation:
        raise ValueError("native assemblies use saved frames; use assembly_simulate check_pairs")
    response = services.worker.request("check_saved_motion", {**document_params(reader, manifest, root),
        "motion": [p.model_dump(mode="json") for p in scene.motion],
        **{k: args[k] for k in ("angles", "pairs", "volume_tolerance") if k in args}}, timeout_s=180)
    if not response.get("ok"):
        raise ValueError(response.get("error", {}).get("message", "artifact motion check failed"))
    return {**response["result"], "artifact_id": manifest.artifact_id}


def export_saved_animation(services, ctx, args):
    from tcad.render.animation import assembly_transition, export_animation
    result, _ = saved_assembly(services, ctx, args)
    mode = args.get('mode', 'motion')
    if mode not in {'motion', 'assemble', 'explode'}:
        raise ValueError('unknown animation mode')
    if mode != 'motion':
        reader, manifest, root = resolve_context(services, ctx, {'artifact_id': result['artifact_id']})
        source = json.loads(reader.read_file(manifest, root, 'ir.json'))
        result = assembly_transition(result, mode=mode,
            grounded_body_ids=source['assembly']['grounded'],
            duration_s=args.get('duration_s', 3), frames=args.get('frames', 41),
            explode_distance_mm=args.get('explode_distance_mm'))
    fmt = args.get("format", "gif")
    if fmt not in {"gif", "mp4", "avi", "webm"}:
        raise ValueError("unknown animation format")
    destination = contained_path(ctx.data_dir, "derived", "animations", result["artifact_id"][7:], content_hash(args))
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / ("animation." + fmt)
    with tempfile.TemporaryDirectory(prefix=".animation-", dir=destination) as scratch:
        candidate = Path(scratch) / target.name
        summary = export_animation(result, candidate, view=args.get("view", "iso"),
            width=args.get("width", 480), height=args.get("height", 360), stride=args.get("stride", 2))
        candidate.replace(target)
    return {**summary, "path": str(target), "artifact_id": result["artifact_id"],
            'mode': mode, 'scope': result['scope']}
