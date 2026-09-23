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


def _measured(index=0, diameter=5.0, center=(10.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0),
              depth=10.0, through=True):
    """One hole as the worker's BRep measurement reports it."""
    return {"index": index, "diameter": diameter, "radius": diameter / 2.0,
            "axis": list(axis), "center": list(center), "depth": depth,
            "through": through, "faces": 1}


def test_hole_diameter():
    """The verdict comes from the measured diameter, not the IR's number."""
    ir = _ir_with_hole(diameter=5.0)
    ok, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=5.0, tol=0.05, target="ft_hole"),
        make_digest(holes=[_measured(diameter=5.0)]), ir)
    assert ok is True, info
    assert info["measured"] == {"hole0": 5.0}

    bad, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=8.0, tol=0.05, target="ft_hole"),
        make_digest(holes=[_measured(diameter=5.0)]), ir)
    assert bad is False
    assert info["measured"] == {"hole0": 5.0}


def test_hole_position():
    """Position = distance from the expected point to the measured axis."""
    ir = _ir_with_hole(pos=(10.0, 0.0, 5.0))
    holes = [_measured(center=(10.0, 0.0, 0.0))]
    ok, _ = evaluate(
        ConstraintExpr(kind="hole_position", value={"x": 10.0, "y": 0.0, "z": 5.0},
                       tol=0.05, target="ft_hole"),
        make_digest(holes=holes), ir)
    assert ok is True
    bad, info = evaluate(
        ConstraintExpr(kind="hole_position", value={"x": 50.0, "y": 0.0, "z": 5.0},
                       tol=0.05, target="ft_hole"),
        make_digest(holes=holes), ir)
    assert bad is False
    assert info["axis_distance_mm"] == 40.0


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


# ── regressions: hole matching/diameter/position ────────────────────────────


def _ir_with_pocket_hole(center=(20.0, 15.0, 0.0), radius=3.0):
    """A hole made as a Pocket from a single-circle sketch (no op=hole)."""
    from tcad.ir.schema import SketchGeom, Vec3
    sk = SketchSpec(
        id="sk_ph", name="ph", plane=PlaneRef(kind="origin_plane", plane="XY"),
        geometry=[SketchGeom(id="c0", kind="circle",
                             points=[Vec3(x=center[0], y=center[1], z=center[2])],
                             radius=radius)],
    )
    pocket = FeatureSpec(id="ft_pocket", name="pocket_hole", op="pocket",
                         profile_sketch="sk_ph", params={"depth": 10.0})
    return IrDocument(model_id="m1", version=1,
                      bodies=[BodySpec(id="b", name="b", sketches=[sk], features=[pocket])])


def test_ir_declared_diameter_can_no_longer_pass_itself():
    """The regression this exists for: `measured` used to echo the IR param, so
    a model that wrote diameter=6 was graded on the number it wrote. Now a
    digest that measured 8 fails a 6 requirement whatever the IR says."""
    ir = _ir_with_hole(diameter=6.0, pos=(10.0, 10.0, 0.0))
    ok, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=6.0, tol=0.05, target="ft_hole"),
        make_digest(holes=[_measured(diameter=8.0, center=(10.0, 10.0, 0.0))]), ir)
    assert ok is False
    assert info["measured"] == {"hole0": 8.0}
    assert info["expected"] == 6.0


def test_hole_requirement_without_brep_evidence_is_unverified():
    """No measurement -> None (never a pass). The hole feature existing in the
    IR is the claim under test, not the proof of it."""
    ir = _ir_with_pocket_hole(radius=3.0)
    res, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=6.0, tol=0.05, confirmed=True),
        make_digest(), ir)
    assert res is None
    assert "no BRep hole measurement" in info["reason"]


def test_pocket_with_circle_profile_still_needs_a_measurement():
    """A circular Pocket IS a hole — matched by the IR, judged by the kernel."""
    ir = _ir_with_pocket_hole(center=(20.0, 15.0, 0.0), radius=3.0)
    holes = [_measured(diameter=6.0, center=(20.0, 15.0, 0.0))]
    ok, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=6.0, tol=0.05, target="ft_pocket"),
        make_digest(holes=holes), ir)
    assert ok is True, info
    assert info["measured"] == {"hole0": 6.0}
    assert info["through"] == {"hole0": True}


def test_declared_hole_the_kernel_never_cut_fails_rather_than_skips():
    """IR says a hole at (10,10); the BRep has one only at (70,10). That is a
    wrong build, not an unmeasurable one."""
    ir = _ir_with_pocket_hole(center=(10.0, 10.0, 0.0))
    holes = [_measured(diameter=6.0, center=(70.0, 10.0, 0.0))]
    ok, info = evaluate(
        ConstraintExpr(kind="hole_position", value={"x": 10.0, "y": 10.0, "z": 0.0},
                       tol=0.05, target="ft_pocket"),
        make_digest(holes=holes), ir)
    assert ok is False
    assert "no measured hole axis passes near" in info["reason"]


def test_pocket_multi_circle_sketch_cannot_be_identified():
    """Two circles in one profile is not an unambiguous hole — refuse to guess
    which measured hole the expression means."""
    from tcad.ir.schema import SketchGeom, Vec3
    sk = SketchSpec(
        id="sk_2c", name="two", plane=PlaneRef(kind="origin_plane", plane="XY"),
        geometry=[
            SketchGeom(id="c0", kind="circle",
                       points=[Vec3(x=0.0, y=0.0, z=0.0)], radius=2.0),
            SketchGeom(id="c1", kind="circle",
                       points=[Vec3(x=10.0, y=0.0, z=0.0)], radius=2.0),
        ],
    )
    pocket = FeatureSpec(id="ft_p2", name="p2", op="pocket",
                         profile_sketch="sk_2c", params={"depth": 5.0})
    ir = IrDocument(model_id="m1", version=1,
                    bodies=[BodySpec(id="b", name="b", sketches=[sk], features=[pocket])])
    res, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=4.0, tol=0.05, target="ft_p2"),
        make_digest(holes=[_measured(diameter=4.0, center=(0.0, 0.0, 0.0))]), ir)
    assert res is None
    assert "cannot identify" in info["reason"]
