"""Real-kernel proof for face-attached sketches + the face inventory (task §6).

Face attachment was implemented in the IR (``PlaneRef(kind="face")``) and in the
compiler, but nothing measured it — and the tool description told the model to
"look the face number up with ir_get / ir_digest — do not guess it" while
``ir_digest`` listed only a *count* of faces. The instruction was unfollowable:
``Face6`` being the top face was knowable only by reading FreeCAD's mind.

Two things are asserted here, both against the real kernel:

  * the digest's face list names the faces the BRep actually has, with the normal
    and centre that let a reader pick one by intent;
  * a sketch attached to a named face builds the geometry that face implies, in
    *that face's* frame — which is what the description promises.

Deterministic in the sense that matters: every number comes from the kernel.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_INTROSPECT

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
ABS = 1e-6


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="faces",
                          startup_timeout_s=180.0, request_timeout_s=180.0)
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def _p(x: float, y: float, z: float = 0.0) -> dict:
    return {"x": float(x), "y": float(y), "z": float(z)}


def plate_ir(model_id: str, w: float = 80.0, h: float = 50.0, t: float = 8.0) -> dict:
    pts = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
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


def boss_on_face_ir(model_id: str, sub: str, *, w: float = 80.0, h: float = 50.0, t: float = 8.0,
                    r: float = 6.0, height: float = 10.0,
                    cx: float = 20.0, cy: float = 25.0) -> dict:
    """Plate, then a cylindrical boss whose sketch is attached to ``sub``."""
    ir = plate_ir(model_id, w, h, t)
    body = ir["bodies"][0]
    body["sketches"].append({
        "id": "sk_boss", "name": "boss",
        "plane": {"kind": "face", "feature_id": "ft_plate", "sub": sub},
        "geometry": [{"id": "c0", "kind": "circle",
                      "points": [_p(cx, cy, 0.0)], "radius": r}],
        "constraints": [{"type": "Radius", "refs": [0], "value": r}],
    })
    body["features"].append({
        "id": "ft_boss", "name": "boss", "op": "pad", "profile_sketch": "sk_boss",
        "refs": ["ft_plate"], "params": {"length": height, "type": "Length"},
    })
    return ir


def _digest(worker, ir: dict, out_dir: Path) -> dict:
    compile_ir(worker, ir, out_dir)
    res = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir), "measure": True}, timeout_s=180.0)
    assert res.get("ok") is True, res
    return res


# ══════════════════════════════════════════════════════════════════════════
# 1. the digest names the faces the BRep has
# ══════════════════════════════════════════════════════════════════════════


def test_the_digest_lists_the_faces_a_model_can_attach_to(worker, tmp_path):
    ir = plate_ir("faces_plate")
    digest = _digest(worker, ir, tmp_path)

    faces = digest.get("faces") or []
    assert faces, "ir_digest must list the faces it tells the model to look up"

    by_name = {f["name"]: f for f in faces}
    # A padded 80×50×8 box has exactly six planar faces, Face1..Face6.
    assert set(by_name) == {f"Face{i}" for i in range(1, 7)}, sorted(by_name)
    assert digest["topology"]["faces"] == 6

    # ...and the descriptors are the real ones: the top face is +Z, 80×50, at z=8.
    top = by_name["Face6"]
    assert top["area"] == pytest.approx(80.0 * 50.0, rel=1e-9)
    assert [round(v) for v in top["normal"]] == [0, 0, 1]
    assert top["center"][2] == pytest.approx(8.0, abs=ABS)
    bottom = [f for f in faces if [round(v) for v in f["normal"]] == [0, 0, -1]]
    assert len(bottom) == 1 and bottom[0]["center"][2] == pytest.approx(0.0, abs=ABS)

    # The side faces carry their own normals, so "pick by intent" is possible.
    normals = sorted(tuple(round(v) for v in f["normal"]) for f in faces)
    assert normals.count((0, 0, 1)) == 1 and normals.count((0, 0, -1)) == 1
    assert sum(1 for n in normals if n[2] == 0) == 4


def test_the_digest_face_list_survives_into_the_model_facing_text(worker, tmp_path):
    """`ir_digest` returns the rendered text, not the raw dict."""
    from tcad.context.digest import render_digest_text
    from tcad.core.types import GeometryDigest
    from tcad.ir.schema import IrDocument

    digest = _digest(worker, plate_ir("faces_text"), tmp_path)
    obj = GeometryDigest.model_validate({k: v for k, v in digest.items() if k != "ok"})
    text = render_digest_text(obj, IrDocument.model_validate(plate_ir("faces_text")))
    assert "planar faces" in text
    assert "Face6" in text and "kind\":\"face" in text
    assert "normal=(0, 0, 1)" in text


def test_a_curved_body_lists_only_its_planar_faces(worker, tmp_path):
    """A cylinder's curved wall is not an attachable FlatFace."""
    ir = {"model_id": "faces_cyl", "version": 0, "bodies": [{
        "id": "body_1", "name": "body_1",
        "sketches": [{"id": "sk", "name": "outer",
                      "plane": {"kind": "origin_plane", "plane": "XY"},
                      "geometry": [{"id": "g0", "kind": "circle",
                                    "points": [_p(0.0, 0.0)], "radius": 15.0}],
                      "constraints": [{"type": "Radius", "refs": [0], "value": 15.0}]}],
        "features": [{"id": "ft", "name": "cyl", "op": "pad", "profile_sketch": "sk",
                      "refs": [], "params": {"length": 40.0, "type": "Length"}}]}]}
    digest = _digest(worker, ir, tmp_path)
    faces = digest["faces"]
    assert len(faces) == 2, f"a cylinder has two planar caps, listed {faces}"
    assert {tuple(round(v) for v in f["normal"]) for f in faces} == {(0, 0, 1), (0, 0, -1)}
    for f in faces:
        assert f["area"] == pytest.approx(PI * 15.0 ** 2, rel=1e-9)


