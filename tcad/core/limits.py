"""Resource budgets shared by every preview producer and consumer.

The viewport mesh budget is one contract with four parties: the worker (which
tessellates), the supervisor (which validates and caches the scene), the server
(which serializes it) and the pick mapping (which indexes it). They used to
each carry their own copy — the server capped at 100k/200k while the scene
validator invented the same numbers again — and a preview that exceeded them
was a dead end.

Standard library only: this module is imported by the FreeCAD worker
interpreter, which has neither pydantic nor numpy.
"""

from __future__ import annotations

# ── scene allocation caps (vertices / triangles) ──────────────────────────
MAX_SCENE_VERTICES = 100_000
MAX_SCENE_FACETS = 200_000

# ── pick-mapping budgets (must match tcad/render/picking.py field limits) ─
MAX_PICK_ENTITIES = 20_000
MAX_PICK_SEGMENTS = 200_000

# ── adaptive preview tessellation ─────────────────────────────────────────
# A preview starts at the default linear deflection and, when the result
# exceeds the caps above, is regenerated from the SAME BRep with a coarser
# deflection instead of being refused. The ladder stops at MAX so a preview
# can never degrade into an unrecognizable blob.
DEFAULT_TESSELLATE_TOLERANCE = 0.5
PREVIEW_TOLERANCE_GROWTH = 1.8
MAX_PREVIEW_TOLERANCE = 5.0
#: Hard ceiling on regeneration attempts (defends against a pathological
#: growth factor supplied by a caller).
MAX_PREVIEW_ATTEMPTS = 8
