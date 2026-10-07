"""The immutable scene shared by interactive viewers and snapshots."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tcad.core.limits import MAX_SCENE_FACETS, MAX_SCENE_VERTICES
from tcad.core.types import BBox, Mesh
from tcad.render.picking import PickMapping
from tcad.ir.animation import pose_frame
from tcad.ir.motion import pose_vertices
from tcad.ir.schema import RotaryMotionSpec

__all__ = ["MAX_SCENE_FACETS", "MAX_SCENE_VERTICES", "SceneMotion", "SceneModel",
           "ScenePreview", "preview_from_build", "render_preview_note"]


class SceneMotion(RotaryMotionSpec):
    body_id: str
    vertex_start: int = Field(ge=0)
    vertex_count: int = Field(gt=0)


class ScenePreview(BaseModel):
    """How the saved preview mesh relates to the CAD result it renders.

    ``ok`` means the base tessellation was published; ``degraded`` means the
    same BRep was re-tessellated at a coarser ``tolerance`` to fit the viewport
    budget; the CAD result is identical either way. There is no
    ``unavailable`` scene: a preview that cannot be published leaves no
    ``scene.json`` at all and says why through the build result instead.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["ok", "degraded"] = "ok"
    tolerance: float = Field(gt=0)
    reason: str = ""
    attempts: list[dict] = Field(default_factory=list)
    bodies: list[dict] = Field(default_factory=list)


class SceneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    mesh: Mesh
    body_ids: list[str] = Field(default_factory=list)
    motion: list[SceneMotion] = Field(default_factory=list)
    animation: dict | None = None
    pick_mapping: PickMapping | None = None
    preview: ScenePreview | None = None

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
                    pick_mapping=result.get("pick_mapping"),
                    preview=ScenePreview.model_validate(result["preview"]) if result.get("preview") else None)
        if assembly is None:
            declared = {b["id"]: b["motion"] for b in ir.get("bodies", []) if b.get("motion")}
            actual = {p.body_id: {k: p.model_dump()[k] for k in ("pivot", "axis", "ratio")}
                      for p in scene.motion}
            if actual != declared:
                raise ValueError("scene motion does not match the build input")
        return scene

    def mesh_for_render(self) -> Mesh:
        if self.pick_mapping is None:
            return self.mesh
        groups = [-1]*len(self.mesh.facets)
        for index, entity in enumerate(self.pick_mapping.entities):
            if entity.entity_kind == 'face':
                start, count = entity.triangle_start, entity.triangle_count
                groups[start:start+count] = [index]*count
        return self.mesh.model_copy(update={'facet_groups': groups})

    def posed_mesh(self, *, angle: float = 0, frame: int = 0) -> Mesh:
        mesh = self.mesh_for_render()
        if not math.isfinite(angle):
            raise ValueError("driver_angle_deg must be finite")
        if self.animation is None and not angle and not frame:
            return mesh
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
        return mesh.model_copy(update={"vertices": vertices, "bbox": bbox})


# ─── preview publication: never a reason a valid build fails ───────────────
#
# The budget above is a VIEWPORT budget. A build whose preview cannot be
# published is still a build: the FCStd, the STEP, the parametric history and
# the Gate verdict all stand on their own. What must not happen is the two
# being conflated — a model told "build failed" because a triangle count
# crossed a line, then deleting structural features to make a number smaller.
# So preview problems come back as *status*, with the real counts and the
# parts that spent the budget, and the caller continues either way.

_UNAVAILABLE_STAGES = {
    "tessellation": "tessellation (mesh generation from the saved BRep)",
    "scene_validation": "scene validation (declaration cross-check)",
}


