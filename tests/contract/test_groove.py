"""Real-kernel proof for ``groove`` (task book §6: verify, then enable).

``revolution`` was promoted once its axis could be expressed; ``groove`` is the
same object family with the opposite sign — a revolved *cut*. The same scalar
``axis`` spelling drives it (``compiler._AXIS_OPS``), but "the axis machinery is
shared" is an argument, not evidence: what has to be measured is that a groove
removes exactly the ring its profile describes, leaves one valid solid, and stays
parametric in the delivered FCStd.

The profile here is an annular section of the cylinder wall, so the arithmetic is
closed-form: revolving it 360° about the axis removes ``π(R²−r²)·w``, and 180°
removes half of that.

Tolerances: analytic shapes, so OCC's error is float accumulation (~1e-10
relative) rather than a modelling approximation. ``1e-6`` relative volume and
``1e-6`` mm absolute size are machine-precision assertions, not an engineering
tolerance — see ``tests/contract/test_samples_acceptance.py`` for the rationale.

Recipe note (measured): the groove profile is closed with a Coincident chain and
takes its shape from the coordinates it is drawn at, like every other profile in
this repo. It must overlap the material, and it must be positioned so that the
sketch's vertical axis is the axis to revolve about (the sketch's own frame
follows any attachment or offset, a fixed global axis would not).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_EXPORT, M_IMPORT_ASSET, M_REOPEN_EDIT

from tests.contract.test_sketch_planes import compile_ir

REPO_ROOT = Path(__file__).resolve().parents[2]

FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]

PI = math.pi
VOL_REL = 1e-6
STEP_REL = 1e-6
ABS = 1e-6


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="groove",
                          startup_timeout_s=180.0, request_timeout_s=180.0)
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


# ══════════════════════════════════════════════════════════════════════════
# IR builder — a cylinder with a circumferential groove
# ══════════════════════════════════════════════════════════════════════════


def grooved_cylinder_ir(
    model_id: str,
    *,
    radius: float,
    height: float,
    groove_inner: float,
    groove_z: float,
    groove_width: float,
    axis: str = "V_Axis",
    angle: float = 360.0,
) -> dict:
    """Cylinder about Z, then an annular groove revolved out of its wall.

    The groove profile is the rectangle x∈[``groove_inner``, ``radius``],
    z∈[``groove_z``, ``groove_z+groove_width``] on XZ. Its outer edge lies exactly
    on the cylinder surface, so the cut is a clean ring.
    """
    def p(x: float, z: float) -> dict:
        return {"x": float(x), "y": 0.0, "z": float(z)}

    rect = [(groove_inner, groove_z), (radius, groove_z),
            (radius, groove_z + groove_width), (groove_inner, groove_z + groove_width)]

    return {
        "model_id": model_id, "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [
                {"id": "sk_outer", "name": "outer",
                 "plane": {"kind": "origin_plane", "plane": "XY"},
                 "geometry": [{"id": "g0", "kind": "circle",
                               "points": [p(0.0, 0.0)], "radius": radius}],
                 "constraints": [{"type": "Radius", "refs": [0], "value": radius}]},
                {"id": "sk_groove", "name": "groove_profile",
                 "plane": {"kind": "origin_plane", "plane": "XZ"},
                 "geometry": [
                     {"id": f"g{i}", "kind": "line",
                      "points": [p(*rect[i]), p(*rect[(i + 1) % 4])]}
                     for i in range(4)
                 ],
                 "constraints": [
                     {"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                     for i in range(4)
                 ]},
            ],
            "features": [
                {"id": "ft_cyl", "name": "cylinder", "op": "pad",
                 "profile_sketch": "sk_outer", "refs": [],
                 "params": {"length": float(height), "type": "Length"}},
                {"id": "ft_groove", "name": "groove", "op": "groove",
                 "profile_sketch": "sk_groove", "refs": ["ft_cyl"],
                 "params": {"angle": float(angle), "type": "Angle", "axis": axis}},
            ],
        }],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


def _volume(radius, height, groove_inner, groove_width, angle=360.0) -> float:
    solid = PI * radius ** 2 * height
    ring = PI * (radius ** 2 - groove_inner ** 2) * groove_width
    return solid - ring * (angle / 360.0)


def grooved_plate_ir(model_id: str, *, extra_params: dict | None = None,
                     angle: float = 90.0) -> dict:
    """An 80×50×8 plate with a quarter-ring groove revolved out of the corner.

    A cylinder cannot show which way a groove sweeps — it is rotationally
    symmetric, so both directions remove the same ring. The plate can: the
    groove profile is the rectangle x∈[30,40], z∈[2,6] on XZ, so the default
    quarter sweep (toward +Y, into the material) removes π(40²−30²)·4/4 = 700π,
    while the opposite sweep has nothing to bite and must be refused. The
    profile stays inside the plate's thickness on purpose — a groove that spans
    it would sever the corner and leave two solids.
    """
    pad = [(0.0, 0.0), (80.0, 0.0), (80.0, 50.0), (0.0, 50.0)]
    cut = [(30.0, 2.0), (40.0, 2.0), (40.0, 6.0), (30.0, 6.0)]
    params = {"angle": float(angle), "type": "Angle", "axis": "V_Axis"}
    params.update(extra_params or {})

    def line(i: int, pts: list[tuple[float, float]], prefix: str, plane: str) -> dict:
        """World coordinates for the plane the profile lives on.

        XY reads the pairs as (x, y); XZ reads them as (x, z) — the third
        component stays 0 either way, which is the WORLD COORDINATES contract.
        """
        (u0, v0), (u1, v1) = pts[i], pts[(i + 1) % len(pts)]
        if plane == "XY":
            p0, p1 = {"x": float(u0), "y": float(v0), "z": 0.0}, {"x": float(u1), "y": float(v1), "z": 0.0}
        else:
            p0, p1 = {"x": float(u0), "y": 0.0, "z": float(v0)}, {"x": float(u1), "y": 0.0, "z": float(v1)}
        return {"id": f"{prefix}{i}", "kind": "line", "points": [p0, p1]}

    return {
        "model_id": model_id, "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [
                {"id": "sk_plate", "name": "plate",
                 "plane": {"kind": "origin_plane", "plane": "XY"},
                 "geometry": [line(i, pad, "q", "XY") for i in range(4)],
                 "constraints": [{"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                                 for i in range(4)]},
                {"id": "sk_cut", "name": "groove_profile",
                 "plane": {"kind": "origin_plane", "plane": "XZ"},
                 "geometry": [line(i, cut, "c", "XZ") for i in range(4)],
                 "constraints": [{"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                                 for i in range(4)]},
            ],
            "features": [
                {"id": "ft_pad", "name": "plate", "op": "pad",
                 "profile_sketch": "sk_plate", "refs": [],
                 "params": {"length": 8.0, "type": "Length"}},
                {"id": "ft_groove", "name": "groove", "op": "groove",
                 "profile_sketch": "sk_cut", "refs": ["ft_pad"], "params": params},
            ],
        }],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


# ══════════════════════════════════════════════════════════════════════════
# 1. the cut removes exactly the ring the profile describes
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    # 9000π − 500π
    dict(radius=15.0, height=40.0, groove_inner=10.0, groove_z=18.0, groove_width=4.0),
    # numerically different in every dimension
    dict(radius=12.0, height=25.0, groove_inner=8.0, groove_z=10.0, groove_width=3.0),
    dict(radius=9.5, height=17.25, groove_inner=6.25, groove_z=6.5, groove_width=2.75),
])
def test_a_groove_removes_its_analytic_ring(worker, tmp_path, case):
    ir = grooved_cylinder_ir(
        f"gr_{case['radius']:.0f}_{case['groove_width']:.0f}".replace(".", "_"), **case)
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    expected = _volume(case["radius"], case["height"], case["groove_inner"],
                       case["groove_width"])
    assert m["solids"] == 1, "开槽后必须仍是单一有效实体"
    assert m["is_valid"] is True
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL)
    # A circumferential groove must not change the outer envelope.
    assert m["bbox"]["x"] == pytest.approx(2 * case["radius"], abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(2 * case["radius"], abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(case["height"], abs=ABS)


def test_half_a_revolution_removes_half_the_ring(worker, tmp_path):
    """The Angle really drives how much material goes, not just the label."""
    full = dict(radius=15.0, height=40.0, groove_inner=10.0, groove_z=18.0, groove_width=4.0)
    a = compile_ir(worker, grooved_cylinder_ir("gr_360", angle=360.0, **full), tmp_path / "a")
    b = compile_ir(worker, grooved_cylinder_ir("gr_180", angle=180.0, **full), tmp_path / "b")
    assert a["ok"] is True and b["ok"] is True, (a.get("errors"), b.get("errors"))

    removed_full = PI * 15.0 ** 2 * 40.0 - a["measurements"]["volume"]
    removed_half = PI * 15.0 ** 2 * 40.0 - b["measurements"]["volume"]
    assert removed_half == pytest.approx(removed_full / 2.0, rel=VOL_REL)
    assert b["measurements"]["volume"] == pytest.approx(_volume(15.0, 40.0, 10.0, 4.0, 180.0),
                                                        rel=VOL_REL)


def test_the_body_z_axis_and_the_sketch_v_axis_agree_for_a_groove(worker, tmp_path):
    kwargs = dict(radius=14.0, height=30.0, groove_inner=9.0, groove_z=12.0, groove_width=3.5)
    a = compile_ir(worker, grooved_cylinder_ir("gr_axis_v", axis="V_Axis", **kwargs), tmp_path / "a")
    b = compile_ir(worker, grooved_cylinder_ir("gr_axis_z", axis="Z", **kwargs), tmp_path / "b")
    assert a["ok"] is True and b["ok"] is True, (a.get("errors"), b.get("errors"))
    assert a["measurements"]["volume"] == pytest.approx(b["measurements"]["volume"], rel=VOL_REL)


def test_an_unrecognised_groove_axis_is_refused_not_guessed(worker, tmp_path):
    ir = grooved_cylinder_ir("gr_bad_axis", radius=15.0, height=40.0, groove_inner=10.0,
                             groove_z=18.0, groove_width=4.0, axis="the-long-one")
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    err = exc.value.rpc_error
    assert err.kind == "semantic", err.kind
    assert err.feature_id == "ft_groove"
    assert "the-long-one" in err.message


def test_a_groove_that_misses_the_material_is_a_build_error(worker, tmp_path):
    """Cutting air must not report success (task §5-B).

    The compiler already diagnoses this specifically — "did not change the
    solid" — rather than reporting a generic failure, so the assertion pins that
    wording: it is the message the model has to act on.
    """
    ir = grooved_cylinder_ir("gr_miss", radius=15.0, height=40.0,
                             groove_inner=20.0,   # entirely outside the cylinder
                             groove_z=18.0, groove_width=4.0)
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    err = exc.value.rpc_error
    assert err.feature_id == "ft_groove"
    assert "did not change the solid" in err.message
    assert "WORLD COORDINATES" in err.message, (
        "the hint must point at the contract that decides whether a cut bites")


def test_the_sweep_direction_decides_whether_a_groove_bites(worker, tmp_path):
    """The default quarter sweep cuts into +Y; ``reversed`` sweeps the other way.

    Measured on a plate rather than a cylinder, because a cylinder is rotationally
    symmetric and cannot tell the two directions apart. Both knobs are accepted by
    the validator for groove (and revolution), so what they do here is asserted,
    not assumed: default removes the full quarter ring, ``reversed`` misses the
    material entirely and is refused by name, ``midplane`` removes half of it.
    """
    plate_v = 80.0 * 50.0 * 8.0
    quarter_ring = PI * (40.0 ** 2 - 30.0 ** 2) * 4.0 / 4.0  # 700π

    res = compile_ir(worker, grooved_plate_ir("gr_plate_default"), tmp_path / "default")
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["volume"] == pytest.approx(plate_v - quarter_ring, rel=VOL_REL)
    assert m["solids"] == 1, "埋入板内的环槽不该把零件切成两块"
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(50.0, abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(8.0, abs=ABS)

    ir = grooved_plate_ir("gr_plate_reversed", extra_params={"reversed": True})
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path / "reversed")
    err = exc.value.rpc_error
    assert err.feature_id == "ft_groove"
    assert "did not change the solid" in err.message, err.message

    res = compile_ir(worker, grooved_plate_ir("gr_plate_mid", extra_params={"midplane": True}),
                     tmp_path / "midplane")
    assert res["ok"] is True, res.get("errors")
    assert res["measurements"]["volume"] == pytest.approx(plate_v - quarter_ring / 2.0,
                                                          rel=VOL_REL), (
        "midplane 只该切掉一半环（±45°）")
    assert res["measurements"]["solids"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 2. the delivered files are the geometry that was measured
# ══════════════════════════════════════════════════════════════════════════


def test_a_grooved_part_delivers_step_fcstd_and_reads_back(worker, tmp_path):
    case = dict(radius=15.0, height=40.0, groove_inner=10.0, groove_z=18.0, groove_width=4.0)
    ir = grooved_cylinder_ir("gr_delivery", **case)
    expected = _volume(case["radius"], case["height"], case["groove_inner"],
                       case["groove_width"])

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["step", "stl", "fcstd"]},
        timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")
    for fmt in ("step", "stl", "fcstd"):
        p = Path(out["files"][fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"

    summary = worker.request_sync(M_IMPORT_ASSET, {"path": out["files"]["step"]},
                                  timeout_s=180.0)
    assert summary["ok"] is True, summary
    assert float(summary["shape_summary"]["volume"]) == pytest.approx(expected, rel=STEP_REL)


def test_the_delivered_fcstd_reopens_and_the_groove_stays_parametric(worker, tmp_path):
    """Halving the groove's Angle must halve what it removes."""
    case = dict(radius=15.0, height=40.0, groove_inner=10.0, groove_z=18.0, groove_width=4.0)
    ir = grooved_cylinder_ir("gr_reopen", **case)
    solid = PI * case["radius"] ** 2 * case["height"]

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["step", "fcstd"]},
        timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": out["files"]["fcstd"],
         "edits": [{"object": "ft_groove", "property": "Angle", "value": 180.0}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")

    states = edited["feature_states"]
    assert states["ft_groove"]["type_id"] == "PartDesign::Groove"
    assert "Invalid" not in states["ft_groove"]["state"], states["ft_groove"]
    assert states["sk_groove"]["type_id"] == "Sketcher::SketchObject"
    assert states["ft_cyl"]["type_id"] == "PartDesign::Pad", "the pad must survive the edit"

    m = edited["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    removed_full = solid - _volume(case["radius"], case["height"], case["groove_inner"],
                                   case["groove_width"], 360.0)
    assert solid - m["volume"] == pytest.approx(removed_full / 2.0, rel=VOL_REL), (
        "180° 开槽必须恰好切掉一半的环 —— 说明 Angle 真的驱动几何")
    assert m["bbox"]["z"] == pytest.approx(case["height"], abs=ABS)
