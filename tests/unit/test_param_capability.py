"""The param allow-list must mean "the compiler can honour this" (task §5-A / R-7).

A key used to sit in ``_VERIFIED_OP_PARAMS`` whenever it matched a FreeCAD
property *by name*. But ``_assign_props`` sets a property from a JSON scalar, and
half of these properties are ``App::PropertyLinkSub`` — the shapes ``setattr``
cannot take from a string. Measured on FreeCAD 26.3.0 / 48708:

    params={'length': 5.0, 'up_to_face': 'Face6'} -> raised
    TypeError: type must be 'DocumentObject', 'NoneType' or
               ('DocumentObject',['String',]) not str

…which happened *after* the patch was persisted and a worker round-trip was spent.
The allow-list is now trimmed to what the compiler honours, and reaching for one
of the removed keys is a validation error that names the alternative.

The classification below is re-derived from the real kernel by
``tests/contract/test_param_capability.py``; keeping a copy here makes the guard
run in the fast unit tier too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tcad.ir.validate import (
    _GUIDANCE_BASE,
    _GUIDANCE_BY_OP,
    _UNVERIFIED_OPS,
    _VERIFIED_OP_PARAMS,
    _guidance_for,
    validate_ir,
)
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument


#: ``op -> {key: property type}`` for every allow-listed key that is NOT settable
#: from a JSON scalar, as measured on the real kernel. (The property *type* is the
#: evidence; the key alone would just be a name.)
MEASURED_UNSUPPORTED: dict[str, dict[str, str]] = {
    "pad": {"profile": "LinkSub", "reference_axis": "LinkSub",
            "start_reference": "LinkSub", "up_to_face": "LinkSub",
            "up_to_shape": "LinkSubList", "depth_type": "absent",
            # App::PropertyVector — `_assign_props` has no Vector branch, so a JSON
            # {"x":…} fails on setattr just like a reference does.
            "direction": "Vector"},
    "pocket": {"profile": "LinkSub", "reference_axis": "LinkSub",
               "start_reference": "LinkSub", "up_to_face": "LinkSub",
               "up_to_shape": "LinkSubList"},
    "revolution": {"profile": "LinkSub", "reference_axis": "LinkSub",
                   "start_reference": "LinkSub", "up_to_face": "LinkSub",
                   "up_to_shape": "LinkSubList"},
    "groove": {"profile": "LinkSub", "reference_axis": "LinkSub",
               "start_reference": "LinkSub", "up_to_face": "LinkSub",
               "up_to_shape": "LinkSubList", "fuse_order": "absent"},
    "hole": {"profile": "LinkSub", "start_reference": "LinkSub",
             "up_to_face": "LinkSub", "up_to_shape": "LinkSubList",
             "add_sub_shape": "PartShape"},
    "fillet": {"base": "LinkSub", "add_sub_shape": "PartShape"},
    "chamfer": {"base": "LinkSub"},
    "mirrored": {"mirror_plane": "LinkSub", "originals": "LinkList"},
    "linear_pattern": {"direction": "LinkSub", "direction2": "LinkSub",
                       "originals": "LinkList", "spacings": "FloatList",
                       "spacings2": "FloatList", "spacing_pattern": "FloatList",
                       "spacing_pattern2": "FloatList",
                       "suppressed_positions": "IntPairList"},
    "circular_pattern": {"axis": "LinkSub", "originals": "LinkList"},
}

#: ``axis`` is the one exception: for revolution/groove the compiler consumes it
#: itself (``_set_axis_reference`` translates the name into the ReferenceAxis
#: LinkSub). Anywhere else it is a raw reference and stays refused.
STRUCTURALLY_HANDLED = {("revolution", "axis"), ("groove", "axis")}


def test_every_measured_unsupported_key_is_out_of_the_allow_list():
    """The regression this round exists to prevent: a key back in the table."""
    offenders = []
    for op, keys in MEASURED_UNSUPPORTED.items():
        allowed = _VERIFIED_OP_PARAMS.get(op, frozenset())
        for key in keys:
            if (op, key) in STRUCTURALLY_HANDLED:
                continue
            if key in allowed:
                offenders.append(f"{op}.{key} ({keys[key]})")
    assert not offenders, (
        "these keys resolve to a property the compiler cannot set from a JSON "
        f"scalar, yet they are advertised as supported: {offenders}")


def test_every_removed_key_has_guidance():
    """A refusal must say what to do instead, not just 'no'."""
    for op, keys in MEASURED_UNSUPPORTED.items():
        for key in keys:
            if (op, key) in STRUCTURALLY_HANDLED:
                continue
            assert _guidance_for(op, key), (
                f"{op}.{key} was removed from the allow-list without guidance")


def test_the_allowed_tables_keep_the_keys_that_do_work():
    """Trimming must not have removed the scalars the samples depend on."""
    assert {"length", "type", "reversed", "midplane"} <= _VERIFIED_OP_PARAMS["pad"]
    assert {"type", "reversed"} <= _VERIFIED_OP_PARAMS["pocket"]
    assert {"angle", "type", "axis"} <= _VERIFIED_OP_PARAMS["revolution"]
    assert {"radius"} <= _VERIFIED_OP_PARAMS["fillet"]


def test_refusal_follows_the_per_op_table_exactly():
    """A key may be refused for one op and honoured for another.

    ``axis`` is honoured by revolution/groove and refused by the pattern ops;
    ``fuse_order`` exists on Revolution but not on Groove. So the refusal has to be
    decided per op — never by the guidance table alone.
    """
    all_keys = set(_GUIDANCE_BASE) | {k for (_o, k) in _GUIDANCE_BY_OP}
    for op, allowed in _VERIFIED_OP_PARAMS.items():
        for key in all_keys:
            issues = validate_ir(_doc(op, {key: "x"}))
            refused = [i for i in issues if i.code == "param_unsupported"]
            expected = bool(_guidance_for(op, key)) and key not in allowed
            assert bool(refused) is expected, (
                f"{op}.{key}: guidance={bool(_guidance_for(op, key))} "
                f"allowed={key in allowed} but refused={bool(refused)}")
    # ...and the two op-specific cases really do differ.
    assert "axis" in _VERIFIED_OP_PARAMS["revolution"]
    assert "axis" not in _VERIFIED_OP_PARAMS["circular_pattern"]
    assert "fuse_order" in _VERIFIED_OP_PARAMS["revolution"]
    assert "fuse_order" not in _VERIFIED_OP_PARAMS["groove"]


# ══════════════════════════════════════════════════════════════════════════
# the validator refuses it, with the alternative named
# ══════════════════════════════════════════════════════════════════════════


def _doc(op: str, params: dict) -> IrDocument:
    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        features=[FeatureSpec(id="f1", name="f", op=op, params=params)])])  # type: ignore[arg-type]


@pytest.mark.parametrize("op,key", [
    ("pad", "up_to_face"), ("pad", "up_to_shape"), ("pad", "reference_axis"),
    ("pad", "start_reference"), ("pad", "profile"),
    ("pocket", "up_to_face"),
    ("hole", "up_to_shape"),
    ("fillet", "base"),
    ("chamfer", "base"),
    ("mirrored", "mirror_plane"), ("mirrored", "originals"),
    ("linear_pattern", "direction"), ("linear_pattern", "spacings"),
    ("circular_pattern", "axis"),
])
def test_an_unhonourable_param_is_an_error_naming_the_alternative(op, key):
    issues = validate_ir(_doc(op, {key: "whatever"}))
    errs = [i for i in issues if i.code == "param_unsupported"]
    assert errs, f"{op}.{key} was accepted; it cannot be honoured"
    assert errs[0].severity == "error"
    assert key in errs[0].message
    assert _guidance_for(op, key) in errs[0].message, "the refusal must carry the guidance"
    assert errs[0].target_id == "f1"


def test_the_deprecated_and_absent_keys_are_refused_too():
    """``depth_type``/``fuse_order`` are not properties at all."""
    for op, key in (("pad", "depth_type"), ("groove", "fuse_order")):
        errs = [i for i in validate_ir(_doc(op, {key: True}))
                if i.code == "param_unsupported"]
        assert errs and errs[0].severity == "error", (op, key)


def test_axis_is_still_accepted_for_revolution():
    """The structurally-handled exception must keep working."""
    issues = validate_ir(_doc("revolution", {"angle": 360.0, "axis": "V_Axis"}))
    assert not [i for i in issues if i.code == "param_unsupported"]
    assert not [i for i in issues if i.severity == "error"], issues


def test_a_genuinely_unknown_key_still_only_warns():
    """Unknown is not the same as known-bad: keep the permissive path."""
    issues = validate_ir(_doc("pad", {"length": 5.0, "wobble": 1}))
    assert [i for i in issues if i.code == "param_unknown"]
    assert not [i for i in issues if i.code == "param_unsupported"]


def test_experimental_ops_are_still_treated_permissively():
    """Their tables are unverified, so key-level refusal would be a guess."""
    assert "multi_transform" in _UNVERIFIED_OPS
    issues = validate_ir(_doc("multi_transform", {"whatever": 1}))
    assert not [i for i in issues if i.code == "param_unsupported"]


# ══════════════════════════════════════════════════════════════════════════
# R-7: the dead `_PROFILE_OPS` set is gone
# ══════════════════════════════════════════════════════════════════════════


def test_the_dead_profile_ops_set_is_gone():
    """It was assigned once and read nowhere — a name the compiler never used."""
    source = (Path(__file__).resolve().parents[2]
              / "tcad" / "worker" / "compiler.py").read_text(encoding="utf-8")
    assert "_PROFILE_OPS" not in source

