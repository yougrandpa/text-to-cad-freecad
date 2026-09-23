"""Tests for tcad.ir.validate — semantic pre-checks."""

from __future__ import annotations

from tcad.ir.validate import ValidationIssue, validate_ir
from tcad.ir.schema import (
    BodySpec, FeatureSpec, IrDocument, PlaneRef, SketchSpec,
)

from .conftest import make_minimal_ir


def test_valid_ir_has_no_errors():
    ir = make_minimal_ir()
    issues = validate_ir(ir)
    assert not any(i.severity == "error" for i in issues), issues


def test_profile_sketch_missing_is_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="f", name="f", op="pad", profile_sketch="ghost",
                    params={"length": 1})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "profile_sketch_missing" and i.severity == "error"
               for i in issues)


def test_ref_missing_is_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="f", name="f", op="pad", refs=["ghost"], params={})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "ref_missing" and i.severity == "error" for i in issues)


def test_cycle_is_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="a", name="a", op="pad", refs=["b"], params={}),
        FeatureSpec(id="b", name="b", op="hole", refs=["a"], params={"diameter": 1}),
    ])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "ref_cycle" and i.severity == "error" for i in issues)


def test_acyclic_dag_passes():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="a", name="a", op="pad", params={"length": 1}),
        FeatureSpec(id="b2", name="b2", op="hole", refs=["a"], params={"diameter": 1}),
        FeatureSpec(id="c", name="c", op="hole", refs=["b2"], params={"diameter": 1}),
    ])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert not any(i.code == "ref_cycle" for i in issues)


def test_duplicate_id_is_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="x", name="x1", op="pad", params={}),
        FeatureSpec(id="x", name="x2", op="hole", params={}),
    ])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code in ("dup_feature_id", "dup_sketch_id")
               and i.severity == "error" for i in issues)


def test_datum_plane_target_must_be_datum_feature():
    body = BodySpec(
        id="b", name="b",
        sketches=[
            SketchSpec(id="sk", name="sk",
                       plane=PlaneRef(kind="datum_plane", feature_id="padfeat")),
        ],
        features=[
            FeatureSpec(id="padfeat", name="pf", op="pad", params={"length": 1}),
        ],
    )
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "datum_plane_target" and i.severity == "error"
               for i in issues)


def test_unknown_param_is_warn_not_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="f", name="f", op="pad",
                    params={"length": 1, "bogus_key": 0})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    warns = [i for i in issues if i.code == "param_unknown"]
    assert warns, issues
    assert all(i.severity == "warn" for i in warns)
    # warns must not block a commit
    assert not any(i.severity == "error" for i in issues)


def test_unverified_op_is_warn():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="f", name="f", op="multi_transform",
                    params={"some_prop": 1})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "op_unverified" and i.severity == "warn" for i in issues)
    assert not any(i.severity == "error" for i in issues)


# ── regressions: non-finite numbers are not dimensions ──────────────────────


def test_nan_geometry_point_is_error():
    import math
    ir = make_minimal_ir()
    sk = ir.bodies[0].sketches[0]
    bad = sk.geometry[0].model_copy(update={"points": [
        sk.geometry[0].points[0].model_copy(update={"x": math.nan}),
        sk.geometry[0].points[1],
    ]})
    ir.bodies[0].sketches[0] = sk.model_copy(update={"geometry": [bad] + list(sk.geometry[1:])})
    issues = validate_ir(ir)
    assert any(i.code == "non_finite_number" and i.severity == "error" for i in issues)


def test_inf_feature_param_is_error():
    body = BodySpec(id="b", name="b", features=[
        FeatureSpec(id="f", name="f", op="pad", params={"length": float("inf")})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "non_finite_number" and i.severity == "error" for i in issues)


def test_nan_constraint_value_is_error():
    from tcad.ir.schema import ConstraintExpr, RequirementSpec
    ir = make_minimal_ir()
    ir.requirements = RequirementSpec(constraints=[
        ConstraintExpr(kind="volume", value=float("nan"), confirmed=True)])
    issues = validate_ir(ir)
    assert any(i.code == "non_finite_number" and i.severity == "error" for i in issues)


# ══════════════════════════════════════════════════════════════════════════
# face-attached sketches must name a real feature and a sub-element
# ══════════════════════════════════════════════════════════════════════════


def _doc_with_face_plane(feature_id, sub):
    from tcad.ir.schema import (
        BodySpec, FeatureSpec, IrDocument, PlaneRef, SketchSpec,
    )
    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        sketches=[SketchSpec(id="sk_boss", name="boss",
                             plane=PlaneRef(kind="face", feature_id=feature_id, sub=sub))],
        features=[FeatureSpec(id="ft_plate", name="plate", op="pad",
                              params={"length": 8.0})],
    )])


