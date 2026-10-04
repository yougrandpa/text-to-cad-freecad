"""Viewer reads immutable artifact scenes, with bounded resources."""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from tcad.core.wiring import StoreAdapter
from tcad.ir.schema import IrDocument, IrPatch, IrPatchOp
from tcad.server.app import create_app
from tcad.server.mesh import MAX_ACTIVE_MESH_REQUESTS, MeshPreviewCache
from tcad.inspect.artifact import ArtifactReader
from tests.fixtures.artifact_scene import publish_scene, tetra_mesh
from tests.unit.test_server import make_services

TestClient = pytest.importorskip("fastapi.testclient").TestClient


class ForbiddenWorker:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("viewer must never compile or drive the worker")


@pytest.fixture
def client(tmp_path):
    services = make_services(tmp_path)
    services.store = StoreAdapter(services.config.storage.data_dir)
    services.store.create("part", IrDocument(model_id="part"))
    services.worker = ForbiddenWorker()
    app = create_app(services)
    manifest, root = publish_scene(services.config.storage.data_dir)
    with TestClient(app) as client:
        client.services = services
        client.app_state = app.state
        client.manifest, client.artifact_root = manifest, root
        yield client


def bump_version(client):
    store = client.services.store
    return store.apply_patch("part", IrPatch(base_version=store.load("part").version,
        ops=[IrPatchOp(op="update_requirement", payload={"raw_text": "new requirement"})]))[0]


def test_mesh_wire_shape_cache_and_no_gate_side_effects(client):
    store = client.services.store
    before = store.verdict("part", 0)
    response = client.get("/models/part/mesh?version=0")
    assert response.status_code == 200, response.text
    expected = {**tetra_mesh(), "vertex_count": 4, "facet_count": 4}
    assert response.json() == {"model_id": "part", "version": 0,
        "artifact_id": client.manifest.artifact_id, "status": "verified", "mesh": expected}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/models/part/mesh").json() == response.json()
    assert not client.services.worker.calls
    assert store.verdict("part", 0) == before
    assert client.services.gate.calls == 0


def test_mesh_shape_is_documented_in_openapi(client):
    spec = client.get("/openapi.json").json()
    fields = spec["components"]["schemas"]["MeshPreviewResponse"]["properties"]
    assert {"artifact_id", "status", "mesh", "animation"} <= fields.keys()


def test_pending_ir_edit_keeps_last_published_geometry(client):
    first = client.get("/models/part/mesh").json()
    bump_version(client)
    assert client.get("/models/part/mesh").json() == first
    assert client.get("/models/part/mesh?version=1").status_code == 404
    publish_scene(client.services.config.storage.data_dir, version=1, attempt="v1", mesh=tetra_mesh(volume=2))
    latest = client.get("/models/part/mesh").json()
    assert latest["version"] == 1 and latest["mesh"]["volume"] == 2
    assert client.get("/models/part/mesh?version=0").json() == first


def test_source_snapshot_changes_and_corruption_cannot_change_scene(client):
    first = client.get("/models/part/mesh").json()
    client.services.store.snapshot_path("part", 0).write_text("not JSON")
    assert client.get("/models/part/mesh?version=0").json() == first
    def forbidden(*args, **kwargs):
        raise AssertionError("viewer read the authoring store")
    client.services.store.load = forbidden
    client.services.store.current_version = forbidden
    assert client.get("/models/part/mesh").json() == first


def test_republish_same_version_uses_new_identity_without_changing_old_queries(client):
    original = client.get("/models/part/mesh").json()
    publish_scene(client.services.config.storage.data_dir, attempt="retry", mesh=tetra_mesh(volume=2))
    assert client.get("/models/part/mesh").json()["mesh"]["volume"] == 2
    assert client.get("/models/part/mesh", params={"artifact_id": client.manifest.artifact_id}).json() == original


def test_force_refresh_never_rebuilds_geometry_or_changes_tolerance(client):
    first = client.get("/models/part/mesh").json()
    assert client.get("/models/part/mesh?force=true").json() == first
    assert client.get("/models/part/mesh?tolerance=1.0").status_code == 409
    assert not client.services.worker.calls


@pytest.mark.parametrize("query", ["version=-1", "version=nope", "tolerance=0", "tolerance=0.01", "tolerance=6", "tolerance=nan", "tolerance=inf"])
def test_detail_and_version_inputs_are_bounded(client, query):
    assert client.get(f"/models/part/mesh?{query}").status_code == 422


