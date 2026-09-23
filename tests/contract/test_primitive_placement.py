"""Real-kernel proof that a primitive's ``placement`` puts it where it says.

R-6 left the five primitives EXPERIMENTAL for one reason: they carry their own
size and nothing else, so they could only ever build at the ORIGIN. On a part
spanning x∈[0,80] that is not "unverified", it is unusable — a boss meant for the
middle of the plate lands on its corner, and a pin that never meets the plate
becomes a second solid the Gate then rejects.

What made it a *silent* gap is measured in /tmp/probe_c31.py: a PartDesign
primitive has both ``AttachmentOffset`` and ``Placement``, and only one of them
does anything while ``MapMode`` is Deactivated — ``AttachmentOffset =
(10,20,0)`` left the built shape at x=[0,80] untouched, while ``Placement``
moved it. Wiring the wrong one would have produced a feature that builds,
recomputes, reports ok, and ignores every placement quietly. So the numbers
below are not decoration: each one is the difference between "the primitive is
there" and "the primitive is at the origin".

Every case is a closed form on the 80×50×8 plate from the task book:
  * additive_cylinder r6 h20 at (10,10,0)  → 32000 + 432π   (8 mm in, 12 mm above)
  * additive_sphere   r6 at (60,40,8)     → 32000 + 144π   (half above the top face)
  * subtractive_cylinder r5 h20 at (20,25,-6) → 32000 − 200π (a real through hole)
  * subtractive_box   10×10×4 at (40,20,4) → 31600         (a blind notch)
  * subtractive_sphere r3 at (40,25,4)    → 32000 − 36π    (fully buried)
  * additive_box 20×10×5 at (50,25,8) rot Z+90 → 33000, z∈[0,13]
A misplaced cut is refused by name ("did not change the solid"), and a primitive
with no placement still lands at the origin — that last one is asserted too, so
the default is a documented fact rather than an assumption.

Tolerances: analytic shapes, so OCC accumulates float error (~1e-10 relative)
rather than modelling error; ``1e-6`` relative volume and ``1e-6`` mm size are
machine-precision assertions (same rationale as test_samples_acceptance.py).
"""

from __future__ import annotations

import math
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
    pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCADCmd build not found"),
]

PI = math.pi
VOL_REL = 1e-6
ABS = 1e-6
PLATE_V = 80.0 * 50.0 * 8.0


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="primitives",
                          startup_timeout_s=180.0, request_timeout_s=180.0)
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def compile_ir(worker, ir: dict, out_dir: Path) -> dict:
    return worker.request_sync(
        M_COMPILE_IR, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0
    )


# ══════════════════════════════════════════════════════════════════════════
# IR builder — the task book's plate plus one placed primitive
# ══════════════════════════════════════════════════════════════════════════


