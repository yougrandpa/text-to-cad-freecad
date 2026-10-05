"""Disk-only artifact queries. No IR store or worker dependency."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tcad.artifacts.manifest import ArtifactSet, ArtifactStatus
from tcad.core.ids import contained_path
from tcad.core.types import GateReport, GeometryDigest
from tcad.store.artifacts import ArtifactStore


class ArtifactReadError(ValueError):
    """The requested build cannot provide trustworthy artifact evidence."""


class ArtifactReader:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def object_dir(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_id):
            raise ArtifactReadError("invalid artifact_id; expected sha256:<64 hex digits>")
        return contained_path(self.data_dir, "artifact_sets", artifact_id.split(":")[1])

    def resolve(self, model_id: str, version: int | None = None,
                artifact_id: str | None = None) -> tuple[ArtifactSet, Path]:
        if artifact_id is not None:
            root = self.object_dir(artifact_id)
        elif version is not None:
            root = ArtifactStore(self.data_dir).dir_for(model_id, version)
        else:
            # Latest published artifact is a query-side index. Never consult
            # current IR: a pending edit must not change the displayed build.
            model_root = ArtifactStore(self.data_dir).dir_for(model_id, 0).parent
            versions = sorted((int(p.name[1:]) for p in model_root.glob("v*")
                               if p.name[1:].isdigit() and (p / "manifest.json").is_file()), reverse=True)
            if not versions:
                raise FileNotFoundError("no published artifact; call ir_commit first")
            root = ArtifactStore(self.data_dir).dir_for(model_id, versions[0])
        try:
            manifest = ArtifactSet.model_validate_json((root / "manifest.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError("no artifact for this build; call ir_commit first") from exc
        except (OSError, ValueError) as exc:
            raise ArtifactReadError("artifact manifest is unreadable or invalid") from exc
        if manifest.model_id != model_id or (version is not None and manifest.ir_version != version):
            raise ArtifactReadError("artifact model/version does not match the request")
        if artifact_id is not None and manifest.artifact_id != artifact_id:
            raise ArtifactReadError("artifact identity does not match the request")
        for name in manifest.files:
            self.read_file(manifest, root, name)
        if "ir.json" in manifest.files:
            source = self.read_file(manifest, root, "ir.json")
            if hashlib.sha256(source).hexdigest() != manifest.ir_sha256:
                raise ArtifactReadError("artifact source does not match its build input hash")
        if "gate_report.json" in manifest.files:
            self.gate_report(manifest, root)
        # Version directories are compatibility aliases. Render from the
        # retained immutable set when present, so concurrent retries cannot
        # replace scene bytes between manifest and geometry reads.
        if artifact_id is None:
            retained = self.object_dir(manifest.artifact_id)
            if retained.is_dir():
                return self.resolve(model_id, version, manifest.artifact_id)
        return manifest, root

    def read_file(self, manifest: ArtifactSet, root: Path, name: str) -> bytes:
        item = manifest.files.get(name)
        if item is None:
            raise ArtifactReadError(f"artifact has no {name}")
        try:
            path = contained_path(root, name)
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ArtifactReadError(f"cannot read artifact file {name}") from exc
        if len(raw) != item.bytes or hashlib.sha256(raw).hexdigest() != item.sha256:
            raise ArtifactReadError(f"artifact file {name} failed its integrity check")
        return raw

    def digest(self, manifest: ArtifactSet, root: Path) -> GeometryDigest:
        try:
            digest = GeometryDigest.model_validate_json(self.read_file(manifest, root, "digest.json"))
        except ValueError as exc:
            raise ArtifactReadError(f"invalid artifact measurements: {exc}") from exc
        if digest.model_id != manifest.model_id or digest.ir_version != manifest.ir_version:
            raise ArtifactReadError("artifact measurements belong to a different model/version")
        return digest

    def scene(self, manifest: ArtifactSet, root: Path):
        from tcad.render.scene import SceneModel

        try:
            scene = SceneModel.model_validate_json(self.read_file(manifest, root, "scene.json"))
        except (ValueError, TypeError) as exc:
            raise ArtifactReadError(f"invalid artifact scene: {exc}; commit again to generate a scene") from exc
        digest = self.digest(manifest, root)
        if (abs(scene.mesh.volume - digest.volume) > max(1e-6, abs(digest.volume) * 1e-6)
                or any(abs(getattr(scene.mesh.bbox, k) - getattr(digest.bbox, k)) > 1e-5
                       for k in ("x", "y", "z", "x_min", "y_min", "z_min"))):
            raise ArtifactReadError("artifact scene geometry disagrees with its measurements")
        return scene

    def gate_report(self, manifest: ArtifactSet, root: Path) -> GateReport:
        try:
            report = GateReport.model_validate_json(self.read_file(manifest, root, "gate_report.json"))
        except ValueError as exc:
            raise ArtifactReadError(f"invalid artifact Gate report: {exc}") from exc
        if (report.model_id != manifest.model_id or report.ir_version != manifest.ir_version
                or report.attempt_id != manifest.attempt_id or report.ir_sha256 != manifest.ir_sha256
                or (manifest.status == ArtifactStatus.VERIFIED and not report.passed)
                or (manifest.status == ArtifactStatus.FAILED and report.passed)):
            raise ArtifactReadError("artifact Gate report does not attest this build")
        return report
