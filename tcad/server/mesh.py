"""Bounded, version-specific geometry previews for the human viewport.

This is deliberately independent of the commit/Gate path. Tessellating a part
does not verify it, and preview work must not write into a published build.
The cache is ephemeral: its key includes the exact IR bytes, not only a version
number, so replacing/restoring a snapshot cannot resurrect stale geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from tcad.core.ids import InvalidIdentifier, contained_path, ensure_safe_id
from tcad.core.types import Mesh
from tcad.worker.protocol import M_TESSELLATE

MIN_TOLERANCE = 0.1
MAX_TOLERANCE = 5.0
MAX_VERTICES = 100_000
MAX_FACETS = 200_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Keep preview callers from occupying every shared ASGI worker thread while
# waiting for the serialized build cache. Admission itself runs on the event loop.
MAX_ACTIVE_MESH_REQUESTS = 4


class PreviewMesh(Mesh):
    """The worker mesh schema, with its explicit allocation counts."""

    vertex_count: int
    facet_count: int


class MeshPreviewResponse(BaseModel):
    model_id: str
    version: int
    mesh: PreviewMesh


class MeshPreviewCache:
    """A small byte-bounded LRU, with one tessellation in flight at a time.

    Separate locks keep cached reads available while another model is building.
    The worker itself is serial; coalescing misses here avoids queueing identical
    expensive requests. No client-controlled, unbounded per-key lock registry.
    """

    def __init__(self, *, max_entries: int = 16, max_bytes: int = 32 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()

    def _get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def get_or_build(self, key: tuple, build: Callable[[], bytes], *, force=False) -> bytes:
        if not force:
            cached = self._get(key)
            if cached is not None:
                return cached
        with self._build_lock:
            if not force:
                cached = self._get(key)
                if cached is not None:
                    return cached
            # A failed refresh must not leave a previous success at this key.
            with self._lock:
                old = self._entries.pop(key, None)
                if old is not None:
                    self._bytes -= len(old)
            value = build()
            with self._lock:
                if self.max_entries > 0 and len(value) <= self.max_bytes:
                    self._entries[key] = value
                    self._bytes += len(value)
                    while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                        _, removed = self._entries.popitem(last=False)
                        self._bytes -= len(removed)
            return value


def _checked_mesh(raw: Any) -> dict:
    """Validate the worker wire shape before a browser allocates GPU buffers."""
    if not isinstance(raw, dict):
        raise HTTPException(502, "worker did not return a mesh object")
    vertices, facets = raw.get("vertices"), raw.get("facets")
    if not isinstance(vertices, list) or not isinstance(facets, list):
        raise HTTPException(502, "worker returned invalid mesh arrays")
    if not vertices or not facets:
        raise HTTPException(422, "model has no previewable solid geometry")
    if len(vertices) > MAX_VERTICES or len(facets) > MAX_FACETS:
        raise HTTPException(413, "mesh is too large for the viewport; use a coarser tolerance")
    try:
        mesh = Mesh.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(502, "worker returned an invalid mesh schema") from exc
    if any(not math.isfinite(v) for vertex in mesh.vertices for v in vertex):
        raise HTTPException(502, "worker returned non-finite mesh coordinates")
    if any(i < 0 or i >= len(mesh.vertices) for face in mesh.facets for i in face):
        raise HTTPException(502, "worker returned an out-of-range mesh index")
    if any(len(set(face)) != 3 for face in mesh.facets):
        raise HTTPException(502, "worker returned a degenerate mesh facet")
    if any(not math.isfinite(v) for v in mesh.bbox.model_dump().values()):
        raise HTTPException(502, "worker returned a non-finite mesh bounding box")
    if not math.isfinite(mesh.volume) or not math.isfinite(mesh.tolerance) or mesh.tolerance <= 0:
        raise HTTPException(502, "worker returned invalid mesh measurements")
    if mesh.volume <= 0:
        raise HTTPException(422, "model has no positive-volume solid geometry")
    result = mesh.model_dump(mode="json")
    result["vertex_count"] = len(mesh.vertices)
    result["facet_count"] = len(mesh.facets)
    return result


def mesh_preview(
    services: Any,
    cache: MeshPreviewCache,
    model_id: str,
    *,
    version: int | None,
    tolerance: float,
    force: bool,
) -> Response:
    """Load, tessellate and encode off the event loop (the endpoint is sync)."""
    try:
        ensure_safe_id(model_id, kind="model_id")
        ir = services.store.load(model_id, version)
    except InvalidIdentifier as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"no such model/version: {model_id}") from exc
    except (OSError, ValueError) as exc:
        # A corrupt/inaccessible snapshot is not an empty model or a 404.
        raise HTTPException(500, "could not read the model snapshot") from exc

    actual_version = int(ir.version)
    if ir.model_id != model_id or actual_version < 0 or (
        version is not None and actual_version != version
    ):
        raise HTTPException(500, "stored model snapshot identity does not match the request")
    ir_data = ir.model_dump(mode="json")
    try:
        fingerprint = hashlib.sha256(
            json.dumps(ir_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError) as exc:
        raise HTTPException(500, "stored model snapshot has invalid numeric data") from exc
    key = (model_id, actual_version, fingerprint, tolerance)

    def build() -> bytes:
        try:
            root = contained_path(Path(services.config.storage.data_dir), ".preview-mesh")
            root.mkdir(parents=True, exist_ok=True)
            # Never write into the Gate's published version directory. Future
            # worker-side build side effects remain isolated and are cleaned up.
            with tempfile.TemporaryDirectory(prefix="mesh-", dir=root) as workdir:
                res = services.worker.request(
                    M_TESSELLATE,
                    {"ir": ir_data, "out_dir": workdir, "tolerance": tolerance},
                    timeout_s=180.0,
                )
        except InvalidIdentifier as exc:
            raise HTTPException(400, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "mesh tessellation timed out") from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, "mesh worker or preview workspace is unavailable") from exc
        if not isinstance(res, dict):
            raise HTTPException(502, "worker returned an invalid response")
        result = res.get("result")
        if not res.get("ok") or (isinstance(result, dict) and result.get("ok") is False):
            nested_error = result.get("error") if isinstance(result, dict) else None
            error = res.get("error") or nested_error or {}
            kind = error.get("kind") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            status = 504 if kind == "timeout" else 503 if kind == "runtime" else 422
            raise HTTPException(status, f"cannot tessellate model: {message or 'tessellate failed'}")
        mesh = _checked_mesh(result.get("mesh") if isinstance(result, dict) else None)
        body = json.dumps(
            {"model_id": model_id, "version": actual_version, "mesh": mesh},
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise HTTPException(413, "mesh response exceeds the viewport size limit")
        return body

    body = cache.get_or_build(key, build, force=force)
    return Response(body, media_type="application/json", headers={"Cache-Control": "no-store"})
