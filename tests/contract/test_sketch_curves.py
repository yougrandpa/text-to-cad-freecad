"""Real-kernel native ellipse/spline forming, constraint and delivery evidence."""

import math
import zipfile
from xml.etree import ElementTree

import pytest

from tcad.core.worker_client import WorkerCallFailed
from tcad.worker.protocol import M_INTROSPECT, M_REOPEN_EDIT
from tests.contract.test_sketch_planes import compile_ir, pytestmark, worker


def point(plane, u, v):
    return dict(zip(("x", "y", "z"), {
        "XY": (u, v, 0), "XZ": (u, 0, v), "YZ": (0, u, v),
    }[plane]))


def curve_ir(plane, kind, *, rotation=0, periodic=True):
    if kind == "ellipse":
        geometry = [{"id": "g0", "kind": kind, "points": [point(plane, 30, 40)],
                     "major_radius": 20, "minor_radius": 10, "rotation": rotation}]
    elif periodic:
        geometry = [{"id": "g0", "kind": kind, "periodic": True,
                     "points": [point(plane, 30 + 20 * math.cos(i * math.pi / 2),
                                      40 + 10 * math.sin(i * math.pi / 2)) for i in range(4)]}]
    else:
        geometry = [
            {"id": "g0", "kind": kind,
             "points": [point(plane, 10, 40), point(plane, 30, 50), point(plane, 50, 40)]},
            {"id": "g1", "kind": "line", "points": [point(plane, 50, 40), point(plane, 10, 40)]},
        ]
    return {"model_id": f"curve_{kind}_{plane}", "bodies": [{"id": "b", "name": "body",
            "sketches": [{"id": "sk", "name": "curve",
                          "plane": {"kind": "origin_plane", "plane": plane},
                          "geometry": geometry,
                          "constraints": [{"type": "Block", "refs": [i]} for i in range(len(geometry))]}],
            "features": [{"id": "pad", "name": "pad", "op": "pad",
                          "profile_sketch": "sk", "params": {"length": 5}}]}]}


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
@pytest.mark.parametrize("rotation", [0, 30])
def test_ellipse_volume_and_rotated_bounds(worker, tmp_path, plane, rotation):
    ir = curve_ir(plane, "ellipse", rotation=rotation)
    result = compile_ir(worker, ir, tmp_path)
    measure = result["measurements"]
    assert measure["is_valid"] and measure["solids"] == 1
    assert measure["volume"] == pytest.approx(math.pi * 20 * 10 * 5, rel=1e-6)
    angle = math.radians(rotation)
    u = 2 * math.hypot(20 * math.cos(angle), 10 * math.sin(angle))
    v = 2 * math.hypot(20 * math.sin(angle), 10 * math.cos(angle))
    axes = {"XY": ("x", "y", "z"), "XZ": ("x", "z", "y"), "YZ": ("y", "z", "x")}[plane]
    for axis, size in zip(axes, (u, v, 5)):
        assert measure["bbox"][axis] == pytest.approx(size, abs=1e-5)
    assert result["round_trip"]["ok"]


@pytest.mark.parametrize('plane', ['XY', 'XZ', 'YZ'])
def test_equal_axis_ellipse_compiles_as_exact_circle_without_solver_abort(worker, tmp_path, plane):
    ir = curve_ir(plane, 'ellipse', rotation=30)
    ir['bodies'][0]['sketches'][0]['geometry'][0]['minor_radius'] = 20
    result = compile_ir(worker, ir, tmp_path)
    measured = result['measurements']
    assert measured['is_valid'] and measured['solids'] == 1
    assert measured['volume'] == pytest.approx(math.pi * 20**2 * 5, rel=1e-6)
    axes = {'XY': ('x', 'y', 'z'), 'XZ': ('x', 'z', 'y'), 'YZ': ('y', 'z', 'x')}[plane]
    for axis, size in zip(axes, (40, 40, 5)):
        assert measured['bbox'][axis] == pytest.approx(size, abs=1e-5)
    assert result['round_trip']['ok']


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
@pytest.mark.parametrize("periodic", [False, True])
def test_spline_native_closed_and_open_profiles(worker, tmp_path, plane, periodic):
    ir = curve_ir(plane, "bspline", periodic=periodic)
    result = compile_ir(worker, ir, tmp_path)
    assert result["measurements"]["is_valid"]
    assert result["measurements"]["solids"] == 1
    volume = result["measurements"]["volume"]
    if periodic:
        # Interpolation is not an analytic ellipse. Distinguish the native curve
        # from the diamond obtained by joining the same four points as lines.
        assert 2 * 20 * 10 * 5 < volume < 4 * 20 * 10 * 5
        larger = curve_ir(plane, "bspline")
        for p in larger["bodies"][0]["sketches"][0]["geometry"][0]["points"]:
            for axis in ("x", "y", "z"):
                p[axis] *= 2
        scaled = compile_ir(worker, larger, tmp_path / "scaled")
        assert scaled["measurements"]["volume"] == pytest.approx(volume * 4, rel=1e-4)
    else:
        # The open three-point interpolant is a parabola, closed by a straight edge.
        assert volume == pytest.approx(2 / 3 * 40 * 10 * 5, rel=1e-5)
    with zipfile.ZipFile(result["fcstd"]) as saved:
        xml = ElementTree.fromstring(saved.read("Document.xml"))
    native = xml.find(".//Geometry[@type='Part::GeomBSplineCurve']/BSplineCurve")
    assert native is not None
    assert native.attrib["IsPeriodic"] == str(int(periodic))
    assert result["round_trip"]["ok"]


@pytest.mark.parametrize("kind", ["ellipse", "bspline"])
def test_fixed_curves_pass_constraint_gate_and_saved_pad_remains_editable(worker, tmp_path, kind):
    ir = curve_ir("XY", kind)
    result = compile_ir(worker, ir, tmp_path)
    digest = worker.request_sync(M_INTROSPECT, {"ir": ir, "out_dir": str(tmp_path / "digest")})
    assert digest["key_dimensions"]["sk__fully_constrained"] == 1
    assert digest["key_dimensions"]["sk__dof"] == 0
    edited = worker.request_sync(M_REOPEN_EDIT, {"fcstd_path": result["fcstd"],
        "edits": [{"object": "pad", "property": "Length", "value": 10}]})
    assert edited["feature_states"]["sk"]["type_id"] == "Sketcher::SketchObject"
    assert edited["measurements"]["volume"] == pytest.approx(result["measurements"]["volume"] * 2, rel=1e-6)


@pytest.mark.parametrize("kind", ["ellipse", "bspline"])
def test_off_plane_curve_fails_with_sketch_identity(worker, tmp_path, kind):
    ir = curve_ir("XY", kind)
    ir["bodies"][0]["sketches"][0]["geometry"][0]["points"][0]["z"] = 2
    with pytest.raises(WorkerCallFailed) as exc:
        compile_ir(worker, ir, tmp_path)
    assert exc.value.rpc_error.feature_id == "sk"
    assert "sketch plane" in exc.value.rpc_error.message
