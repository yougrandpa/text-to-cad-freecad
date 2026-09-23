"""A build that changed nothing must fail loudly, at the feature that did nothing.

This exists because of a real defect found on the live kernel:

  * FreeCAD treats a cut of nothing as **success**. A Pocket whose profile
    completely misses the material computes "fine": the feature's State stays
    ``Up-to-date`` (never ``Invalid``), the Body Tip keeps the previous solid,
    and — before the fix — ``compile_ir`` / ``export_artifacts`` /
    ``tessellate`` / ``introspect_document`` all reported ``ok=true``. The
    declared hole simply did not exist in the result, and nothing said so.
  * Worse, OCC still rebuilds the solid during the disjoint boolean, so the tip
    shape object differs; only the last few digits move (measured |dV| ~ 3e-10
    on a 4.3e5 mm³ part). Identity comparison alone therefore misses it, which
    is why the compiler compares consecutive tip shapes with both identity
    (``isSame``/``isEqual``) and volume/area epsilons.

Three things are asserted against the real kernel:

  1. a pocket whose circle lies entirely outside the plate makes *every* worker
     method fail, and the error names the pocket feature;
  2. a pocket that *does* intersect still builds (the no-op detector must not
     false-positive on real cuts);
  3. sketch-level ``reversed`` genuinely flips the extrusion side of the solid
     (the bbox moves, the volume does not) — the escape hatch a model needs
     when it realises it extruded the wrong way.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_COMPILE_IR

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


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="build_failures",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


# ══════════════════════════════════════════════════════════════════════════
# IR builders
# ══════════════════════════════════════════════════════════════════════════


def plate_with_hole_ir(model_id: str, cx: float, cy: float, radius: float) -> dict:
    """An 80×50×8 XY plate with one Ø(2r) hole circle at world (cx, cy).

    *cx/cy* are world coordinates on the plate face; a circle outside
    x∈[0,80], y∈[0,50] makes the pocket miss the material entirely.
    """
    ir = rect_ir("XY", 80, 50, 8, model_id=model_id)
    b = ir["bodies"][0]
    b["sketches"][0]["id"] = "sk_plate"
    b["features"][0]["id"] = "ft_plate"
    b["features"][0]["profile_sketch"] = "sk_plate"
    b["sketches"].append({
        "id": "sk_hole", "name": "hole",
        "plane": {"kind": "origin_plane", "plane": "XY"},
        "geometry": [{"id": "h0", "kind": "circle",
                      "points": [{"x": cx, "y": cy, "z": 0}], "radius": radius}],
        "constraints": [{"type": "Radius", "refs": [0], "value": radius}],
    })
    b["features"].append({
        "id": "ft_hole", "name": "hole", "op": "pocket",
        "profile_sketch": "sk_hole", "refs": ["ft_plate"],
        "params": {"type": "ThroughAll", "reversed": True},
    })
    return ir


def raised(worker, method: str, params: dict) -> tuple[str, str | None, str]:
    """The (kind, feature_id, message) a failed worker call surfaces."""
    with pytest.raises(WorkerCallFailed) as exc:
        worker.request_sync(method, params, timeout_s=180.0)
    err = exc.value.rpc_error
    return err.kind, err.feature_id, err.message


# ══════════════════════════════════════════════════════════════════════════
# 1. a pocket that misses the material fails on every worker method
# ══════════════════════════════════════════════════════════════════════════


def test_a_pocket_whose_profile_misses_the_material_fails_loudly(worker, tmp_path):
    """The circle at (200,200) is nowhere near the 80×50 plate.

    Before the fix this compiled to ``ok=true`` with a valid-looking solid that
    simply had no hole — the "切除未相交" failure class silently reported as
    success. Now the compile must fail and name the pocket.
    """
    ir = plate_with_hole_ir("disjoint_pocket", 200.0, 200.0, 3.0)

    kind, feature_id, message = raised(
        worker, M_COMPILE_IR, {"ir": ir, "out_dir": str(tmp_path)}
    )

    assert kind == "compile"
    assert feature_id == "ft_hole"
    assert "did not change the solid" in message
    assert "WORLD COORDINATES" in message, "没有提示模型去核对坐标契约"


def test_the_disjoint_build_cannot_be_exported_tessellated_or_introspected(worker, tmp_path):
    """A failed build must stay failed at every downstream method.

    Each of these re-runs the build internally; each must surface the same
    no-op error rather than returning geometry for a hole that does not exist.
    """
    ir = plate_with_hole_ir("disjoint_downstream", 200.0, 200.0, 3.0)
    params = {"ir": ir, "out_dir": str(tmp_path)}

    for method in ("export_artifacts", "tessellate", "introspect_document"):
        kind, _feature_id, message = raised(worker, method, params)
        assert kind == "compile", f"{method} 没有报告构建失败: {kind}"
        assert "ft_hole" in message, f"{method} 的错误没有定位到孔特征"
        assert "did not change the solid" in message, f"{method}: {message}"

    export_params = dict(params, exports=["step", "stl"])
    with pytest.raises(WorkerCallFailed):
        worker.request_sync("export_artifacts", export_params, timeout_s=180.0)
    assert not list(tmp_path.glob("*.step")), "失败的构建不许留下 STEP 文件"
    assert not list(tmp_path.glob("*.stl")), "失败的构建不许留下 STL 文件"


# ══════════════════════════════════════════════════════════════════════════
# 2. a pocket that DOES intersect must still build — no false positives
# ══════════════════════════════════════════════════════════════════════════


def test_an_intersecting_pocket_still_builds_and_cuts_real_volume(worker, tmp_path):
    """Ø6 through hole at (10,10) in the 80×50×8 plate → V = 32000 − π·3²·8.

    The no-op detector compares consecutive tip shapes on *every* compile, so a
    genuine cut must pass it — and the removed volume must match the circle,
    which is what proves the hole is where the IR put it.
    """
    ir = plate_with_hole_ir("intersecting_pocket", 10.0, 10.0, 3.0)

    res = compile_ir(worker, ir, tmp_path)

    assert res["ok"] is True, f"真实切除被误报为 no-op: {res.get('errors')}"
    m = res["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(32000.0 - 3.141592653589793 * 9.0 * 8.0, rel=1e-6)


# ══════════════════════════════════════════════════════════════════════════
# 3. sketch-level reversed flips the extrusion side of the solid
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("reversed_flag,y_min", [(False, -10.0), (True, 0.0)])
def test_sketch_reversed_flips_the_extrusion_side(worker, tmp_path, reversed_flag, y_min):
    """XZ profile padded 10: the default extrudes toward −Y, ``reversed`` toward +Y.

    A model that discovers it extruded away from the body must be able to flip
    the side with the documented sketch flag — and the fix must keep honouring
    it, since the no-op error message tells the model exactly to do this.
    """
    ir = rect_ir("XZ", 40, 20, 10, model_id=f"reversed_{reversed_flag}")
    ir["bodies"][0]["sketches"][0]["reversed"] = reversed_flag

    res = compile_ir(worker, ir, tmp_path)

    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]
    assert m["volume"] == pytest.approx(8000.0, rel=1e-6), "翻转方向不许改变体积"
    assert m["bbox"]["y_min"] == pytest.approx(y_min, abs=1e-6), (
        f"reversed={reversed_flag} 应该让实体落在 y_min={y_min}"
    )


# ══════════════════════════════════════════════════════════════════════════
# 4. the pad-param knobs the tool description names, on the real kernel
# ══════════════════════════════════════════════════════════════════════════


def test_the_pad_param_knobs_do_what_the_description_says(worker, tmp_path):
    """The description tells the model to set these *in the pad's params*.

    It promises three distinct outcomes — default toward −Y on XZ, ``reversed``
    toward the other side, ``midplane`` centred — and it recommends this route
    specifically (``test_sketch_reversed_...`` above exercises the sketch-level
    flag, which is a different field the model is never told about). Promising a
    knob that does nothing is worse than not offering it, because the model
    plans around it and the build still reports success, so all three are
    measured here rather than assumed from the prose.
    """
    cases = [
        ("default", {"length": 10.0, "type": "Length"}, (-10.0, 0.0), "默认朝 −Y"),
        ("reversed", {"length": 10.0, "type": "Length", "reversed": True},
         (0.0, 10.0), "reversed 朝 +Y"),
        ("midplane", {"length": 10.0, "type": "Length", "midplane": True},
         (-5.0, 5.0), "midplane 对称"),
    ]
    for model_id, params, (y_min, y_max), label in cases:
        ir = rect_ir("XZ", 40, 20, 10, model_id=f"pad_knob_{model_id}")
        ir["bodies"][0]["features"][0]["params"] = params

        res = compile_ir(worker, ir, tmp_path)

        assert res["ok"] is True, f"{label}: {res.get('errors')}"
        m = res["measurements"]
        assert m["volume"] == pytest.approx(8000.0, rel=1e-6), (
            f"{label}: 换向/居中都不该改变体积"
        )
        assert m["bbox"]["y_min"] == pytest.approx(y_min, abs=1e-6), label
        assert m["bbox"]["y"] == pytest.approx(y_max - y_min, abs=1e-6), label


def test_a_typo_in_a_param_is_refused_not_ignored(worker, tmp_path):
    """``reveresd`` must fail the build, not quietly build the default.

    The generic param path maps snake_case onto real FreeCAD properties, so a
    misspelled key has no property to land on. If that were skipped silently the
    model would get a successful build in the wrong direction and no reason to
    look again — the exact failure mode the description's warnings exist to
    prevent.
    """
    ir = rect_ir("XZ", 40, 20, 10, model_id="typo_param")
    ir["bodies"][0]["features"][0]["params"] = {
        "length": 10.0, "type": "Length", "reveresd": True,
    }

    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)

    error = exc.value.rpc_error
    assert "reveresd" in error.message, error.message
    assert "PartDesign::Pad" in error.message, error.message


# ══════════════════════════════════════════════════════════════════════════
# 5. coordinates + a contradicting origin bind = a DIFFERENT shape, no error
# ══════════════════════════════════════════════════════════════════════════


def test_a_contradictory_origin_bind_moves_the_profile_instead_of_failing(worker, tmp_path):
    """Documented trap, found by writing this test's probe backwards.

    To the solver a point's coordinates and its constraints are the same kind of
    fact, and the constraints win: a 20×20 square written at (10,10)…(30,30) that
    *also* says "line0 start == origin" is not an error. FreeCAD drags the profile
    over to the origin and the build reports success — with a pocket that is
    neither the square nor where it was written. Measured here: the cut has a
    600 mm² footprint (not 400) with its floor at z=6, so it removes 3600 mm³
    instead of 2400.

    Nothing in the pipeline is *wrong* — this is Sketcher semantics — which is
    why it is pinned as behaviour rather than fixed: what matters is that the tool
    description says so (bind the origin only when the profile really starts
    there; place offset profiles with ``offset``), and that a model that reads the
    numbers back with ir_digest can see the difference.
    """
    def rect(x0: float, y0: float, w: float, h: float, prefix: str) -> list[dict]:
        pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        return [
            {"id": f"{prefix}{i}", "kind": "line",
             "points": [{"x": px, "y": py, "z": 0.0},
                        {"x": pts[(i + 1) % 4][0], "y": pts[(i + 1) % 4][1], "z": 0.0}]}
            for i, (px, py) in enumerate(pts)
        ]

    def square(x0: float, y0: float, size: float, prefix: str) -> list[dict]:
        return rect(x0, y0, size, size, prefix)

    # 闭合链 + 把 line0 起点绑到草图原点（局部坐标 0 处，与坐标自洽）
    chain = [{"type": "Coincident", "refs": [0, 2, 1, 1]},
             {"type": "Coincident", "refs": [1, 2, 2, 1]},
             {"type": "Coincident", "refs": [2, 2, 3, 1]},
             {"type": "Coincident", "refs": [3, 2, 0, 1]},
             {"type": "Coincident", "refs": [0, 1, -1, 1]}]

    def pocket_ir(model_id: str, cut_points: list[dict], origin_bind: bool) -> dict:
        cons = chain if origin_bind else chain[:-1]
        return {
            "model_id": model_id, "version": 0,
            "bodies": [{
                "id": "body_1", "name": "body_1",
                "sketches": [
                    {"id": "sk_plate", "name": "plate",
                     "plane": {"kind": "origin_plane", "plane": "XY"},
                     "geometry": rect(0.0, 0.0, 80.0, 50.0, "p"),
                     "constraints": chain},
                    {"id": "sk_cut", "name": "cut",
                     "plane": {"kind": "origin_plane", "plane": "XY"},
                     "geometry": cut_points, "constraints": cons},
                ],
                "features": [
                    {"id": "ft_pad", "name": "pad", "op": "pad",
                     "profile_sketch": "sk_plate",
                     "params": {"length": 8.0, "type": "Length"}, "refs": []},
                    {"id": "ft_cut", "name": "cut", "op": "pocket",
                     "profile_sketch": "sk_cut",
                     "params": {"length": 6.0, "type": "Length", "reversed": True},
                     "refs": ["ft_pad"]},
                ],
            }],
            "requirements": {"raw_text": "", "constraints": []},
        }

    res = compile_ir(worker, pocket_ir("contradictory_bind", square(10.0, 10.0, 20.0, "c"),
                                       origin_bind=True), tmp_path)

    assert res["ok"] is True, f"这个陷阱本应静默通过: {res.get('errors')}"
    volume = res["measurements"]["volume"]
    assert volume != pytest.approx(32000.0 - 400.0 * 6.0, rel=1e-9), (
        "若这里相等，说明求解器没有挪动轮廓——描述的警告需要重写"
    )
    assert volume == pytest.approx(32000.0 - 600.0 * 6.0, rel=1e-6), (
        f"实测按 600 mm² 底面积切掉 6 mm，得到 {volume}"
    )