# ══════════════════════════════════════════════════════════════════════════
# 2. attaching to a named face builds the geometry that face implies
# ══════════════════════════════════════════════════════════════════════════


def test_a_boss_attached_to_the_top_face_sits_on_the_plate(worker, tmp_path):
    """The whole loop: digest says Face6 is the top face, a sketch uses it."""
    ir = boss_on_face_ir("face_boss_top", "Face6")
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    expected = 80.0 * 50.0 * 8.0 + PI * 6.0 ** 2 * 10.0
    assert m["solids"] == 1, "贴面凸台必须与底板合并成单一实体"
    assert m["is_valid"] is True
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL)
    # It grows upwards from the plate's top face, and nothing sticks out sideways.
    assert m["bbox"]["z"] == pytest.approx(18.0, abs=ABS)
    assert m["bbox"]["z_min"] == pytest.approx(0.0, abs=ABS)
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(50.0, abs=ABS)


@pytest.mark.parametrize("case", [
    dict(w=60.0, h=40.0, t=6.0, r=4.0, height=7.0, cx=15.0, cy=12.0),
    dict(w=100.0, h=30.0, t=12.0, r=5.5, height=3.25, cx=70.0, cy=20.0),
])
def test_the_boss_volume_is_exact_for_other_dimensions(worker, tmp_path, case):
    ir = boss_on_face_ir(f"face_boss_{case['w']:.0f}_{case['t']:.0f}", "Face6", **case)
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    expected = case["w"] * case["h"] * case["t"] + PI * case["r"] ** 2 * case["height"]
    assert res["measurements"]["volume"] == pytest.approx(expected, rel=VOL_REL)
    assert res["measurements"]["bbox"]["z"] == pytest.approx(
        case["t"] + case["height"], abs=ABS)


def test_a_face_attached_sketch_is_in_that_faces_frame(worker, tmp_path):
    """The same profile on a side face grows sideways, not upwards.

    This is the semantics the tool description promises ("face attachment
    inherits that face's coordinate system"). If the compiler mapped the profile
    into world axes regardless of the face, the boss would come out in the wrong
    place — which is exactly what would be silently wrong.
    """
    ir = boss_on_face_ir("face_boss_side", "Face1")   # normal -Y
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    # Same added material, different direction.
    expected = 80.0 * 50.0 * 8.0 + PI * 6.0 ** 2 * 10.0
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL)
    assert m["solids"] == 1
    # It grew along -Y (Face1's outward normal), so the envelope changed in Y and Z
    # but not in X — the plate's own top/bottom were not disturbed.
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)
    assert m["bbox"]["y"] > 50.0 + ABS, "the boss should protrude along the face normal"
    assert m["bbox"]["z_min"] < 0.0 - ABS


def test_an_unknown_face_name_is_a_build_error(worker, tmp_path):
    """Guessing a face name must fail loudly, not attach to something else."""
    ir = boss_on_face_ir("face_boss_bad", "Face99")
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    err = exc.value.rpc_error
    # The message must name the bad sub-element AND what is available — the first
    # version of this failure said "check your world coordinates", which is a
    # different problem entirely.
    assert "Face99" in err.message, err.message
    assert "does not exist on feature" in err.message, err.message
    assert "Face6" in err.message, f"the available faces must be listed: {err.message}"
    assert "ir_digest" in err.message, err.message


def test_the_digest_face_names_still_resolve_after_a_parameter_change(worker, tmp_path):
    """A face name the digest gave must still be a real face on the next build.

    Face indices are assigned by the kernel and are not guaranteed stable across
    arbitrary edits; what this pins is the weaker, honest property that the names
    the digest advertises are usable on the build that produced them.
    """
    base = plate_ir("faces_stable", w=80.0, h=50.0, t=8.0)
    digest = _digest(worker, base, tmp_path / "a")
    top = [f["name"] for f in digest["faces"]
           if [round(v) for v in f["normal"]] == [0, 0, 1]]
    assert top, "no +Z face advertised"
    ir = boss_on_face_ir("faces_stable_boss", top[0])
    res = compile_ir(worker, ir, tmp_path / "b")
    assert res["ok"] is True, res.get("errors")
