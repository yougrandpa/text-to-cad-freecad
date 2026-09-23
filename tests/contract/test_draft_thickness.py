"""Real-kernel proof for draft (a neutral plane + drafted faces) and thickness.

Both ops lived in the capability table as EXPERIMENTAL with the same one-line
gap: "needs NeutralPlane / FaceLists as LinkSub — unsettable from the IR". That
gap is now closed the way ``fillet`` and ``mirrored`` closed theirs — the IR
grew no new field, it reuses the two carriers it already had:

  * ``base_feature`` + ``sub_elements`` — the face list these ops work on
    (``fillet``/``chamfer`` use the same pair for *edges*);
  * the typed ``plane`` field — the draft's ``NeutralPlane`` (the mirror's
    ``MirrorPlane`` before it).

Two kernel facts decided the design and are asserted here, not assumed:

  * a ``draft`` with no ``NeutralPlane`` does NOT raise — FreeCAD logs "Failed
    to add some face for drafting, skip" and returns a NULL shape (probe
    /tmp/probe_draft2.py). Same silent-failure family as a mirror with no plane,
    which is why the validator refuses a plane-less draft by name;
  * the numbers are analytic. Drafting all four side faces of a 40×40×20 box by
    ``a`` about the bottom gives a prismatoid, V = h/6·(A0 + 4·Am + A1) with the
    half-width 20 − z·tan a, and a thickness of ``v`` with the top face open
    leaves the walls: 40·40·20 − (40−2v)²·(20−v). Tolerances are machine
    precision (1e-6) for the same reason as the other sample tests: OCC's error
    on these shapes is float accumulation, not modelling error, and a looser
    bound would stop distinguishing "drafted the named faces" from "drafted
    something".
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerHandle
from tcad.worker.protocol import M_EXPORT, M_IMPORT_ASSET, M_REOPEN_EDIT

from tests.contract.test_sketch_planes import compile_error, compile_ir

REPO_ROOT = Path(__file__).resolve().parents[2]
FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]

VOL_REL = 1e-6
STEP_REL = 1e-6
ABS = 1e-6

SIDE = 40.0
HEIGHT = 20.0
BOX_V = SIDE * SIDE * HEIGHT
TAN5 = math.tan(math.radians(5.0))


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="draft",
                          startup_timeout_s=180.0, request_timeout_s=180.0)
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def _p(x: float, y: float, z: float = 0.0) -> dict:
    return {"x": float(x), "y": float(y), "z": float(z)}


def box_ir(model_id: str) -> dict:
    """A 40×40×20 box on XY: four side faces, one bottom, one top."""
    pts = [(0.0, 0.0), (SIDE, 0.0), (SIDE, SIDE), (0.0, SIDE)]
    return {"model_id": model_id, "version": 0, "bodies": [{
        "id": "body_1", "name": "body_1",
        "sketches": [{
            "id": "sk_box", "name": "outline",
            "plane": {"kind": "origin_plane", "plane": "XY"},
            "geometry": [
                {"id": f"g{i}", "kind": "line",
                 "points": [_p(*pts[i]), _p(*pts[(i + 1) % 4])]}
                for i in range(4)
            ],
            "constraints": [{"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                            for i in range(4)],
        }],
        "features": [{"id": "ft_box", "name": "box", "op": "pad",
                      "profile_sketch": "sk_box", "refs": [],
                      "params": {"length": HEIGHT, "type": "Length"}}],
    }]}


def _add(ir: dict, *, op: str, faces: list[str], plane: dict | None,
         params: dict) -> dict:
    feature = {"id": f"ft_{op}", "name": op, "op": op, "refs": ["ft_box"],
               "base_feature": "ft_box", "sub_elements": list(faces),
               "params": dict(params)}
    if plane is not None:
        feature["plane"] = plane
    ir["bodies"][0]["features"].append(feature)
    return ir


def _faces_by_normal(worker, ir: dict, out_dir: Path, want: tuple) -> list[str]:
    """Face names from the digest's own descriptors — never hard-coded.

    This is the choice the tool description asks the model to make ("call
    ir_digest and pick by normal/centre"), so a digest that stopped describing
    faces would make these tests fail rather than pass by luck.
    """
    from tcad.worker.protocol import M_INTROSPECT

    compile_ir(worker, ir, out_dir)
    res = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir), "measure": True},
        timeout_s=180.0)
    assert res.get("ok") is True, res
    names = [f["name"] for f in (res.get("faces") or [])
             if tuple(round(v) for v in f["normal"]) == want]
    assert names, f"no face with normal {want} in {res.get('faces')}"
    return sorted(names)


def _side_faces(worker, ir: dict, out_dir: Path) -> list[str]:
    """The four vertical faces of the box, by their outward normals."""
    faces = []
    for want in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        faces += _faces_by_normal(worker, ir, out_dir, want)
    assert len(faces) == 4, faces
    return sorted(faces)



def _prismatoid(angle_deg: float) -> float:
    """Volume of the box drafted inwards on all four sides about z=0."""
    tan = math.tan(math.radians(angle_deg))

    def area(z: float) -> float:
        return (SIDE - 2.0 * z * tan) ** 2

    return HEIGHT / 6.0 * (area(0.0) + 4.0 * area(HEIGHT / 2.0) + area(HEIGHT))


# ══════════════════════════════════════════════════════════════════════════
# 1. draft: the named faces are re-shaped, by the angle asked for
# ══════════════════════════════════════════════════════════════════════════


def test_a_draft_off_the_xy_plane_shrinks_the_box_by_the_analytic_prismatoid(
        worker, tmp_path):
    """5° off z=0 on the four side faces: 29282.0083 mm³, footprint unchanged."""
    ir = box_ir("dr_xy")
    sides = _side_faces(worker, ir, tmp_path)
    _add(ir, op="draft", faces=sides,
         plane={"kind": "origin_plane", "plane": "XY"}, params={"angle": 5.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(_prismatoid(5.0), rel=VOL_REL)
    # The neutral plane is the bottom, so the footprint cannot grow.
    assert m["bbox"]["x"] == pytest.approx(SIDE, abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(SIDE, abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(HEIGHT, abs=ABS)


def test_the_neutral_plane_can_be_a_named_face_instead_of_an_origin_plane(
        worker, tmp_path):
    """The same 5° about the *bottom face* must give the same prismatoid."""
    ir = box_ir("dr_face")
    sides = _side_faces(worker, ir, tmp_path)
    bottom = _faces_by_normal(worker, ir, tmp_path, (0, 0, -1))
    assert len(bottom) == 1, bottom
    _add(ir, op="draft", faces=sides,
         plane={"kind": "face", "feature_id": "ft_box", "sub": bottom[0]},
         params={"angle": 5.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(_prismatoid(5.0), rel=VOL_REL)


def test_reversed_grows_the_draft_outwards(worker, tmp_path):
    """reversed=true flips the taper: the box gets wider, not narrower."""
    ir = box_ir("dr_rev")
    sides = _side_faces(worker, ir, tmp_path)
    _add(ir, op="draft", faces=sides,
         plane={"kind": "origin_plane", "plane": "XY"},
         params={"angle": 5.0, "reversed": True})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    grown = SIDE + 2.0 * HEIGHT * TAN5
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(HEIGHT / 6.0 * (
        SIDE ** 2 + 4.0 * (SIDE + HEIGHT * TAN5) ** 2 + grown ** 2), rel=VOL_REL)
    assert m["bbox"]["x"] == pytest.approx(grown, abs=ABS)


def test_only_the_listed_faces_are_drafted(worker, tmp_path):
    """Naming the two ±X faces leaves the Y footprint at 40 mm.

    This is the assertion that tells "drafted the faces I listed" from
    "drafted everything": the top cross-section becomes 36.5×40, so the volume
    is the box minus 2·tan5°·h²/2·40 = 30600.18 mm³, not the prismatoid.
    """
    ir = box_ir("dr_two")
    px = _faces_by_normal(worker, ir, tmp_path, (1, 0, 0))
    nx = _faces_by_normal(worker, ir, tmp_path, (-1, 0, 0))
    _add(ir, op="draft", faces=px + nx,
         plane={"kind": "origin_plane", "plane": "XY"}, params={"angle": 5.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["volume"] == pytest.approx(SIDE * (SIDE * HEIGHT - TAN5 * HEIGHT ** 2),
                                        rel=VOL_REL)
    assert m["bbox"]["y"] == pytest.approx(SIDE, abs=ABS), "Y 向没有倒角面"


# ══════════════════════════════════════════════════════════════════════════
# 2. thickness: the named face opens, the walls stay
# ══════════════════════════════════════════════════════════════════════════


def test_opening_the_top_face_shells_the_box_to_the_analytic_wall_volume(
        worker, tmp_path):
    """value=2 with the top face open leaves 40·40·20 − 36·36·18 = 8672 mm³."""
    ir = box_ir("th_top")
    top = _faces_by_normal(worker, ir, tmp_path, (0, 0, 1))
    _add(ir, op="thickness", faces=top, plane=None, params={"value": 2.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(
        BOX_V - (SIDE - 4.0) ** 2 * (HEIGHT - 2.0), rel=VOL_REL)
    assert m["bbox"]["z"] == pytest.approx(HEIGHT, abs=ABS)


def test_a_thicker_wall_removes_more_material(worker, tmp_path):
    """Value is a wall thickness, not a decoration: 2 → 4 changes the volume."""
    ir = box_ir("th_4")
    top = _faces_by_normal(worker, ir, tmp_path, (0, 0, 1))
    _add(ir, op="thickness", faces=top, plane=None, params={"value": 4.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    assert res["measurements"]["volume"] == pytest.approx(
        BOX_V - (SIDE - 8.0) ** 2 * (HEIGHT - 4.0), rel=VOL_REL)


def test_opening_a_side_face_leaves_a_cup_open_on_that_side(worker, tmp_path):
    """The opened face is the one NAMED: ±X open leaves 2 mm of wall in X only."""
    ir = box_ir("th_side")
    px = _faces_by_normal(worker, ir, tmp_path, (1, 0, 0))
    _add(ir, op="thickness", faces=px, plane=None, params={"value": 2.0})

    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    # A box with one side open and 2 mm walls on the other five faces.
    assert m["volume"] == pytest.approx(
        BOX_V - (SIDE - 2.0) * (SIDE - 4.0) * (HEIGHT - 4.0), rel=VOL_REL)
    assert m["bbox"]["x"] == pytest.approx(SIDE, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 3. they are parametric features, not one-shot shapes
# ══════════════════════════════════════════════════════════════════════════


def test_the_delivered_fcstd_reopens_and_the_thickness_stays_parametric(
        worker, tmp_path):
    """Reopening the file and raising Value 2 → 4 must thin the walls again."""
    ir = box_ir("th_reopen")
    top = _faces_by_normal(worker, ir, tmp_path, (0, 0, 1))
    _add(ir, op="thickness", faces=top, plane=None, params={"value": 2.0})

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"),
                   "exports": ["step", "fcstd"]}, timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")
    for fmt in ("step", "fcstd"):
        p = Path(out["files"][fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"

    summary = worker.request_sync(M_IMPORT_ASSET, {"path": out["files"]["step"]},
                                  timeout_s=180.0)
    assert summary["ok"] is True, summary
    assert float(summary["shape_summary"]["volume"]) == pytest.approx(
        BOX_V - (SIDE - 4.0) ** 2 * (HEIGHT - 2.0), rel=STEP_REL)

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": out["files"]["fcstd"],
         "edits": [{"object": "ft_thickness", "property": "Value", "value": 4.0}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")
    states = edited["feature_states"]
    assert states["ft_thickness"]["type_id"] == "PartDesign::Thickness"
    assert "Invalid" not in states["ft_thickness"]["state"], states["ft_thickness"]
    assert states["ft_box"]["type_id"] == "PartDesign::Pad", "改壁厚不能毁掉底板特征"
    m = edited["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(
        BOX_V - (SIDE - 8.0) ** 2 * (HEIGHT - 4.0), rel=VOL_REL), (
        "Value 2 → 4 之后必须按新壁厚重算 —— 说明它是参数化特征")


def test_the_delivered_fcstd_reopens_and_the_draft_stays_parametric(
        worker, tmp_path):
    """Reopening the file and raising Angle 5 → 10 must re-taper the box.

    (The neutral plane must be XY here: drafting all four side faces about XZ
    was measured to return a NULL shape — the two faces parallel to the pull
    direction cannot be drafted — see the loud-failure test in section 4.)
    """
    ir = box_ir("dr_reopen")
    sides = _side_faces(worker, ir, tmp_path)
    _add(ir, op="draft", faces=sides,
         plane={"kind": "origin_plane", "plane": "XY"}, params={"angle": 5.0})

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"),
                   "exports": ["fcstd"]}, timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": out["files"]["fcstd"],
         "edits": [{"object": "ft_draft", "property": "Angle", "value": 10.0}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")
    states = edited["feature_states"]
    assert states["ft_draft"]["type_id"] == "PartDesign::Draft"
    assert "Invalid" not in states["ft_draft"]["state"], states["ft_draft"]
    m = edited["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(_prismatoid(10.0), rel=VOL_REL), (
        "Angle 5 → 10 之后必须按新角度重算")


# ══════════════════════════════════════════════════════════════════════════
# 4. failures are nameable, not silent
# ══════════════════════════════════════════════════════════════════════════


def test_a_face_name_that_does_not_exist_is_refused_with_the_available_names(
        worker, tmp_path):
    ir = box_ir("dr_bad_face")
    _add(ir, op="draft", faces=["Face99"],
         plane={"kind": "origin_plane", "plane": "XY"}, params={"angle": 5.0})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "semantic", kind
    assert feature_id == "ft_draft", feature_id
    assert "Face99" in message and "Available faces" in message, message


def test_a_draft_without_a_neutral_plane_is_refused_rather_than_null(
        worker, tmp_path):
    """The kernel does not raise here — it returns a NULL shape and keeps the
    previous Tip. The compiler must refuse first, or the build would look fine
    and deliver the un-drafted box."""
    ir = box_ir("dr_no_plane")
    _add(ir, op="draft", faces=["Face1"], plane=None, params={"angle": 5.0})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "schema", kind
    assert feature_id == "ft_draft", feature_id
    assert "plane" in message and "NeutralPlane" in message, message


def test_a_face_parallel_to_the_neutral_plane_is_refused_by_name(worker, tmp_path):
    """The ±Y faces lie parallel to the XZ neutral plane: no intersection line.

    Measured: FreeCAD marks the whole feature Invalid for this (probe: ±Y about
    XZ, all four side faces about XZ, ±Z about XY) — a loud failure, but one that
    only names the feature. The compiler checks the two references it just set
    and says which face is the problem and what to do instead.
    """
    ir = box_ir("dr_parallel")
    py = _faces_by_normal(worker, ir, tmp_path, (0, 1, 0))
    ny = _faces_by_normal(worker, ir, tmp_path, (0, -1, 0))
    _add(ir, op="draft", faces=py + ny,
         plane={"kind": "origin_plane", "plane": "XZ"}, params={"angle": 5.0})

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "semantic", kind
    assert feature_id == "ft_draft", feature_id
    for name in py + ny:
        assert name in message, message
    assert "parallel to the neutral plane" in message, message

