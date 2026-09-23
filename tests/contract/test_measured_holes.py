"""Holes counted, sized and depth-checked from the real BRep — not from the IR.

Why this exists: the Gate used to answer a hole requirement by reading the IR
(``params["diameter"]`` or the profile circle's radius) and calling the result
"measured". That is the generator grading its own paper — a model that wrote
`diameter: 6` passed a 6 mm requirement even if the kernel cut 8 mm, cut
nothing, or cut in the wrong place.

So the worker now measures holes off the built shape (concave cylindrical
faces: diameter, axis, depth, through/blind) and the Gate judges only against
that. Everything below is asserted against real OCC geometry, because none of
it is knowable from a fake:

  * four through holes must come back as FOUR measured holes at the declared
    axes with the declared diameter and a depth equal to the plate thickness —
    the volume identity alone cannot tell four Ø6 holes from two Ø8.5 ones;
  * a blind pocket must report its depth and NOT be through (§6 requires 盲孔,
    and nothing anywhere had tested a finite-depth pocket before);
  * a padded cylinder's outer surface must NOT be reported as a hole —
    otherwise a boss is evidence of the bore that was asked for.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcad.core.types import GeometryDigest
from tcad.core.worker_client import WorkerHandle
from tcad.ir.schema import ConstraintExpr, IrDocument
from tcad.verify.specexpr import evaluate
from tcad.worker.protocol import M_INTROSPECT

from tests.contract.test_samples_acceptance import holes_ir, tube_ir

REPO_ROOT = Path(__file__).resolve().parents[2]

FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not Path(FREECAD_CMD).exists(),
        reason="FreeCADCmd build not found (free-cad/FreeCAD/build/debug/bin/FreeCADCmd)",
    ),
]

ABS = 1e-6  # integer-mm geometry: a wrong extent is a bug, not noise


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="holes",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def digest_of(worker, ir: dict, tmp_path: Path) -> GeometryDigest:
    res = worker.request_sync(M_INTROSPECT, {"ir": ir, "out_dir": str(tmp_path)},
                              timeout_s=180.0)
    assert res.get("ok") is True, res
    return GeometryDigest.model_validate({k: v for k, v in res.items() if k != "ok"})


def blind_hole_ir(model_id="blind", depth=3.0):
    """Plate + one Ø10 pocket 3 mm deep — a 盲孔, not a through hole."""
    ir = holes_ir(model_id, 80, 50, 8, [(40.0, 25.0, 5.0)], "打一个直径10、深3的盲孔")
    ir["bodies"][0]["features"][1]["params"] = {
        "type": "Length", "length": float(depth), "reversed": True,
    }
    return ir


# ══════════════════════════════════════════════════════════════════════════
# 1. count, size and position come from the geometry
# ══════════════════════════════════════════════════════════════════════════


def test_four_through_holes_are_measured_as_four_holes(worker, tmp_path):
    holes = digest_of(worker, holes_ir(
        "mh_four", 80, 50, 8,
        [(10.0, 10.0, 3.0), (70.0, 10.0, 3.0), (10.0, 40.0, 3.0), (70.0, 40.0, 3.0)],
        "四个直径 6 的 Z 向通孔"), tmp_path).holes

    assert len(holes) == 4, [h.model_dump() for h in holes]
    for h in holes:
        assert h.diameter == pytest.approx(6.0, abs=ABS)
        assert h.axis[2] == pytest.approx(1.0, abs=ABS), "孔轴应归一化为 +Z"
        assert h.depth == pytest.approx(8.0, abs=ABS), "贯穿孔深度应等于板厚"
        assert h.through is True
    assert sorted((h.center[0], h.center[1]) for h in holes) == [
        (10.0, 10.0), (10.0, 40.0), (70.0, 10.0), (70.0, 40.0)]


def test_a_wrong_radius_is_measured_wrong(worker, tmp_path):
    """Same four positions, Ø8 instead of Ø6 — the measurement has to move.

    If this produced the same numbers as the test above, the "measurement"
    would be coming from somewhere other than the geometry.
    """
    digest = digest_of(worker, holes_ir(
        "mh_big", 80, 50, 8,
        [(10.0, 10.0, 4.0), (70.0, 10.0, 4.0), (10.0, 40.0, 4.0), (70.0, 40.0, 4.0)],
        "四个直径 8 的 Z 向通孔"), tmp_path)

    assert len(digest.holes) == 4
    assert {round(h.diameter, 6) for h in digest.holes} == {8.0}


# ══════════════════════════════════════════════════════════════════════════
# 2. blind holes — depth measured, not assumed through
# ══════════════════════════════════════════════════════════════════════════


def test_blind_pocket_reports_its_depth_and_is_not_through(worker, tmp_path):
    holes = digest_of(worker, blind_hole_ir(depth=3.0), tmp_path).holes

    assert len(holes) == 1, [h.model_dump() for h in holes]
    h = holes[0]
    assert h.diameter == pytest.approx(10.0, abs=ABS)
    assert h.depth == pytest.approx(3.0, abs=ABS), "盲孔深度必须实测，不能当成贯穿"
    assert h.through is False
    assert h.center[0] == pytest.approx(40.0, abs=ABS)
    assert h.center[1] == pytest.approx(25.0, abs=ABS)


def test_depth_separates_blind_from_through(worker, tmp_path):
    """The requirement a model gets wrong most often: 通孔 vs 盲孔.

    A 3 mm blind hole and an 8 mm through hole have the same diameter and axis;
    only the measured depth tells them apart, which is why depth is part of the
    digest rather than left to the volume.
    """
    blind = digest_of(worker, blind_hole_ir("mh_blind", depth=3.0), tmp_path).holes[0]
    through = digest_of(worker, holes_ir(
        "mh_through", 80, 50, 8, [(40.0, 25.0, 5.0)], "直径 10 的通孔"), tmp_path).holes[0]

    assert blind.through is False and through.through is True
    assert through.depth - blind.depth == pytest.approx(5.0, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 3. an outer cylinder is not a hole
# ══════════════════════════════════════════════════════════════════════════


def test_a_padded_cylinder_contributes_only_its_bore(worker, tmp_path):
    """Sample C's outer Ø30 surface is convex: material outside, void inside.

    Counting it would let a boss stand in for the coaxial Ø10 bore, and the
    digest would report two holes where there is one.
    """
    holes = digest_of(worker, tube_ir("mh_tube", 30, 40, 10, "圆筒"), tmp_path).holes

    assert len(holes) == 1, [h.model_dump() for h in holes]
    assert holes[0].diameter == pytest.approx(10.0, abs=ABS)
    assert holes[0].depth == pytest.approx(40.0, abs=ABS)
    assert holes[0].through is True


def test_a_plate_with_no_holes_measures_no_holes(worker, tmp_path):
    from tests.contract.test_sketch_planes import rect_ir

    digest = digest_of(worker, rect_ir("XY", 40, 20, 5, model_id="mh_solid"), tmp_path)
    assert digest.holes == []
    assert "no concave cylindrical face" in digest.text


# ══════════════════════════════════════════════════════════════════════════
# 4. the Gate judges a hole requirement with these numbers
# ══════════════════════════════════════════════════════════════════════════


def test_confirmed_hole_requirement_is_graded_by_the_kernel(worker, tmp_path):
    ir_dict = holes_ir(
        "mh_gate", 80, 50, 8,
        [(10.0, 10.0, 3.0), (70.0, 10.0, 3.0), (10.0, 40.0, 3.0), (70.0, 40.0, 3.0)],
        "四个直径 6 的 Z 向通孔")
    digest = digest_of(worker, ir_dict, tmp_path)
    ir = IrDocument.model_validate(ir_dict)

    ok, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=6.0, tol=0.05, confirmed=True),
        digest, ir)
    assert ok is True, info
    assert sorted(info["measured"].values()) == [6.0, 6.0, 6.0, 6.0]

    # The requirement the built part does not meet must not pass, and the
    # report has to show the measured value it missed.
    bad, info = evaluate(
        ConstraintExpr(kind="hole_diameter", value=8.0, tol=0.05, confirmed=True),
        digest, ir)
    assert bad is False
    assert set(info["measured"].values()) == {6.0}


def test_digest_text_carries_the_measurement(worker, tmp_path):
    """The model reads the digest as text — it has to see what was measured,
    or it will keep asserting its own IR numbers back at us."""
    text = digest_of(worker, holes_ir(
        "mh_text", 80, 50, 8, [(10.0, 10.0, 3.0)], "一个直径 6 的通孔"), tmp_path).text

    assert "holes measured on the BRep (1)" in text
    assert "d=6.0000" in text or "6.0000" in text
    assert "THROUGH" in text
