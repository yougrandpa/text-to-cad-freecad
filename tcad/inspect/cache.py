"""Rebuildable scene, mesh, bounds and topology indices from artifact evidence."""

import hashlib
import json
import tempfile
from pathlib import Path

from tcad.build.digest import canonical_bytes, content_hash
from tcad.core.ids import contained_path


class InspectionCache:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "derived" / "inspect"

    def query(self, reader, manifest, directory, kind):
        if kind not in {"scene", "mesh", "bounds", "topology"}:
            raise ValueError("unknown inspection index")
        # Always verify authoritative evidence before trusting any cached index.
        scene = reader.scene(manifest, directory)
        digest = reader.digest(manifest, directory)
        measurements = digest.model_dump(mode="json")
        identity = content_hash({"schema": 1, "scene": manifest.files["scene.json"].sha256,
                                 "measurements": manifest.files["digest.json"].sha256})
        values = {"scene": scene.model_dump(mode="json"), "mesh": scene.mesh.model_dump(mode="json"),
                  "bounds": digest.bbox.model_dump(mode="json"),
                  "topology": {"counts": digest.topology.model_dump(mode="json"),
                               "faces": measurements["faces"], "edges": measurements["edges"], "holes": measurements["holes"]}}
        expected = canonical_bytes(values[kind])
        path = contained_path(self.root, identity, kind + ".json")
        try:
            if path.read_bytes() == expected:
                return values[kind]
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(expected)
            candidate = Path(temporary.name)
        try:
            candidate.replace(path)
        finally:
            candidate.unlink(missing_ok=True)
        return values[kind]