def test_a_face_plane_must_reference_an_existing_feature():
    issues = validate_ir(_doc_with_face_plane("ft_nope", "Face6"))
    errs = [i for i in issues if i.code == "face_target"]
    assert errs and errs[0].severity == "error"
    assert "ft_nope" in errs[0].message
    assert errs[0].target_id == "sk_boss"


def test_a_face_plane_without_a_sub_element_is_refused():
    """Without a sub-element FreeCAD leaves the sketch unattached, and the
    profile then collapses into a misleading 'check your coordinates' error."""
    issues = validate_ir(_doc_with_face_plane("ft_plate", ""))
    errs = [i for i in issues if i.code == "face_sub_missing"]
    assert errs and errs[0].severity == "error"
    assert "Face6" in errs[0].message, "the message must show the expected spelling"


def test_a_well_formed_face_plane_passes():
    issues = validate_ir(_doc_with_face_plane("ft_plate", "Face6"))
    assert not [i for i in issues if i.code in ("face_target", "face_sub_missing")]


# ── patterns and mirrors: the reference the kernel silently ignores ─────────
#
# FreeCAD does NOT fail when a pattern's direction is unset — it returns a
# valid solid with ONE occurrence — and a mirror without a plane returns a NULL
# shape. Both are checked here so the refusal happens before the patch is
# persisted, with the alternative named.


def _doc_with_feature(op, params=None, plane=None, refs=None):
    from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument, PlaneRef
    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        features=[
            FeatureSpec(id="ft_plate", name="plate", op="pad", params={"length": 8.0}),
            FeatureSpec(id="ft_f", name="f", op=op, refs=refs if refs is not None else ["ft_plate"],
                        params=params or {}, plane=PlaneRef(**plane) if plane else None),
        ],
    )])


def test_a_pattern_without_an_axis_is_refused():
    issues = validate_ir(_doc_with_feature("linear_pattern", {"occurrences": 3}))
    errs = [i for i in issues if i.code == "pattern_axis_missing"]
    assert errs and errs[0].severity == "error"
    assert errs[0].target_id == "ft_f"
    assert "axis" in errs[0].message


def test_a_pattern_axis_must_be_a_body_axis_not_a_sketch_axis():
    """H_Axis belongs to the profile sketch; a pattern has no profile."""
    issues = validate_ir(_doc_with_feature("polar_pattern", {"axis": "H_Axis"}))
    errs = [i for i in issues if i.code == "pattern_axis_unknown"]
    assert errs and errs[0].severity == "error"
    assert "H_Axis" in errs[0].message and "'x'" in errs[0].message, errs[0].message


def test_a_pattern_with_a_body_axis_passes():
    issues = validate_ir(_doc_with_feature("linear_pattern", {"axis": "Y"}))
    assert not [i for i in issues if i.code.startswith("pattern_axis")]


def test_a_mirror_without_a_plane_is_refused():
    issues = validate_ir(_doc_with_feature("mirrored"))
    errs = [i for i in issues if i.code == "plane_missing"]
    assert errs and errs[0].severity == "error"
    assert "plane" in errs[0].message
    assert "MirrorPlane" in errs[0].message
    assert "null shape" in errs[0].message


def test_a_mirror_origin_plane_must_be_one_of_the_three():
    """An unknown datum is stopped at the schema boundary, so it can never reach
    FreeCAD as a mirror plane that silently yields a NULL shape."""
    import pytest
    from pydantic import ValidationError

    from tcad.ir.schema import PlaneRef
    with pytest.raises(ValidationError):
        PlaneRef(kind="origin_plane", plane="AB")  # type: ignore[arg-type]


def test_a_mirror_face_plane_reuses_the_attachment_checks():
    """A named face is checked exactly as a sketch attachment is."""
    issues = validate_ir(_doc_with_feature(
        "mirrored", plane={"kind": "face", "feature_id": "ft_nope", "sub": "Face6"}))
    assert [i for i in issues if i.code == "face_target"]
    issues = validate_ir(_doc_with_feature(
        "mirrored", plane={"kind": "face", "feature_id": "ft_plate", "sub": ""}))
    assert [i for i in issues if i.code == "face_sub_missing"]


def test_a_well_formed_mirror_plane_passes():
    issues = validate_ir(_doc_with_feature(
        "mirrored", plane={"kind": "origin_plane", "plane": "XZ"}))
    assert not [i for i in issues if i.code.startswith("plane")]


def test_a_plane_on_an_op_that_has_no_use_for_it_is_refused():
    """Silently dropping the field is how "I mirrored it" and "nothing happened"
    end up looking the same."""
    issues = validate_ir(_doc_with_feature(
        "pad", plane={"kind": "origin_plane", "plane": "XZ"}))
    errs = [i for i in issues if i.code == "plane_unused"]
    assert errs and errs[0].severity == "error"
    assert "mirrored" in errs[0].message


# ── draft / thickness: faces as sub-elements, and their own plane ───────────


