"""Bounded artifact scene responses for the human viewport.

Mesh serialization is cached by immutable artifact identity. Every query reads
the saved scene, including integrity checks; no source or live worker is used.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from tcad.core.ids import InvalidIdentifier, ensure_safe_id
from tcad.core.types import Mesh
from tcad.ir.schema import RotaryMotionSpec

MIN_TOLERANCE = 0.1
MAX_TOLERANCE = 5.0
MAX_VERTICES = 100_000
MAX_FACETS = 200_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Keep preview callers from occupying every shared ASGI worker thread while
# waiting for scene serialization. Admission itself runs on the event loop.
MAX_ACTIVE_MESH_REQUESTS = 4


class PreviewMesh(Mesh):
    """The worker mesh schema, with its explicit allocation counts."""

    vertex_count: int
    facet_count: int


class PreviewMotion(RotaryMotionSpec):
    body_id: str
    vertex_start: int = Field(ge=0)
    vertex_count: int = Field(gt=0)


class MeshPreviewResponse(BaseModel):
    model_id: str
    version: int
    artifact_id: str
    status: str
    mesh: PreviewMesh
    motion: list[PreviewMotion] = Field(default_factory=list)
    animation: dict | None = None


def _checked_animation(result: dict, ir_data: dict, vertex_count: int) -> dict:
    """Validate bounded native poses before sending worker data to the viewport."""
    parts, frames = result.get("parts"), result.get("frames")
    expected = {body["id"] for body in ir_data["bodies"]}
    if not isinstance(parts, list) or not isinstance(frames, list) or not 1 <= len(frames) <= 600:
        raise HTTPException(502, "invalid native animation frames")
    ids, ranges = set(), []
    for part in parts:
        id, start, count = part.get("body_id"), part.get("vertex_start"), part.get("vertex_count")
        if (id not in expected or id in ids or type(start) is not int or type(count) is not int
                or start < 0 or count <= 0 or start+count > vertex_count
                or any(start < hi and start+count > lo for lo,hi in ranges)):
            raise HTTPException(502, "invalid native animation body range")
        ids.add(id)
        ranges.append((start,start+count))
    if ids != expected or sum(hi-lo for lo,hi in ranges) != vertex_count:
        raise HTTPException(502, "incomplete native animation bodies")
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != ids:
            raise HTTPException(502, "incomplete native animation pose")
        for matrix in frame.values():
            if (not isinstance(matrix,list) or len(matrix) != 16
                    or not all(type(v) in (int,float) and math.isfinite(v) for v in matrix)
                    or any(abs(matrix[i]-v) > 1e-6 for i,v in ((12,0),(13,0),(14,0),(15,1)))):
                raise HTTPException(502, "invalid native animation transform")
    spec = ir_data["assembly"]
    if result.get("start") != spec["start"] or result.get("step") != spec["step"]:
        raise HTTPException(502, "animation time does not match declaration")
    return {"parts": parts, "frames": frames, "start": result["start"], "step": result["step"],
            "solver": "FreeCAD Assembly"}


def _checked_motion(raw: Any, ir_data: dict, vertex_count: int) -> list[dict]:
    """Accept only measured ranges corresponding to the stored declarations."""
    declared = {b["id"]: b["motion"] for b in ir_data["bodies"] if b.get("motion")}
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) != len(declared):
        raise HTTPException(502, "worker returned incomplete body motion ranges")
    out, seen, ranges = [], set(), []
    for value in raw:
        try:
            item = PreviewMotion.model_validate(value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(502, "worker returned invalid body motion") from exc
        spec = {k: item.model_dump()[k] for k in ("pivot", "axis", "ratio")}
        start, end = item.vertex_start, item.vertex_start + item.vertex_count
        if (item.body_id in seen or declared.get(item.body_id) != spec or end > vertex_count
                or math.hypot(*item.axis.as_tuple()) <= 1e-12
                or not all(math.isfinite(v) for v in (*item.pivot.as_tuple(), *item.axis.as_tuple()))
                or any(start < hi and end > lo for lo, hi in ranges)):
            raise HTTPException(502, "worker motion does not match the stored model")
        seen.add(item.body_id)
        ranges.append((start, end))
        out.append(item.model_dump(mode="json"))
    return out


class MeshPreviewCache:
    """A small byte-bounded LRU, with one serialization in flight at a time.

    Separate locks keep cached reads available while another model is encoding.
    Coalescing misses here avoids queueing identical
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
    data_dir: str | Path,
    cache: MeshPreviewCache,
    model_id: str,
    *,
    version: int | None,
    tolerance: float,
    force: bool,
    artifact_id: str | None = None,
) -> Response:
    """Encode the saved scene; never consult source IR or drive FreeCAD."""
    from tcad.inspect.artifact import ArtifactReader

    try:
        ensure_safe_id(model_id, kind="model_id")
        reader = ArtifactReader(data_dir)
        manifest, root = reader.resolve(model_id, version, artifact_id)
        # Read and validate before using a cache hit: corrupted evidence must
        # not be hidden behind an earlier successful response.
        scene = reader.scene(manifest, root)
    except InvalidIdentifier as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not math.isclose(tolerance, scene.mesh.tolerance):
        raise HTTPException(409, f"saved scene tolerance is {scene.mesh.tolerance}; requested {tolerance}")
    key = (manifest.artifact_id, 1)

    def build() -> bytes:
        mesh = _checked_mesh(scene.mesh.model_dump(mode="json"))
        payload = {"model_id": manifest.model_id, "version": manifest.ir_version,
                   "artifact_id": manifest.artifact_id, "status": manifest.status.value, "mesh": mesh}
        if scene.motion:
            payload["motion"] = [part.model_dump(mode="json") for part in scene.motion]
        if scene.animation:
            payload["animation"] = scene.animation
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise HTTPException(413, "mesh response exceeds the viewport size limit")
        return body

    body = cache.get_or_build(key, build, force=force)
    return Response(body, media_type="application/json", headers={"Cache-Control": "no-store"})
