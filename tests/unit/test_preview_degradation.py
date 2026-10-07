"""Adaptive preview: coarser mesh from the same BRep, never a dead build."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tcad.worker.pick_mapping import mesh_digest
from tcad.worker.preview import adaptive_pick_mesh, summarize, top_contributors


class Vec:
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class FakeFace:
    def __init__(self, triangles):
        self.triangles = triangles

    def cleaned(self):
        return FakeFace(self.triangles)

    def tessellate(self, tolerance):
        count = max(2, int(self.triangles / max(tolerance, 1e-3)))
        points = [Vec(0.0, 0.0, float(i)) for i in range(count + 1)]
        return points, [[0, i, i + 1] for i in range(count)]


class FakeEdge:
    def __init__(self, points=4):
        self.count = points

    def discretize(self, Deflection):
        return [Vec(0.0, 0.0, 0.0)] * self.count


class FakeShape:
    def __init__(self, triangles=(8,), edges=(4,)):
        self.Faces = [FakeFace(t) for t in triangles]
        self.Edges = [FakeEdge(n) for n in edges]


def test_base_tolerance_fits_and_pick_mapping_matches_the_mesh():
    result = adaptive_pick_mesh([("arm", FakeShape())], 0.5)
    assert result["status"] == "ok"
    assert result["tolerance"] == 0.5
    indexed = result["pick_mesh"]
    mapping = indexed.mapping()
    assert mapping["mesh_digest"] == mesh_digest(indexed.vertices, indexed.facets)
    assert [part["body_id"] for part in mapping["parts"]] == ["arm"]


def test_over_budget_regenerates_coarser_from_the_same_shape(monkeypatch):
    import tcad.worker.preview as preview
    monkeypatch.setattr(preview, "MAX_SCENE_VERTICES", 1_000)
    monkeypatch.setattr(preview, "MAX_SCENE_FACETS", 1_000)
    shape = FakeShape(triangles=(600,))  # 1200 facets at t=0.5, 666 at t=0.9
    result = adaptive_pick_mesh([("shell", shape)], 0.5)
    assert result["status"] == "degraded"
    assert result["tolerance"] > 0.5
    assert result["pick_mesh"] is not None
    assert len(result["attempts"]) >= 2
    assert result["attempts"][0]["violations"] and not result["attempts"][-1]["violations"]
    # Same real BRep: the coarser retry reports the actual chosen tolerance.
    assert result["pick_mesh"].mapping()["mesh_digest"] == mesh_digest(
        result["pick_mesh"].vertices, result["pick_mesh"].facets)


def test_preview_discards_cached_triangulation_on_a_copy(monkeypatch):
    import tcad.worker.preview as preview
    monkeypatch.setattr(preview, 'MAX_SCENE_VERTICES', 1_000)
    monkeypatch.setattr(preview, 'MAX_SCENE_FACETS', 1_000)

    class CachedFace(FakeFace):
        def tessellate(self, tolerance):
            # Represents a fine export mesh ignoring later coarse requests.
            raise AssertionError('Preview reused original cached triangulation')

    shape = FakeShape()
    original = CachedFace(600)
    shape.Faces = [original]
    result = adaptive_pick_mesh([('curved', shape)], 0.5)
    assert result['status'] == 'degraded'
    assert shape.Faces[0] is original
    assert result['attempts'][-1]['facets'] < result['attempts'][0]['facets']


def test_every_rung_over_budget_is_unavailable_with_contributions(monkeypatch):
    import tcad.worker.preview as preview
    monkeypatch.setattr(preview, "MAX_SCENE_VERTICES", 50)
    monkeypatch.setattr(preview, "MAX_SCENE_FACETS", 50)
    bodies = [("heavy", FakeShape(triangles=(4000,))),
              ("light", FakeShape(triangles=(80,)))]
    result = adaptive_pick_mesh(bodies, 0.5)
    assert result["status"] == "unavailable"
    assert result["pick_mesh"] is None
    assert "vertices" in result["reason"] and "facets" in result["reason"]
    ranked = top_contributors(result["bodies"])
    assert ranked[0]["body_id"] == "heavy"
    summary = summarize(result)
    assert summary["status"] == "unavailable"
    assert summary["bodies"][0]["body_id"] == "heavy"
    assert summary["attempts"][-1]["violations"]


def test_degradation_ladder_is_bounded_by_the_max_tolerance(monkeypatch):
    import tcad.worker.preview as preview
    monkeypatch.setattr(preview, "MAX_SCENE_VERTICES", 1)
    monkeypatch.setattr(preview, "MAX_SCENE_FACETS", 1)
    monkeypatch.setattr(preview, "MAX_PREVIEW_TOLERANCE", 1.0)
    result = adaptive_pick_mesh([("x", FakeShape(triangles=(10_000,)))], 0.5,
                                growth=2.0, max_tolerance=1.0)
    assert result["status"] == "unavailable"
    tolerances = [attempt["tolerance"] for attempt in result["attempts"]]
    assert tolerances == [0.5, 1.0], tolerances


def test_preview_requires_a_positive_growth_factor():
    with pytest.raises(ValueError):
        adaptive_pick_mesh([("x", FakeShape())], 0.5, growth=1.0)


# ─── supervisor-side classification ───────────────────────────────────────

def _tetra_mesh():
    return {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "facets": [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
            "bbox": {"x": 1, "y": 1, "z": 1, "x_min": 0, "y_min": 0, "z_min": 0},
            "volume": 1.0, "tolerance": 0.5}


def test_preview_from_build_returns_scene_and_degraded_status():
    from tcad.render.scene import preview_from_build
    result = {"mesh": _tetra_mesh(), "motion": [],
              "preview": {"status": "degraded", "tolerance": 0.9,
                          "reason": "regenerated", "attempts": [], "bodies": []}}
    scene, preview = preview_from_build(result, {"bodies": []})
    assert scene is not None and scene.preview.status == "degraded"
    assert preview["status"] == "degraded"
    assert preview["counts"] == {"vertices": None, "facets": None}


def test_preview_from_build_classifies_missing_mesh_as_unavailable():
    from tcad.render.scene import preview_from_build
    result = {"mesh": None, "preview": {"status": "unavailable", "tolerance": 5.0,
              "reason": "preview mesh exceeds the viewport budget",
              "attempts": [{"tolerance": 5.0, "vertices": 400_000, "facets": 800_000,
                            "violations": ["vertices 400000 > 100000"]}],
              "bodies": [{"body_id": "shell", "vertices": 400_000, "facets": 800_000}]}}
    scene, preview = preview_from_build(result, {"bodies": []})
    assert scene is None
    assert preview["status"] == "unavailable" and preview["stage"] == "tessellation"
    assert preview["counts"] == {"vertices": 400_000, "facets": 800_000}
    assert preview["limits"] == {"vertices": 100_000, "facets": 200_000}


def test_preview_from_build_never_raises_for_an_invalid_scene(monkeypatch):
    from tcad.render.scene import preview_from_build
    monkeypatch.setattr("tcad.render.scene.MAX_SCENE_VERTICES", 2)
    result = {"mesh": _tetra_mesh()}
    scene, preview = preview_from_build(result, {"bodies": []})
    assert scene is None
    assert preview["status"] == "unavailable" and preview["stage"] == "scene_validation"


def test_render_preview_note_carries_facts_and_no_destructive_advice():
    from tcad.render.scene import render_preview_note
    preview = {"status": "unavailable", "stage": "tessellation", "tolerance": 5.0,
               "reason": "preview mesh exceeds the viewport budget at the coarsest tolerance "
                         "vertices 400000 > 100000",
               "counts": {"vertices": 400_000, "facets": 800_000},
               "limits": {"vertices": 100_000, "facets": 200_000},
               "bodies": [{"body_id": "shell", "vertices": 400_000, "facets": 800_000}]}
    note = render_preview_note(preview)
    assert "PREVIEW UNAVAILABLE" in note and "not a CAD build failure" in note
    assert "stage:" in note and "type: preview_mesh_limit" in note
    assert "400000" in note and "100000" in note and "shell" in note
    assert "do NOT delete structural features" in note
    assert render_preview_note({"status": "ok"}) == ""
    assert render_preview_note(None) == ""
    degraded = render_preview_note({"status": "degraded", "tolerance": 0.9,
                                    "limits": {"vertices": 100_000, "facets": 200_000}})
    assert "PREVIEW DEGRADED" in degraded and "0.9" in degraded