def plate_with(model_id: str, placement: dict, **feature) -> dict:
    """An 80×50×8 plate on XY, then one primitive positioned by ``placement``.

    ``placement`` is passed as a separate argument (not merged into ``feature``)
    so every call site has to state where the primitive goes: a test that forgets
    it would be testing the origin case by accident.
    """
    corners = [(0.0, 0.0), (80.0, 0.0), (80.0, 50.0), (0.0, 50.0)]

    def line(i: int) -> dict:
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        return {"id": f"g{i}", "kind": "line",
                "points": [{"x": x0, "y": y0, "z": 0.0},
                           {"x": x1, "y": y1, "z": 0.0}]}

    extra = {"id": "ft_prim", "name": "primitive", "refs": ["ft_plate"]}
    extra.update(feature)
    if placement is not None:
        extra["placement"] = placement

    return {
        "model_id": model_id, "version": 0,
        "bodies": [{
            "id": "body_1", "name": "body_1",
            "sketches": [
                {"id": "sk_plate", "name": "plate_outline",
                 "plane": {"kind": "origin_plane", "plane": "XY"},
                 "geometry": [line(i) for i in range(4)],
                 "constraints": [
                     {"type": "Coincident", "refs": [i, 2, (i + 1) % 4, 1]}
                     for i in range(4)
                 ]},
            ],
            "features": [
                {"id": "ft_plate", "name": "plate", "op": "pad",
                 "profile_sketch": "sk_plate", "refs": [],
                 "params": {"length": 8.0, "type": "Length"}},
                extra,
            ],
        }],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


def pos(x: float, y: float, z: float) -> dict:
    return {"position": {"x": x, "y": y, "z": z}}


# ══════════════════════════════════════════════════════════════════════════
# 1. additive primitives: they merge where they were put
# ══════════════════════════════════════════════════════════════════════════


def test_a_placed_cylinder_pin_merges_into_the_plate(worker, tmp_path):
    """r6 h20 at (10,10,0): 8 mm of its height is inside the plate, 12 above."""
    ir = plate_with("prim_pin", pos(10.0, 10.0, 0.0),
                    op="additive_cylinder", params={"radius": 6.0, "height": 20.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1, "引脚没和板子合体：placement 没生效或被放到了原点"
    assert m["volume"] == pytest.approx(PLATE_V + 432.0 * PI, rel=VOL_REL)
    bbox = m["bbox"]
    assert bbox["z"] == pytest.approx(20.0, abs=ABS), "引脚应高出板面，z 不会只有 8"
    assert bbox["x_min"] == pytest.approx(0.0, abs=ABS)
    assert bbox["y_min"] == pytest.approx(0.0, abs=ABS)


def test_a_placed_sphere_sits_half_in_the_material(worker, tmp_path):
    """r6 centred on the top face adds exactly half a sphere and no more."""
    ir = plate_with("prim_ball", pos(60.0, 40.0, 8.0),
                    op="additive_sphere", params={"radius": 6.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V + 144.0 * PI, rel=VOL_REL)
    assert m["bbox"]["z"] == pytest.approx(14.0, abs=ABS)


def test_a_placed_box_can_be_turned_about_its_own_position(worker, tmp_path):
    """20×10×5 turned 90° about Z occupies x∈[40,50], y∈[25,45].

    Axis-aligned footprints make the rotation measurable exactly: the same box
    un-turned would span x∈[50,70]. The turn must not change the volume — only
    where the 1000 mm³ of boss sits.
    """
    ir = plate_with("prim_boss", {"position": {"x": 50.0, "y": 25.0, "z": 8.0},
                                  "axis": {"x": 0.0, "y": 0.0, "z": 1.0},
                                  "angle": 90.0},
                    op="additive_box",
                    params={"length": 20.0, "width": 10.0, "height": 5.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V + 1000.0, rel=VOL_REL)
    assert m["bbox"]["z"] == pytest.approx(13.0, abs=ABS), "凸台应在板面之上（8→13）"


def test_a_cylinder_turned_about_y_lies_along_x(worker, tmp_path):
    """A 90° turn about Y puts the pin's axis along +X, half sunk in the plate.

    Measured case (probe P7): the cylinder at (30,25,10) spans x∈[30,50],
    z∈[4,16]; the part below the plate's top face is a circular segment of area
    36·acos(1/3) − 2·√32 = 33.0008 per mm, so the union is
    32000 + 720π − 33.0008·20.
    """
    ir = plate_with("prim_pin_y", {"position": {"x": 30.0, "y": 25.0, "z": 10.0},
                                   "axis": {"x": 0.0, "y": 1.0, "z": 0.0},
                                   "angle": 90.0},
                    op="additive_cylinder",
                    params={"radius": 6.0, "height": 20.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    segment = 36.0 * math.acos(2.0 / 6.0) - 2.0 * math.sqrt(32.0)
    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V + 720.0 * PI - segment * 20.0,
                                        rel=VOL_REL)
    assert m["bbox"]["z"] == pytest.approx(16.0, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 2. subtractive primitives: they cut where they were put
# ══════════════════════════════════════════════════════════════════════════


def test_a_placed_cylinder_drills_a_through_hole(worker, tmp_path):
    """r5 h20 at (20,25,-6) spans z∈[-6,14] — through the 8 mm plate."""
    ir = plate_with("prim_hole", pos(20.0, 25.0, -6.0),
                    op="subtractive_cylinder", params={"radius": 5.0, "height": 20.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V - 200.0 * PI, rel=VOL_REL)
    bbox = m["bbox"]
    assert (bbox["x"], bbox["y"], bbox["z"]) == (
        pytest.approx(80.0, abs=ABS), pytest.approx(50.0, abs=ABS),
        pytest.approx(8.0, abs=ABS)), "通孔不该改变包围盒"


def test_a_placed_box_cuts_a_blind_notch(worker, tmp_path):
    """10×10×4 at (40,20,4) sits inside the plate's thickness: −400 exactly."""
    ir = plate_with("prim_notch", pos(40.0, 20.0, 4.0),
                    op="subtractive_box",
                    params={"length": 10.0, "width": 10.0, "height": 4.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V - 400.0, rel=VOL_REL)


def test_a_buried_sphere_removes_its_own_volume(worker, tmp_path):
    """r3 fully inside the plate (z∈[1,7]) removes 36π — nothing more, nothing less."""
    ir = plate_with("prim_scoop", pos(40.0, 25.0, 4.0),
                    op="subtractive_sphere", params={"radius": 3.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V - 36.0 * PI, rel=VOL_REL)


def test_no_placement_still_lands_at_the_origin(worker, tmp_path):
    """The old behaviour, pinned: without a placement the primitive is at (0,0,0).

    Kept as a test rather than a comment because it is what the model will get
    if it forgets the field. r3 at the plate's corner removes one octant
    (4.5π ≈ 14.14) — measured, and obviously not what "a 3 mm scoop" means.
    """
    ir = plate_with("prim_origin", None,
                    op="subtractive_sphere", params={"radius": 3.0})
    res = compile_ir(worker, ir, tmp_path)
    assert res["ok"] is True, res.get("errors")

    m = res["measurements"]
    assert m["solids"] == 1
    assert m["volume"] == pytest.approx(PLATE_V - 4.5 * PI, rel=VOL_REL)


# ══════════════════════════════════════════════════════════════════════════
# 3. a placed cut that misses is an error, not a quiet no-op
# ══════════════════════════════════════════════════════════════════════════


def test_a_placed_cut_that_misses_the_material_is_refused_by_name(worker, tmp_path):
    """Far off the plate the cut changes nothing — the compiler must say so."""
    ir = plate_with("prim_miss", pos(200.0, 200.0, 0.0),
                    op="subtractive_cylinder", params={"radius": 3.0, "height": 5.0})
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    err = exc.value.rpc_error
    assert err.kind == "compile", err.kind
    assert err.feature_id == "ft_prim"
    assert "did not change the solid" in err.message
