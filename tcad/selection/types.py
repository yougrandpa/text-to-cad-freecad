"""Artifact-bound semantic and local geometry references."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SelectionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class SelectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ir_version: int = Field(ge=0)
    body_id: str = Field(min_length=1, max_length=128)
    entity_kind: Literal["body", "sketch", "feature", "face", "edge"]
    sketch_id: str | None = Field(default=None, min_length=1, max_length=128)
    feature_id: str | None = Field(default=None, min_length=1, max_length=128)

    local_sub_id: str | None = Field(default=None, pattern=r"^(Face|Edge)[1-9][0-9]*$")

    @model_validator(mode="after")
    def target_matches_kind(self):
        if (self.sketch_id is not None) != (self.entity_kind == "sketch"):
            raise ValueError("sketch_id is required only for sketches")
        if (self.feature_id is not None) != (self.entity_kind == "feature"):
            raise ValueError("feature_id is required only for features")
        if (self.local_sub_id is not None) != (self.entity_kind in {"face", "edge"}):
            raise ValueError("local_sub_id is required only for geometry")
        if self.local_sub_id and not self.local_sub_id.startswith(self.entity_kind.title()):
            raise ValueError("local_sub_id does not match entity kind")
        return self

    @property
    def target_id(self) -> str:
        return self.local_sub_id or self.sketch_id or self.feature_id or self.body_id


class SelectionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    selection_refs: list[SelectionRef] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def one_publication(self):
        refs = self.selection_refs
        if len({(r.model_id, r.artifact_id, r.ir_version) for r in refs}) != 1:
            raise ValueError("references must come from one model publication")
        if len({(r.body_id, r.entity_kind, r.target_id) for r in refs}) != len(refs):
            raise ValueError("duplicate reference")
        return self


class ResolvedSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: SelectionRef
    label: str
    editable: bool
    capabilities: list[str]
    hole_sketch_id: str | None = None
    evidence: dict = Field(default_factory=dict)
