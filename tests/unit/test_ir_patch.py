"""Tests for tcad.ir.patch — the only IR mutation path."""

from __future__ import annotations

import pytest

from tcad.core.types import ToolError, ToolErrorKind
from tcad.ir.patch import PatchError, apply_patch
from tcad.ir.schema import IrPatch, IrPatchOp

from .conftest import make_minimal_ir, make_two_feature_ir


def test_stale_base_version_rejected():
    ir = make_minimal_ir(version=0)
    patch = IrPatch(
        base_version=7,  # wrong — current is 0
        ops=[IrPatchOp(op="rename", target_id="ft_pad",
                       payload={"name": "base_pad"}, reason="r")],
    )
    with pytest.raises(PatchError) as exc:
        apply_patch(ir, patch)
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC
    assert "stale" in exc.value.error.message.lower()


def test_dependent_removal_refused_with_dependents_named():
    ir = make_two_feature_ir()
    patch = IrPatch(
        base_version=0,
        ops=[IrPatchOp(op="remove_feature", target_id="a")],
    )
    with pytest.raises(PatchError) as exc:
        apply_patch(ir, patch)
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC
    assert "b" in exc.value.error.message  # dependent named


def test_rename_preserves_uniqueness():
    ir = make_two_feature_ir()  # 'a'→name "base", 'b'→name "hole"
    # rename 'b' to the existing name "base" (collision) -> refused
    patch = IrPatch(
        base_version=0,
        ops=[IrPatchOp(op="rename", target_id="b", payload={"name": "base"})],
    )
    with pytest.raises(PatchError) as exc:
        apply_patch(ir, patch)
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC
    assert "collide" in exc.value.error.message.lower()


def test_rename_success_updates_name_only():
    ir = make_minimal_ir(version=0)
    patch = IrPatch(
        base_version=0,
        ops=[IrPatchOp(op="rename", target_id="ft_pad",
                       payload={"name": "base_pad"}, reason="clarify")],
    )
    out = apply_patch(ir, patch)
    assert out.ir.find_feature("ft_pad").name == "base_pad"
    assert out.version == 1
    assert "ft_pad" in out.renamed_ids
    # original IR untouched (pure)
    assert ir.version == 0
    assert ir.find_feature("ft_pad").name == "pad"


def test_add_feature_mints_id_and_bumps_version():
    ir = make_minimal_ir(version=0)
    patch = IrPatch(
        base_version=0,
        summary="add a hole",
        ops=[IrPatchOp(
            op="add_feature",
            payload={"op": "hole", "profile_sketch": "sk_outline",
                     "params": {"diameter": 3}, "name": "hole1"},
            reason="need a hole")],
    )
    out = apply_patch(ir, patch)
    assert out.version == 1
    assert out.applied == 1
    assert len(out.created_ids) == 1
    new = out.ir.find_feature(out.created_ids[0])
    assert new is not None and new.op == "hole"


def test_remove_feature_no_dependents_ok():
    ir = make_minimal_ir(version=0)
    patch = IrPatch(
        base_version=0,
        ops=[IrPatchOp(op="remove_feature", target_id="ft_pad")],
    )
    out = apply_patch(ir, patch)
    assert out.ir.find_feature("ft_pad") is None
    assert out.version == 1


def test_update_feature_replaces_list_but_appends_with_suffix():
    ir = make_minimal_ir(version=0)
    # replace refs entirely
    p1 = IrPatch(base_version=0, ops=[IrPatchOp(
        op="update_feature", target_id="ft_pad",
        payload={"refs": ["nonexistent"]}, reason="r")])
    with pytest.raises(PatchError):  # refs must resolve -> error
        apply_patch(ir, p1)
    # append via *_append does not clobber
    p2 = IrPatch(base_version=0, ops=[IrPatchOp(
        op="add_feature",
        payload={"op": "datum_plane", "name": "dp", "refs": []}, reason="r")])
    out = apply_patch(ir, p2)
    dp_id = out.created_ids[0]
    p3 = IrPatch(base_version=1, ops=[IrPatchOp(
        op="update_feature", target_id=dp_id,
        payload={"refs_append": ["ft_pad"]}, reason="attach")])
    out2 = apply_patch(out.ir, p3)
    assert out2.ir.find_feature(dp_id).refs == ["ft_pad"]


def test_patch_creating_cycle_is_rejected():
    ir = make_two_feature_ir()  # a (no refs), b refs a
    # make 'a' reference 'b' -> a->b->a cycle
    patch = IrPatch(base_version=0, ops=[IrPatchOp(
        op="update_feature", target_id="a",
        payload={"refs": ["b"]}, reason="cross-link")])
    with pytest.raises(PatchError) as exc:
        apply_patch(ir, patch)
    assert exc.value.error.kind == ToolErrorKind.SEMANTIC
    assert "cycle" in exc.value.error.message.lower()


def test_cascade_removal_rewires_dependents():
    ir = make_two_feature_ir()
    patch = IrPatch(base_version=0, ops=[IrPatchOp(
        op="remove_feature", target_id="a", payload={"cascade": True})])
    out = apply_patch(ir, patch)
    assert out.ir.find_feature("a") is None
    assert "a" not in out.ir.find_feature("b").refs  # rewired
