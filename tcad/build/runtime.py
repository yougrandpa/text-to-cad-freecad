"""Build orchestration: canonical input, reusable geometry, independent Gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

from tcad.artifacts.cache import GeometryCache
from tcad.build.digest import build_digest, compiler_identity, content_hash
from tcad.build.scheduler import BuildScheduler


class BuildRuntime(BuildScheduler):
    def __init__(self, data_dir, pool, worker, **options):
        super().__init__(data_dir, pool, **options)
        self.data_dir = Path(data_dir)
        self.worker = worker
        self.cache = GeometryCache(data_dir)
        self.compiler = compiler_identity()
        self._backend = None
        self._backend_lock = threading.Lock()

    def _backend_identity(self):
        from tcad.build.pool import check_cancelled
        while not self._backend_lock.acquire(timeout=0.1):
            check_cancelled()
        try:
            check_cancelled()
            if self._backend is None:
                info = self.worker.request("ping", {}, timeout_s=120)
                if not info.get("ok"):
                    raise RuntimeError("FreeCAD backend fingerprint unavailable")
                self._backend = info["result"].get("freecad_version")
                if not self._backend:
                    raise RuntimeError("worker did not report a FreeCAD version")
            return self._backend
        finally:
            self._backend_lock.release()

    def geometry(self, ir, directory, exports):
        from tcad.build.pool import check_cancelled
        check_cancelled()
        backend = self._backend_identity()
        from tcad.build.components import reference_components, compile_components
        from tcad.build.graph import BuildGraph, BuildNode
        import tempfile
        references = reference_components(self.data_dir, ir)
        digest = build_digest(ir, compiler=self.compiler, freecad=backend, exports=exports,
                              assets={id: part["sha256"] for id, part in references.items()})
        self.progress("cache", digest=digest)
        with self.cache.lock(digest):
            if self.cache.restore(digest, directory, ir["model_id"], ir["version"]):
                self.progress("geometry_reused")
                self._write_build(directory, digest, backend, True)
                return {"ok": True, "cache_hit": True, "build_digest": digest}
            self.progress("compile")
            with tempfile.TemporaryDirectory(prefix=".components-", dir=directory) as scratch:
                self.progress("components")
                components, nodes = compile_components(self, ir, backend, scratch, references)
                graph = BuildGraph(nodes=nodes + [BuildNode(id="@assembly", digest=digest,
                                                           dependencies=[n.id for n in nodes])])
                (Path(directory) / "components.json").write_text(graph.model_dump_json(), encoding="utf-8")
                self.progress("assemble")
                response = self.worker.request("build_artifacts", {
                    "ir": ir, "out_dir": directory, "exports": exports,
                    "components": components}, timeout_s=240)
            if response.get("ok"):
                check_cancelled()
                from tcad.render.scene import SceneModel
                scene = SceneModel.from_build(response["result"]["scene"], ir)
                (Path(directory) / "scene.json").write_text(scene.model_dump_json(), encoding="utf-8")
                self.cache.store(digest, directory, ir["model_id"])
            self._write_build(directory, digest, backend, False)
            return {**response, "cache_hit": False, "build_digest": digest}

    def _write_build(self, directory, digest, backend, cache_hit):
        (Path(directory) / "build.json").write_text(json.dumps({
            "build_digest": digest, "compiler": self.compiler,
            "freecad_version": backend, "geometry_cache_hit": cache_hit}), encoding="utf-8")

    async def commit(self, services, model_id, version, factory):
        ir = services.store.load(model_id, version)
        # Registry identity includes requirements: changing verification intent
        # must not coalesce with a job that grades a different input.
        digest = "sha256:" + content_hash(ir.model_dump(mode="json"))
        return await self.run(model_id, version, digest, factory)
