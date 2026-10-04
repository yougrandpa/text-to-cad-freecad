"""Viewer and snapshot consume the same pinned, validated scene."""

import asyncio
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tcad.config.schema import Config
from tcad.core.types import ToolContext
from tcad.core.wiring import RendererAdapter, StoreAdapter, build_context_loader
from tcad.inspect.artifact import ArtifactReader
from tcad.render.scene import SceneModel
from tcad.server.app import artifact_url_for, create_app
from tcad.tools.geo_tools import geo_view_handler
from tcad.verify.gate import Gate
from tests.fixtures.artifact_scene import publish_scene, tetra_mesh


def forbidden(*args, **kwargs):
    raise AssertionError("render query touched IR or FreeCAD")


def services(data_dir, renderer=None):
    return SimpleNamespace(store=SimpleNamespace(load=forbidden, current_version=forbidden),
        worker=SimpleNamespace(request=forbidden), renderer=renderer or RendererAdapter())


def context(data_dir):
    return ToolContext(model_id="part", thread_id="thread", turn_id="turn", data_dir=str(data_dir), visual_ok=True)


async def test_view_and_web_snapshot_share_scene_cache_and_immutable_files(tmp_path):
    manifest, root = publish_scene(tmp_path)
    originals = {p.name: p.read_bytes() for p in root.iterdir()}
    config = Config()
    config.storage.data_dir = str(tmp_path)
    app = create_app(config=config)
    with TestClient(app) as client:
        store = StoreAdapter(tmp_path)
        from tcad.ir.schema import IrDocument
        store.create("part", IrDocument(model_id="part"))
        for path in ("ir", "artifacts", "verdict"):
            assert client.get(f"/models/part/{path}").status_code == 200
        assert app.state.services is None
        mesh = client.get("/models/part/mesh").json()
        assert mesh["artifact_id"] == manifest.artifact_id
        result = await geo_view_handler(services(tmp_path),
            {"artifact_id": manifest.artifact_id, "views": ["iso"]}, context(tmp_path))
        assert result.ok, result.error
        assert manifest.artifact_id in result.content
        image = Path(result.images[0].path)
        assert "derived" in image.parts and root not in image.parents
        url = artifact_url_for(str(image))
        assert client.get(url).content == image.read_bytes()
        response = client.get("/models/part/render", params={"artifact_id": manifest.artifact_id})
        assert response.status_code == 200, response.text
        assert response.headers["x-artifact-id"] == mesh["artifact_id"]
        assert response.content == image.read_bytes()
        assert {p.name: p.read_bytes() for p in root.iterdir()} == originals
        assert app.state.services is None
        # The entire derived cache can be discarded and reconstructed.
        shutil.rmtree(tmp_path / "derived")
        assert client.get("/models/part/render").content == response.content


async def test_snapshot_renderer_runs_off_event_loop(tmp_path):
    manifest, _ = publish_scene(tmp_path)
    renderer = RendererAdapter()
    original = renderer.render
    def slow(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)
    renderer.render = slow
    task = asyncio.create_task(geo_view_handler(services(tmp_path, renderer),
        {"artifact_id": manifest.artifact_id}, context(tmp_path)))
    ticks = 0
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    assert (await task).ok
    assert ticks >= 10


async def test_unbuilt_snapshot_requires_commit_and_cannot_borrow_old_scene(tmp_path):
    publish_scene(tmp_path)
    svc = services(tmp_path)
    svc.store.current_version = lambda _: 1
    result = await geo_view_handler(svc, {}, context(tmp_path))
    assert not result.ok
    assert "ir_commit first" in result.error.message


def test_prescribed_pose_keeps_scene_immutable_and_matches_viewer_math():
    motion = {"body_id": "arm", "vertex_start": 0, "vertex_count": 4,
              "pivot": {"x": 0, "y": 0, "z": 0},
              "axis": {"x": 0, "y": 0, "z": 1}, "ratio": 1}
    scene = SceneModel(mesh=tetra_mesh(), body_ids=["arm"], motion=[motion])
    original = scene.model_dump_json()
    posed = scene.posed_mesh(angle=90)
    assert posed.vertices[1] == pytest.approx([0, 1, 0])
    assert posed.vertices[2] == pytest.approx([-1, 0, 0])
    assert scene.model_dump_json() == original


def native_scene():
    return SceneModel(mesh=tetra_mesh(), body_ids=["arm"], animation={
        "parts": [{"body_id": "arm", "vertex_start": 0, "vertex_count": 4}],
        "frames": [{"arm": [1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]} for x in (0, 10)],
        "start": 0, "step": 0.1, "solver": "FreeCAD Assembly"})


def test_native_snapshot_applies_the_saved_frame_and_bounds():
    scene = native_scene()
    posed = scene.posed_mesh(frame=1)
    assert posed.vertices[0] == [10, 0, 0]
    assert posed.bbox.x_min == 10
    with pytest.raises(ValueError, match="frame_index"):
        scene.posed_mesh(frame=2)
    with pytest.raises(ValueError, match="native scenes"):
        scene.posed_mesh(angle=10)


@pytest.mark.parametrize("change", ["overlap", "matrix", "time", "incomplete"])
def test_invalid_saved_animation_is_rejected(change):
    raw = native_scene().model_dump(mode="json")
    animation = raw["animation"]
    if change == "overlap":
        animation["parts"].append(animation["parts"][0])
    elif change == "matrix":
        animation["frames"][0]["arm"][0] = float("nan")
    elif change == "time":
        animation["step"] = 0
    else:
        animation["frames"][0] = {}
    with pytest.raises(ValidationError):
        SceneModel.model_validate(raw)


def test_scene_mismatch_blocks_gate_even_when_file_hashes_are_valid(tmp_path):
    manifest, root = publish_scene(tmp_path)
    # Re-index a modified scene to distinguish geometric consistency from the
    # basic file-integrity guard.
    reader = ArtifactReader(tmp_path)
    scene = reader.scene(manifest, root)
    scene.mesh.volume = 999
    (root / "scene.json").write_text(scene.model_dump_json())
    from tcad.store.artifacts import ArtifactStore
    ArtifactStore(tmp_path).write_manifest(root, model_id="part", version=0,
        attempt_id=manifest.attempt_id, ir_sha256=manifest.ir_sha256, status="verifying")
    report = Gate(build_context_loader(StoreAdapter(tmp_path), None)).evaluate_artifact(root)
    assert not report.passed
    assert "gate:cannot_attest_no_measurements" in report.blocking_failures


def test_snapshot_paths_do_not_allow_cache_escape(tmp_path):
    from tcad.render.snapshot import snapshot_dir
    with pytest.raises(ValueError):
        snapshot_dir(tmp_path, "sha256:" + "f" * 64, "part", "../outside")
