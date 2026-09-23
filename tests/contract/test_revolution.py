"""Real-kernel proof for ``revolution`` (task book §6: verify, then enable).

Why this file exists
--------------------
``tcad/ir/capability.py`` used to list ``revolution`` as EXPERIMENTAL with the
note "no real-kernel test". That was honest, but it left the op unusable: a
PartDesign::Revolution needs a ``ReferenceAxis``, which is an
``App::PropertyLinkSub`` — a shape no JSON scalar can carry. The compiler created
the object, never set the axis, and the feature either errored or produced
nothing while the build still reported success.

The compiler now translates a scalar ``axis`` name into that LinkSub
(``_set_axis_reference``). This module is the measurement that turns the claim
into a fact, on the real FreeCAD kernel:

  * the revolved solid's volume equals the analytic value of the profile of
    revolution, for two numerically different parts (a hard-coded example cannot
    pass both);
  * ``axis="Z"`` (the body's own Z axis) is equivalent to ``axis="V_Axis"`` (the
    sketch's vertical axis) for a sketch on XZ — i.e. both spellings reach the
    same real axis;
  * an unrecognised axis name is a *structured refusal*, never a silent build
    against a guessed axis;
  * the delivered STEP re-imports to the same volume, and the delivered FCStd
    reopens, keeps its Revolution feature, and responds to a parametric edit of
    its Angle.

Recipe note (measured, not assumed)
-----------------------------------
The profile is closed with a Coincident chain and takes its shape from the
coordinates it was drawn at — the same idiom the sample acceptance tests use.
Adding ``DistanceX``/``DistanceY`` on top of an already-closed chain was measured
to silently *move* a vertex (a shaft that should measure 2720π came out at
38453 mm³ with no error at all), so this file does not pretend those dimensions
are a safe way to size a revolved profile.

Tolerances: the shapes here are analytic, so OCC's error is float accumulation
(~1e-10 relative), not a modelling approximation. ``1e-6`` relative volume and
``1e-6`` mm absolute size are therefore machine-precision assertions, not
"engineering tolerance" — see ``tests/contract/test_samples_acceptance.py``.
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
    pytest.mark.skipif(
        not Path(FREECAD_CMD).exists(),
        reason="FreeCADCmd build not found",
    ),
]

PI = math.pi
VOL_REL = 1e-6
STEP_REL = 1e-6
ABS = 1e-6


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="revolution",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


# ══════════════════════════════════════════════════════════════════════════
# IR builder — a stepped shaft as a profile of revolution
# ══════════════════════════════════════════════════════════════════════════


def stepped_shaft_ir(
    model_id: str,
    *,
    r1: float,
    h1: float,
    r2: float,
    h2: float,
    axis: str = "V_Axis",
    angle: float = 360.0,
) -> dict:
    """Steps [(0,0) (r1,0) (r1,h1) (r2,h1) (r2,h1+h2) (0,h1+h2)] on XZ.

    The last edge lies on the axis, which is what makes the profile enclose a
    solid of revolution rather than a ring.
    """
    pts = [(0.0, 0.0), (r1, 0.0), (r1, h1), (r2, h1), (r2, h1 + h2), (0.0, h1 + h2)]
    n = len(pts)

    def p(x: float, z: float) -> dict:
        return {"x": float(x), "y": 0.0, "z": float(z)}

    return {
        "model_id": model_id,
        "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [{
                "id": "sk_shaft", "name": "shaft_profile",
                "plane": {"kind": "origin_plane", "plane": "XZ"},
                "geometry": [
                    {"id": f"g{i}", "kind": "line",
                     "points": [p(*pts[i]), p(*pts[(i + 1) % n])]}
                    for i in range(n)
                ],
                "constraints": [
                    {"type": "Coincident", "refs": [i, 2, (i + 1) % n, 1]}
                    for i in range(n)
                ],
            }],
            "features": [{
                "id": "ft_shaft", "name": "shaft", "op": "revolution",
                "profile_sketch": "sk_shaft", "refs": [],
                "params": {"angle": float(angle), "type": "Angle", "axis": axis},
            }],
        }],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


def _volume(r1: float, h1: float, r2: float, h2: float) -> float:
    return PI * (r1 ** 2 * h1 + r2 ** 2 * h2)


# ══════════════════════════════════════════════════════════════════════════
# 1. the geometry is what the profile of revolution says it is
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    dict(r1=10.0, h1=20.0, r2=6.0, h2=20.0),      # 2720π  ≈ 8545.13
    dict(r1=8.0, h1=5.0, r2=14.0, h2=30.0),       # 6200π  ≈ 19477.87
    dict(r1=12.5, h1=7.5, r2=3.25, h2=12.0),      # 1299.5625π ≈ 4082.65
])
def test_a_revolved_stepped_shaft_measures_its_analytic_volume(worker, tmp_path, case):
    ir = stepped_shaft_ir(f"rev_{case['r1']:.0f}_{case['r2']:.0f}".replace(".", "_"), **case)
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    expected = _volume(case["r1"], case["h1"], case["r2"], case["h2"])
    assert m["solids"] == 1, "旋转体必须是单一有效实体"
    assert m["is_valid"] is True
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL)

    total_h = case["h1"] + case["h2"]
    assert m["bbox"]["x"] == pytest.approx(2 * max(case["r1"], case["r2"]), abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(2 * max(case["r1"], case["r2"]), abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(total_h, abs=ABS)
    assert m["bbox"]["z_min"] == pytest.approx(0.0, abs=ABS), (
        "回转体应落在 z>=0：轮廓没有被静默平移")


def test_the_body_z_axis_and_the_sketch_v_axis_reach_the_same_axis(worker, tmp_path):
    """Both spellings of 'revolve about Z' must produce the same solid.

    They resolve to different LinkSubs (``body.Origin.OriginFeatures[2]`` vs
    ``(sketch, ["V_Axis"])``), so agreeing here is evidence the translation is
    right, not a tautology.
    """
    kwargs = dict(r1=9.0, h1=12.0, r2=4.0, h2=16.0)
    a = compile_ir(worker, stepped_shaft_ir("rev_axis_v", axis="V_Axis", **kwargs), tmp_path / "a")
    b = compile_ir(worker, stepped_shaft_ir("rev_axis_z", axis="Z", **kwargs), tmp_path / "b")
    assert a["ok"] is True and b["ok"] is True, (a.get("errors"), b.get("errors"))

    assert a["measurements"]["volume"] == pytest.approx(
        b["measurements"]["volume"], rel=VOL_REL)
    for axis_name in ("x", "y", "z"):
        assert a["measurements"]["bbox"][axis_name] == pytest.approx(
            b["measurements"]["bbox"][axis_name], abs=ABS)


def test_an_unrecognised_axis_is_refused_not_guessed(worker, tmp_path):
    """A wrong axis yields a plausible solid of the wrong size — refuse instead."""
    ir = stepped_shaft_ir("rev_bad_axis", r1=10.0, h1=20.0, r2=6.0, h2=20.0,
                          axis="the-long-one")
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    err = exc.value.rpc_error
    assert err.kind == "semantic", err.kind
    assert err.feature_id == "ft_shaft"
    assert "the-long-one" in err.message
    # The message has to be actionable, not just a rejection.
    assert "v_axis" in err.message.lower()


def test_the_direction_knobs_move_the_sweep_without_changing_it(worker, tmp_path):
    """``reversed``/``midplane`` are real on a Revolution, and they only move it.

    The per-op param whitelist accepts both on revolution, but no test had
    measured what they do here — and a knob that silently does nothing is worse
    than an absent one, because the model plans around it. A 90° sweep is the
    smallest case that separates the three: the shaft profile (max r=10) lands
    entirely on +Y by default, entirely on −Y when reversed, and symmetric about
    the profile plane when midplane — with the same quarter-volume throughout.
    """
    quarter = PI * (10.0 ** 2 * 20.0 + 6.0 ** 2 * 20.0) / 4.0  # 680π
    cases = [
        ("default", {}, 0.0, 10.0),
        ("reversed", {"reversed": True}, -10.0, 10.0),
        ("midplane", {"midplane": True}, -7.0710678118654755, 14.142135623730951),
    ]
    for name, extra, y_min, y_extent in cases:
        ir = stepped_shaft_ir(f"rev_knob_{name}", r1=10.0, h1=20.0, r2=6.0, h2=20.0,
                              angle=90.0)
        ir["bodies"][0]["features"][0]["params"].update(extra)

        res = compile_ir(worker, ir, tmp_path / name)

        assert res["ok"] is True, f"{name}: {res.get('errors')}"
        m = res["measurements"]
        assert m["volume"] == pytest.approx(quarter, rel=VOL_REL), (
            f"{name}: 换向/居中都不该改变 90° 回转体的体积")
        assert m["bbox"]["y_min"] == pytest.approx(y_min, abs=ABS), name
        assert m["bbox"]["y"] == pytest.approx(y_extent, abs=ABS), name
        assert m["bbox"]["z"] == pytest.approx(40.0, abs=ABS), (
            f"{name}: 扫掠方向只能在两个侧向之间变，高度不许动")


# ══════════════════════════════════════════════════════════════════════════
# 2. the delivered files are the geometry that was measured
# ══════════════════════════════════════════════════════════════════════════


def test_a_revolved_part_delivers_step_fcstd_and_reads_back(worker, tmp_path):
    case = dict(r1=10.0, h1=20.0, r2=6.0, h2=20.0)
    ir = stepped_shaft_ir("rev_delivery", **case)
    expected = _volume(**case)

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["step", "stl", "fcstd"]},
        timeout_s=180.0,
    )
    assert out["ok"] is True, out.get("errors")
    for fmt in ("step", "stl", "fcstd"):
        p = Path(out["files"][fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"

    summary = worker.request_sync(
        M_IMPORT_ASSET, {"path": out["files"]["step"]}, timeout_s=180.0)
    assert summary["ok"] is True, summary
    assert float(summary["shape_summary"]["volume"]) == pytest.approx(expected, rel=STEP_REL)


def test_the_delivered_fcstd_reopens_and_stays_parametric(worker, tmp_path):
    """A Revolve is not a parametric model unless reopening it still edits.

    Halving the Angle must halve the volume, and the Revolution feature must
    still be in the tree — that is the difference between a parametric document
    and a frozen mesh in a placeholder feature.
    """
    case = dict(r1=10.0, h1=20.0, r2=6.0, h2=20.0)
    ir = stepped_shaft_ir("rev_reopen", **case)
    full = _volume(**case)

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["step", "fcstd"]},
        timeout_s=180.0,
    )
    assert out["ok"] is True, out.get("errors")
    fcstd = out["files"]["fcstd"]

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": fcstd, "edits": [{"object": "ft_shaft", "property": "Angle",
                                         "value": 180.0}]},
        timeout_s=180.0,
    )
    assert edited["ok"] is True, edited.get("errors")

    states = edited["feature_states"]
    assert states["ft_shaft"]["type_id"] == "PartDesign::Revolution"
    assert "Invalid" not in states["ft_shaft"]["state"], states["ft_shaft"]
    assert states["sk_shaft"]["type_id"] == "Sketcher::SketchObject"

    m = edited["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(full / 2.0, rel=VOL_REL), (
        "180° 回转必须恰好是 360° 的一半 —— 说明 Angle 真的驱动几何")
