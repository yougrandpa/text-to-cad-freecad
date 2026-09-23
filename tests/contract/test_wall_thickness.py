"""Minimum wall thickness measured on the real BRep — and judged against the
requirement the user actually stated.

Why this exists: ``wall_thickness`` was the one requirement kind with no
verifier anywhere. The first tier's ``WallThicknessCheck`` compares the digest
against the *config default* (``wall_thickness_min_mm``) and is advisory; the
second tier deliberately skips the kind (``checks_spec.FIRST_TIER_OWNED``) so it
would not double-count. Net effect: a confirmed "壁厚 3 mm" was accepted into the
requirement contract, and then nothing ever looked at it — while
``digest.key_dimensions["min_wall_thickness"]`` was, it turns out, never written
by any code in ``tcad/`` at all (only by test fixtures). The check could only
ever SKIP in production.

So two things are asserted here, and neither is knowable from a fake:

  * the worker measures the wall for real — an 80x50x8 plate reports 8.0 mm, a
    Ø30/Ø10 tube reports 10.0 mm (outer minus inner radius), and a plate with a
    through hole still reports the plate thickness rather than the ligament;
  * the Gate judges a confirmed requirement against that number and can FAIL.

Method note: the pairwise opposed-face distance (``distToShape`` + ``isInside``)
is used rather than a ray cast because the OCC ``IntCurvesFace`` intersector is
not present in this FreeCAD build's bindings — probed, ``ModuleNotFoundError``.
See ``tcad/worker/introspect.py::_measure_min_wall_thickness`` for the limits
that follow from that choice.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcad.core.types import CheckContext, CheckStatus, GeometryDigest, Severity
from tcad.core.worker_client import WorkerHandle
from tcad.ir.schema import ConstraintExpr, IrDocument
from tcad.verify.checks_solid import WallThicknessCheck
from tcad.worker.protocol import M_INTROSPECT

from tests.contract.test_samples_acceptance import holes_ir, plate_ir, tube_ir

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

ABS = 1e-6  # integer-mm geometry: a wrong wall is a bug, not noise


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="wall",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def measure(worker, ir: dict, tmp_path: Path) -> GeometryDigest:
    res = worker.request_sync(M_INTROSPECT, {"ir": ir, "out_dir": str(tmp_path)},
                              timeout_s=180.0)
    assert res.get("ok") is True, res
    return GeometryDigest.model_validate({k: v for k, v in res.items() if k != "ok"})


def gate_ctx(digest: GeometryDigest, required: float | None) -> CheckContext:
    ir = IrDocument(model_id=digest.model_id, version=digest.ir_version)
    if required is not None:
        ir.requirements.constraints = [
            ConstraintExpr(kind="wall_thickness", value=required, confirmed=True,
                           tol=0.01, source_text=f"壁厚 {required}mm"),
        ]
    return CheckContext(model_id=digest.model_id, ir_version=digest.ir_version,
                        ir=ir, artifact_dir=".", digest=digest)


@pytest.mark.parametrize(
    "make_ir, expected_wall, why",
    [
        (lambda: plate_ir("wall_plate", 80, 50, 8, "80x50x8 底板"),
         8.0, "a plate's wall is its thickness"),
        (lambda: tube_ir("wall_tube", 30.0, 40.0, 10.0, "外径30 内孔10 高40 圆筒"),
         10.0, "a tube's wall is outer radius minus inner radius"),
        (lambda: holes_ir("wall_hole", 80, 50, 8, [(10.0, 10.0, 3.0)],
                          "80x50x8 底板，一个直径6的通孔"),
         8.0, "a through hole must not be mistaken for the wall"),
    ],
    ids=["plate", "tube", "plate_with_hole"],
)
def test_the_worker_measures_the_real_wall(worker, tmp_path, make_ir, expected_wall, why):
    d = measure(worker, make_ir(), tmp_path)
    assert "min_wall_thickness" in d.key_dimensions, (
        "the worker did not measure a wall at all — every wall requirement will "
        "be unverifiable")
    assert d.key_dimensions["min_wall_thickness"] == pytest.approx(expected_wall, abs=ABS), why
    assert "min_wall_thickness" in d.text, "a measurement the model cannot see is wasted"


def test_a_tube_is_not_measured_as_its_thickness(worker, tmp_path):
    """Guard against the degenerate answer: for a Ø30/Ø10 tube the wall (10) is
    much larger than the bore radius, so a check that returned the smallest
    distance at all — e.g. the bounding box's short side — would look plausible
    and be wrong."""
    d = measure(worker, tube_ir("wall_tube_2", 30.0, 40.0, 10.0, "圆筒"), tmp_path)
    assert d.key_dimensions["min_wall_thickness"] == pytest.approx(10.0, abs=ABS)
    assert d.key_dimensions["min_wall_thickness"] != pytest.approx(20.0, abs=0.5)


def test_a_confirmed_requirement_is_satisfied_by_the_real_measurement(worker, tmp_path):
    d = measure(worker, plate_ir("wall_ok", 80, 50, 8, "厚8的底板"), tmp_path)
    r = WallThicknessCheck().run(gate_ctx(d, required=8.0))
    assert r.status == CheckStatus.PASS, r.message
    assert r.severity == Severity.BLOCKING
    assert r.measurements["min_wall_thickness"] == pytest.approx(8.0, abs=ABS)


def test_a_confirmed_requirement_the_real_part_misses_fails(worker, tmp_path):
    """Ask for a 9 mm wall on an 8 mm plate: the Gate must say so, not fall back
    to the advisory default (which would have passed)."""
    d = measure(worker, plate_ir("wall_bad", 80, 50, 8, "厚8的底板"), tmp_path)
    r = WallThicknessCheck().run(gate_ctx(d, required=9.0))
    assert r.status == CheckStatus.FAIL, r.message
    assert r.severity == Severity.BLOCKING
    assert "9" in r.message and "8" in r.message
