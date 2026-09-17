"""Artifact addressing (design §4.4, L4).

The digest and the exported STEP/STL are the artefacts the verification Gate
reads back *from disk* through its independent read path (CQRS, design §4.6).
This module is what makes that path work: it assigns **deterministic**,
**documented** paths so the Gate can find an artefact by ``(model_id, version,
fmt)`` without any shared in-memory state with the loop.

Path layout
-----------
    <data_dir>/artifacts/<model_id>/v<version>/
        model.step          # fmt="step"
        model.stl           # fmt="stl"
        model.brep          # fmt="brep"
        model.FCStd         # fmt="fcstd"
        digest.json         # fmt="digest"  (GeometryDigest)
        view_<name>.png     # fmt="view:<name>"

All paths are pure functions of the arguments — no hidden state, no UUIDs — so
two processes (the compiler writing, the Gate reading) agree on the location.
"""

from __future__ import annotations

import json
from pathlib import Path

from tcad.core.types import GeometryDigest

_DEFAULT_DATA_DIR = "data"

# fmt -> file extension (no dot)
_EXT: dict[str, str] = {
    "step": "step",
    "stl": "stl",
    "brep": "brep",
    "fcstd": "FCStd",
    "digest": "json",
}


class ArtifactStore:
    def __init__(self, data_dir: str | os.PathLike[str] = _DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    # ── directories ───────────────────────────────────────────────────────────────

    def dir_for(self, model_id: str, version: int) -> Path:
        """Directory that holds every artefact for ``(model_id, version)``."""
        return self.data_dir / "artifacts" / model_id / f"v{version}"

    def ensure(self, model_id: str, version: int) -> Path:
        """Create the artefact directory if needed; return it."""
        d = self.dir_for(model_id, version)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── path resolution ────────────────────────────────────────────────────────────

    def path_for(self, model_id: str, version: int, fmt: str) -> Path:
        """Deterministic absolute path for an artefact.

        ``fmt`` is one of ``step|stl|brep|fcstd|digest`` or ``view:<name>`` for a
        rendered PNG. The base filename is always ``model`` (or ``view_<name>``).
        """
        d = self.dir_for(model_id, version)
        if fmt.startswith("view:"):
            name = fmt.split(":", 1)[1] or "iso"
            return d / f"view_{name}.png"
        ext = _EXT.get(fmt)
        if ext is None:
            raise ValueError(
                f"unknown artifact format '{fmt}'; "
                f"expected one of {sorted(_EXT)} or 'view:<name>'")
        return d / f"model.{ext}"

    # ── exports (STEP/STL/BREP/FCStd) ──────────────────────────────────────────────

    def list_exports(self, model_id: str, version: int) -> list[str]:
        """Return the export formats present on disk for this version."""
        d = self.dir_for(model_id, version)
        if not d.is_dir():
            return []
        present: list[str] = []
        for fmt, ext in _EXT.items():
            if fmt == "digest":
                continue
            if (d / f"model.{ext}").exists():
                present.append(fmt)
        return present

    # ── digest (the Gate's independent read) ──────────────────────────────────────

    def write_digest(self, model_id: str, version: int,
                     digest: GeometryDigest | dict) -> Path:
        """Persist a :class:`GeometryDigest` (or its dict form) as ``digest.json``."""
        d = self.ensure(model_id, version)
        path = d / "model.json"  # _EXT["digest"] == "json"
        if isinstance(digest, GeometryDigest):
            data = digest.model_dump_json()
        else:
            data = json.dumps(digest, ensure_ascii=False, indent=2)
        path.write_text(data)
        return path

    def read_digest(self, model_id: str, version: int) -> GeometryDigest | None:
        """Load ``digest.json`` if present, else ``None``."""
        path = self.dir_for(model_id, version) / "model.json"
        if not path.exists():
            return None
        return GeometryDigest.model_validate_json(path.read_text())
