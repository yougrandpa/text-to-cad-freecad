"""The three mandatory acceptance samples, run against the real geometry kernel.

This is the §7 acceptance layer below the LLM: every number below is measured
by FreeCAD itself, never taken from the IR params. The NL→IR step for these
sentences (with differently-worded Chinese/English phrasings) is exercised at
the real-LLM e2e layer; the ``raw_text`` each builder carries is the sentence
that layer must produce this IR from. Different *numbers* per variant are
tested here, so nothing can pass by special-casing one figure.

  * Sample A — plate + rectangular through-slot. The throughness check is the
    volume identity V = w·h·t − slot_w·slot_h·t together with solids == 1: a
    blind slot would leave more material, an extra solid would raise the
    solids count, so neither can masquerade as the requested opening.
  * Sample B — plate + four Z-through holes, then "将这四个孔的直径改为 8，
    其余不变" across a worker restart. Closing worker #1 and reopening the
    delivered .FCStd in worker #2 is the kernel-level proof of "重启服务后
    继续修改": the file on disk carries the live Body/Sketch/Feature history.
    Preservation of the hole *centres* is asserted from the reopened document
    itself (circle centres in world coordinates), not inferred from a volume
    delta; the volume delta 4·π·(r₂²−r₁²)·t additionally proves only the radii
    changed (count, throughness, plate dims, thickness all intact).
  * Sample C — tube (axis Z) with coaxial through bore, then "高度改为 50" via
    a feature-property edit. x/y extents and the bore circle are asserted
    unchanged, so the only permitted difference is the height and its volume.

Tolerances (explicit, with reasons):

  * Volume rel = 1e-6 against the analytic formula. Measured OCC noise on these
    parts is ~1e-10 relative, while a wrong hole radius or slot size moves the
    volume by ≥1e-4 relative — 1e-6 sits six orders above the noise and two+
    below any real modelling error.
  * STEP readback rel = 1e-6: the .step on disk is re-imported and measured by
    the kernel and compared to the same analytic value. ASCII STEP truncates
    coordinates to ~15 significant digits (~1e-11 relative at these volumes),
    so 1e-6 leaves margin without hiding a geometry error.
  * Bbox abs = 1e-6: every coordinate here is an integer millimetre value and
    the plane transforms preserve integers to fp epsilon, so a wrong extent is
    a bug, not noise.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerHandle
from tcad.worker.protocol import (
    M_COMPILE_IR,
    M_EXPORT,
    M_IMPORT_ASSET,
    M_REOPEN_EDIT,
)

from tests.contract.test_sketch_planes import compile_ir, rect_ir

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

PI = math.pi
VOL_REL = 1e-6      # see module docstring: OCC noise ~1e-10, real errors ≥1e-4
STEP_REL = 1e-6     # ASCII STEP truncates at ~15 significant digits (~1e-11 here)
ABS = 1e-6          # integer-mm coordinates, exact through plane transforms


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="samples",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


# ══════════════════════════════════════════════════════════════════════════
# IR builders — world coordinates, written the way the model writes them
# ══════════════════════════════════════════════════════════════════════════


def _coincident_chain(n: int) -> list[dict]:
    """Close an n-line loop end-to-end. Deliberately NO origin anchor: a
    Coincident that pins a corner which is not actually at the origin
    conflicts with the given coordinates, and the solver silently drags just
    that corner to the origin — shearing the profile into a different shape
    with NO error (measured: slot volume off by exactly the sheared area)."""
    return [{"type": "Coincident", "refs": [i, 2, (i + 1) % n, 1]}
            for i in range(n)]


def plate_ir(model_id: str, w: float, h: float, t: float, raw_text: str) -> dict:
    ir = rect_ir("XY", w, h, t, model_id=model_id)
    ir["requirements"]["raw_text"] = raw_text
    ir["bodies"][0]["sketches"][0]["id"] = "sk_plate"
    ir["bodies"][0]["features"][0]["id"] = "ft_plate"
    ir["bodies"][0]["features"][0]["profile_sketch"] = "sk_plate"
    return ir


def slot_ir(model_id: str, w, h, t, x0, y0, sw, sh, raw_text: str) -> dict:
    """Plate + rectangular through-slot whose opening is x∈[x0,x0+sw], y∈[y0,y0+sh]."""
    ir = plate_ir(model_id, w, h, t, raw_text)
    pts = [(x0, y0), (x0 + sw, y0), (x0 + sw, y0 + sh), (x0, y0 + sh)]
    ir["bodies"][0]["sketches"].append({
        "id": "sk_slot", "name": "slot",
        "plane": {"kind": "origin_plane", "plane": "XY"},
        "geometry": [
            {"id": f"g{i}", "kind": "line",
             "points": [{"x": px, "y": py, "z": 0}, {"x": qx, "y": qy, "z": 0}]}
            for i, ((px, py), (qx, qy)) in enumerate(zip(pts, pts[1:] + pts[:1]))
        ],
        "constraints": _coincident_chain(4),
    })
    ir["bodies"][0]["features"].append({
        "id": "ft_slot", "name": "slot", "op": "pocket",
        "profile_sketch": "sk_slot", "refs": ["ft_plate"],
        "params": {"type": "ThroughAll", "reversed": True},
    })
    return ir


def holes_ir(model_id: str, w, h, t, holes: list[tuple[float, float, float]],
             raw_text: str) -> dict:
    """Plate + N through holes; *holes* is [(x, y, radius), ...] on the XY plane."""
    ir = plate_ir(model_id, w, h, t, raw_text)
    ir["bodies"][0]["sketches"].append({
        "id": "sk_holes", "name": "holes",
        "plane": {"kind": "origin_plane", "plane": "XY"},
        "geometry": [
            {"id": f"h{i}", "kind": "circle",
             "points": [{"x": x, "y": y, "z": 0}], "radius": r}
            for i, (x, y, r) in enumerate(holes)
        ],
        "constraints": [
            {"type": "Radius", "refs": [i], "value": r}
            for i, (_x, _y, r) in enumerate(holes)
        ],
    })
    ir["bodies"][0]["features"].append({
        "id": "ft_holes", "name": "holes", "op": "pocket",
        "profile_sketch": "sk_holes", "refs": ["ft_plate"],
        "params": {"type": "ThroughAll", "reversed": True},
    })
    return ir


def tube_ir(model_id: str, od: float, height: float, bore_d: float,
            raw_text: str) -> dict:
    """Tube about the Z axis at the origin: outer circle pad + coaxial bore."""
    r_out, r_in = od / 2.0, bore_d / 2.0

    def circle(cid: str, r: float) -> dict:
        return {"id": cid, "kind": "circle",
                "points": [{"x": 0.0, "y": 0.0, "z": 0}], "radius": r}

    return {
        "model_id": model_id, "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [
                {"id": "sk_tube", "name": "tube_profile",
                 "plane": {"kind": "origin_plane", "plane": "XY"},
                 "geometry": [circle("g0", r_out)],
                 "constraints": [{"type": "Radius", "refs": [0], "value": r_out}]},
                {"id": "sk_bore", "name": "bore",
                 "plane": {"kind": "origin_plane", "plane": "XY"},
                 "geometry": [circle("g0", r_in)],
                 "constraints": [{"type": "Radius", "refs": [0], "value": r_in}]},
            ],
            "features": [
                {"id": "ft_tube", "name": "tube", "op": "pad",
                 "profile_sketch": "sk_tube", "refs": [],
                 "params": {"length": float(height), "type": "Length"}},
                {"id": "ft_bore", "name": "bore", "op": "pocket",
                 "profile_sketch": "sk_bore", "refs": ["ft_tube"],
                 "params": {"type": "ThroughAll", "reversed": True}},
            ],
        }],
        "requirements": {"raw_text": raw_text, "constraints": []},
        "notes": [],
    }


# ══════════════════════════════════════════════════════════════════════════
# Delivery helpers: one build, real files on disk, STEP read back by the kernel
# ══════════════════════════════════════════════════════════════════════════


def export_all(worker, ir: dict, out_dir: Path) -> dict:
    return worker.request_sync(
        M_EXPORT,
        {"ir": ir, "out_dir": str(out_dir), "exports": ["step", "stl", "fcstd"]},
        timeout_s=180.0,
    )


def step_volume(worker, path: str) -> float:
    """Re-import a STEP file and measure it — evidence the bytes on disk are
    the geometry we think they are, not a stale or truncated export."""
    res = worker.request_sync(M_IMPORT_ASSET, {"path": path}, timeout_s=180.0)
    assert res["ok"] is True, f"STEP re-import failed: {res}"
    return float(res["shape_summary"]["volume"])


# ══════════════════════════════════════════════════════════════════════════
# Sample A — plate with a rectangular through-slot
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    dict(w=80.0, h=50.0, t=8.0, x0=20.0, y0=15.0, sw=40.0, sh=20.0,
         raw_text="80×50×8 的矩形板，中心有 40×20 的矩形贯穿开口",
         volume=80 * 50 * 8 - 40 * 20 * 8),                    # 25600
    dict(w=100.0, h=60.0, t=10.0, x0=25.0, y0=20.0, sw=50.0, sh=25.0,
         raw_text="Plate 100 wide, 60 deep, 10 thick; cut a 50 by 25 rectangular "
                  "opening straight through, corners at x 25..75, y 20..45",
         volume=100 * 60 * 10 - 50 * 25 * 10),                 # 47500
])
def test_sample_a_plate_with_rectangular_through_slot(worker, tmp_path, case):
    ir = slot_ir(f"sample_a_{case['w']:.0f}", case["w"], case["h"], case["t"],
                 case["x0"], case["y0"], case["sw"], case["sh"],
                 raw_text=case["raw_text"])

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["solids"] == 1, "开口必须是贯穿槽，不能多出实体"
    assert m["is_valid"] is True
    assert m["volume"] == pytest.approx(case["volume"], rel=VOL_REL)
    assert m["bbox"]["x"] == pytest.approx(case["w"], abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(case["h"], abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(case["t"], abs=ABS)

    out = export_all(worker, ir, tmp_path / "export")
    assert out["ok"] is True, out.get("errors")
    files = out["files"]
    for fmt in ("step", "stl", "fcstd"):
        p = Path(files[fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"
    assert step_volume(worker, files["step"]) == pytest.approx(
        case["volume"], rel=STEP_REL), "回读 STEP 的体积与解析值不符"


# ══════════════════════════════════════════════════════════════════════════
# Sample B — four through holes, resized across a worker (service) restart
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    dict(w=80.0, h=50.0, t=8.0,
         holes=[(10.0, 10.0, 3.0), (70.0, 10.0, 3.0),
                (10.0, 40.0, 3.0), (70.0, 40.0, 3.0)],
         new_radius=4.0,
         raw_text="80×50×8 的矩形板，四个直径 6 的 Z 向通孔，位置 (10,10)、(70,10)、(10,40)、(70,40)",
         edit_text="将这四个孔的直径改为 8，其余不变",
         volume=32000 - 4 * PI * 3.0**2 * 8,                   # 32000 − 288π
         volume_after=32000 - 4 * PI * 4.0**2 * 8),            # 32000 − 512π
    dict(w=100.0, h=60.0, t=10.0,
         holes=[(12.0, 14.0, 2.5), (85.0, 15.0, 2.5), (50.0, 45.0, 2.5)],
         new_radius=3.0,
         raw_text="A 100×60×10 plate with three Ø5 through holes on the Z axis at "
                  "(12,14), (85,15) and (50,45)",
         edit_text="enlarge those three holes to Ø6, keep everything else as-is",
         volume=60000 - 3 * PI * 2.5**2 * 10,                  # 60000 − 187.5π
         volume_after=60000 - 3 * PI * 3.0**2 * 10),           # 60000 − 270π
])
def test_sample_b_holes_resized_after_worker_restart(tmp_path, case):
    ir = holes_ir(f"sample_b_{case['w']:.0f}", case["w"], case["h"], case["t"],
                  case["holes"], raw_text=case["raw_text"])
    n = len(case["holes"])
    out_dir = tmp_path / "build"

    # ── turn 1 on worker #1: build, verify the analytic volume, deliver ──
    w1 = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="sample_b_turn1",
                      startup_timeout_s=180.0, request_timeout_s=180.0)
    w1.start()
    try:
        res = compile_ir(w1, ir, out_dir)
        assert res["ok"] is True, res.get("errors")
        m1 = res["measurements"]
        assert m1["solids"] == 1 and m1["is_valid"] is True
        assert m1["volume"] == pytest.approx(case["volume"], rel=VOL_REL)
        assert m1["bbox"]["x"] == pytest.approx(case["w"], abs=ABS)
        assert m1["bbox"]["y"] == pytest.approx(case["h"], abs=ABS)
        assert m1["bbox"]["z"] == pytest.approx(case["t"], abs=ABS)

        exported = w1.request_sync(
            M_EXPORT, {"ir": ir, "out_dir": str(out_dir),
                       "exports": ["fcstd", "step"]}, timeout_s=180.0)
        assert exported["ok"] is True, exported.get("errors")
        fcstd = Path(exported["files"]["fcstd"])
        assert fcstd.exists() and fcstd.stat().st_size > 0
    finally:
        w1.close()

    # ── restart: worker #2 reopens the delivered file and applies the edit ──
    w2 = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="sample_b_turn2",
                      startup_timeout_s=180.0, request_timeout_s=180.0)
    w2.start()
    try:
        edited = w2.request_sync(
            M_REOPEN_EDIT,
            {"fcstd_path": str(fcstd),
             "edits": [{"object": "sk_holes", "constraint": "Radius",
                        "value": case["new_radius"]}]},
            timeout_s=180.0,
        )
        assert edited["ok"] is True, f"{case['edit_text']}: {edited.get('errors')}"
        m2 = edited["measurements"]
        assert m2["solids"] == 1 and m2["is_valid"] is True
        assert m2["volume"] == pytest.approx(case["volume_after"], rel=VOL_REL)
        expected_delta = case["volume_after"] - case["volume"]
        assert m2["volume"] - m1["volume"] == pytest.approx(expected_delta, rel=VOL_REL), (
            "体积变化必须恰好等于 n·π·(r₂²−r₁²)·t —— 板尺寸/厚度/孔数/贯穿性都不许变"
        )
        assert m2["bbox"]["x"] == pytest.approx(case["w"], abs=ABS)
        assert m2["bbox"]["y"] == pytest.approx(case["h"], abs=ABS)
        assert m2["bbox"]["z"] == pytest.approx(case["t"], abs=ABS)

        states = edited["feature_states"]
        for name in ("sk_plate", "ft_plate", "sk_holes", "ft_holes"):
            assert name in states, f"重启后特征历史缺了 {name}"
        assert all("Invalid" not in s["state"] for s in states.values()), (
            "修改 + 重算后出现 Invalid 特征"
        )

        # hole centres preserved and radii == new value, read from the doc itself
        sk = edited["sketches"]["sk_holes"]
        circles = sorted(sk["circles"], key=lambda c: (c["x"], c["y"]))
        expected = sorted((x, y) for x, y, _r in case["holes"])
        assert len(circles) == n, f"重启后草图里只剩 {len(circles)} 个圆"
        for circ, (x, y) in zip(circles, expected):
            assert circ["x"] == pytest.approx(x, abs=ABS), "孔心 x 变了"
            assert circ["y"] == pytest.approx(y, abs=ABS), "孔心 y 变了"
            assert circ["radius"] == pytest.approx(case["new_radius"], abs=ABS)
        radii = [c["value"] for c in sk["constraints"] if c["type"] == "Radius"]
        assert len(radii) == n
        assert all(r == pytest.approx(case["new_radius"], abs=ABS) for r in radii)
    finally:
        w2.close()


# ══════════════════════════════════════════════════════════════════════════
# Sample C — tube with coaxial bore, then a height-only change
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    dict(od=30.0, height=40.0, bore=10.0, new_height=50.0,
         raw_text="轴线沿 Z、外径 30、高度 40、同轴通孔直径 10 的圆筒",
         edit_text="把高度改成 50，其它尺寸不动",
         volume=PI * (15.0**2 - 5.0**2) * 40,                  # 8000π
         volume_after=PI * (15.0**2 - 5.0**2) * 50),           # 10000π
    dict(od=40.0, height=30.0, bore=12.0, new_height=45.0,
         raw_text="A cylindrical tube about the Z axis, 40 mm outer diameter, "
                  "30 mm tall, with a coaxial Ø12 hole all the way through",
         edit_text="make it 45 mm tall, keep the outer diameter and the bore",
         volume=PI * (20.0**2 - 6.0**2) * 30,                  # 10920π
         volume_after=PI * (20.0**2 - 6.0**2) * 45),           # 16380π
])
def test_sample_c_tube_height_change_is_the_only_difference(worker, tmp_path, case):
    ir = tube_ir(f"sample_c_{case['od']:.0f}", case["od"], case["height"],
                 case["bore"], raw_text=case["raw_text"])

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m1 = res["measurements"]
    assert m1["solids"] == 1 and m1["is_valid"] is True
    assert m1["volume"] == pytest.approx(case["volume"], rel=VOL_REL)
    assert m1["bbox"]["x"] == pytest.approx(case["od"], abs=ABS)
    assert m1["bbox"]["y"] == pytest.approx(case["od"], abs=ABS)
    assert m1["bbox"]["z"] == pytest.approx(case["height"], abs=ABS)

    exported = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "export"),
                   "exports": ["fcstd"]}, timeout_s=180.0)
    assert exported["ok"] is True, exported.get("errors")
    fcstd = Path(exported["files"]["fcstd"])
    assert fcstd.exists() and fcstd.stat().st_size > 0

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": str(fcstd),
         "edits": [{"object": "ft_tube", "property": "Length",
                    "value": case["new_height"]}]},
        timeout_s=180.0,
    )
    assert edited["ok"] is True, f"{case['edit_text']}: {edited.get('errors')}"
    m2 = edited["measurements"]
    assert m2["solids"] == 1 and m2["is_valid"] is True
    assert m2["volume"] == pytest.approx(case["volume_after"], rel=VOL_REL)
    assert m2["volume"] - m1["volume"] == pytest.approx(
        case["volume_after"] - case["volume"], rel=VOL_REL)
    assert m2["bbox"]["z"] == pytest.approx(case["new_height"], abs=ABS)
    assert m2["bbox"]["x"] == pytest.approx(case["od"], abs=ABS), "外径变了"
    assert m2["bbox"]["y"] == pytest.approx(case["od"], abs=ABS), "外径变了"

    # the coaxial bore is untouched: same radius, same axis (origin)
    circles = edited["sketches"]["sk_bore"]["circles"]
    assert len(circles) == 1
    assert circles[0]["radius"] == pytest.approx(case["bore"] / 2.0, abs=ABS)
    assert circles[0]["x"] == pytest.approx(0.0, abs=ABS)
    assert circles[0]["y"] == pytest.approx(0.0, abs=ABS)

    states = edited["feature_states"]
    assert all("Invalid" not in s["state"] for s in states.values())
