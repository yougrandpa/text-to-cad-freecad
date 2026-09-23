"""Real-kernel proof for ``mirrored`` and the pattern ops (task §6).

``mirrored`` / ``linear_pattern`` / ``polar_pattern`` were carried in the IR and
compiled, but nothing measured them, so the capability table kept them at
EXPERIMENTAL — the same missing evidence ``revolution`` (§13), ``groove`` (§22),
face-attached sketches (§23) and ``fillet``/``chamfer`` (§24) each got before
being promoted. Two of the three could not work at all: their FreeCAD objects
take a **LinkSub** reference (``MirrorPlane``, ``Direction``, ``Axis``) that the
IR had no field for, so the reference was simply never set.

The identities are analytic and exact:

* mirroring doubles the material and reflects the envelope — no clearance, no
  approximation, so a mirror that reflects across the wrong plane, or drops the
  original, cannot satisfy both the volume and the bounding box;
* a pattern of a hole repeats the *same* cut, so the volume is
  ``w·h·t − n·π·r²·t`` for ``n`` occurrences that do not overlap. The positions
  are read back from the **measured** holes in ``ir_digest``, never from the IR
  parameters — that is what makes this evidence about geometry rather than about
  arithmetic on the request.

The axes and planes are named the way the tool description tells the model to
name them (``params.axis`` = "X"/"Y"/"Z", ``plane`` = origin plane / face), so a
description that drifted from the compiler would fail here.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_EXPORT, M_IMPORT_ASSET, M_INTROSPECT, M_REOPEN_EDIT

from tests.contract.test_sketch_planes import compile_ir, compile_error

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
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="patterns",
                          startup_timeout_s=180.0, request_timeout_s=180.0)
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def _p(x: float, y: float, z: float = 0.0) -> dict:
    return {"x": float(x), "y": float(y), "z": float(z)}


def plate_ir(model_id: str, x0: float = 0.0, x1: float = 80.0,
             y0: float = 0.0, y1: float = 50.0, t: float = 8.0) -> dict:
    """A rectangular plate on XY. The bounds are explicit so a part can be placed
    symmetrically about the origin when a polar pattern needs its axis inside."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return {"model_id": model_id, "version": 0, "bodies": [{
        "id": "body_1", "name": "body_1",
        "sketches": [{
            "id": "sk_plate", "name": "outline",
            "plane": {"kind": "origin_plane", "plane": "XY"},
            "geometry": [
                {"id": f"g{i}", "kind": "line",
                 "points": [_p(*pts[i]), _p(*pts[(i + 1) % 4])]}
                for i in range(4)
            ],
            "constraints": [{"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                            for i in range(4)],
        }],
        "features": [{"id": "ft_plate", "name": "plate", "op": "pad",
                      "profile_sketch": "sk_plate", "refs": [],
                      "params": {"length": t, "type": "Length"}}],
    }]}


def _add_hole(ir: dict, holes: list[tuple[float, float, float]]) -> dict:
    """One circle sketch of N through holes; *holes* is [(x, y, radius), …]."""
    ir["bodies"][0]["sketches"].append({
        "id": "sk_holes", "name": "holes",
        "plane": {"kind": "origin_plane", "plane": "XY"},
        "geometry": [
            {"id": f"h{i}", "kind": "circle", "points": [_p(x, y)], "radius": r}
            for i, (x, y, r) in enumerate(holes)
        ],
        "constraints": [{"type": "Radius", "refs": [i], "value": r}
                        for i, (_x, _y, r) in enumerate(holes)],
    })
    ir["bodies"][0]["features"].append({
        "id": "ft_holes", "name": "holes", "op": "pocket",
        "profile_sketch": "sk_holes", "refs": ["ft_plate"],
        "params": {"type": "ThroughAll", "reversed": True},
    })
    return ir


def _digest(worker, ir: dict, out_dir: Path) -> dict:
    compile_ir(worker, ir, out_dir)
    res = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir), "measure": True}, timeout_s=180.0)
    assert res.get("ok") is True, res
    return res


def _hole_centres(worker, ir: dict, out_dir: Path) -> list[tuple[float, float]]:
    """The measured hole axes, as (x, y), sorted — the model's own evidence path."""
    centres = []
    for h in _digest(worker, ir, out_dir).get("holes") or []:
        c = h["center"]
        centres.append((round(float(c[0]), 6), round(float(c[1]), 6)))
    return sorted(centres)


