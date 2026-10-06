"""Loft section references survive the validated tool mutation boundary."""
import pytest

from tcad.ir.patch import PatchError, apply_patch
from tcad.ir.schema import IrDocument, IrPatch, IrPatchOp


def document():
    return IrDocument(model_id="loft_patch", bodies=[{"id": "b", "name": "body", "sketches": [
        {"id": name, "name": name, "plane": {"kind": "origin_plane", "plane": "XY"},
         "geometry": [{"id": "c", "kind": "circle", "points": [{"x": 0, "y": 0, "z": 0}], "radius": 2}]}
        for name in ["first", "middle", "end"]]}])


def test_add_and_update_loft_keep_ordered_typed_sections():
    ir = document()
    added = apply_patch(ir, IrPatch(base_version=0, ops=[IrPatchOp(op="add_feature", payload={
        "id": "loft", "name": "shell", "body_id": "b", "op": "additive_loft", "profile_sketch": "first",
        "sections": ["middle", "end"], "params": {"ruled": False}})]))
    assert added.ir.find_feature("loft").sections == ["middle", "end"]
    updated = apply_patch(added.ir, IrPatch(base_version=1, ops=[IrPatchOp(op="update_feature", target_id="loft",
        payload={"sections": ["end"]})]))
    assert updated.ir.find_feature("loft").sections == ["end"]
    assert added.ir.find_feature("loft").sections == ["middle", "end"]


@pytest.mark.parametrize("sections", ["middle", [123], {"id": "middle"}])
def test_bad_section_types_fail_as_structured_patch_errors(sections):
    ir = document()
    with pytest.raises(PatchError) as failure:
        apply_patch(ir, IrPatch(base_version=0, ops=[IrPatchOp(op="add_feature", payload={
            "id": "loft", "body_id": "b", "op": "additive_loft", "profile_sketch": "first", "sections": sections})]))
    assert "sections" in failure.value.error.message
    assert not ir.all_features()
