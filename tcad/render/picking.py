"""Validate complete, bounded topology maps against exactly one scene mesh."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PickPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body_id: str
    vertex_start: int = Field(ge=0)
    vertex_count: int = Field(gt=0)


class PickEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body_id: str
    entity_kind: Literal["face", "edge"]
    local_sub_id: str = Field(pattern=r"^(Face|Edge)[1-9][0-9]*$")
    triangle_start: int = Field(ge=0)
    triangle_count: int = Field(ge=0)
    segments: list[list[int]] = Field(max_length=200_000)


class PickMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    generator: Literal["freecad-face-mesh-v1"]
    mesh_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parts: list[PickPart] = Field(max_length=1000)
    entities: list[PickEntity] = Field(max_length=20_000)

    def check(self, mesh, body_ids):
        encoded = json.dumps([mesh.vertices, mesh.facets], allow_nan=False, separators=(",", ":")).encode()
        if self.mesh_digest != "sha256:" + hashlib.sha256(encoded).hexdigest():
            raise ValueError("pick mapping mesh identity mismatch")
        parts = {p.body_id: p for p in self.parts}
        if len(parts) != len(self.parts) or set(parts) != set(body_ids):
            raise ValueError("pick mapping body identities mismatch")
        cursor = 0
        for part in self.parts:
            if part.vertex_start != cursor:
                raise ValueError("pick mapping body ranges must partition vertices")
            cursor += part.vertex_count
        if cursor != len(mesh.vertices):
            raise ValueError("pick mapping body ranges are incomplete")
        covered, seen, segments = 0, set(), 0
        for entity in self.entities:
            key = (entity.body_id, entity.local_sub_id)
            part = parts.get(entity.body_id)
            if part is None or key in seen or not entity.local_sub_id.startswith(entity.entity_kind.title()):
                raise ValueError("pick mapping entity identity mismatch")
            seen.add(key)
            lo, hi = part.vertex_start, part.vertex_start + part.vertex_count
            if entity.entity_kind == "face":
                if entity.segments or entity.triangle_start != covered:
                    raise ValueError("pick mapping face ranges must partition facets")
                covered += entity.triangle_count
                if covered > len(mesh.facets) or any(i < lo or i >= hi
                        for face in mesh.facets[entity.triangle_start:covered] for i in face):
                    raise ValueError("pick mapping face crosses body boundary")
            else:
                if entity.triangle_start or entity.triangle_count or not entity.segments:
                    raise ValueError("pick mapping edge is invalid")
                segments += len(entity.segments)
                if segments > 200_000 or any(len(s) != 2 or s[0] == s[1] or any(i < lo or i >= hi for i in s)
                                           for s in entity.segments):
                    raise ValueError("pick mapping edge crosses body boundary")
        if covered != len(mesh.facets):
            raise ValueError("pick mapping facets are incomplete")