def _face_with_normal(worker, ir: dict, out_dir: Path, normal: tuple[float, float, float]) -> str:
    """The planar face whose outward normal is *normal*, picked from the digest.

    Same discipline as the edge tests: the name comes from ``ir_digest`` because
    that is where the tool description tells the model to get it.
    """
    for f in _digest(worker, ir, out_dir).get("faces") or []:
        n = f.get("normal") or [0.0, 0.0, 0.0]
        if all(abs(float(n[i]) - normal[i]) < 1e-6 for i in range(3)):
            return f["name"]
    raise AssertionError(f"no planar face with normal {normal} in the digest")


def _mirrored(ir: dict, plane: dict, fid: str = "ft_mirror") -> dict:
    ir["bodies"][0]["features"].append({
        "id": fid, "name": fid, "op": "mirrored", "refs": ["ft_plate"],
        "plane": plane,
    })
    return ir


def _pattern(ir: dict, op: str, params: dict, on: str = "ft_holes",
             fid: str | None = None) -> dict:
    ir["bodies"][0]["features"].append({
        "id": fid or f"ft_{op}", "name": op, "op": op,
        "refs": [on], "params": params,
    })
    return ir


# ══════════════════════════════════════════════════════════════════════════
# 1. mirror: the reflected copy is real material, in the right place
# ══════════════════════════════════════════════════════════════════════════


def test_mirroring_across_an_origin_plane_reflects_the_material(worker, tmp_path):
    """Mirror across XZ (normal Y): the envelope flips in Y, volume doubles."""
    ir = _mirrored(plate_ir("mir_xz"), {"kind": "origin_plane", "plane": "XZ"})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(2.0 * 80.0 * 50.0 * 8.0, rel=VOL_REL), (
        "镜像必须真的多出一份材料，而不是原地复制")
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(100.0, abs=ABS), "Y 方向的包络应翻倍"
    assert m["bbox"]["z"] == pytest.approx(8.0, abs=ABS)


