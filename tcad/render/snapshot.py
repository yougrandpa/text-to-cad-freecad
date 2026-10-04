"""Render a pinned artifact scene into a disposable, isolated image cache."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from tcad.core.ids import contained_path, ensure_safe_id

VIEWS = frozenset({"iso", "front", "top", "right"})
STYLES = frozenset({"flat", "flat_edges", "edges_only"})


def snapshot_dir(data_dir, artifact_id, model_id, key):
    from tcad.inspect.artifact import ArtifactReader

    ensure_safe_id(model_id, kind="model_id")
    ensure_safe_id(key, kind="snapshot key")
    reader = ArtifactReader(data_dir)
    reader.object_dir(artifact_id)  # validate the identity before path use
    return contained_path(data_dir, "derived", "snapshots", artifact_id.split(":")[1], model_id, key)


def render_snapshot(reader, manifest, root, renderer, *, views, style="flat_edges",
                    width=768, height=576, angle=0, frame=0, force=False):
    if not views or any(view not in VIEWS for view in views):
        raise ValueError("unknown snapshot view; expected iso/front/top/right")
    if style not in STYLES:
        raise ValueError("unknown snapshot style")
    if not 16 <= width <= 4096 or not 16 <= height <= 4096:
        raise ValueError("width/height must be within [16, 4096]")
    scene = reader.scene(manifest, root)
    mesh = scene.posed_mesh(angle=angle, frame=frame)
    settings = {"renderer": 1, "style": style, "width": width, "height": height,
                "angle": angle, "frame": frame, "supersample": getattr(renderer, "supersample", 2)}
    key = hashlib.sha256(json.dumps(settings, sort_keys=True, allow_nan=False).encode()).hexdigest()
    destination = snapshot_dir(reader.data_dir, manifest.artifact_id, manifest.model_id, key)
    destination.mkdir(parents=True, exist_ok=True)
    from tcad.core.types import ImageRef
    from tcad.core.wiring import estimate_image_tokens

    images = []
    for view in views:
        target = destination / f"{view}.png"
        if not target.is_file() or force:
            # Each renderer invocation owns scratch filenames. Concurrent
            # snapshots cannot overwrite one another's partially written PNG.
            with tempfile.TemporaryDirectory(prefix=".render-", dir=destination) as scratch:
                produced = renderer.render(mesh,
                    out_dir=scratch, views=[view], style=style, width=width, height=height)
                if not produced:
                    raise ValueError("renderer produced no snapshot")
                source = contained_path(scratch, Path(produced[0].path).name)
                candidate = Path(scratch) / "complete.png"
                shutil.copyfile(source, candidate)
                candidate.replace(target)
        images.append(ImageRef(path=str(target), view=view, width=width, height=height,
                               tokens_estimate=estimate_image_tokens(width, height)))
    return images