def _doc_with_faces(op: str, *, faces=("Face1",), plane=None, params=None):
    from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument, PlaneRef

    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        features=[
            FeatureSpec(id="ft_plate", name="plate", op="pad", params={"length": 8.0}),
            FeatureSpec(id="ft_f", name="f", op=op, refs=["ft_plate"],
                        base_feature="ft_plate", sub_elements=list(faces),
                        params=params or {}, plane=PlaneRef(**plane) if plane else None),
        ],
    )])


def test_a_draft_without_a_neutral_plane_is_refused():
    """Measured: without NeutralPlane the kernel returns a NULL shape and keeps
    the previous Tip — the build would deliver the un-drafted box and look fine."""
    issues = validate_ir(_doc_with_faces("draft", params={"angle": 5.0}))
    errs = [i for i in issues if i.code == "plane_missing"]
    assert errs and errs[0].severity == "error"
    assert "NeutralPlane" in errs[0].message
    assert "null shape" in errs[0].message


def test_a_draft_without_faces_selects_nothing_and_is_refused():
    issues = validate_ir(_doc_with_faces(
        "draft", faces=(), plane={"kind": "origin_plane", "plane": "XY"},
        params={"angle": 5.0}))
    errs = [i for i in issues if i.code == "sub_elements_missing"]
    assert errs and errs[0].severity == "error"
    assert "face names" in errs[0].message and '"Face6"' in errs[0].message


def test_a_thickness_without_faces_selects_nothing_and_is_refused():
    issues = validate_ir(_doc_with_faces("thickness", faces=(),
                                         params={"value": 2.0}))
    errs = [i for i in issues if i.code == "sub_elements_missing"]
    assert errs and "face names" in errs[0].message


def test_a_well_formed_draft_passes():
    issues = validate_ir(_doc_with_faces(
        "draft", plane={"kind": "origin_plane", "plane": "XY"},
        params={"angle": 5.0}))
    assert not [i for i in issues if i.severity == "error"], issues


def test_a_plane_on_thickness_is_refused():
    """thickness opens a face; it reads no plane, and a plane would sit unread."""
    issues = validate_ir(_doc_with_faces(
        "thickness", plane={"kind": "origin_plane", "plane": "XY"},
        params={"value": 2.0}))
    errs = [i for i in issues if i.code == "plane_unused"]
    assert errs and errs[0].severity == "error"
    assert "NeutralPlane" in errs[0].message


# ── placement: primitives only, and complete ────────────────────────────────


def _doc_with_placement(op: str, placement: dict | None):
    from tcad.ir.schema import (BodySpec, FeatureSpec, IrDocument, PlacementSpec,
                                Vec3)

    spec = None
    if placement is not None:
        pos = placement["position"]
        spec = PlacementSpec(
            position=Vec3(**pos),
            axis=Vec3(**placement["axis"]) if placement.get("axis") else None,
            angle=float(placement.get("angle", 0.0)))
    return IrDocument(model_id="m", bodies=[BodySpec(
        id="b", name="b",
        features=[FeatureSpec(id="ft_p", name="p", op=op, placement=spec,
                              params={"radius": 3.0})])])


def test_a_placement_on_a_primitive_is_accepted():
    issues = validate_ir(_doc_with_placement(
        "additive_cylinder", {"position": {"x": 10.0, "y": 10.0, "z": 0.0}}))
    assert not [i for i in issues if i.severity == "error"], issues


def test_a_placement_on_an_op_that_positions_itself_is_refused():
    """A pad's position comes from its sketch; a second answer must not sit
    there unread, or "I placed it" and "it ignored me" look the same."""
    issues = validate_ir(_doc_with_placement(
        "pad", {"position": {"x": 10.0, "y": 10.0, "z": 0.0}}))
    errs = [i for i in issues if i.code == "placement_unused"]
    assert errs and errs[0].severity == "error"
    assert "sketch" in errs[0].message


def test_an_angle_without_an_axis_is_refused():
    """FreeCAD reads a missing axis as "no rotation" — a turned boss would come
    out un-turned and build cleanly."""
    issues = validate_ir(_doc_with_placement(
        "additive_cylinder",
        {"position": {"x": 0.0, "y": 0.0, "z": 0.0}, "angle": 90.0}))
    errs = [i for i in issues if i.code == "placement_axis_missing"]
    assert errs and errs[0].severity == "error"
    assert "placement.axis" in errs[0].message


def test_a_zero_axis_is_refused():
    issues = validate_ir(_doc_with_placement(
        "additive_cylinder",
        {"position": {"x": 0.0, "y": 0.0, "z": 0.0},
         "axis": {"x": 0.0, "y": 0.0, "z": 0.0}, "angle": 45.0}))
    assert [i for i in issues if i.code == "placement_axis_zero"]


def test_a_non_finite_placement_is_refused():
    issues = validate_ir(_doc_with_placement(
        "additive_box", {"position": {"x": float("nan"), "y": 0.0, "z": 0.0}}))
    assert [i for i in issues if i.code == "non_finite_number"]