def test_mirroring_across_the_origin_XY_plane_reflects_in_z(worker, tmp_path):
    """A different plane must give a different envelope — the plane is honoured."""
    ir = _mirrored(plate_ir("mir_xy"), {"kind": "origin_plane", "plane": "XY"})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["volume"] == pytest.approx(2.0 * 80.0 * 50.0 * 8.0, rel=VOL_REL)
    assert m["bbox"]["z"] == pytest.approx(16.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(50.0, abs=ABS)


def test_mirroring_across_a_named_face_uses_that_face(worker, tmp_path):
    """Mirror across the part's own +X face: the far side grows, the near side does not."""
    ir = plate_ir("mir_face")
    face = _face_with_normal(worker, ir, tmp_path / "digest", (1.0, 0.0, 0.0))
    _mirrored(ir, {"kind": "face", "feature_id": "ft_plate", "sub": face})

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["volume"] == pytest.approx(2.0 * 80.0 * 50.0 * 8.0, rel=VOL_REL)
    assert m["bbox"]["x"] == pytest.approx(160.0, abs=ABS), (
        f"以 +X 面（{face}）镜像应把材料翻到 x=160，而不是留在 80")
    assert m["bbox"]["y"] == pytest.approx(50.0, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 2. linear pattern: n copies of the same cut
# ══════════════════════════════════════════════════════════════════════════


def test_a_linear_pattern_repeats_a_hole_the_expected_number_of_times(worker, tmp_path):
    """3 occurrences over 30 mm from x=20 → holes at 20 / 35 / 50."""
    ir = _add_hole(plate_ir("lin_extent"), [(20.0, 25.0, 3.0)])
    _pattern(ir, "linear_pattern", {"axis": "X", "mode": "Extent",
                                    "length": 30.0, "occurrences": 3})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(
        80.0 * 50.0 * 8.0 - 3 * PI * 9.0 * 8.0, rel=VOL_REL), (
        "阵列把同一个孔在三个位置各切一次，削掉的量必须是 3 倍")
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(50.0, abs=ABS)

    centres = _hole_centres(worker, ir, tmp_path / "digest")
    assert [c[0] for c in centres] == pytest.approx([20.0, 35.0, 50.0], abs=ABS), (
        "孔位必须来自 BRep 实测（digest），而不是 IR 参数")
    assert all(abs(c[1] - 25.0) < ABS for c in centres), centres


def test_the_spacing_mode_uses_the_offset_instead_of_the_extent(worker, tmp_path):
    """Spacing + offset 25, 2 occurrences → holes at 20 and 45 (not 20 and 50)."""
    ir = _add_hole(plate_ir("lin_spacing"), [(20.0, 25.0, 3.0)])
    _pattern(ir, "linear_pattern", {"axis": "X", "mode": "Spacing",
                                    "offset": 25.0, "occurrences": 2})

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    assert res["measurements"]["volume"] == pytest.approx(
        80.0 * 50.0 * 8.0 - 2 * PI * 9.0 * 8.0, rel=VOL_REL)

    centres = _hole_centres(worker, ir, tmp_path / "digest2")
    assert [c[0] for c in centres] == pytest.approx([20.0, 45.0], abs=ABS)


def test_the_pattern_direction_is_honoured_not_ignored(worker, tmp_path):
    """Along Y instead of X: same volume, different envelope, different positions."""
    ir = _add_hole(plate_ir("lin_along_y"), [(20.0, 15.0, 3.0)])
    _pattern(ir, "linear_pattern", {"axis": "Y", "mode": "Spacing",
                                    "offset": 15.0, "occurrences": 2})

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    assert res["measurements"]["volume"] == pytest.approx(
        80.0 * 50.0 * 8.0 - 2 * PI * 9.0 * 8.0, rel=VOL_REL)

    centres = _hole_centres(worker, ir, tmp_path / "digest")
    assert sorted(c[1] for c in centres) == pytest.approx([15.0, 30.0], abs=ABS)
    assert all(abs(c[0] - 20.0) < ABS for c in centres), centres


# ══════════════════════════════════════════════════════════════════════════
# 3. polar pattern: the classic "N copies about an axis"
# ══════════════════════════════════════════════════════════════════════════


def test_a_polar_pattern_repeats_a_hole_about_the_named_axis(worker, tmp_path):
    """A plate centred on the origin, one hole at (15,0), 4 copies over 270°.

    Spacing is ``angle / (occurrences − 1)`` = 90°, so the measured centres are
    (15,0), (0,15), (−15,0), (0,−15) — a set that no wrong axis can produce.
    """
    ir = _add_hole(plate_ir("pol_plate", -30.0, 30.0, -30.0, 30.0), [(15.0, 0.0, 3.0)])
    _pattern(ir, "polar_pattern", {"axis": "Z", "angle": 270.0, "occurrences": 4})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(
        60.0 * 60.0 * 8.0 - 4 * PI * 9.0 * 8.0, rel=VOL_REL)
    assert m["bbox"]["x"] == pytest.approx(60.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(60.0, abs=ABS)

    got = _hole_centres(worker, ir, tmp_path / "digest")
    assert got == pytest.approx(
        [(-15.0, 0.0), (0.0, -15.0), (0.0, 15.0), (15.0, 0.0)], abs=ABS), (
        f"四个孔必须落在 0°/90°/180°/270° 上，实测为 {got}")


def test_two_occurrences_over_180_degrees_give_the_opposite_hole(worker, tmp_path):
    ir = _add_hole(plate_ir("pol_half", -30.0, 30.0, -30.0, 30.0), [(15.0, 0.0, 3.0)])
    _pattern(ir, "polar_pattern", {"axis": "Z", "angle": 180.0, "occurrences": 2})

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    assert res["measurements"]["volume"] == pytest.approx(
        60.0 * 60.0 * 8.0 - 2 * PI * 9.0 * 8.0, rel=VOL_REL)
    got = _hole_centres(worker, ir, tmp_path / "digest")
    assert got == pytest.approx([(-15.0, 0.0), (15.0, 0.0)], abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 4. parametric, not one-shot
# ══════════════════════════════════════════════════════════════════════════


def test_the_delivered_fcstd_reopens_and_the_pattern_stays_parametric(worker, tmp_path):
    """Reopening the file and raising Occurrences 3 → 5 must add two more cuts."""
    ir = _add_hole(plate_ir("lin_reopen"), [(10.0, 25.0, 3.0)])
    _pattern(ir, "linear_pattern", {"axis": "X", "mode": "Spacing",
                                    "offset": 10.0, "occurrences": 3})

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["step", "fcstd"]},
        timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")
    for fmt in ("step", "fcstd"):
        p = Path(out["files"][fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"

    summary = worker.request_sync(M_IMPORT_ASSET, {"path": out["files"]["step"]},
                                  timeout_s=180.0)
    assert summary["ok"] is True, summary
    assert float(summary["shape_summary"]["volume"]) == pytest.approx(
        80.0 * 50.0 * 8.0 - 3 * PI * 9.0 * 8.0, rel=STEP_REL)

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": out["files"]["fcstd"],
         "edits": [{"object": "ft_linear_pattern", "property": "Occurrences",
                    "value": 5}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")

    states = edited["feature_states"]
    assert states["ft_linear_pattern"]["type_id"] == "PartDesign::LinearPattern"
    assert "Invalid" not in states["ft_linear_pattern"]["state"], states["ft_linear_pattern"]
    assert states["ft_holes"]["type_id"] == "PartDesign::Pocket", "改阵列不能毁掉被阵列的切除"

    m = edited["measurements"]
    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(
        80.0 * 50.0 * 8.0 - 5 * PI * 9.0 * 8.0, rel=VOL_REL), (
        "Occurrences 3 → 5 之后必须多切两个孔 —— 说明它是参数化特征")


def test_the_delivered_fcstd_reopens_and_the_mirror_stays_parametric(worker, tmp_path):
    ir = _mirrored(plate_ir("mir_reopen"), {"kind": "origin_plane", "plane": "YZ"})
    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"), "exports": ["fcstd"]},
        timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")
    fcstd = out["files"]["fcstd"]
    assert Path(fcstd).exists()

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": fcstd,
         "edits": [{"object": "ft_mirror", "property": "Suppressed", "value": True}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")
    m = edited["measurements"]
    assert m["volume"] == pytest.approx(80.0 * 50.0 * 8.0, rel=VOL_REL), (
        "抑制镜像后应只剩底板 —— 说明镜像是一棵真实特征树上的节点")
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 5. failures are nameable, not silent
# ══════════════════════════════════════════════════════════════════════════


def test_a_pattern_without_an_axis_is_refused_rather_than_built_once(worker, tmp_path):
    """FreeCAD answers a missing Direction with ONE occurrence, not an error."""
    ir = _add_hole(plate_ir("lin_no_axis"), [(20.0, 25.0, 3.0)])
    _pattern(ir, "linear_pattern", {"mode": "Extent", "length": 30.0, "occurrences": 3})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert feature_id == "ft_linear_pattern", feature_id
    assert "axis" in message, message
    assert "occurrence" in message or "single" in message, (
        f"拒绝理由应说明缺轴会静默只生成一份：{message}")


def test_a_pattern_axis_that_is_not_a_body_axis_is_refused(worker, tmp_path):
    """H_Axis is the *sketch's* axis; a pattern repeats features, not a profile."""
    ir = _add_hole(plate_ir("lin_bad_axis"), [(20.0, 25.0, 3.0)])
    _pattern(ir, "linear_pattern", {"axis": "H_Axis", "mode": "Extent",
                                    "length": 30.0, "occurrences": 3})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "semantic", kind
    assert feature_id == "ft_linear_pattern", feature_id
    assert "H_Axis" in message, message


def test_a_mirror_without_a_plane_is_refused(worker, tmp_path):
    ir = plate_ir("mir_no_plane")
    _mirrored(ir, {})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert feature_id == "ft_mirror", feature_id
    assert "plane" in message, message


def test_a_mirror_face_that_does_not_exist_is_refused_with_the_available_faces(worker, tmp_path):
    ir = plate_ir("mir_bad_face")
    _mirrored(ir, {"kind": "face", "feature_id": "ft_plate", "sub": "Face99"})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "semantic", kind
    assert feature_id == "ft_mirror", feature_id
    assert "Face99" in message, message
    assert "Available faces" in message and "Face1" in message, (
        "报错必须列出这台机器上真实存在的面名")


def test_an_unknown_mirror_origin_plane_is_refused(worker, tmp_path):
    ir = _mirrored(plate_ir("mir_bad_plane"), {"kind": "origin_plane", "plane": "AB"})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert feature_id == "ft_mirror", feature_id
    assert "AB" in message and "XY" in message, message


def test_a_pattern_of_nothing_is_not_reported_as_a_success(worker, tmp_path):
    """No refs → nothing to repeat. Building it would leave the body Tip unchanged,
    which looks like success and changes nothing."""
    ir = plate_ir("lin_no_originals")
    ir["bodies"][0]["features"].append({
        "id": "ft_linear_pattern", "name": "pattern", "op": "linear_pattern",
        "refs": [], "params": {"axis": "X", "mode": "Extent",
                               "length": 30.0, "occurrences": 3},
    })

    try:
        res = compile_ir(worker, ir, tmp_path)
    except WorkerCallFailed as exc:
        assert exc.rpc_error.message, exc.rpc_error
        return
    assert res.get("ok") is False, (
        f"没有可重复特征的阵列不能算成功构建：{res}")