@pytest.mark.parametrize("url", ["/models/missing/mesh", "/models/part/mesh?version=999"])
def test_missing_artifact_is_404(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("model_id", [".private", "bad%5Cname", "bad%20name", "x" * 65])
def test_identifier_validation_precedes_artifact_access(client, model_id):
    assert client.get(f"/models/{model_id}/mesh").status_code == 400


def test_corrupt_scene_is_rejected_even_with_cached_mesh(client):
    client.get("/models/part/mesh")
    (client.artifact_root / "scene.json").write_text("not JSON")
    response = client.get("/models/part/mesh")
    assert response.status_code == 409
    assert "integrity" in response.json()["detail"]


@pytest.mark.parametrize("field,value", [
    ("vertices", []), ("vertices", [[float("nan"), 0, 0]] * 4),
    ("facets", [[0, 1, 99]]), ("facets", [[-1, 1, 2]]),
    ("facets", [[0, 0, 1]]), ("facets", [[0, 1]]),
    ("volume", 0), ("volume", float("inf")), ("tolerance", -1),
])
def test_invalid_scene_geometry_cannot_be_loaded(client, field, value):
    from tcad.render.scene import SceneModel
    from pydantic import ValidationError
    mesh = tetra_mesh()
    mesh[field] = value
    with pytest.raises(ValidationError):
        SceneModel(mesh=mesh)


@pytest.mark.parametrize("limit", ["MAX_VERTICES", "MAX_FACETS", "MAX_RESPONSE_BYTES"])
def test_mesh_allocation_and_response_limits(client, monkeypatch, limit):
    monkeypatch.setattr(f"tcad.server.mesh.{limit}", 2)
    assert client.get("/models/part/mesh").status_code == 413


def test_scene_object_symlink_escape_is_refused(client, tmp_path):
    root = client.artifact_root
    import shutil
    shutil.rmtree(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    assert client.get("/models/part/mesh").status_code == 400


def test_cache_is_bounded_by_bytes_and_count_and_coalesces_concurrent_misses():
    cache = MeshPreviewCache(max_entries=2, max_bytes=8)
    calls = []

    def build():
        calls.append(1)
        time.sleep(0.01)
        return b"1234"

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: cache.get_or_build(("a",), build), range(4))) == [b"1234"] * 4
    assert len(calls) == 1
    cache.get_or_build(("b",), build)
    cache.get_or_build(("c",), build)
    assert len(cache._entries) == 2 and cache._bytes == 8
    cache.get_or_build(("a",), build)
    assert len(calls) == 4, "least-recently-used entry must be evicted"
    cache.get_or_build(("large",), lambda: b"123456789")
    assert ("large",) not in cache._entries


def test_health_and_interrupt_remain_responsive_during_scene_read(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    from tcad.inspect.artifact import ArtifactReader
    original = ArtifactReader.scene

    def slow_request(*args, **kwargs):
        started.set()
        assert release.wait(5), "test failed to release the worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(ArtifactReader, "scene", slow_request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as session:
            mesh_task = asyncio.create_task(session.get("/models/part/mesh"))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                health = await asyncio.wait_for(session.get("/health"), timeout=1)
                stop = await asyncio.wait_for(session.post("/chat/interrupt", json={"request_id": "pending"}), timeout=1)
                assert health.status_code == stop.status_code == 200
            finally:
                release.set()
            assert (await mesh_task).status_code == 200

    asyncio.run(scenario())


def test_mesh_fanout_is_rejected_before_exhausting_shared_threads(client, monkeypatch):
    """Even abandoned callers retain their bounded slots until CAD work ends."""
    started, release = threading.Event(), threading.Event()
    from tcad.inspect.artifact import ArtifactReader
    original = ArtifactReader.scene

    def slow_request(*args, **kwargs):
        started.set()
        assert release.wait(5), "test failed to release the worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(ArtifactReader, "scene", slow_request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as session:
            pending = [asyncio.create_task(session.get("/models/part/mesh"))
                       for _ in range(MAX_ACTIVE_MESH_REQUESTS)]
            try:
                assert await asyncio.to_thread(started.wait, 3)
                # Registration happens before the first await in the endpoint;
                # let all admitted callers reach that point.
                for _ in range(100):
                    if len(client.app_state.mesh_preview_jobs) == MAX_ACTIVE_MESH_REQUESTS:
                        break
                    await asyncio.sleep(0.005)
                assert len(client.app_state.mesh_preview_jobs) == MAX_ACTIVE_MESH_REQUESTS

                overflow = await asyncio.wait_for(asyncio.gather(*[
                    session.get("/models/part/mesh") for _ in range(40)
                ]), timeout=1)
                assert {response.status_code for response in overflow} == {429}
                assert all(response.headers["retry-after"] == "1" for response in overflow)
                assert not client.services.worker.calls
                health = await asyncio.wait_for(session.get("/health"), timeout=1)
                stop = await asyncio.wait_for(session.post("/chat/interrupt", json={"request_id": "pending"}), timeout=1)
                assert health.status_code == stop.status_code == 200

                # Browser AbortController cancels the wait, not the sync build.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                assert len(client.app_state.mesh_preview_jobs) == MAX_ACTIVE_MESH_REQUESTS
                after_cancel = await asyncio.wait_for(session.get("/models/part/mesh"), timeout=1)
                assert after_cancel.status_code == 429
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.wait_for(asyncio.gather(
                    *list(client.app_state.mesh_preview_jobs), return_exceptions=True,
                ), timeout=3)
            assert not client.app_state.mesh_preview_jobs
            assert not client.services.worker.calls
            # Completion frees the slots, and the successfully built mesh is cached.
            assert (await session.get("/models/part/mesh")).status_code == 200
            assert not client.services.worker.calls

    asyncio.run(scenario())
