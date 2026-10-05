"""Geometry identity, independent of authoring version and Gate policy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def compiler_identity() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "worker").glob("*.py"))
    paths += sorted((root / "ir").glob("*.py"))
    paths += [root / "build" / "components.py", root / "build" / "digest.py",
              root / "build" / "graph.py", root / "render" / "scene.py"]
    return content_hash({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in paths})


def build_digest(ir: dict, *, compiler: str, freecad: object,
                 exports: list[str], assets: dict | None = None) -> str:
    geometry = {k: v for k, v in ir.items()
                if k not in {"model_id", "version", "requirements", "notes"}}
    if assets:
        geometry["bodies"] = [
            {**body, "part_ref": {k: v for k, v in body["part_ref"].items()
                                  if k not in {"model_id", "artifact_id"}}}
            if body.get("part_ref") and body["id"] in assets else body
            for body in ir.get("bodies", [])]
    return "sha256:" + content_hash({"schema": 1, "ir": geometry,
        "compiler": compiler, "freecad": freecad, "exports": sorted(set(exports)),
        "assets": assets or {}})
