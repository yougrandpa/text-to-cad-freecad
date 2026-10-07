"""Real-kernel proof for fillet/chamfer on named edges (task §6).

``fillet``/``chamfer`` were carried in the IR (``base_feature`` + ``sub_elements``),
validated, and compiled — but nothing ever *measured* them, so the capability table
kept them at EXPERIMENTAL. This is the same missing evidence that ``revolution``
(§13), ``groove`` (§22) and face-attached sketches (§23) each got before being
promoted: the kernel either removes exactly the material the maths says, or the
feature does not claim to work.

The numbers are exact rather than tolerance-fitted. Rounding the four vertical
edges of a box of thickness ``t`` with radius ``r`` removes, per edge, the
square-minus-quarter-circle cross-section ``(1 - π/4)·r²`` times the edge length
``t``; beveling them with size ``d`` removes a right triangle ``d²/2`` times
``t``. A fillet that rounds "something" but not the named edges — or an edge name
that silently resolves to a different edge — cannot satisfy these identities.

The edge names are taken from ``ir_digest`` in the tests, never hard-coded: the
tool description tells the model to look them up, so if the digest stopped being
usable the tests must fail with it.
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
    handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="fillet",
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


def _with_edge_feature(ir: dict, op: str, edges: list[str], value: float) -> dict:
    key = "radius" if op == "fillet" else "size"
    ir["bodies"][0]["features"].append({
        "id": f"ft_{op}", "name": op, "op": op, "refs": ["ft_plate"],
        "base_feature": "ft_plate", "sub_elements": list(edges),
        "params": {key: float(value)},
    })
    return ir


def _digest(worker, ir: dict, out_dir: Path) -> dict:
    compile_ir(worker, ir, out_dir)
    res = worker.request_sync(
        M_INTROSPECT, {"ir": ir, "out_dir": str(out_dir), "measure": True}, timeout_s=180.0)
    assert res.get("ok") is True, res
    return res


def _vertical_edge_names(worker, ir: dict, out_dir: Path) -> list[str]:
    """The four edges running along Z, picked by the digest's own descriptors.

    This is exactly the choice the tool description asks the model to make, so a
    digest that stopped describing edges would make this fail rather than pass
    by luck.
    """
    names = []
    for e in _digest(worker, ir, out_dir).get("edges") or []:
        d = e.get("direction") or [0.0, 0.0, 0.0]
        if e.get("kind") == "Line" and abs(d[2]) > 0.99 and abs(d[0]) < 1e-9 and abs(d[1]) < 1e-9:
            names.append(e["name"])
    return sorted(names)


def _fillet_removal(r: float, t: float, n: int = 4) -> float:
    return n * (1.0 - PI / 4.0) * r ** 2 * t


def _chamfer_removal(d: float, t: float, n: int = 4) -> float:
    return n * (d ** 2 / 2.0) * t


# ══════════════════════════════════════════════════════════════════════════
# 1. the digest describes the edges a fillet can select
# ══════════════════════════════════════════════════════════════════════════


def test_the_digest_lists_edges_with_the_descriptors_needed_to_pick_one(worker, tmp_path):
    ir = plate_ir("fil_edges")
    digest = _digest(worker, ir, tmp_path)

    edges = digest.get("edges") or []
    assert edges, "ir_digest must list the edges it tells the model to look up"
    assert digest["topology"]["edges"] == 12, digest["topology"]

    vertical = _vertical_edge_names(worker, ir, tmp_path)
    assert len(vertical) == 4, f"a padded box has 4 vertical edges, found {vertical}"

    by_name = {e["name"]: e for e in edges}
    for name in vertical:
        e = by_name[name]
        assert e["kind"] == "Line"
        assert e["length"] == pytest.approx(8.0, abs=ABS), e
        # The mid-point says *where* the edge is, so "the four corners" is a
        # decision a reader can make from the digest alone.
        assert e["mid"][2] == pytest.approx(4.0, abs=ABS), e

    mids = sorted((round(by_name[n]["mid"][0]), round(by_name[n]["mid"][1])) for n in vertical)
    assert mids == [(0, 0), (0, 50), (80, 0), (80, 50)], mids


def test_the_digest_edge_list_survives_into_the_model_facing_text(worker, tmp_path):
    """The model reads rendered text, not the raw dict."""
    from tcad.context.digest import render_digest_text
    from tcad.core.types import GeometryDigest
    from tcad.ir.schema import IrDocument

    ir = plate_ir("fil_text")
    digest = _digest(worker, ir, tmp_path)
    obj = GeometryDigest.model_validate({k: v for k, v in digest.items() if k != "ok"})
    text = render_digest_text(obj, IrDocument.model_validate(ir))
    assert "edges" in text
    assert "base_feature" in text and "sub_elements" in text
    assert "Edge1" in text


# ══════════════════════════════════════════════════════════════════════════
# 2. the round/bevel removes exactly the corner material, in the real kernel
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", [
    dict(w=80.0, h=50.0, t=8.0, r=5.0),
    dict(w=60.0, h=40.0, t=6.0, r=3.0),
    dict(w=100.0, h=30.0, t=10.0, r=4.5),
])
def test_filleting_the_named_edges_removes_exactly_the_corner_material(worker, tmp_path, case):
    ir = plate_ir(f"fil_{int(case['w'])}x{int(case['h'])}", case["w"], case["h"], case["t"])
    edges = _vertical_edge_names(worker, ir, tmp_path)
    ir = _with_edge_feature(ir, "fillet", edges, case["r"])

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    expected = case["w"] * case["h"] * case["t"] - _fillet_removal(case["r"], case["t"])
    assert m["solids"] == 1, "圆角后仍必须是单一有效实体"
    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL), (
        f"r={case['r']} 的四条竖边圆角应恰好削掉 (1-π/4)r²t × 4")
    # Rounding is inward: the envelope cannot grow, and the plate keeps its size.
    assert m["bbox"]["x"] == pytest.approx(case["w"], abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(case["h"], abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(case["t"], abs=ABS)


@pytest.mark.parametrize("case", [
    dict(w=80.0, h=50.0, t=8.0, d=3.0),
    dict(w=60.0, h=40.0, t=6.0, d=2.0),
])
def test_chamfering_the_named_edges_removes_exactly_the_corner_material(worker, tmp_path, case):
    ir = plate_ir(f"chm_{int(case['w'])}x{int(case['h'])}", case["w"], case["h"], case["t"])
    edges = _vertical_edge_names(worker, ir, tmp_path / "digest")
    ir = _with_edge_feature(ir, "chamfer", edges, case["d"])

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    m = res["measurements"]

    expected = case["w"] * case["h"] * case["t"] - _chamfer_removal(case["d"], case["t"])
    assert m["solids"] == 1
    assert m["is_valid"] is True, m
    assert m["volume"] == pytest.approx(expected, rel=VOL_REL), (
        f"size={case['d']} 的四条竖边倒角应恰好削掉 (d²/2)t × 4")
    assert m["bbox"]["x"] == pytest.approx(case["w"], abs=ABS)
    assert m["bbox"]["y"] == pytest.approx(case["h"], abs=ABS)
    assert m["bbox"]["z"] == pytest.approx(case["t"], abs=ABS)


def test_filleting_only_the_named_edges_leaves_the_others_sharp(worker, tmp_path):
    """Two of four vertical edges: half the removal, not all of it."""
    ir = plate_ir("fil_two_edges")
    edges = _vertical_edge_names(worker, ir, tmp_path)[:2]
    assert len(edges) == 2
    ir = _with_edge_feature(ir, "fillet", edges, 5.0)

    res = compile_ir(worker, ir, tmp_path / "build")
    assert res["ok"] is True, res.get("errors")
    expected = 80.0 * 50.0 * 8.0 - _fillet_removal(5.0, 8.0, n=2)
    assert res["measurements"]["volume"] == pytest.approx(expected, rel=VOL_REL)


# ══════════════════════════════════════════════════════════════════════════
# 3. it is a parametric feature, not a one-shot shape
# ══════════════════════════════════════════════════════════════════════════


def test_the_delivered_fcstd_reopens_and_the_fillet_stays_parametric(worker, tmp_path):
    """Reopening the delivered file and changing Radius must re-cut the solid."""
    ir = plate_ir("fil_reopen")
    edges = _vertical_edge_names(worker, ir, tmp_path)
    ir = _with_edge_feature(ir, "fillet", edges, 5.0)

    out = worker.request_sync(
        M_EXPORT, {"ir": ir, "out_dir": str(tmp_path / "out"),
                   "exports": ["step", "fcstd"]},
        timeout_s=180.0)
    assert out["ok"] is True, out.get("errors")
    for fmt in ("step", "fcstd"):
        p = Path(out["files"][fmt])
        assert p.exists() and p.stat().st_size > 0, f"{fmt} 没有交付真实文件"

    summary = worker.request_sync(M_IMPORT_ASSET, {"path": out["files"]["step"]},
                                  timeout_s=180.0)
    assert summary["ok"] is True, summary
    assert float(summary["shape_summary"]["volume"]) == pytest.approx(
        80.0 * 50.0 * 8.0 - _fillet_removal(5.0, 8.0), rel=STEP_REL)

    edited = worker.request_sync(
        M_REOPEN_EDIT,
        {"fcstd_path": out["files"]["fcstd"],
         "edits": [{"object": "ft_fillet", "property": "Radius", "value": 8.0}]},
        timeout_s=180.0)
    assert edited["ok"] is True, edited.get("errors")

    states = edited["feature_states"]
    assert states["ft_fillet"]["type_id"] == "PartDesign::Fillet", states["ft_fillet"]
    assert "Invalid" not in states["ft_fillet"]["state"], states["ft_fillet"]
    assert states["ft_plate"]["type_id"] == "PartDesign::Pad", "改半径不能毁掉底板特征"
    assert states["sk_plate"]["type_id"] == "Sketcher::SketchObject"

    m = edited["measurements"]
    assert m["solids"] == 1 and m["is_valid"] is True
    assert m["volume"] == pytest.approx(
        80.0 * 50.0 * 8.0 - _fillet_removal(8.0, 8.0), rel=VOL_REL), (
        "Radius 5 → 8 之后必须按新半径重新削料 —— 说明它是参数化特征")
    assert m["bbox"]["x"] == pytest.approx(80.0, abs=ABS)


# ══════════════════════════════════════════════════════════════════════════
# 4. failures are nameable, not silent
# ══════════════════════════════════════════════════════════════════════════


def test_an_edge_name_that_does_not_exist_is_refused_with_the_available_names(worker, tmp_path):
    ir = plate_ir("fil_bad_edge")
    _with_edge_feature(ir, "fillet", ["Edge99"], 2.0)

    kind, feature_id, message = compile_error(worker, ir, tmp_path)
    assert kind == "semantic", kind
    assert feature_id == "ft_fillet", feature_id
    assert "Edge99" in message and "Available edges" in message, message
    assert "Edge1(" in message, "报错必须列出这台机器上真实存在的边名"


def test_a_fillet_that_cannot_be_built_is_not_reported_as_a_success(worker, tmp_path):
    """A radius the geometry cannot take must fail loudly, in some named way.

    Which way depends on the kernel (a structured build error, or a shape that
    comes back invalid / not a single solid); what is not allowed is a clean
    ``ok=True`` with the un-filleted volume, which is what "created the object
    and did not look" would produce.
    """
    ir = plate_ir("fil_too_big")
    edges = _vertical_edge_names(worker, ir, tmp_path)
    ir = _with_edge_feature(ir, "fillet", edges, 40.0)

    try:
        res = compile_ir(worker, ir, tmp_path / "build")
    except WorkerCallFailed as exc:
        err = exc.rpc_error
        assert err.feature_id == "ft_fillet", err
        assert err.message, err
        assert 'preceding solid feature' in err.hint and 'Reduce radius' in err.hint, err
        return

    m = res.get("measurements") or {}
    assert res.get("ok") is False or m.get("is_valid") is not True, (
        f"半径 40 无法在 8mm 厚的板上成立，却被当成成功构建：{res}")
