"""Unit tests for the ConstraintExpr evaluator (design §4.6 item 5)."""

from __future__ import annotations

from tcad.ir.schema import (
    BodySpec, ConstraintExpr, FeatureSpec, IrDocument, PlaneRef, SketchSpec,
)
from tcad.verify.specexpr import evaluate
from tests.fixtures.gate_fixtures import make_digest


def _ir_with_hole(diameter=5.0, pos=(10.0, 0.0, 5.0)):
    sk = SketchSpec(id="sk_h", name="h", plane=PlaneRef(kind="origin_plane", plane="XY"))
    hole = FeatureSpec(id="ft_hole", name="hole", op="hole", profile_sketch="sk_h",
                       params={"Diameter": diameter, "x": pos[0], "y": pos[1], "z": pos[2]})
    return IrDocument(model_id="m1", version=1,
                      bodies=[BodySpec(id="b", name="b", sketches=[sk], features=[hole])])


def _ir_with_mirror(target="ft_orig"):
    orig = FeatureSpec(id=target, name="orig", op="pad", params={"length": 5.0})
    mir = FeatureSpec(id="ft_mirror", name="mir", op="mirrored", refs=[target])
    return IrDocument(model_id="m1", version=1,
                      bodies=[BodySpec(id="b", name="b", features=[orig, mir])])


def test_bbox_pass_and_fail():
    d = make_digest(bbox=(60, 40, 10))
    ok, info = evaluate(ConstraintExpr(kind="bbox", value={"x": 60, "y": 40, "z": 10}, tol=0.05), d, IrDocument(model_id="m1", version=1))
    assert ok is True
    bad, _ = evaluate(ConstraintExpr(kind="bbox", value={"x": 100, "y": 40, "z": 10}, tol=0.05), d, IrDocument(model_id="m1", version=1))
    assert bad is False


def test_volume_relative_tolerance():
    d = make_digest(volume=24000.0)
    ok, info = evaluate(ConstraintExpr(kind="volume", value=24000.0, tol=0.01), d, IrDocument(model_id="m1", version=1))
    assert ok is True
    bad, info = evaluate(ConstraintExpr(kind="volume", value=1000.0, tol=0.01), d, IrDocument(model_id="m1", version=1))
    assert bad is False
    assert info["rel_error"] > 0.01


def test_count_and_feature_count():
    d = make_digest(solids=1)
    ir = IrDocument(model_id="m1", version=1, bodies=[BodySpec(id="b", name="b",
                  features=[FeatureSpec(id="f1", name="f1", op="pad")])])
    ok, _ = evaluate(ConstraintExpr(kind="count", value=1), d, ir)
    assert ok is True
    fc_ok, _ = evaluate(ConstraintExpr(kind="feature_count", value=1, tol=0), d, ir)
    assert fc_ok is True
    fc_bad, _ = evaluate(ConstraintExpr(kind="feature_count", value=3, tol=0), d, ir)
    assert fc_bad is False


def test_hole_diameter():
    ir = _ir_with_hole(diameter=5.0)
    d = make_digest()
    ok, info = evaluate(ConstraintExpr(kind="hole_diameter", value=5.0, tol=0.05, target="ft_hole"), d, ir)
    assert ok is True
    bad, _ = evaluate(ConstraintExpr(kind="hole_diameter", value=8.0, tol=0.05, target="ft_hole"), d, ir)
    assert bad is False


def test_hole_position():
    ir = _ir_with_hole(pos=(10.0, 0.0, 5.0))
    d = make_digest()
    ok, _ = evaluate(ConstraintExpr(kind="hole_position", value={"x": 10.0, "y": 0.0, "z": 5.0}, tol=0.05, target="ft_hole"), d, ir)
    assert ok is True
    bad, _ = evaluate(ConstraintExpr(kind="hole_position", value={"x": 50.0, "y": 0.0, "z": 5.0}, tol=0.05, target="ft_hole"), d, ir)
    assert bad is False


def test_symmetric_found():
    ir = _ir_with_mirror("ft_orig")
    d = make_digest()
    ok, info = evaluate(ConstraintExpr(kind="symmetric", target="ft_orig"), d, ir)
    assert ok is True
    assert info["measured"]["counterpart"] == "ft_mirror"


def test_wall_thickness():
    d = make_digest(min_wall=2.0)
    ok, _ = evaluate(ConstraintExpr(kind="wall_thickness", value=1.0, tol=0.05), d, IrDocument(model_id="m1", version=1))
    assert ok is True
    d2 = make_digest(min_wall=0.3)
    bad, _ = evaluate(ConstraintExpr(kind="wall_thickness", value=1.0, tol=0.05), d2, IrDocument(model_id="m1", version=1))
    assert bad is False


def test_unmeasurable_returns_none():
    # no worker-provided min wall -> None (caller must SKIP, never pass)
    d = make_digest(measurements_available=True)
    d.key_dimensions.pop("min_wall_thickness", None)
    res, _ = evaluate(ConstraintExpr(kind="wall_thickness", value=1.0), d, IrDocument(model_id="m1", version=1))
    assert res is None
