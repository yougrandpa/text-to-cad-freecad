"""Real FreeCAD produces a scene once; queries and snapshots only read it."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tcad.config.loader import load_default_config, resolve_paths
from tcad.core.types import ToolContext
from tcad.core.wiring import build_services
from tcad.inspect.artifact import ArtifactReader
from tcad.ir.schema import IrDocument
from tcad.loop.commit import run_commit
from tcad.server.app import create_app
from tcad.tools.geo_tools import geo_view_handler
from tests.contract.test_primitive_placement import FREECAD_CMD
from tests.contract.test_native_assembly import native_ir
from tests.contract.test_e2e_pipeline import make_ir

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.contract, pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason="FreeCAD unavailable")]


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path_factory.mktemp("scene_contract"))
    cfg.storage.sqlite_path = str(Path(cfg.storage.data_dir) / "tcad.sqlite3")
    cfg.runtime.freecad_cmd = FREECAD_CMD
    resolve_paths(cfg, root=REPO_ROOT)
    svc = build_services(cfg)
    try:
        yield svc
    finally:
        svc._worker_handle.close()


@pytest.mark.parametrize("kind", ["static", "prescribed", "native", "native_static"])
def test_saved_scene_survives_source_changes_and_worker_unavailability(services, kind):
    model_id = "scene-" + kind
    if kind.startswith("native"):
        data = native_ir()
        data["model_id"] = model_id
        for body in data["bodies"]:
            for feature in body["features"]:
                feature["name"] = feature["id"]
        if kind == "native_static":
            data["assembly"]["drivers"] = []
        ir = IrDocument.model_validate(data)
    else:
        ir = make_ir(model_id=model_id)
        if kind == "prescribed":
            from tcad.ir.schema import RotaryMotionSpec
            ir.bodies[0].motion = RotaryMotionSpec(pivot={"x": 0, "y": 0, "z": 0},
                                                  axis={"x": 0, "y": 0, "z": 1})
    services.store.create(model_id, ir)
    result, report = asyncio.run(run_commit(services, model_id, 0, "freeze scene", str(REPO_ROOT),
                                            services.config.storage.data_dir))
    assert report is not None and report.passed, result.content
    reader = ArtifactReader(services.config.storage.data_dir)
    manifest, root = reader.resolve(model_id, 0)
    scene = reader.scene(manifest, root)
    assert bool(scene.animation) == kind.startswith("native")
    assert bool(scene.motion) == (kind == "prescribed")
    # Changing source cannot change artifact geometry or captured native poses.
    services.store.snapshot_path(model_id, 0).write_text("broken authoring snapshot")
    request = services.worker.request
    def forbidden(*args, **kwargs):
        raise AssertionError("artifact rendering touched FreeCAD")
    services.worker.request = forbidden
    try:
        with TestClient(create_app(services)) as client:
            response = client.get(f"/models/{model_id}/mesh")
            assert response.status_code == 200, response.text
            assert response.json()["artifact_id"] == manifest.artifact_id
            snapshot = client.get(f"/models/{model_id}/render")
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.content.startswith(b"\x89PNG")
            ctx = ToolContext(model_id=model_id, thread_id="scene-thread", turn_id="scene-turn",
                              data_dir=services.config.storage.data_dir, visual_ok=True)
            args = {"artifact_id": manifest.artifact_id}
            if kind == "native":
                args["frame_index"] = len(scene.animation["frames"]) - 1
            elif kind == "prescribed":
                args["driver_angle_deg"] = 90
            result = asyncio.run(geo_view_handler(services, args, ctx))
            assert result.ok, result.error
            assert Path(result.images[0].path).is_file()
    finally:
        services.worker.request = request