def _preview_payload(result: dict) -> dict:
    """Normalize the worker's preview summary into the supervisor's shape."""
    raw = result.get("preview") or {}
    attempts = [a for a in raw.get("attempts") or [] if isinstance(a, dict)]
    last = attempts[-1] if attempts else {}
    return {
        "status": raw.get("status") or "unknown",
        "tolerance": raw.get("tolerance"),
        "reason": raw.get("reason") or "",
        "attempts": attempts,
        "bodies": [b for b in raw.get("bodies") or [] if isinstance(b, dict)],
        "counts": {"vertices": last.get("vertices"), "facets": last.get("facets")},
        "limits": {"vertices": MAX_SCENE_VERTICES, "facets": MAX_SCENE_FACETS},
    }


def preview_from_build(result: dict, ir: dict) -> tuple["SceneModel | None", dict]:
    """Validate a worker preview result; preview problems become status.

    Returns ``(scene, preview)``. ``scene`` is ``None`` whenever the preview
    cannot be published — the caller records ``preview`` and carries on with
    the CAD result. This function never raises for preview-only problems; a
    programmer error in the *caller's* data still surfaces, because that is
    not a preview problem.
    """
    preview = _preview_payload(result)
    if not result.get("mesh") or preview["status"] == "unavailable":
        preview.update({
            "status": "unavailable",
            "stage": "tessellation",
            "reason": preview["reason"] or "worker returned no preview mesh",
        })
        return None, preview
    try:
        scene = SceneModel.from_build(result, ir)
    except ValueError as exc:
        issues = exc.errors() if hasattr(exc, "errors") else []
        message = "; ".join(issue["msg"] for issue in issues) if issues else str(exc)
        preview.update({
            "status": "unavailable",
            "stage": "scene_validation",
            "reason": "preview scene validation failed: " + message,
        })
        return None, preview
    return scene, preview


def render_preview_note(preview: dict | None) -> str:
    """The model-facing sentence for a preview status, or "" when there is none.

    Carries the facts a repair needs and nothing a repair does not: the stage,
    the error class, the actual counts against the limits, the bodies that
    spent the budget, and the recovery options. Deleting structure is never
    one of them — the CAD result is not in question.
    """
    if not preview or preview.get("status") in (None, "ok", "unknown"):
        return ""
    counts = preview.get("counts") or {}
    limits = preview.get("limits") or {}
    bodies = preview.get("bodies") or []
    if preview["status"] == "degraded":
        return (
            f"PREVIEW DEGRADED — the saved preview was regenerated at tolerance "
            f"{preview.get('tolerance')} mm (coarser than the default) so it fits the "
            f"viewport budget ({limits.get('vertices')} vertices / {limits.get('facets')} facets). "
            f"The CAD result, exports and parametric history are unchanged. No repair needed."
        )
    stage = _UNAVAILABLE_STAGES.get(preview.get("stage"), preview.get("stage") or "unknown")
    kind = ("preview_mesh_limit" if preview.get("stage") != "scene_validation"
            else "preview_scene_invalid")
    vertices, facets = counts.get("vertices"), counts.get("facets")
    measured = (f"actual: {vertices} vertices, {facets} facets; " if vertices is not None else "")
    parts = "; ".join(
        f"body {b.get('body_id')} spent {b.get('vertices')} vertices/{b.get('facets')} facets"
        for b in bodies[:3]) or "per-body counts unavailable"
    return (
        "PREVIEW UNAVAILABLE — this is a PREVIEW failure, not a CAD build failure. "
        "The geometry compiled, the exports are valid and the Gate still graded them; "
        "only the viewport scene could not be published.\n"
        f"  stage: {stage}\n"
        f"  type: {kind} ({measured}limit: {limits.get('vertices')} vertices, "
        f"{limits.get('facets')} facets)\n"
        f"  main contributors: {parts}\n"
        f"  reason: {preview.get('reason') or 'mesh exceeds the viewport budget'}\n"
        "  recovery (do NOT delete structural features to shrink a triangle count): "
        "keep the CAD result and exports. Adaptive coarsening already tried the listed "
        "tolerances; repeating an unchanged commit will not fix this limit. Report the "
        "preview diagnostics for renderer repair; visual acceptance remains unresolved "
        "until a preview or external CAD inspection is available."
    )
