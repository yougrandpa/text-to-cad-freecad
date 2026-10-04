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
            raise ArtifactReadError("a version or artifact_id is required")
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
        if manifest.status == ArtifactStatus.VERIFIED:
            source = self.read_file(manifest, root, "ir.json")
            if hashlib.sha256(source).hexdigest() != manifest.ir_sha256:
                raise ArtifactReadError("artifact source does not match its build input hash")
            self.gate_report(manifest, root)
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

    def gate_report(self, manifest: ArtifactSet, root: Path) -> GateReport:
        try:
            report = GateReport.model_validate_json(self.read_file(manifest, root, "gate_report.json"))
        except ValueError as exc:
            raise ArtifactReadError(f"invalid artifact Gate report: {exc}") from exc
        if (report.model_id != manifest.model_id or report.ir_version != manifest.ir_version
                or report.attempt_id != manifest.attempt_id or report.ir_sha256 != manifest.ir_sha256
                or (manifest.status == ArtifactStatus.VERIFIED and not report.passed)):
            raise ArtifactReadError("artifact Gate report does not attest this build")
        return report
