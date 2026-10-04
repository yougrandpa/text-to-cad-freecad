"""HTTP mesh previews: exact versions, honest failures and bounded resources."""

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

TestClient = pytest.importorskip("fastapi.testclient").TestClient
from tests.unit.test_server import make_services  # noqa: E402


def tetra_mesh(*, volume=1.0, tolerance=0.5):
    return {
        "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "facets": [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
        "bbox": {"x": 1, "y": 1, "z": 1, "x_min": 0, "y_min": 0, "z_min": 0},
        "volume": volume,
        "tolerance": tolerance,
        "vertex_count": 4,
        "facet_count": 4,
    }


class MeshWorker:
    def __init__(self):
        self.calls = []
        self.response = None

    def request(self, method, params, *, timeout_s):
        # A sync RPC on the ASGI event loop would freeze chat and stop buttons.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert method == "tessellate"
        assert Path(params["out_dir"]).is_dir()
        self.calls.append(copy.deepcopy(params))
        if self.response is not None:
            return copy.deepcopy(self.response)
        return {"ok": True, "result": {"ok": True, "mesh": tetra_mesh(
            volume=params["ir"]["version"] + 1.0, tolerance=params["tolerance"],
        )}}


@pytest.fixture
def client(tmp_path):
    services = make_services(tmp_path)
    services.store = StoreAdapter(services.config.storage.data_dir)
    services.store.create("part", IrDocument(model_id="part"))
    services.worker = MeshWorker()
    app = create_app(services)
    with TestClient(app) as client:
        client.services = services
        client.app_state = app.state
        yield client


def bump_version(client):
    store = client.services.store
    return store.apply_patch("part", IrPatch(
        base_version=store.load("part").version,
        ops=[IrPatchOp(op="update_requirement", payload={"raw_text": "new requirement"})],
    ))[0]


def test_mesh_wire_shape_cache_and_no_gate_side_effects(client):
    store = client.services.store
    before = store.verdict("part", 0)
    response = client.get("/models/part/mesh?version=0")
    assert response.status_code == 200, response.text
    assert response.json() == {"model_id": "part", "version": 0, "mesh": tetra_mesh()}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert client.get("/models/part/mesh").json() == response.json()
    assert len(client.services.worker.calls) == 1
    assert store.verdict("part", 0) == before
    assert before["verified"] is False
    assert client.services.gate.calls == 0
    assert not store.artifact_dir("part", 0).exists()
    workdir = Path(client.services.worker.calls[0]["out_dir"])
    assert ".preview-mesh" in workdir.parts
    assert not workdir.exists(), "preview scratch directories must be cleaned up"


