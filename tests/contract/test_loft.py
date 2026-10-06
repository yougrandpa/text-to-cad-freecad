"""Native section lofts, independent positioned planes and live FCStd evidence."""
import copy
import json
import math
import subprocess

import pytest

from tcad.ir.schema import IrDocument
from tcad.ir.validate import validate_ir
from tcad.worker.protocol import M_BUILD_ARTIFACTS, M_REOPEN_EDIT
from tests.contract.test_sketch_planes import compile_ir, pytestmark, worker
from tests.contract.test_param_capability import FREECAD_CMD, REPO_ROOT


def loft_ir(*, subtract=False, curved=False, rotated=False):
    sketches, features = [], []
    for i, (height, radius) in enumerate([(0, 2), (5, 3), (10, 4)]):
        position = {"x": 0, "y": height if rotated else 0, "z": 0 if rotated else height}
        placement = {"position": position}
        if rotated:
            placement.update(axis={"x": 1, "y": 0, "z": 0}, angle=90)
        features.append({"id": f"plane{i}", "name": f"plane{i}", "op": "datum_plane", "placement": placement})
        def point(u, v):
            return {"x": u, "y": height if rotated else v, "z": v if rotated else height}
        geometry = {"id": "curve", "kind": "circle", "points": [point(0, 0)], "radius": radius}
        if curved:
            geometry = {"id": "curve", "kind": "bspline", "periodic": True,
                        "points": [point(radius * math.cos(a), radius * .6 * math.sin(a)) for a in [0, math.pi / 2, math.pi, 3 * math.pi / 2]]}
        sketches.append({"id": f"s{i}", "name": f"section{i}", "plane": {"kind": "datum_plane", "feature_id": f"plane{i}"},
                         "geometry": [geometry], "constraints": [{"type": "Block", "refs": [0]}]})
    if subtract:
        features.insert(0, {"id": "block", "name": "block", "op": "additive_box",
                           "placement": {"position": {"x": -5, "y": -5, "z": 0}},
                           "params": {"length": 10, "width": 10, "height": 10}})
    features.append({"id": "loft", "name": "loft", "op": "subtractive_loft" if subtract else "additive_loft",
                     "profile_sketch": "s0", "sections": ["s1", "s2"], "params": {"ruled": False, "closed": False}})
    return {"model_id": "loft_test", "bodies": [{"id": "b", "name": "loft body", "sketches": sketches, "features": features}]}


@pytest.mark.parametrize("rotated", [False, True])
@pytest.mark.parametrize("curved", [False, True])
def test_native_loft_and_placed_planes_export_real_solids(worker, tmp_path, rotated, curved):
    ir = loft_ir(curved=curved, rotated=rotated)
    assert not [i for i in validate_ir(IrDocument.model_validate(ir)) if i.severity == "error"]
    result = compile_ir(worker, ir, tmp_path)
    measured = result["measurements"]
    assert measured["is_valid"] and measured["solids"] == 1
    assert measured["bbox"]["y" if rotated else "z"] == pytest.approx(10)
    assert result["round_trip"]["ok"], result["round_trip"]
    if not curved:
        assert measured["volume"] == pytest.approx(math.pi * 10 * (2**2 + 2*4 + 4**2) / 3, rel=1e-6)
    reopened = worker.request_sync(M_REOPEN_EDIT, {"fcstd_path": result["fcstd"],
        "edits": [{"object": "loft", "property": "Ruled", "value": True}]})
    assert reopened["ok"], reopened
    assert reopened["feature_states"]["loft"]["type_id"] == "PartDesign::AdditiveLoft"
    assert reopened["feature_states"]["plane2"]["type_id"] == "PartDesign::Plane"
    assert reopened["measurements"]["volume"] == pytest.approx(measured["volume"], rel=1e-6)


def test_native_subtractive_loft_removes_analytic_bore(worker, tmp_path):
    result = compile_ir(worker, loft_ir(subtract=True), tmp_path)
    assert result["measurements"]["volume"] == pytest.approx(1000 - math.pi * 10 * 28 / 3, rel=1e-6)
    assert result["round_trip"]["ok"]


def test_trailing_datum_plane_keeps_solid_tip_topology(worker, tmp_path):
    ir = {"model_id": "datum_digest", "bodies": [{"id": "b", "name": "b", "features": [
        {"id": "box", "name": "box", "op": "additive_box",
         "params": {"length": 10, "width": 10, "height": 10}},
        {"id": "plane", "name": "plane", "op": "datum_plane",
         "placement": {"position": {"x": 0, "y": 0, "z": 20}}},
    ]}]}
    worker.request_sync(M_BUILD_ARTIFACTS, {
        "ir": ir, "out_dir": str(tmp_path), "exports": ["fcstd"],
    })
    digest = json.loads((tmp_path / "digest.json").read_text(encoding="utf-8"))
    assert digest["volume"] == pytest.approx(1000)
    assert len(digest["faces"]) == 6
    assert len(digest["edges"]) == 12
    assert {face["feature_id"] for face in digest["faces"]} == {"box"}
    assert {edge["feature_id"] for edge in digest["edges"]} == {"box"}
    assert digest["faces"] == digest["feature_geometry"]["box"]["faces"]
    assert digest["edges"] == digest["feature_geometry"]["box"]["edges"]
    plane_faces = digest["feature_geometry"]["plane"]["faces"]
    assert len(plane_faces) == 1 and plane_faces[0]["feature_id"] == "plane"


def test_reopened_datum_position_moves_the_loft_section(worker, tmp_path):
    result = compile_ir(worker, loft_ir(), tmp_path)
    script = tmp_path / "edit_plane.py"
    script.write_text('import FreeCAD, json\n'
        f'doc=FreeCAD.openDocument({result["fcstd"]!r})\n'
        'plane=doc.getObject("plane2")\n'
        'position=plane.Placement\nposition.Base.z=12\nplane.Placement=position\n'
        'doc.recompute()\nshape=doc.getObject("b").Shape\n'
        'print("###PLANE###"+json.dumps({"z":shape.BoundBox.ZLength,"volume":shape.Volume,"valid":shape.isValid()}))\n'
        'FreeCAD.closeDocument(doc.Name)\n', encoding="utf-8")
    process = subprocess.run([FREECAD_CMD, "--console", "-P", str(REPO_ROOT), str(script)],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr[-2000:]
    measured = json.loads(next(line[len("###PLANE###"):] for line in process.stdout.splitlines() if line.startswith("###PLANE###")))
    assert measured["valid"] and measured["z"] == pytest.approx(12)
    assert measured["volume"] > result["measurements"]["volume"]


def test_loft_rejects_missing_duplicate_foreign_and_unused_sections():
    for change, code in [(lambda f: f.update(sections=[]), "loft_sections_missing"),
                         (lambda f: f.update(sections=["s0"]), "loft_sections_invalid"),
                         (lambda f: f.update(sections=["missing"]), "loft_sections_invalid"),
                         (lambda f: f.update(op="pad"), "loft_sections_unused")]:
        ir = copy.deepcopy(loft_ir())
        change(ir["bodies"][0]["features"][-1])
        assert code in [i.code for i in validate_ir(IrDocument.model_validate(ir))]
