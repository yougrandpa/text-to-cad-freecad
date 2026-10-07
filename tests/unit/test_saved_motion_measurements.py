"""Motion evidence must include a closed cycle's interior and pinned input."""

import copy
import json
import math
from types import SimpleNamespace

import pytest

from tcad.core.types import ToolContext
from tcad.inspect.motion import assembly_definition, measure_saved_motion, summarize_interferences
from tcad.ir.assembly import AssemblySpec
from tcad.ir.schema import BodySpec, IrDocument
from tcad.render.scene import SceneModel
from tcad.tools.base import execute_tool
from tcad.tools.geo_tools import assembly_solve_handler, build_geo_tools
from tests.fixtures.artifact_scene import publish_scene, tetra_mesh


def rotation(degrees, pivot=(0, 0, 0)):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    x, y, _ = pivot
    return [c, -s, 0, x - c * x + s * y, s, c, 0, y - s * x - c * y,
            0, 0, 1, 0, 0, 0, 0, 1]


def motion(angles=(0, 90, 0, -90, 0)):
    return {"parts": [{"body_id": "arm", "vertex_start": 0, "vertex_count": 4}],
            "frames": [{"arm": rotation(a)} for a in angles],
            "start": 2, "step": 0.25, "solver": "FreeCAD Assembly"}


def test_closed_cycle_measures_all_frames_and_point_phase_without_mutation():
    animation = motion()
    original = copy.deepcopy(animation)
    result = measure_saved_motion(animation, tetra_mesh()["vertices"],
        track_points=[{"name": "tip", "body_id": "arm", "point": [1, 0, 0]}])
    assert result["frames_examined"] == 5
    assert result["moving_bodies"] == ["arm"]
    body = result["bodies"][0]
    assert body["max_rotation_from_first_deg"] == 90
    assert body["sampled_rotation_path_deg"] == 360
    track = result["point_tracks"][0]
    assert track["span_mm"] == [1, 2, 0]
    samples = {p["frame_index"]: p for p in track["samples"]}
    assert samples[1]["world_mm"] == [0, 1, 0]
    assert samples[3]["world_mm"] == [0, -1, 0]
    assert samples[1]["time_s"] == 2.25
    assert samples[0]["world_mm"] == samples[4]["world_mm"]
    assert animation == original


def test_reference_point_is_pre_solve_world_geometry_not_first_pose():
    animation = motion((90, 180))
    result = measure_saved_motion(animation, tetra_mesh()["vertices"],
        track_points=[{"name": "tip", "body_id": "arm", "point": [1, 0, 0]}])
    assert result["point_tracks"][0]["samples"][0]["world_mm"] == [0, 1, 0]
    assert result["bodies"][0]["max_rotation_from_first_deg"] == 90


def test_distant_pivot_does_not_report_matrix_translation_as_center_motion():
    center = [100, 200, 0]
    vertices = [[99, 199, 0], [101, 199, 0], [101, 201, 0], [99, 201, 0]]
    animation = motion((0, 90))
    animation["frames"][1]["arm"] = rotation(90, center)
    result = measure_saved_motion(animation, vertices)
    assert result["moving_bodies"] == ["arm"]  # orientation still changes
    assert result["bodies"][0]["preview_bbox_center"]["span_mm"] == [0, 0, 0]


def test_samples_are_bounded_but_sparse_selection_does_not_hide_interior():
    animation = motion(tuple(i * 10 for i in range(600)))
    result = measure_saved_motion(animation, tetra_mesh()["vertices"], sample_frames=[0, 599])
    body = result["bodies"][0]
    assert body["max_rotation_from_first_deg"] == 180
    assert body["sampled_rotation_path_deg"] == 5990
    assert len(body["preview_bbox_center"]["samples"]) == 2
    defaults = measure_saved_motion(animation, tetra_mesh()["vertices"])
    assert len(defaults["bodies"][0]["preview_bbox_center"]["samples"]) <= 9


def test_static_and_linear_motion_are_measured_without_angular_assumptions():
    animation = motion((0, 0))
    assert measure_saved_motion(animation, tetra_mesh()["vertices"])["moving_bodies"] == []
    animation["frames"][1]["arm"][11] = 8
    result = measure_saved_motion(animation, tetra_mesh()["vertices"])
    assert result["bodies"][0]["max_rotation_from_first_deg"] == 0
    assert result["bodies"][0]["preview_bbox_center"]["span_mm"] == [0, 0, 8]


