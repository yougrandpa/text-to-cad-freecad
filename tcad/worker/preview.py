"""Adaptive preview tessellation: coarser mesh, same BRep.

The viewport mesh is bounded (``tcad/core/limits``), and that bound is about
the *preview*, not the CAD. Refusing to publish a preview — or worse, refusing
the whole build — because a dense fillet fan crossed the cap conflated the two:
a model was told to simplify geometry it had already built correctly.

So a preview that exceeds the caps is regenerated from the same compiled BRep
with a coarser linear deflection, on a fixed ladder up to
``MAX_PREVIEW_TOLERANCE``. The shell, the parametric history and the exported
STEP/FCStd are untouched; only the triangle budget of the preview changes.
Every attempt reports the real counts, so the supervisor — not this module —
decides what the model is told.

Runs inside FreeCADCmd: standard library plus sibling worker modules only.
"""

from __future__ import annotations

from tcad.core.limits import (
    MAX_PICK_ENTITIES,
    MAX_PICK_SEGMENTS,
    MAX_PREVIEW_ATTEMPTS,
    MAX_PREVIEW_TOLERANCE,
    MAX_SCENE_FACETS,
    MAX_SCENE_VERTICES,
    PREVIEW_TOLERANCE_GROWTH,
)
from tcad.worker.pick_mapping import PickMesh


def _ladder(tolerance, *, growth, max_tolerance, attempts):
    """The tolerances to try, coarse-bound first, strictly increasing."""
    values = [float(tolerance)]
    for _ in range(attempts - 1):
        nxt = round(values[-1] * growth, 6)
        if nxt <= values[-1]:
            break
        values.append(nxt)
    return [min(v, max_tolerance) for v in values
            if v <= max_tolerance] or [float(min(tolerance, max_tolerance))]


def _attempt(bodies, tolerance):
    """One tessellation pass over every body; returns (PickMesh, violations)."""
    indexed = PickMesh()
    for body_id, shape in bodies:
        indexed.add(body_id, shape, tolerance)
    vertices, facets = len(indexed.vertices), len(indexed.facets)
    segments = sum(len(entity["segments"]) for entity in indexed.entities)
    violations = []
    if vertices > MAX_SCENE_VERTICES:
        violations.append(f"vertices {vertices} > {MAX_SCENE_VERTICES}")
    if facets > MAX_SCENE_FACETS:
        violations.append(f"facets {facets} > {MAX_SCENE_FACETS}")
    if len(indexed.entities) > MAX_PICK_ENTITIES:
        violations.append(f"pick entities {len(indexed.entities)} > {MAX_PICK_ENTITIES}")
    if segments > MAX_PICK_SEGMENTS:
        violations.append(f"pick segments {segments} > {MAX_PICK_SEGMENTS}")
    return indexed, {"tolerance": tolerance, "vertices": vertices, "facets": facets,
                     "entities": len(indexed.entities), "segments": segments,
                     "bodies": list(indexed.stats), "violations": violations}


def adaptive_pick_mesh(bodies, tolerance, *, growth=None, max_tolerance=None,
                       attempts=None):
    """Tessellate ``bodies`` [(body_id, shape)] within the preview budget.

    Returns ``{"status", "tolerance", "attempts", "bodies", "reason",
    "pick_mesh"}``:

    * ``ok``          — the base tolerance fits; ``pick_mesh`` is usable.
    * ``degraded``    — a coarser tolerance fits; same real BRep, fewer
      triangles, ``pick_mesh`` is usable.
    * ``unavailable`` — every rung on the ladder exceeds a budget.
      ``pick_mesh`` is ``None``; ``bodies`` and ``attempts`` say exactly which
      parts spent the budget and by how much, so the failure can be reported
      without guessing (the supervisor never receives a half-built index).

    ``MAX_PREVIEW_TOLERANCE`` bounds the ladder: past it the preview would stop
    representing the part, so reporting "no preview" is more honest than
    showing one the model (and the user) cannot trust.
    """
    growth = PREVIEW_TOLERANCE_GROWTH if growth is None else float(growth)
    max_tolerance = MAX_PREVIEW_TOLERANCE if max_tolerance is None else float(max_tolerance)
    attempts = MAX_PREVIEW_ATTEMPTS if attempts is None else int(attempts)
    if growth <= 1.0:
        raise ValueError("preview tolerance growth must exceed 1")
    if not bodies:
        return {"status": "unavailable", "tolerance": float(tolerance),
                "attempts": [], "bodies": [], "pick_mesh": None,
                "reason": "saved artifact has no solid to display"}

    ladder = _ladder(tolerance, growth=growth, max_tolerance=max_tolerance,
                     attempts=max(1, attempts))
    history = []
    for index, current in enumerate(ladder):
        indexed, record = _attempt(bodies, current)
        history.append(record)
        if not record["violations"]:
            status = "ok" if index == 0 else "degraded"
            reason = "" if status == "ok" else (
                f"preview regenerated at coarser tolerance {current:g} mm "
                f"(base {float(tolerance):g} mm); the CAD result is unchanged"
            )
            return {"status": status, "tolerance": current, "attempts": history,
                    "bodies": record["bodies"], "pick_mesh": indexed, "reason": reason}

    last = history[-1]
    return {"status": "unavailable", "tolerance": last["tolerance"],
            "attempts": history, "bodies": last["bodies"], "pick_mesh": None,
            "reason": ("preview mesh exceeds the viewport budget at the coarsest "
                       "tolerance " + "; ".join(last["violations"]))}


def top_contributors(bodies, limit=3):
    """The bodies that spent most of the vertex budget, biggest first."""
    ranked = sorted(bodies or [], key=lambda item: (-item.get("vertices", 0),
                                                    -item.get("facets", 0)))
    return ranked[:limit]


def summarize(result, *, limit=3):
    """The supervisor-facing view: no mesh object, only serializable facts."""
    summary = {key: result.get(key) for key in
               ("status", "tolerance", "reason")}
    summary["attempts"] = [
        {key: record.get(key) for key in
         ("tolerance", "vertices", "facets", "entities", "segments", "violations")}
        for record in result.get("attempts", [])
    ]
    summary["bodies"] = [
        {"body_id": item.get("body_id"), "vertices": item.get("vertices"),
         "facets": item.get("facets")}
        for item in top_contributors(result.get("bodies"), limit=limit)
    ]
    return summary
