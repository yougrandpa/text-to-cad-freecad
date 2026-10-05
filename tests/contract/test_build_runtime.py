"""Real-kernel evidence for reusable geometry and component-level assembly."""

import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest

from tcad.core.types import ToolContext
from tcad.core.wiring import build_services
from tcad.config.loader import load_default_config
from tcad.inspect.artifact import ArtifactReader
from tcad.ir.schema import IrDocument, IrPatch, IrPatchOp
from tcad.loop.commit import run_commit
from tcad.tools.geo_tools import asset_export_handler, geo_check_motion_handler
from tests.contract.test_e2e_pipeline import make_ir
from tests.contract.test_primitive_placement import FREECAD_CMD

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.contract, pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCAD unavailable")]


@pytest.fixture
def services(tmp_path):
    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.runtime.freecad_cmd = FREECAD_CMD
    cfg.runtime.worker_pool_size = 2
    svc = build_services(cfg)
    try:
        yield svc
    finally:
        svc.worker.close()


def commit(services, model_id, version=0):
    return asyncio.run(run_commit(services, model_id, version, "runtime test", str(ROOT), services.config.storage.data_dir))


def test_geometry_reused_across_versions_but_gate_uses_new_requirements(services):
    model_id = "cache-real"
    services.store.create(model_id, make_ir(model_id=model_id))
    result, report = commit(services, model_id)
    assert report.passed, result.content
    reader = ArtifactReader(services.store.data_dir)
    first, root = reader.resolve(model_id, 0)
    build = json.loads(reader.read_file(first, root, "build.json"))
    assert not build["geometry_cache_hit"]
    patch = IrPatch(base_version=0, ops=[IrPatchOp(op="update_requirement", payload={
        "constraints": [{"kind": "volume", "value": 1, "confirmed": True,
                         "source_text": "volume must be 1 mm3", "tol": 0.001}]})])
    services.store.apply_patch(model_id, patch)
    calls = []
    original = services.worker.request
    def record(method, *args, **kwargs):
        calls.append(method)
        return original(method, *args, **kwargs)
    services.worker.request = record
    result, report = commit(services, model_id, 1)
    assert report is not None and not report.passed
    assert "build_artifacts" not in calls
    failed = services.build_runtime.registry.list(model_id)[0]
    assert failed.state == "failed" and failed.artifact_id
    manifest, attempt = reader.resolve(model_id, artifact_id=failed.artifact_id)
    assert manifest.status.value == "failed"
    assert reader.scene(manifest, attempt).mesh.volume > 1
    reused = json.loads(reader.read_file(manifest, attempt, "build.json"))
    assert reused["geometry_cache_hit"] and reused["build_digest"] == build["build_digest"]
    assert reader.resolve(model_id)[0].artifact_id == first.artifact_id


def test_parallel_components_preserve_parametric_fcstd_and_pinned_partref(services):
    model_id = "components-real"
    base = make_ir(model_id=model_id)
    second = copy.deepcopy(base.bodies[0])
    second.id, second.name = "second", "second"
    for sketch in second.sketches:
        original_id = sketch.id
        sketch.id = "second_" + sketch.id
        for feature in second.features:
            if feature.profile_sketch == original_id:
                feature.profile_sketch = sketch.id
    for feature in second.features:
        feature.id = "second_" + feature.id
    base.bodies.append(second)
    services.store.create(model_id, base)
    original = services.worker.request
    concurrent = threading.Barrier(2)
    child_calls = []
    def record(method, params=None, **kwargs):
        if method == "build_artifacts" and params["ir"]["model_id"].startswith("part-"):
            child_calls.append(params["ir"]["bodies"][0]["id"])
            concurrent.wait(timeout=5)
        return original(method, params, **kwargs)
    services.worker.request = record
    result, report = commit(services, model_id)
    services.worker.request = original
    assert report and report.passed, result.content
    assert set(child_calls) == {base.bodies[0].id, second.id}
    reader = ArtifactReader(services.store.data_dir)
    manifest, root = reader.resolve(model_id, 0)
    graph = json.loads(reader.read_file(manifest, root, "components.json"))
    assert len(graph["nodes"]) == 3
    fcstd = root / (model_id + ".FCStd")
    response = services.worker.request("reopen_edit_measure", {"fcstd_path": str(fcstd),
        "edits": [{"object": second.features[0].id, "property": "Length", "value": 12}]})
    assert response["ok"], response
    assert response["result"]["measurements"]["volume"] != reader.digest(manifest, root).volume
    body_id = base.bodies[0].id
    source = IrDocument(model_id="reference-real", bodies=[{
        "id": "instance", "name": "instance", "part_ref": {"model_id": model_id,
        "artifact_id": manifest.artifact_id, "body_id": body_id,
        "placement": {"position": {"x": 100, "y": 0, "z": 0}}}}])
    services.store.create(source.model_id, source)
    result, report = commit(services, source.model_id)
    assert report and report.passed, result.content
    reference, ref_root = reader.resolve(source.model_id, 0)
    assert reader.digest(reference, ref_root).bbox.x_min == pytest.approx(100)
    ctx = ToolContext(model_id=source.model_id, thread_id="t", turn_id="t", data_dir=str(reader.data_dir))
    # Conversion reads the pinned document. Authoring bytes are no longer needed.
    services.store.snapshot_path(source.model_id, 0).write_text("broken source")
    exported = asyncio.run(asset_export_handler(services, {"fmt": "brep", "artifact_id": reference.artifact_id}, ctx))
    assert exported.ok, exported.error
    checked = asyncio.run(geo_check_motion_handler(services, {"artifact_id": reference.artifact_id}, ctx))
    assert checked.ok, checked.error
    assert json.loads(checked.content)["sampled_clear"]


