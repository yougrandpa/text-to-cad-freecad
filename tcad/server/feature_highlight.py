"""Bounded, cached outlines for tree selections in an immutable artifact."""

import json
import math
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response

from tcad.inspect.artifact import ArtifactReader
from tcad.server.mesh import MeshPreviewCache


def feature_highlight(data_dir: str | Path, cache: MeshPreviewCache, worker: Any, model_id: str,
                      artifact_id: str, body_id: str, kind: str, node_id: str) -> Response:
    if kind not in ("sketch", "feature"):
        raise HTTPException(400, "selection kind must be sketch or feature")
    reader = ArtifactReader(data_dir)
    try:
        manifest, root = reader.resolve(model_id, artifact_id=artifact_id)
        ir = json.loads(reader.read_file(manifest, root, "ir.json"))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    body = next((item for item in ir["bodies"] if item["id"] == body_id), None)
    definitions = body.get("sketches" if kind == "sketch" else "features", []) if body else []
    if not any(item["id"] == node_id for item in definitions):
        raise HTTPException(404, "selected node is absent from this artifact")
    filename = next((name for name in manifest.files if name.lower().endswith(".fcstd")), None)
    if filename is None:
        raise HTTPException(409, "artifact has no saved FreeCAD document")

    def build():
        result = worker.request("read_feature_highlight", {"fcstd_path": str(root / filename),
            "body": body, "kind": kind, "node_id": node_id, "tolerance": 0.1}, timeout_s=30)
        if not result.get("ok"):
            raise HTTPException(502, "selected geometry could not be read from the saved document")
        vertices = result.get("result", {}).get("vertices")
        if (not isinstance(vertices, list) or len(vertices) > 100_000 or len(vertices) % 2
                or any(not isinstance(p, list) or len(p) != 3 or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in vertices)):
            raise HTTPException(502, "invalid selected geometry")
        return json.dumps({"artifact_id": artifact_id, "body_id": body_id, "entity_kind": kind,
            "node_id": node_id, "vertices": vertices}, allow_nan=False, separators=(",", ":")).encode()

    raw = cache.get_or_build(("feature_highlight", artifact_id, body_id, kind, node_id), build)
    return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})