def test_mesh_shape_is_documented_in_openapi(client):
    spec = client.get("/openapi.json").json()
    schema = spec["paths"]["/models/{model_id}/mesh"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/MeshPreviewResponse")
    fields = spec["components"]["schemas"]["PreviewMesh"]["properties"]
    assert {"vertices", "facets", "bbox", "volume", "tolerance", "vertex_count", "facet_count"} <= fields.keys()


def test_latest_and_old_versions_never_share_meshes(client):
    first = client.get("/models/part/mesh").json()
    bump_version(client)
    latest = client.get("/models/part/mesh").json()
    assert latest["version"] == 1
    assert latest["mesh"]["volume"] == 2
    assert client.get("/models/part/mesh?version=0").json() == first
    assert len(client.services.worker.calls) == 2


def test_snapshot_replacement_invalidates_same_version_cache(client):
    client.get("/models/part/mesh?version=0")
    store = client.services.store
    ir = store.load("part", 0)
    ir.notes = ["snapshot restored from another source"]
    store.snapshot_path("part", 0).write_text(ir.model_dump_json())
    assert client.get("/models/part/mesh?version=0").status_code == 200
    assert len(client.services.worker.calls) == 2
    assert client.services.worker.calls[-1]["ir"]["notes"] == ir.notes


def test_tolerance_and_force_are_part_of_cache_contract(client):
    client.get("/models/part/mesh")
    response = client.get("/models/part/mesh?tolerance=1.0")
    assert response.json()["mesh"]["tolerance"] == 1
    client.get("/models/part/mesh?tolerance=1.0")
    client.get("/models/part/mesh?tolerance=1.0&force=true")
    assert len(client.services.worker.calls) == 3


@pytest.mark.parametrize("query", ["version=-1", "version=nope", "tolerance=0", "tolerance=0.01", "tolerance=6", "tolerance=nan", "tolerance=inf"])
def test_detail_and_version_inputs_are_bounded(client, query):
    assert client.get(f"/models/part/mesh?{query}").status_code == 422
    assert client.services.worker.calls == []


@pytest.mark.parametrize("url", ["/models/missing/mesh", "/models/part/mesh?version=999"])
def test_missing_model_or_version_is_404(client, url):
    assert client.get(url).status_code == 404
    assert client.services.worker.calls == []


@pytest.mark.parametrize("model_id", [".private", "bad%5Cname", "bad%20name", "x" * 65])
def test_identifier_validation_precedes_store_or_worker_access(client, model_id):
    assert client.get(f"/models/{model_id}/mesh").status_code == 400
    assert client.services.worker.calls == []


def test_corrupt_snapshot_is_a_read_error_even_with_cached_mesh(client):
    client.get("/models/part/mesh")
    client.services.store.snapshot_path("part", 0).write_text("{not JSON")
    response = client.get("/models/part/mesh")
    assert response.status_code == 500
    assert "read" in response.json()["detail"]
    assert len(client.services.worker.calls) == 1


def test_wrong_snapshot_identity_is_never_served(client):
    store = client.services.store
    doc = store.load("part")
    doc.version = 25
    store.snapshot_path("part", 0).write_text(doc.model_dump_json())
    assert client.get("/models/part/mesh?version=0").status_code == 500
    assert client.services.worker.calls == []


@pytest.mark.parametrize("kind,expected", [("compile", 422), ("timeout", 504), ("runtime", 503)])
def test_worker_failure_is_honest_and_does_not_serve_stale_mesh(client, kind, expected):
    assert client.get("/models/part/mesh").status_code == 200
    client.services.worker.response = {"ok": False, "error": {"kind": kind, "message": "test failure"}}
    response = client.get("/models/part/mesh?force=true")
    assert response.status_code == expected
    assert "test failure" in response.json()["detail"]
    assert "mesh" not in response.json()
    assert client.get("/models/part/mesh").status_code == expected


def test_nested_worker_failure_is_not_success(client):
    client.services.worker.response = {"ok": True, "result": {"ok": False, "error": "no solid"}}
    response = client.get("/models/part/mesh")
    assert response.status_code == 422
    assert "no solid" in response.json()["detail"]


@pytest.mark.parametrize("field,value,expected", [
    ("vertices", [], 422),
    ("vertices", [[float("nan"), 0, 0]] * 4, 502),
    ("facets", [[0, 1, 99]], 502),
    ("facets", [[-1, 1, 2]], 502),
    ("facets", [[0, 0, 1]], 502),
    ("facets", [[0, 1]], 502),
    ("volume", 0, 422),
    ("volume", float("inf"), 502),
    ("tolerance", -1, 502),
])
def test_invalid_geometry_is_not_cached_or_given_to_browser(client, field, value, expected):
    mesh = tetra_mesh()
    mesh[field] = value
    client.services.worker.response = {"ok": True, "result": {"mesh": mesh}}
    assert client.get("/models/part/mesh").status_code == expected
    client.services.worker.response = None
    assert client.get("/models/part/mesh").status_code == 200
    assert len(client.services.worker.calls) == 2


@pytest.mark.parametrize("limit", ["MAX_VERTICES", "MAX_FACETS", "MAX_RESPONSE_BYTES"])
def test_mesh_allocation_and_response_limits(client, monkeypatch, limit):
    monkeypatch.setattr(f"tcad.server.mesh.{limit}", 2)
    assert client.get("/models/part/mesh").status_code == 413


def test_preview_workspace_symlink_escape_is_refused(client, tmp_path):
    data_dir = Path(client.services.config.storage.data_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_dir / ".preview-mesh").symlink_to(outside, target_is_directory=True)
    assert client.get("/models/part/mesh").status_code == 400
    assert client.services.worker.calls == []
    assert list(outside.iterdir()) == []


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


def test_health_and_interrupt_remain_responsive_during_tessellation(client):
    started, release = threading.Event(), threading.Event()
    worker = client.services.worker
    original = worker.request

    def slow_request(*args, **kwargs):
        started.set()
        assert release.wait(5), "test failed to release the worker"
        return original(*args, **kwargs)

    worker.request = slow_request

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


def test_mesh_fanout_is_rejected_before_exhausting_shared_threads(client):
    """Even abandoned callers retain their bounded slots until CAD work ends."""
    started, release = threading.Event(), threading.Event()
    worker = client.services.worker
    original = worker.request

    def slow_request(*args, **kwargs):
        started.set()
        assert release.wait(5), "test failed to release the worker"
        return original(*args, **kwargs)

    worker.request = slow_request

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
                assert len(worker.calls) == 0, "only the blocked coalesced build should have started"
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
            assert len(worker.calls) == 1
            # Completion frees the slots, and the successfully built mesh is cached.
            assert (await session.get("/models/part/mesh")).status_code == 200
            assert len(worker.calls) == 1

    asyncio.run(scenario())
