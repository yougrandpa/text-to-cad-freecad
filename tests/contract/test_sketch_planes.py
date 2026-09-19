"""A sketch's coordinates are WORLD coordinates, on every origin plane.

This exists because of a real defect, found by a real request ("创建一个手机支架模型")
that could not be built at all:

  * Sketcher stores geometry in the sketch's **local (u, v) frame**;
  * the compiler handed it world points unchanged, so only the XY plane worked by
    coincidence. A profile on XZ or YZ collapsed onto a line — no wire, no solid;
  * and the compile reported ``ok=false`` with an **empty error list**, which the
    RPC layer turned into the message "handler reported failure". Neither the
    model nor the user could see what had happened, so the model spent the turn
    probing and simplifying instead of repairing.

Three things must hold, and each is asserted against the real geometry kernel
because none of them is knowable from a fake:

  1. the same profile shape builds on XY, XZ and YZ (verified mapping:
     XY→(x,y), XZ→(x,z), YZ→(y,z));
  2. a failure says *which* sketch/feature and *why* — never a bare generic string;
  3. the pad's extrusion direction follows the plane normal, which is what the
     tool description promises the model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_COMPILE_IR

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


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="planes",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


# ══════════════════════════════════════════════════════════════════════════
# IR builders — written the way the model writes them (world coordinates)
# ══════════════════════════════════════════════════════════════════════════


def _rect_points(plane: str, w: float, h: float) -> list[dict]:
    """A w×h rectangle in world coordinates, lying in *plane*."""
    if plane == "XY":
        return [{"x": 0, "y": 0, "z": 0}, {"x": w, "y": 0, "z": 0},
                {"x": w, "y": h, "z": 0}, {"x": 0, "y": h, "z": 0}]
    if plane == "XZ":
        return [{"x": 0, "y": 0, "z": 0}, {"x": w, "y": 0, "z": 0},
                {"x": w, "y": 0, "z": h}, {"x": 0, "y": 0, "z": h}]
    return [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": w, "z": 0},
            {"x": 0, "y": w, "z": h}, {"x": 0, "y": 0, "z": h}]


def rect_ir(plane: str, w: float, h: float, length: float, *, model_id: str = "m") -> dict:
    pts = _rect_points(plane, w, h)
    geometry = [
        {"id": f"g{i}", "kind": "line", "points": [pts[i], pts[(i + 1) % 4]]}
        for i in range(4)
    ]
    constraints = [
        {"type": "Coincident", "refs": [0, 2, 1, 1]},
        {"type": "Coincident", "refs": [1, 2, 2, 1]},
        {"type": "Coincident", "refs": [2, 2, 3, 1]},
        {"type": "Coincident", "refs": [3, 2, 0, 1]},
        {"type": "Coincident", "refs": [0, 1, -1, 1]},
    ]
    return {
        "model_id": model_id,
        "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [{
                "id": "sk1", "name": "profile",
                "plane": {"kind": "origin_plane", "plane": plane},
                "geometry": geometry, "constraints": constraints,
            }],
            "features": [{
                "id": "ft1", "name": "pad", "op": "pad", "profile_sketch": "sk1",
                "params": {"length": length, "type": "Length"}, "refs": [],
            }],
        }],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


def compile_ir(worker, ir: dict, out_dir: Path) -> dict:
    return worker.request_sync(
        M_COMPILE_IR, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0
    )


def compile_error(worker, ir: dict, out_dir: Path) -> tuple[str, str | None, str]:
    """The (kind, feature_id, message) the supervisor would surface."""
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, out_dir)
    err = exc.value.rpc_error
    return err.kind, err.feature_id, err.message


# ══════════════════════════════════════════════════════════════════════════
# 1. the same profile builds on all three origin planes
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "plane,bbox_extent",
    [
        ("XY", {"x": 40.0, "y": 20.0, "z": 5.0}),
        ("XZ", {"x": 40.0, "y": 5.0, "z": 20.0}),
        ("YZ", {"x": 5.0, "y": 40.0, "z": 20.0}),
    ],
)
def test_a_world_coordinate_profile_builds_on_every_origin_plane(
    worker, tmp_path, plane, bbox_extent
):
    """40×20 padded 5 → a real 4000 mm³ solid, in the plane it was written for.

    The bbox is asserted too: it is what proves the sketch landed in the intended
    plane rather than merely producing *some* solid.
    """
    res = compile_ir(worker, rect_ir(plane, 40, 20, 5, model_id=f"plane_{plane}"), tmp_path)

    assert res["ok"] is True, f"{plane}: {res.get('errors')}"
    assert res["errors"] == []
    measurements = res["measurements"]
    assert measurements["solids"] == 1
    assert measurements["volume"] == pytest.approx(4000.0, rel=1e-6)
    for axis, extent in bbox_extent.items():
        assert measurements["bbox"][axis] == pytest.approx(extent, abs=1e-6), (
            f"{plane}: bbox.{axis} 不对 —— 草图没有落在目标平面上"
        )


def test_extrusion_direction_follows_the_plane_normal(worker, tmp_path):
    """The direction the tool description promises the model.

    A pad extrudes along the plane's normal: XY→+Z, XZ→−Y, YZ→+X. A model that
    plans a wedge across the width of a phone stand depends on knowing which way
    the material goes.
    """
    xy = compile_ir(worker, rect_ir("XY", 40, 20, 5, model_id="dir_xy"), tmp_path)
    xz = compile_ir(worker, rect_ir("XZ", 40, 20, 5, model_id="dir_xz"), tmp_path)
    yz = compile_ir(worker, rect_ir("YZ", 40, 20, 5, model_id="dir_yz"), tmp_path)

    assert xy["measurements"]["bbox"]["z_min"] == pytest.approx(0.0)
    assert xz["measurements"]["bbox"]["y_min"] == pytest.approx(-5.0)
    assert yz["measurements"]["bbox"]["x_min"] == pytest.approx(0.0)


def test_the_phone_stand_profile_that_could_not_be_built_now_builds(worker, tmp_path):
    """The real request, reduced to its geometry.

    This is the side profile from the failing session ("创建一个手机支架模型"): a
    wedge on the YZ plane, extruded across the width. Before the world→sketch-frame
    transform existed this produced *no solid and no error at all*.
    """
    pts = [
        {"x": 0, "y": 0, "z": 0},    # front bottom
        {"x": 0, "y": 0, "z": 24},   # front lip top
        {"x": 0, "y": 12, "z": 24},  # lip thickness
        {"x": 0, "y": 12, "z": 14},  # tray floor
        {"x": 0, "y": 25, "z": 14},
        {"x": 0, "y": 90, "z": 65},  # back rest, inclined
        {"x": 0, "y": 90, "z": 0},   # back bottom
    ]
    geometry = [
        {"id": f"g{i}", "kind": "line", "points": [pts[i], pts[(i + 1) % len(pts)]]}
        for i in range(len(pts))
    ]
    constraints = [
        {"type": "Coincident", "refs": [i, 2, (i + 1) % len(pts), 1]}
        for i in range(len(pts))
    ] + [{"type": "Coincident", "refs": [0, 1, -1, 1]}]
    ir = {
        "model_id": "stand",
        "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [{
                "id": "sk_stand", "name": "stand_profile",
                "plane": {"kind": "origin_plane", "plane": "YZ"},
                "geometry": geometry, "constraints": constraints,
            }],
            "features": [{
                "id": "ft_stand", "name": "stand", "op": "pad",
                "profile_sketch": "sk_stand",
                "params": {"length": 80.0, "type": "Length"}, "refs": [],
            }],
        }],
        "requirements": {"raw_text": "一个手机支架", "constraints": []},
        "notes": [],
    }

    res = compile_ir(worker, ir, tmp_path)

    assert res["ok"] is True, f"手机支架侧面轮廓仍然建不出来：{res.get('errors')}"
    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] > 0
    assert m["bbox"]["x"] == pytest.approx(80.0), "没有沿 X 挤出 80mm"
    assert m["bbox"]["y"] == pytest.approx(90.0)
    assert m["bbox"]["z"] == pytest.approx(65.0)


# ══════════════════════════════════════════════════════════════════════════
# 2. a failure names the sketch and the reason
# ══════════════════════════════════════════════════════════════════════════


def test_a_local_frame_profile_gets_a_message_that_says_so(worker, tmp_path):
    """The mistake is easy to make and, before, impossible to see.

    Writing (u, v, 0) for a YZ sketch collapses every point onto x=0. The error
    must name the sketch, say the profile encloses no area, and explain the
    coordinate rule — otherwise the model can only guess (and, in the failing
    session, it guessed for four steps and then blamed the worker).
    """
    ir = rect_ir("YZ", 90, 60, 80, model_id="local_frame")
    sk = ir["bodies"][0]["sketches"][0]
    local = [(0, 0), (90, 0), (90, 60), (0, 60)]
    for g, (u, v) in zip(sk["geometry"], local):
        g["points"] = [{"x": float(u), "y": float(v), "z": 0.0} for _ in g["points"]]

    kind, feature_id, message = compile_error(worker, ir, tmp_path)

    assert kind == "compile"
    assert feature_id == "sk1"
    assert "no area" in message
    assert "local (u, v)" in message, "没有告诉模型它写的是局部坐标"
    assert "YZ" in message, "没有说明 YZ 平面的坐标取哪两个分量"


def test_an_open_profile_names_the_sketch_that_does_not_close(worker, tmp_path):
    """A gap between two curves: the wire exists but is not closed."""
    ir = rect_ir("YZ", 90, 60, 80, model_id="open_profile")
    sk = ir["bodies"][0]["sketches"][0]
    sk["constraints"] = [c for c in sk["constraints"] if c["refs"] != [1, 2, 2, 1]]
    sk["geometry"][2]["points"][0] = {"x": 0.0, "y": 90.0, "z": 30.0}

    kind, feature_id, message = compile_error(worker, ir, tmp_path)

    assert kind == "compile"
    assert feature_id == "body_1"
    assert "sk1" in message, "没有指出是哪个草图"
    assert "closed=0" in message
    assert "gap" in message or "not closed" in message


def test_an_empty_document_says_there_is_nothing_to_build(worker, tmp_path):
    """Committing before patching is a normal mistake; it must not be a mystery."""
    ir = {
        "model_id": "empty", "version": 0, "bodies": [],
        "requirements": {"raw_text": "", "constraints": []}, "notes": [],
    }
    kind, _feature_id, message = compile_error(worker, ir, tmp_path)

    assert kind == "schema"
    assert "no bodies" in message
    assert "ir_patch" in message, "没有告诉模型下一步该做什么"


def test_no_failure_is_reported_without_a_reason(worker, tmp_path):
    """The generic string was the real complaint: "一直有报错" and nothing to read.

    Whatever the cause, a failed compile must carry a message that names something
    — a sketch, a feature, or at least what the payload contained.
    """
    for ir in (
        rect_ir("YZ", 90, 60, 80, model_id="ok_case"),
        {
            "model_id": "empty2", "version": 0, "bodies": [],
            "requirements": {"raw_text": "", "constraints": []}, "notes": [],
        },
    ):
        try:
            res = compile_ir(worker, ir, tmp_path)
        except WorkerCallFailed as exc:
            assert "handler reported failure" != exc.rpc_error.message.strip()
            assert len(exc.rpc_error.message) > 20
        else:
            assert res["ok"] is True


# ══════════════════════════════════════════════════════════════════════════
# 3. a sketch may hang off a feature's face
#
# The tool description advertises `plane: {"kind":"face", ...}` — "put a hole in
# the top face" is the most natural second operation in CAD. It could never work:
# every sketch was built before any feature, so the face's owner did not exist
# yet and the attachment failed with "face target not found", followed by a
# cascade that buried that one useful line.
# ══════════════════════════════════════════════════════════════════════════


def test_a_sketch_on_a_features_face_is_built_after_that_feature(worker, tmp_path):
    """Discriminating on purpose.

    The boss spans world z 8..13 on the box's y=0 face (Face3). Read as local
    (u, v) instead of world coordinates it would land at z 0..5, and the union
    bbox would stop at the box's own z=10 — so `z == 13` proves both the ordering
    fix and the world-coordinate reading at once.
    """
    ir = {
        "model_id": "face_attach", "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [{
                "id": "sk_side", "name": "boss_profile",
                "plane": {"kind": "face", "feature_id": "ft_box", "sub": "Face3"},
                "geometry": [
                    {"id": f"g{i}", "kind": "line", "points": [p, q]}
                    for i, (p, q) in enumerate([
                        ({"x": 30, "y": 0, "z": 8}, {"x": 40, "y": 0, "z": 8}),
                        ({"x": 40, "y": 0, "z": 8}, {"x": 40, "y": 0, "z": 13}),
                        ({"x": 40, "y": 0, "z": 13}, {"x": 30, "y": 0, "z": 13}),
                        ({"x": 30, "y": 0, "z": 13}, {"x": 30, "y": 0, "z": 8}),
                    ])
                ],
                "constraints": [
                    {"type": "Coincident", "refs": [0, 2, 1, 1]},
                    {"type": "Coincident", "refs": [1, 2, 2, 1]},
                    {"type": "Coincident", "refs": [2, 2, 3, 1]},
                    {"type": "Coincident", "refs": [3, 2, 0, 1]},
                ],
            }],
            "features": [
                {"id": "ft_box", "name": "box", "op": "additive_box",
                 "params": {"length": 40, "width": 20, "height": 10}, "refs": []},
                {"id": "ft_pad", "name": "boss", "op": "pad",
                 "profile_sketch": "sk_side",
                 "params": {"length": 5.0, "type": "Length"}, "refs": ["ft_box"]},
            ],
        }],
        "requirements": {"raw_text": "", "constraints": []}, "notes": [],
    }

    res = compile_ir(worker, ir, tmp_path)

    assert res["ok"] is True, f"面上草图的附着仍然失败：{res.get('errors')}"
    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(8000.0 + 250.0, rel=1e-6), "凸台没有长在盒子上"
    assert m["bbox"]["y"] == pytest.approx(25.0), "凸台没有朝 -Y 挤出"
    assert m["bbox"]["z"] == pytest.approx(13.0), "凸台没有落在世界坐标 z=8..13"


def test_a_dependency_cycle_is_reported_rather_than_guessed_at(worker, tmp_path):
    """Two features referencing each other can never be built in any order.

    Silence here would be the worst answer: the build would produce something
    arbitrary and the model would have no way to know its IR was incoherent.
    """
    def feature(fid, refs):
        return {"id": fid, "name": fid, "op": "pad", "profile_sketch": "sk1",
                "params": {"length": 5.0, "type": "Length"}, "refs": refs}

    ir = {
        "model_id": "cycle", "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [{
                "id": "sk1", "name": "p",
                "plane": {"kind": "origin_plane", "plane": "XY"},
                "geometry": [
                    {"id": "g0", "kind": "line", "points": [{"x": 0, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0}]},
                    {"id": "g1", "kind": "line", "points": [{"x": 10, "y": 0, "z": 0}, {"x": 10, "y": 10, "z": 0}]},
                    {"id": "g2", "kind": "line", "points": [{"x": 10, "y": 10, "z": 0}, {"x": 0, "y": 0, "z": 0}]},
                ],
                "constraints": [],
            }],
            "features": [feature("ft_a", ["ft_b"]), feature("ft_b", ["ft_a"])],
        }],
        "requirements": {"raw_text": "", "constraints": []}, "notes": [],
    }

    kind, feature_id, message = compile_error(worker, ir, tmp_path)

    assert kind == "semantic"
    assert feature_id == "body_1"
    assert "circular or unresolvable" in message
    assert "ft_a" in message and "ft_b" in message


# ══════════════════════════════════════════════════════════════════════════
# 4. a malformed constraint must not take the kernel down
#
# `Sketcher.Constraint(type, *refs)` does not validate its arguments: an
# unrecognised shape **segfaults FreeCAD** (SIGSEGV 11, measured). Our own recipe
# built `Constraint("Radius", geoId)` — which is one of them — so asking for a
# Ø8 hole killed the geometry worker and, with no restart policy, the whole
# session. Native crashes cannot be caught, so they must be prevented.
# ══════════════════════════════════════════════════════════════════════════


def _hole_ir(model_id: str, constraints: list[dict], *, with_pocket: bool = True) -> dict:
    """A 90×60 rectangular pad (80 wide) plus, optionally, a Ø8 hole profile.

    *constraints* are attached to the circle's sketch, which is the only thing
    that differs between the cases below.
    """
    ir = rect_ir("YZ", 90, 60, 80, model_id=model_id)
    b = ir["bodies"][0]
    b["sketches"][0]["id"] = "sk_stand"
    b["features"][0]["id"] = "ft1"
    b["features"][0]["profile_sketch"] = "sk_stand"
    if not with_pocket:
        return ir
    b["sketches"].append({
        "id": "sk_hole", "name": "hole",
        "plane": {"kind": "origin_plane", "plane": "XY"},
        "geometry": [{"id": "h0", "kind": "circle",
                      "points": [{"x": 40, "y": 45, "z": 0}], "radius": 4.0}],
        "constraints": constraints,
    })
    b["features"].append({
        "id": "ft_hole", "name": "hole", "op": "pocket",
        "profile_sketch": "sk_hole", "refs": ["ft1"],
        "params": {"type": "ThroughAll", "reversed": True},
    })
    return ir


def test_a_radius_constraint_builds_instead_of_crashing_the_worker(worker, tmp_path):
    """The exact IR a real model produced for "开一个直径 8mm 的通孔".

    It carries `{"type": "Radius", "refs": [0], "value": 4.0}`. Built the old way
    (`Constraint("Radius", 0)` + `setDatum`) that is a segfault; with the value in
    the constructor it is an ordinary sketch.

    The removed volume is asserted against π·r²·h, which is what makes this a test
    of the *constraint* and not merely of "a hole happened": the circle's radius
    has to survive into the cut.
    """
    with_hole = compile_ir(
        worker, _hole_ir("radius_ok", [{"type": "Radius", "refs": [0], "value": 4.0}]),
        tmp_path,
    )
    assert with_hole["ok"] is True, with_hole.get("errors")
    m = with_hole["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True

    solid = compile_ir(worker, _hole_ir("radius_solid", [], with_pocket=False), tmp_path)
    removed = solid["measurements"]["volume"] - m["volume"]
    expected = 3.141592653589793 * 4.0**2 * 60.0   # the Ø8 hole through 60mm
    assert removed == pytest.approx(expected, rel=0.02), (
        f"切掉的体积是 {removed:.1f}，期望约 {expected:.1f} —— 半径约束没有生效"
    )


def test_a_wrong_constraint_shape_is_refused_and_the_worker_survives(worker, tmp_path):
    """Three shapes that segfault the kernel, and the proof that they no longer do.

    The last assertion is the point: a *later, valid* compile on the same worker
    must still work. Preventing the crash is only worth anything if the process is
    genuinely still there — and a native crash would have taken it with it.
    """
    for model_id, con in (
        ("missing_value", {"type": "Radius", "refs": [0]}),
        ("bad_arity", {"type": "Coincident", "refs": [0, 1]}),
        ("unknown_type", {"type": "Explode", "refs": [0]}),
    ):
        kind, feature_id, message = compile_error(
            worker, _hole_ir(model_id, [con]), tmp_path
        )
        assert kind == "semantic", f"{model_id}: {kind} / {message}"
        assert feature_id == "sk_hole", model_id
        assert len(message) > 40, f"{model_id}: 错误信息不足以让人修好它"

    assert worker.is_alive() is True, "拒绝一个畸形约束时把 worker 弄死了"
    res = compile_ir(worker, _hole_ir("after_refusal", []), tmp_path)
    assert res["ok"] is True, "拒绝之后 worker 已经不能用了"
