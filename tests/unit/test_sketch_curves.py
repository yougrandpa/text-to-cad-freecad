"""Native curve payloads must survive tools/patches without silent reinterpretation."""

import pytest
from pydantic import ValidationError

from tcad.ir.patch import apply_patch
from tcad.ir.schema import BodySpec, IrDocument, IrPatch, IrPatchOp, SketchGeom
from tcad.tools.ir_tools import _ir_patch_schema
from tcad.tools.schema_check import check


def point(x=0, y=0, z=0):
    return {"x": x, "y": y, "z": z}


def ellipse(**changes):
    return {"id": "g", "kind": "ellipse", "points": [point()],
            "major_radius": 20, "minor_radius": 10, "rotation": 30, **changes}


def spline(**changes):
    return {"id": "g", "kind": "bspline",
            "points": [point(), point(10, 10), point(20)], **changes}


@pytest.mark.parametrize("data", [ellipse(), ellipse(major_radius=10), spline(),
                                  spline(periodic=True)])
def test_curves_roundtrip_through_tool_schema_and_patch(data):
    payload = {"body_id": "b", "id": "sk", "name": "curve",
               "plane": {"kind": "origin_plane", "plane": "XY"},
               "geometry": [data], "constraints": [{"type": "Block", "refs": [0]}]}
    args = {"base_version": 0, "ops": [{"op": "add_sketch", "target_id": "sk",
                                          "payload": payload, "reason": "curve"}]}
    assert check(args, _ir_patch_schema()) == []
    doc = IrDocument(model_id="curves", bodies=[BodySpec(id="b", name="body")])
    result = apply_patch(doc, IrPatch.model_validate(args)).ir
    rebuilt = IrDocument.model_validate(result.model_dump())
    assert rebuilt.bodies[0].sketches[0].geometry[0] == SketchGeom.model_validate(data)
    update = IrPatch(base_version=rebuilt.version, ops=[IrPatchOp(
        op="update_sketch", target_id="sk", payload={"geometry": [data]}, reason="edit")])
    assert apply_patch(rebuilt, update).ir.bodies[0].sketches[0].geometry[0].kind == data["kind"]


@pytest.mark.parametrize("data", [
    ellipse(points=[]), ellipse(points=[point(), point(1)]),
    ellipse(major_radius=None), ellipse(minor_radius=0), ellipse(major_radius=5),
    ellipse(major_radius=float("inf")), ellipse(rotation=float("nan")),
    ellipse(points=[point(z=float("nan"))]), ellipse(periodic=True), ellipse(radius=2),
    spline(points=[point()]), spline(points=[point(), point()]),
    spline(periodic=True, points=[point(), point(1)]),
    spline(periodic=True, points=[point(), point(1), point()]),
    spline(points=[point()] * 129), spline(periodic="false"),
    spline(degree=3), spline(weights=[1, 1, 1]), spline(major_radius=2),
])
def test_invalid_curves_are_rejected_before_freecad(data):
    with pytest.raises(ValidationError):
        SketchGeom.model_validate(data)
