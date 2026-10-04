"""Regression coverage from the crank-driven sharpener agent trial."""
import math

import pytest
from fastapi import HTTPException

from tcad.core.types import CheckContext, CheckStatus, GeometryDigest, Topology
from tcad.ir.patch import PatchError, apply_patch
from tcad.ir.schema import BodySpec, IrDocument, IrPatch, IrPatchOp
from tcad.server.mesh import _checked_motion
from tcad.verify.checks_solid import SolidCountCheck

MOTION = {"pivot": {"x": 28, "y": 0, "z": 38},
          "axis": {"x": 0, "y": 1, "z": 0}, "ratio": 1}


def patch(ir, *ops):
    return apply_patch(ir, IrPatch(base_version=ir.version, ops=[IrPatchOp(**op) for op in ops])).ir


def test_independent_body_routes_sketch_and_feature_atomically():
    ir = IrDocument(model_id="sharpener")
    built = patch(ir,
        dict(op="add_body", payload={"id": "housing", "name": "housing"}),
        dict(op="add_body", payload={"id": "shaft", "name": "shaft", "motion": MOTION}),
        dict(op="add_sketch", payload={"id": "profile", "body_id": "shaft",
             "require_fully_constrained": False,
             "geometry": [{"id": "circle", "kind": "circle", "points": [{"x": 28, "y": 0, "z": 0}], "radius": 3}]}),
        dict(op="add_feature", payload={"id": "pad", "body_id": "shaft", "op": "pad",
             "profile_sketch": "profile", "params": {"length": 10}}))
    assert ir.bodies == []
    assert built.version == 1
    assert not built.bodies[0].features
    assert built.bodies[1].features[0].id == "pad"
    assert built.bodies[1].sketches[0].id == "profile"
    assert built.bodies[1].motion.ratio == 1
    assert "body_id" not in built.bodies[1].features[0].model_dump()


def test_invalid_body_motion_is_rejected_without_mutation():
    ir = IrDocument(model_id="part", bodies=[BodySpec(id="body", name="body")])
    for motion in ({**MOTION, "axis": {"x": 0, "y": 0, "z": 0}},
                   {**MOTION, "pivot": {"x": math.inf, "y": 0, "z": 0}},
                   {**MOTION, "ratio": math.nan}):
        with pytest.raises(PatchError):
            patch(ir, dict(op="update_body", target_id="body", payload={"motion": motion}))
    assert ir.bodies[0].motion is None


def test_body_ids_cannot_collide_and_unknown_payload_is_actionable():
    ir = IrDocument(model_id="part", bodies=[BodySpec(id="body", name="body")])
    with pytest.raises(PatchError, match="already exists"):
        patch(ir, dict(op="add_body", payload={"id": "body"}))
    with pytest.raises(PatchError, match="does not accept"):
        patch(ir, dict(op="add_body", payload={"id": "new", "features": []}))


@pytest.mark.parametrize("per_body,total,passed", [
    ({"housing": 1, "shaft": 1}, 2, True),
    ({"housing": 2, "shaft": 0}, 2, False),
    ({"housing": 1}, 2, False),
    ({"housing": 1, "shaft": 1}, 3, False),
])
def test_assembly_count_requires_each_real_body_solid(per_body, total, passed):
    ir = IrDocument(model_id="part", bodies=[BodySpec(id=id, name=id) for id in ("housing", "shaft")])
    ctx = CheckContext(model_id="part", ir_version=0, ir=ir, artifact_dir="",
                       digest=GeometryDigest(model_id="part", ir_version=0,
                           topology=Topology(solids=total), body_solids=per_body))
    assert (SolidCountCheck().run(ctx).status == CheckStatus.PASS) == passed


def test_preview_motion_matches_declared_body_and_vertex_range():
    ir = IrDocument(model_id="part", bodies=[BodySpec(id="shaft", name="shaft", motion=MOTION)])
    raw = [{"body_id": "shaft", "vertex_start": 2, "vertex_count": 3, **MOTION}]
    assert _checked_motion(raw, ir.model_dump(), 5)[0]["ratio"] == 1
    for bad in ([], [{**raw[0], "vertex_count": 4}], [{**raw[0], "ratio": -1}]):
        with pytest.raises(HTTPException) as exc:
            _checked_motion(bad, ir.model_dump(), 5)
        assert exc.value.status_code == 502


@pytest.mark.parametrize("change,passes", [
    ({}, True), ({"is_valid": False}, False), ({"area": 999}, False),
    ({"solids": 1}, False), ({"bbox": {"x": 2}}, False),
])
def test_step_assembly_seam_change_needs_full_geometric_evidence(change, passes):
    from tcad.core.types import BBox
    from tcad.verify.checks_solid import RoundTripCheck
    ir = IrDocument(model_id="part", bodies=[BodySpec(id=id, name=id) for id in ("housing", "shaft")])
    measured = {"volume": 100, "area": 80, "faces": 12, "edges": 23,
                "solids": 2, "is_valid": True,
                "bbox": {"x": 10,"y": 5,"z": 2,"x_min": 0,"y_min": 0,"z_min": 0}, **change}
    class Worker:
        def request(self, *_args):
            return {"shape_summary": measured}
    ctx = CheckContext(model_id="part", ir_version=0, ir=ir, artifact_dir="", exports={"step": "part.step"},
                       worker=Worker(), digest=GeometryDigest(model_id="part", ir_version=0,
                       volume=100, area=80, bbox=BBox(x=10,y=5,z=2), topology=Topology(solids=2,faces=12,edges=24)))
    r = RoundTripCheck().run(ctx)
    assert (r.status == CheckStatus.PASS) == passes
    if passes:
        assert r.measurements["topology_changed"] is True


def test_python_motion_preserves_axis_points_and_signed_ratio():
    from tcad.ir.motion import pose_vertices
    part={**MOTION,"vertex_start":1,"vertex_count":1,"ratio":-0.5}
    original=[[28,5,38],[31,0,38]]
    posed=pose_vertices(original,[part],180)
    assert posed[0] == original[0]
    assert posed[1] == pytest.approx([28,0,41])
    assert original[1] == [31,0,38]
