"""The on-disk contract shared by verification and artifact queries."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactStatus(str, Enum):
    BUILDING = "building"
    DRAFT = "draft"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"


class ArtifactFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class ArtifactSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    ir_version: int = Field(ge=0)
    attempt_id: str = Field(min_length=1)
    ir_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ArtifactStatus
    files: dict[str, ArtifactFile]

    @model_validator(mode="after")
    def check_identity(self) -> "ArtifactSet":
        for name in self.files:
            if not name or name in {".", "..", "manifest.json"} or "/" in name or "\\" in name:
                raise ValueError("artifact file names must be single path components")
        expected = artifact_identity(
            self.model_id, self.ir_version, self.attempt_id, self.ir_sha256,
            {name: item.model_dump() for name, item in self.files.items()},
        )
        if self.artifact_id != expected:
            raise ValueError("artifact identity does not match the manifest")
        if self.status == ArtifactStatus.VERIFIED:
            required = {"ir.json", "digest.json", "gate_report.json", "build_stamp.json"}
            if not required <= self.files.keys():
                raise ValueError("verified artifacts require source, measurements, stamp and Gate report")
            if not any(name.lower().endswith((".step", ".stl", ".brep", ".fcstd")) for name in self.files):
                raise ValueError("verified artifacts require a geometry export")
        return self


class AttemptArtifact(ArtifactSet):
    status: Literal[ArtifactStatus.DRAFT, ArtifactStatus.VERIFYING, ArtifactStatus.FAILED]


class PublishedArtifact(ArtifactSet):
    status: Literal[ArtifactStatus.VERIFIED] = ArtifactStatus.VERIFIED


def artifact_identity(model_id: str, version: int, attempt_id: str,
                      ir_sha256: str, files: dict) -> str:
    """Bind identity to the build inputs and every indexed output byte.

    This is an artifact identity, not the reusable BuildDigest planned for the
    cache phase: different attempts deliberately retain distinct provenance.
    """
    payload = {"model_id": model_id, "ir_version": version,
               "attempt_id": attempt_id, "ir_sha256": ir_sha256, "files": files}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
