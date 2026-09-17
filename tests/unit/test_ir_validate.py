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
        FeatureSpec(id="f", name="f", op="draft",
                    params={"some_prop": 1})])
    ir = IrDocument(model_id="m", bodies=[body])
    issues = validate_ir(ir)
    assert any(i.code == "op_unverified" and i.severity == "warn" for i in issues)
    assert not any(i.severity == "error" for i in issues)
