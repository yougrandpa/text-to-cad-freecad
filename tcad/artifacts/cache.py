"""Disposable geometry cache. No Gate verdict or project state is reused."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

from tcad.build.digest import canonical_bytes
from tcad.core.ids import contained_path, ensure_safe_id

_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


class GeometryCache:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "cache" / "geometry"

    def directory(self, digest):
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid build digest")
        return contained_path(self.root, digest[7:])

    @contextmanager
    def lock(self, digest):
        key = str(self.directory(digest).resolve())
        with _LOCKS_GUARD:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = _LOCKS[key] = threading.RLock()
        # A parent build may wait for child builds on other threads. Unrelated
        # digests must never share a lock, even when their hash prefixes collide.
        from tcad.build.pool import check_cancelled
        while not lock.acquire(timeout=0.1):
            check_cancelled()
        try:
            check_cancelled()
            yield
        finally:
            lock.release()

    def restore(self, digest, target, model_id, version):
        root = self.directory(digest)
        try:
            index = json.loads((root / "cache.json").read_bytes())
            if index["digest"] != digest:
                return False
            ensure_safe_id(index["model_id"], kind="cached model_id")
            if not isinstance(index["files"], dict):
                return False
            for name in index["files"]:
                if (not isinstance(name, str) or not name or name in {".", ".."}
                        or "/" in name or "\\" in name):
                    return False
            files = {name: contained_path(root, name).read_bytes() for name in index["files"]}
            if any(hashlib.sha256(raw).hexdigest() != index["files"][name] for name, raw in files.items()):
                return False
            # Validate before writing any bytes into a fresh attempt.
            from tcad.core.types import GeometryDigest
            from tcad.render.scene import SceneModel
            measurements = GeometryDigest.model_validate_json(files["digest.json"])
            SceneModel.model_validate_json(files["scene.json"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        destination = Path(target)
        for name, raw in files.items():
            if name == "digest.json":
                measurements.model_id, measurements.ir_version = model_id, version
                lines = measurements.text.splitlines()
                if lines:
                    lines[0] = f"model: {model_id} (v{version})"
                    measurements.text = "\n".join(lines)
                raw = measurements.model_dump_json().encode()
            elif name.startswith(index["model_id"] + "."):
                name = model_id + name[len(index["model_id"]):]
            contained_path(destination, name).write_bytes(raw)
        return True

    def store(self, digest, source, model_id):
        root = self.directory(digest)
        root.parent.mkdir(parents=True, exist_ok=True)
        names = [p.name for p in Path(source).iterdir()
                 if p.is_file() and (p.suffix.lower() in {".fcstd", ".step", ".stl", ".brep"}
                                    or p.name in {"digest.json", "scene.json", "components.json"})
                 and p.name != "roundtrip.step"]
        if not {"digest.json", "scene.json", model_id + ".FCStd"} <= set(names):
            return
        with tempfile.TemporaryDirectory(prefix=".cache-", dir=root.parent) as scratch:
            candidate = Path(scratch) / "entry"
            candidate.mkdir()
            hashes = {}
            for name in names:
                raw = contained_path(source, name).read_bytes()
                (candidate / name).write_bytes(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            (candidate / "cache.json").write_bytes(canonical_bytes({
                "digest": digest, "model_id": model_id, "files": hashes}))
            # Invalid entries are disposable; replace while holding the digest lock.
            if root.exists():
                shutil.rmtree(root)
            candidate.replace(root)