@pytest.mark.parametrize("kwargs", [
    {"sample_frames": [5]}, {"sample_frames": [-1]}, {"sample_frames": [0.5]},
    {"sample_frames": list(range(13))},
    {"track_points": [{"name": "p", "body_id": "missing", "point": [0, 0, 0]}]},
    {"track_points": [{"name": "p", "body_id": "arm", "point": [0, float("nan"), 0]}]},
    {"track_points": [{"name": "p", "body_id": "arm", "point": [0, 0, 0]}] * 2},
])
def test_invalid_probes_or_sample_indices_fail_honestly(kwargs):
    with pytest.raises(ValueError):
        measure_saved_motion(motion(), tetra_mesh()["vertices"], **kwargs)


def test_collision_summary_includes_pairs_after_first_ten_examples():
    items = [{"frame": i, "bodies": ["base", "arm"], "overlap_mm3": 2 + i} for i in range(12)]
    items.append({"frame": 17, "bodies": ["follower", "arm"], "overlap_mm3": 50})
    result = summarize_interferences(items)
    assert result[0] == {"bodies": ["arm", "follower"], "overlap_samples": 1,
                         "min_overlap_mm3": 50, "max_overlap_mm3": 50, "peak_frame": 17}
    assert result[1]["overlap_samples"] == 12
    assert result[1]["min_overlap_mm3"] == 2
    assert result[1]["max_overlap_mm3"] == 13


def test_matching_formulas_are_counted_as_separate_inputs():
    result = assembly_definition({"grounded": ["base"], "drivers": [
        {"joint_id": "a", "type": "Angular", "formula": "sin(time)"},
        {"joint_id": "b", "type": "Angular", "formula": "sin(time)"}]})
    assert result["prescribed_driver_count"] == 2


async def test_solve_honors_pinned_scene_and_saved_definition_without_current_ir(tmp_path):
    assembly = AssemblySpec(grounded=["base"], start=2, end=3, step=0.25, joints=[
        {"id": "axis", "type": "Revolute", "side1": {"body_id": "base"},
         "side2": {"body_id": "arm"}}], drivers=[
        {"joint_id": "axis", "type": "Angular", "formula": "pi*time"}])
    source = IrDocument(model_id="part", bodies=[BodySpec(id=i, name=i) for i in ("base", "arm")],
                        assembly=assembly)
    mesh = tetra_mesh(volume=2)
    mesh["vertices"] += [[x + 2, y, z] for x, y, z in mesh["vertices"][:]]
    mesh["facets"] += [[i + 4 for i in f] for f in mesh["facets"][:]]
    mesh["bbox"]["x"] = 3
    animation = motion()
    animation["parts"].append({"body_id": "base", "vertex_start": 4, "vertex_count": 4})
    for frame in animation["frames"]:
        frame["base"] = rotation(0)
    scene = SceneModel(mesh=mesh, body_ids=["base", "arm"], animation=animation)
    manifest, root = publish_scene(tmp_path, scene=scene, source_ir=source)
    original = {p.name: p.read_bytes() for p in root.iterdir()}
    def forbidden(*args, **kwargs):
        raise AssertionError("pinned motion evidence must not load current source or run FreeCAD")
    services = SimpleNamespace(store=SimpleNamespace(load=forbidden, current_version=forbidden),
                               worker=SimpleNamespace(request=forbidden))
    ctx = ToolContext(model_id="part", thread_id="t", turn_id="turn", data_dir=str(tmp_path))
    result = await assembly_solve_handler(services, {"artifact_id": manifest.artifact_id}, ctx)
    assert result.ok, result.error
    payload = json.loads(result.content)
    assert payload["artifact_id"] == manifest.artifact_id
    assert payload["assembly_definition"]["prescribed_driver_count"] == 1
    assert payload["motion_summary"]["bodies"][0]["max_rotation_from_first_deg"] == 90
    assert {p.name: p.read_bytes() for p in root.iterdir()} == original


async def test_point_schema_rejection_precedes_saved_artifact_reads():
    spec = build_geo_tools(SimpleNamespace())["assembly_simulate"]
    ctx = ToolContext(model_id="part", thread_id="t", turn_id="turn")
    outcome = await execute_tool(spec, {"track_points": [{"body_id": "arm", "point": [0, 0]}]}, ctx)
    assert not outcome.result.ok
    assert "schema" in outcome.result.error.message
