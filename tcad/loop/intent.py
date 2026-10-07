"""Design intent: the short part plan, and what recovery did to it.

A model that "recovers" by simplifying the design is cheaper to detect than to
prevent. What must not happen is the simplification passing silently as a
valid recovery: the curved backrest quietly becomes a flat panel, every build
is green, and the user learns about it from the render.

So the design carries a short PART PLAN — one entry per part, with its goal
and whether the USER required it or the model chose it — recorded when the
design starts and kept true as it evolves. At every commit the plan is checked
against the built IR:

  * a planned part that is not in the geometry is LOST;
  * a user-required part the model marks simplified/dropped is a DESIGN
    DEGRADATION — recorded, surfaced, and reflected in the completion review;
  * a model-origin part that changed is just a change.

And because "a new IR version" is not a synonym for progress, the recorded
gate reports are aggregated across versions: the same blocking failure class
recurring after several edits is reported by name and count, so the next
repair starts from evidence instead of optimism.

This module never changes a verdict. Geometry validity, visual conformance
and physical performance remain separate acceptance items.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tcad.core.ids import contained_path, ensure_safe_id


class PlannedPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
    goal: str = Field(min_length=1, max_length=240)
    #: "user" = explicit in the request; "model" = the model's own choice.
    origin: Literal["user", "model"] = "model"
    status: Literal["planned", "simplified", "dropped"] = "planned"
    note: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def note_required_for_user_reduction(self):
        if self.status != "planned" and self.origin == "user" and not self.note.strip():
            raise ValueError(
                f"part {self.id!r} is user-required and {self.status}; say in `note` "
                "what changed and why")
        return self


class IntentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    recorded_at: float = Field(default_factory=time.time)
    recorded_version: int = 0
    parts: list[PlannedPart] = Field(min_length=1, max_length=40)


class PlanCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[PlannedPart] = Field(min_length=1, max_length=40)
    reason: str = Field(default="auto: model-authored part plan")


# ─── storage (beside the IR snapshots, not inside the artifact directory) ──


def _plan_path(data_dir, model_id: str) -> Path:
    ensure_safe_id(model_id, kind="model_id")
    root = Path(data_dir) / "intents"
    root.mkdir(parents=True, exist_ok=True)
    return contained_path(root, model_id + ".json")


def write_plan(data_dir, model_id: str, plan: IntentPlan) -> Path:
    path = _plan_path(data_dir, model_id)
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    return path


def read_plan(data_dir, model_id: str) -> IntentPlan | None:
    path = _plan_path(data_dir, model_id)
    if not path.is_file():
        return None
    try:
        return IntentPlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ─── plan vs. geometry ────────────────────────────────────────────────────

_CURVE_WORDS = ("弧", "曲", "圆", "椭", "扇", "弯", "curved", "curve", "arc",
                "round", "loft", "spline", "circular", "spherical")
_CURVED_OPS = frozenset({
    "additive_loft", "subtractive_loft", "revolution", "groove",
    "additive_cylinder", "subtractive_cylinder", "additive_sphere",
    "subtractive_sphere", "additive_cone", "subtractive_cone",
})
_CURVED_SKETCH_KINDS = frozenset({"ellipse", "bspline", "arc"})


def _ir_index(ir) -> dict[str, tuple]:
    """token -> (body, matched) so a part id can be matched deterministically."""
    index: dict[str, tuple] = {}
    for body in getattr(ir, "bodies", []):
        for token in {body.id, body.name}:
            if token:
                index.setdefault(token, (body, body))
        for feature in body.features:
            if feature.recipe_id and not feature.suppress and feature.op != 'datum_plane':
                index.setdefault(feature.recipe_id, (body, feature))
            for token in {feature.id, feature.name}:
                if token:
                    index.setdefault(token, (body, feature))
        for sketch in body.sketches:
            for token in {sketch.id, sketch.name}:
                if token:
                    index.setdefault(token, (body, sketch))
    return index


def _curvature_without_curves(part: PlannedPart, match, ir) -> bool:
    """Advisory: the goal asks for curvature, the built features have none.

    Only a flag, never a verdict — a straight approximation of a curved goal
    is exactly the "backrest became a flat panel" degradation, and it is
    cheap to point at; the task review still decides.
    """
    goal = part.goal.lower()
    if not any(word in goal for word in _CURVE_WORDS):
        return False
    body, _matched = match
    for feature in body.features:
        if feature.op in _CURVED_OPS:
            return False
    for sketch in body.sketches:
        if any(geom.kind in _CURVED_SKETCH_KINDS for geom in sketch.geometry):
            return False
    return True


def review_intent(plan: IntentPlan, ir) -> dict:
    """Classify every planned part against the built document."""
    index = _ir_index(ir)
    kept, lost, reduced, degraded, advisories = [], [], [], [], []
    for part in plan.parts:
        match = index.get(part.id)
        if part.status == "planned" and match is None:
            state = "lost"
        elif part.status == "planned":
            state = "kept"
        else:
            state = part.status  # simplified / dropped: an acknowledged change
        entry = {"id": part.id, "goal": part.goal, "origin": part.origin,
                 "state": state, "note": part.note}
        if state == "kept":
            kept.append(entry)
            if _curvature_without_curves(part, match, ir):
                advisories.append(
                    f"part {part.id!r} plans curvature ({part.goal!r}) but its features are "
                    "straight primitives — verify visually, or mark it simplified")
        elif state == "lost":
            lost.append(entry)
        else:
            reduced.append(entry)
        if part.origin == "user" and state in ("lost", "simplified", "dropped"):
            degraded.append(entry)
    return {"kept": kept, "lost": lost, "reduced": reduced,
            "degraded": degraded, "advisories": advisories}


def degradation_items(data_dir, model_id, ir) -> list[str]:
    """Remaining-work lines for the completion review (empty when clean)."""
    plan = read_plan(data_dir, model_id)
    if plan is None:
        return []
    review = review_intent(plan, ir)
    items = []
    for entry in review["degraded"]:
        items.append(
            f"设计退化：用户要求的部件 {entry['id']!r}（{entry['goal']}）当前状态为 "
            f"{entry['state']}" + (f"（{entry['note']}）" if entry["note"] else "")
            + "；继续完善或保持待审草稿。")
    for entry in review["lost"]:
        if entry["origin"] != "user":
            continue
        items.append(
            f"设计退化：计划中的用户部件 {entry['id']!r}（{entry['goal']}）在几何中没有对应实体。")
    items.extend(f"需要复核：{note}" for note in review["advisories"])
    return items


# ─── failure history across versions ──────────────────────────────────────


def _class_of(failure: str) -> str:
    """The check identity of a blocking failure string, without the detail."""
    return failure.split(":", 1)[0].strip() or failure.strip()


def failure_history(data_dir, model_id: str, *, limit: int = 8) -> dict:
    """Aggregate the recorded gate reports: what keeps failing, how often."""
    from tcad.store.artifacts import read_gate_report

    ensure_safe_id(model_id, kind="model_id")
    root = Path(data_dir) / "gate_reports" / model_id
    versions = []
    if root.is_dir():
        for path in root.glob("v*.json"):
            stem = path.stem[1:]
            if stem.isdigit():
                versions.append(int(stem))
    versions = sorted(versions)[-limit:]
    classes: dict[str, list[int]] = {}
    graded, failed = [], []
    for version in versions:
        report = read_gate_report(data_dir, model_id, version)
        if report is None:
            continue
        graded.append(version)
        if report.get("passed"):
            continue
        failed.append(version)
        for failure in report.get("blocking_failures") or []:
            classes.setdefault(_class_of(str(failure)), []).append(version)
    # Keep the full history for metrics, but only prescribe repairs for a class
    # that still fails in the most recently graded version.
    repeated = {name: vs for name, vs in classes.items()
                if len(vs) >= 2 and graded and vs[-1] == graded[-1]}
    return {"graded_versions": graded, "failed_versions": failed,
            "classes": classes, "repeated": repeated}


def recovery_note(data_dir, model_id: str) -> str:
    """A RECOVERY CHECK paragraph, or "" when no class has repeated."""
    history = failure_history(data_dir, model_id)
    if not history["repeated"]:
        return ""
    lines = ["RECOVERY CHECK — the same failure class keeps returning across versions:"]
    for name, versions in sorted(history["repeated"].items(), key=lambda item: -len(item[1])):
        lines.append(f"  - {name}: failed {len(versions)} of the last "
                     f"{len(history['graded_versions'])} graded version(s) "
                     f"(v{', v'.join(str(v) for v in versions)})")
    lines.append(
        "  A new IR version is not evidence of progress. Fix the recurring class "
        "itself (or record why it no longer applies) before treating this attempt "
        "as a recovery; geometry validity, visual conformance and physical "
        "performance remain separately accepted.")
    return "\n".join(lines)


def intent_note(data_dir, model_id: str, ir) -> str:
    """The DESIGN INTENT CHECK paragraph for a commit result, or ""."""
    plan = read_plan(data_dir, model_id)
    if plan is None:
        return ""
    review = review_intent(plan, ir)
    lines = [f"DESIGN INTENT CHECK — plan recorded at v{plan.recorded_version}:"]
    lines.append("  kept: " + (", ".join(e["id"] for e in review["kept"]) or "none"))
    if review["lost"]:
        lines.append("  LOST (planned, no geometry): " +
                     ", ".join(f"{e['id']} ({e['origin']})" for e in review["lost"]))
    if review["reduced"]:
        lines.append("  simplified/dropped: " + ", ".join(
            f"{e['id']} ({e['state']})" for e in review["reduced"]))
    if review["degraded"]:
        lines.append("  ⚠ USER-REQUIRED PARTS DEGRADED: " + ", ".join(
            f"{e['id']} ({e['state']})" for e in review["degraded"]))
        lines.append("  Record the degradation and either continue refining or deliver a "
                     "draft pending acceptance — do not report the design as complete.")
    for note in review["advisories"]:
        lines.append(f"  verify: {note}")
    return "\n".join(lines)


def design_notes(data_dir, model_id: str, ir) -> str:
    """Every intent/recovery observation for a commit result (may be "")."""
    parts = [note for note in (intent_note(data_dir, model_id, ir),
                               recovery_note(data_dir, model_id)) if note]
    return "\n".join(parts)
