"""Real kernel proof for conical cuts and independent rigid-body previews."""
import math
from pathlib import Path

import pytest

from tests.contract.test_primitive_placement import worker, FREECAD_CMD  # shared real-worker fixture

pytestmark = [pytest.mark.contract, pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found")]


def test_placed_cone_has_analytic_volume_and_axis(worker, tmp_path):
    ir = {"model_id": "cone", "version": 0, "bodies": [{"id": "cone", "name": "cone",
          "features": [{"id": "cone_feature", "op": "additive_cone",
                        "params": {"radius1": 5, "radius2": 0, "height": 10},
                        "placement": {"position": {"x": 20, "y": 10, "z": 10},
                                      "axis": {"x": 0, "y": 1, "z": 0}, "angle": 90}}]}]}
    result = worker.request_sync("introspect_document", {"ir": ir, "out_dir": str(tmp_path)}, timeout_s=180)
    assert result["ok"], result
    d = result
    assert d["volume"] == pytest.approx(math.pi * 25 * 10 / 3, rel=1e-6)
    assert d["body_solids"] == {"cone": 1}
    assert d["bbox"]["x_min"] == pytest.approx(20, abs=1e-6)
    assert d["bbox"]["x"] == pytest.approx(10, abs=1e-6)
    assert d["bbox"]["y"] == pytest.approx(10, abs=1e-6)


def test_conical_bore_removes_analytic_volume(worker, tmp_path):
    ir = {"model_id": "cut", "bodies": [{"id": "holder", "name": "holder", "features": [
          {"id": "block", "op": "additive_box", "params": {"length": 20, "width": 20, "height": 20}},
          {"id": "cone_cut", "op": "subtractive_cone",
           "params": {"radius1": 5, "radius2": 0, "height": 10},
           "placement": {"position": {"x": 10, "y": 10, "z": 0}}}]}]}
    result = worker.request_sync("introspect_document", {"ir": ir, "out_dir": str(tmp_path)}, timeout_s=180)
    assert result["ok"], result
    d = result
    assert d["is_valid"] and d["body_solids"] == {"holder": 1}
    assert d["volume"] == pytest.approx(8000 - math.pi * 25 * 10 / 3, rel=1e-6)


def test_motion_tessellates_real_bodies_and_leaves_static_body_fixed(worker, tmp_path):
    ir = {"model_id": "assembly", "bodies": [
         {"id": "housing", "name": "housing", "features": [{"id": "base", "op": "additive_box",
          "params": {"length": 4, "width": 4, "height": 4}}]},
         {"id": "crank", "name": "crank", "motion": {"pivot": {"x": 10, "y": 0, "z": 0},
          "axis": {"x": 0, "y": 0, "z": 1}, "ratio": 1}, "features": [{"id": "arm", "op": "additive_box",
          "params": {"length": 4, "width": 2, "height": 2}, "placement": {"position": {"x": 10, "y": 0, "z": 0}}}]}]}
    def mesh(angle):
        r = worker.request_sync("tessellate", {"ir": ir, "out_dir": str(tmp_path),
                                "driver_angle_deg": angle}, timeout_s=180)
        assert r["ok"], r
        return r
    zero, turned = mesh(0), mesh(90)
    assert len(zero["motion"]) == 1
    start = zero["motion"][0]["vertex_start"]
    assert zero["mesh"]["vertices"][:start] == turned["mesh"]["vertices"][:start]
    assert zero["mesh"]["facets"] == turned["mesh"]["facets"]
    for a,b in zip(zero["mesh"]["vertices"][start:],turned["mesh"]["vertices"][start:]):
        assert b == pytest.approx([10-a[1],a[0]-10,a[2]],abs=1e-6)


def test_sampled_brep_collision_uses_pivot_and_detects_interior_overlap(worker, tmp_path):
    ir = {'model_id': 'collision', 'bodies': [
        {'id': 'fixed', 'name': 'fixed', 'features': [{'id': 'box', 'op': 'additive_box',
         'params': {'length': 2, 'width': 2, 'height': 2}, 'placement': {'position': {'x': -1, 'y': 4, 'z': 0}}}]},
        {'id': 'arm', 'name': 'arm', 'motion': {'pivot': {'x': 0, 'y': 0, 'z': 0},
         'axis': {'x': 0, 'y': 0, 'z': 1}, 'ratio': 1}, 'features': [{'id': 'arm_box', 'op': 'additive_box',
         'params': {'length': 6, 'width': 2, 'height': 2}}]}]}
    result = worker.request_sync('check_motion', {'ir': ir, 'out_dir': str(tmp_path), 'angles': [0, -90, 90]}, timeout_s=180)
    assert result['ok'], result
    assert result['pairs_checked'] == 1
    assert len(result['interferences']) == 1
    assert result['interferences'][0]['angle_deg'] == 90
    assert result['interferences'][0]['overlap_mm3'] == pytest.approx(4)
