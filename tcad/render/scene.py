"""The immutable scene shared by interactive viewers and snapshots."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tcad.core.types import BBox, Mesh
from tcad.render.picking import PickMapping
from tcad.ir.animation import pose_frame
from tcad.ir.motion import pose_vertices
from tcad.ir.schema import RotaryMotionSpec

MAX_SCENE_VERTICES = 100_000
MAX_SCENE_FACETS = 200_000


class SceneMotion(RotaryMotionSpec):
    body_id: str
    vertex_start: int = Field(ge=0)
    vertex_count: int = Field(gt=0)


class SceneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    mesh: Mesh
    body_ids: list[str] = Field(default_factory=list)
    motion: list[SceneMotion] = Field(default_factory=list)
    animation: dict | None = None
    pick_mapping: PickMapping | None = None

    @model_validator(mode="before")
    @classmethod
    def bound_arrays(cls, value):
        if isinstance(value, dict) and isinstance(value.get("mesh"), dict):
            mesh = value["mesh"]
            if (len(mesh.get("vertices") or []) > MAX_SCENE_VERTICES
                    or len(mesh.get("facets") or []) > MAX_SCENE_FACETS):
                raise ValueError("scene mesh exceeds allocation limits")
        return value

    @model_validator(mode="after")
    def check_geometry(self) -> "SceneModel":
        mesh = self.mesh
        if not mesh.vertices or not mesh.facets or mesh.volume <= 0:
            raise ValueError("scene has no positive-volume solid geometry")
        if (not all(math.isfinite(v) for p in mesh.vertices for v in p)
                or not all(math.isfinite(v) for v in mesh.bbox.model_dump().values())
                or not math.isfinite(mesh.volume) or not math.isfinite(mesh.tolerance)
                or mesh.tolerance <= 0):
            raise ValueError("scene contains invalid numeric geometry")
        if any(len(set(face)) != 3 or any(i < 0 or i >= len(mesh.vertices) for i in face)
               for face in mesh.facets):
            raise ValueError("scene contains invalid mesh facets")
        if len(set(self.body_ids)) != len(self.body_ids):
            raise ValueError("scene has duplicate body identities")
        seen, ranges = set(), []
        for part in self.motion:
            start, end = part.vertex_start, part.vertex_start + part.vertex_count
            if (part.body_id not in self.body_ids or part.body_id in seen or end > len(mesh.vertices)
                    or math.hypot(*part.axis.as_tuple()) <= 1e-12
                    or not all(math.isfinite(v) for v in (*part.pivot.as_tuple(), *part.axis.as_tuple(), part.ratio))
                    or any(start < hi and end > lo for lo, hi in ranges)):
                raise ValueError("scene has invalid body motion ranges")
            seen.add(part.body_id)
            ranges.append((start, end))
        if self.animation is not None:
            if self.motion:
                raise ValueError("a scene cannot mix prescribed and native motion")
            self._check_animation()
        if self.pick_mapping is not None:
            self.pick_mapping.check(mesh, self.body_ids)
            for part in self.motion or (self.animation or {}).get("parts", []):
                raw = part.model_dump() if hasattr(part, "model_dump") else part
                if not any(p.model_dump() == {k: raw[k] for k in ("body_id", "vertex_start", "vertex_count")}
                           for p in self.pick_mapping.parts):
                    raise ValueError("pick mapping disagrees with pose ranges")
        return self

    def _check_animation(self) -> None:
        animation = self.animation
        parts, frames = animation.get("parts"), animation.get("frames")
        if not isinstance(parts, list) or not isinstance(frames, list) or not 1 <= len(frames) <= 600:
            raise ValueError("scene has invalid native animation frames")
        ids, ranges = set(), []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("scene has invalid native part")
            body, start, count = part.get("body_id"), part.get("vertex_start"), part.get("vertex_count")
            if (body not in self.body_ids or body in ids or type(start) is not int or type(count) is not int
                    or start < 0 or count <= 0 or start + count > len(self.mesh.vertices)
                    or any(start < hi and start + count > lo for lo, hi in ranges)):
                raise ValueError("scene has invalid native body ranges")
            ids.add(body)
            ranges.append((start, start + count))
        if ids != set(self.body_ids) or sum(hi - lo for lo, hi in ranges) != len(self.mesh.vertices):
            raise ValueError("scene has incomplete native bodies")
        for frame in frames:
            if not isinstance(frame, dict) or set(frame) != ids:
                raise ValueError("scene has incomplete native poses")
            for matrix in frame.values():
                if (not isinstance(matrix, list) or len(matrix) != 16
                        or not all(type(v) in (int, float) and math.isfinite(v) for v in matrix)
                        or any(abs(matrix[i] - v) > 1e-6 for i, v in ((12, 0), (13, 0), (14, 0), (15, 1)))):
                    raise ValueError("scene has invalid native transforms")
        if (not all(type(animation.get(k)) in (int, float) and math.isfinite(animation[k])
                    for k in ("start", "step")) or animation["step"] <= 0):
            raise ValueError("scene has invalid animation timing")

    @classmethod
    def from_build(cls, result: dict, ir: dict) -> "SceneModel":
        """Validate authoring declarations once, while producing the artifact."""
        assembly = ir.get("assembly")
        animation = None
        if assembly is not None:
            if result.get("start") != assembly["start"] or result.get("step") != assembly["step"]:
                raise ValueError("scene animation timing does not match the build input")
            animation = {k: result[k] for k in ("parts", "frames", "start", "step", "solver")}
            animation.update({k: result[k] for k in ('scope','suspension_angles_deg','max_swing_deg') if k in result})
        scene = cls(mesh=result["mesh"], body_ids=[body["id"] for body in ir.get("bodies", [])],
                    motion=[] if assembly else result.get("motion", []), animation=animation,
                    pick_mapping=result.get("pick_mapping"))
        if assembly is None:
            declared = {b["id"]: b["motion"] for b in ir.get("bodies", []) if b.get("motion")}
            actual = {p.body_id: {k: p.model_dump()[k] for k in ("pivot", "axis", "ratio")}
                      for p in scene.motion}
            if actual != declared:
                raise ValueError("scene motion does not match the build input")
        return scene

    def posed_mesh(self, *, angle: float = 0, frame: int = 0) -> Mesh:
        if not math.isfinite(angle):
            raise ValueError("driver_angle_deg must be finite")
        if self.animation is None and not angle and not frame:
            return self.mesh
        if self.animation is not None:
            if angle:
                raise ValueError("native scenes use frame_index, not driver_angle_deg")
            if not 0 <= frame < len(self.animation["frames"]):
                raise ValueError("frame_index is outside the saved animation")
            vertices = pose_frame(self.mesh.vertices, self.animation["parts"], self.animation["frames"][frame])
        else:
            if frame:
                raise ValueError("this scene has no native animation frames")
            vertices = pose_vertices(self.mesh.vertices, [p.model_dump() for p in self.motion], angle)
        lows = [min(p[j] for p in vertices) for j in range(3)]
        highs = [max(p[j] for p in vertices) for j in range(3)]
        bbox = BBox(**dict(zip(("x", "y", "z", "x_min", "y_min", "z_min"),
                              [highs[j] - lows[j] for j in range(3)] + lows)))
        return self.mesh.model_copy(update={"vertices": vertices, "bbox": bbox})
