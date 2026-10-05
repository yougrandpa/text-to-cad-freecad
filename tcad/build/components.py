"""Resolve pinned parts and compile independent bodies through pool leases."""

from __future__ import annotations

import contextvars
import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tcad.artifacts.manifest import ArtifactStatus
from tcad.build.digest import build_digest, content_hash
from tcad.build.graph import BuildGraph, BuildNode
from tcad.build.pool import check_cancelled, worker_owner
from tcad.inspect.artifact import ArtifactReader


def reference_components(data_dir, ir):
    reader = ArtifactReader(data_dir)
    components = {}
    for body in ir["bodies"]:
        reference = body.get("part_ref")
        if not reference:
            continue
        manifest, root = reader.resolve(reference["model_id"], artifact_id=reference["artifact_id"])
        if manifest.status != ArtifactStatus.VERIFIED:
            raise ValueError("PartRef requires a verified published artifact")
        source = json.loads(reader.read_file(manifest, root, "ir.json"))
        source_body = next((b for b in source["bodies"] if b["id"] == reference["body_id"]), None)
        if source_body is None:
            raise ValueError("PartRef body does not exist in the pinned artifact")
        name = manifest.model_id + ".FCStd"
        reader.read_file(manifest, root, name)
        components[body["id"]] = {"path": str(root / name),
            "sha256": manifest.files[name].sha256, "body_id": source_body["id"],
            "body_name": source_body["name"], "reference": True,
            "artifact_id": manifest.artifact_id, "placement": reference.get("placement")}
    return components


def compile_components(runtime, ir, backend, directory, references):
    nodes = []
    components = dict(references)
    inline = [b for b in ir["bodies"] if not b.get("part_ref")]
    if len(ir["bodies"]) < 2:
        inline = []  # Single parts remain one sequential build.

    def compile_body(body):
        check_cancelled()
        clean = {**body, "motion": None}
        part_ir = {**ir, "model_id": "part-" + content_hash(clean)[:24], "version": 0,
                   "bodies": [clean], "assembly": None, "requirements": {}, "notes": []}
        digest = build_digest(part_ir, compiler=runtime.compiler, freecad=backend, exports=["fcstd"])
        target = Path(directory) / content_hash(body["id"])
        target.mkdir()
        with runtime.cache.lock(digest):
            if not runtime.cache.restore(digest, target, part_ir["model_id"], 0):
                response = runtime.worker.request("build_artifacts", {
                    "ir": part_ir, "out_dir": str(target), "exports": ["fcstd"]}, timeout_s=240)
                if not response.get("ok"):
                    raise ValueError(response.get("error", {}).get("message", "component build failed"))
                check_cancelled()
                from tcad.render.scene import SceneModel
                scene = SceneModel.from_build(response["result"]["scene"], part_ir)
                (target / "scene.json").write_text(scene.model_dump_json(), encoding="utf-8")
                runtime.cache.store(digest, target, part_ir["model_id"])
        document = target / (part_ir["model_id"] + ".FCStd")
        sha = hashlib.sha256(document.read_bytes()).hexdigest()
        return body["id"], {"path": str(document), "sha256": sha,
                              "body_id": body["id"], "body_name": body["name"]}, digest

    with ThreadPoolExecutor(max_workers=len(runtime.pool.handles)) as executor:
        futures = [executor.submit(contextvars.copy_context().run, compile_body, body) for body in inline]
        try:
            for future in as_completed(futures):
                id, component, digest = future.result()
                components[id] = component
                nodes.append(BuildNode(id=id, digest=digest, document_sha256=component["sha256"]))
        except BaseException:
            for future in futures:
                future.cancel()
            runtime.pool.cancel_owner(worker_owner.get(), "component build failed")
            raise
    for id, reference in references.items():
        nodes.append(BuildNode(id=id, digest="sha256:" + content_hash({
                               "document": reference["sha256"], "body": reference["body_id"]}),
                               document_sha256=reference["sha256"], source_artifact=reference["artifact_id"]))
    # Completion order is nondeterministic; the recorded build graph is not.
    return components, sorted(nodes, key=lambda node: node.id)