def test_cache_can_be_deleted_and_native_graph_matches_sequential_solver(services, tmp_path):
    import shutil
    from tests.contract.test_native_assembly import native_ir
    source = native_ir()
    for body in source["bodies"]:
        for feature in body["features"]:
            feature["name"] = feature["id"]
    ir = IrDocument.model_validate(source)
    ir.model_id = "graph-native"
    services.store.create(ir.model_id, ir)
    result, report = commit(services, ir.model_id)
    assert report and report.passed, result.content
    reader = ArtifactReader(services.store.data_dir)
    manifest, root = reader.resolve(ir.model_id, 0)
    scene = reader.scene(manifest, root)
    graph = json.loads(reader.read_file(manifest, root, "components.json"))
    assert graph["nodes"][-1]["dependencies"] == ["arm", "base"]
    serial = services.worker.request("build_artifacts", {"ir": ir.model_dump(mode="json"),
        "out_dir": str(tmp_path / "serial"), "exports": ["fcstd"]}, timeout_s=240)
    assert serial["ok"], serial
    serial_scene = serial["result"]["scene"]
    assert scene.mesh.volume == pytest.approx(serial_scene["mesh"]["volume"])
    for frame, reference in zip(scene.animation["frames"], serial_scene["frames"], strict=True):
        for body in frame:
            assert frame[body] == pytest.approx(reference[body])
    # Removing all disposable geometry does not remove project evidence.
    shutil.rmtree(Path(services.store.data_dir) / "cache")
    result, report = commit(services, ir.model_id)
    assert report and report.passed, result.content
    rebuilt, rebuilt_root = reader.resolve(ir.model_id, 0)
    assert rebuilt.artifact_id != manifest.artifact_id
    assert not json.loads(reader.read_file(rebuilt, rebuilt_root, "build.json"))["geometry_cache_hit"]
    assert reader.scene(*reader.resolve(ir.model_id, artifact_id=manifest.artifact_id)).mesh.volume == scene.mesh.volume


def test_cancelled_real_build_cannot_publish_and_pool_recovers(services):
    from tcad.build.pool import check_cancelled
    model_id = "cancel-real"
    services.store.create(model_id, make_ir(model_id=model_id))
    original = services.build_runtime.geometry
    entered = threading.Event()
    def blocked(*args):
        # Make the real backend warm before stopping the enclosing transaction.
        services.worker.request("ping", {})
        entered.set()
        while True:
            check_cancelled()
            threading.Event().wait(0.01)
    services.build_runtime.geometry = blocked
    async def stop():
        task = asyncio.create_task(run_commit(services, model_id, 0, "cancel", str(ROOT), services.config.storage.data_dir))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(stop())
    assert services.build_runtime.registry.list(model_id)[0].state == "cancelled"
    assert not services.store.artifact_dir(model_id, 0).exists()
    assert not list((Path(services.store.data_dir) / "artifacts" / model_id).glob("*staging*"))
    services.build_runtime.geometry = original
    result, report = commit(services, model_id)
    assert report and report.passed, result.content
