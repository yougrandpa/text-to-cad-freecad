import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tcad.build.digest import build_digest
from tcad.build.jobs import BuildJob, JobRegistry
from tcad.build.pool import WarmWorkerPool, worker_owner
from tcad.build.scheduler import BuildScheduler
from tcad.core.types import ToolResult, GateReport
from tcad.artifacts.cache import GeometryCache
from tcad.build.execution import run_blocking
from tcad.build.pool import worker_cancellation
from tcad.build.worker_client import WorkerAborted


@pytest.mark.parametrize("reference", ["plane", "refs", "base_feature", "profile_sketch"])
def test_cross_body_dependencies_require_joint_build(reference):
    from tcad.build.components import requires_joint_build
    ir = {"bodies": [{"id": "a", "features": [{"id": "support"}]},
                     {"id": "b", "features": [{"id": "button"}]}]}
    assert not requires_joint_build(ir)
    node = ir["bodies"][1]["features"][0]
    node[reference] = {"feature_id": "support"} if reference == "plane" else ["support"] if reference == "refs" else "support"
    assert requires_joint_build(ir)
    node[reference] = {"feature_id": "button"} if reference == "plane" else ["button"] if reference == "refs" else "button"
    assert not requires_joint_build(ir)


def test_component_preview_limit_error_names_body_and_retains_actionable_hint(tmp_path):
    from tcad.build.components import compile_components, ComponentBuildFailed
    from tcad.render.scene import MAX_SCENE_VERTICES
    runtime = SimpleNamespace(compiler="test", pool=SimpleNamespace(handles=[1], cancel_owner=lambda *a: None),
        cache=GeometryCache(tmp_path / "cache"), worker=SimpleNamespace(request=lambda *a, **kw: {
            "ok": True, "result": {"scene": {"mesh": {"vertices": [[0, 0, 0]] * (MAX_SCENE_VERTICES + 1)}}}}))
    ir = {"model_id": "mouse", "bodies": [{"id": "left_key", "name": "left key"},
                                          {"id": "right_key", "name": "right key"}]}
    with pytest.raises(ComponentBuildFailed) as caught:
        compile_components(runtime, ir, ["1", "0"], tmp_path, {})
    error = caught.value.error
    assert "body left_key" in error["message"] and "allocation limits" in error["message"]
    assert error["kind"] == "runtime" and "CAD body compiled" in error["hint"]
    assert len(error["message"]) < 300
    from tcad.loop.commit import _worker_error
    assert _worker_error(error).error.hint == error["hint"]


def test_digest_ignores_version_but_keeps_geometry_backend_and_asset_identity():
    ir = {"model_id": "a", "version": 1, "bodies": [{"id": "b", "size": 10}], "requirements": {}}
    kwargs = dict(compiler="c1", freecad=["1", "0"], exports=["stl", "step"], assets={"part": "h1"})
    digest = build_digest(ir, **kwargs)
    assert digest == build_digest({**ir, "model_id": "b", "version": 2, "notes": ["undo"]}, **kwargs)
    assert digest == build_digest(ir, **{**kwargs, "exports": ["step", "stl"]})
    for changed in ({"compiler": "c2"}, {"freecad": ["1", "1"]}, {"assets": {"part": "h2"}}):
        assert digest != build_digest(ir, **{**kwargs, **changed})
    assert digest != build_digest({**ir, "bodies": [{"id": "b", "size": 11}]}, **kwargs)


def test_registry_recovers_interrupted_jobs_and_preserves_terminal_records(tmp_path):
    registry = JobRegistry(tmp_path)
    running = registry.save(BuildJob(model_id="a", ir_version=0, digest="d", state="running"))
    done = registry.save(BuildJob(model_id="a", ir_version=1, digest="d", state="published"))
    recovered = JobRegistry(tmp_path)
    assert recovered.get(running.job_id).state == "interrupted"
    assert recovered.get(done.job_id).state == "published"
    with pytest.raises(ValueError):
        recovered.get("../escape")


class FakePool:
    def __init__(self):
        self.cancelled = []
    def cancel_owner(self, owner, reason):
        self.cancelled.append(owner)
    def release_owner(self, owner):
        pass


@pytest.mark.asyncio
async def test_duplicate_jobs_share_work_and_one_cancelled_waiter_cannot_cancel_others(tmp_path):
    scheduler = BuildScheduler(tmp_path, FakePool())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def build():
        calls.append(worker_owner.get())
        entered.set()
        await release.wait()
        scheduler.progress("published", artifact_id="artifact")
        return ToolResult(ok=True), GateReport(model_id="a", ir_version=0, passed=True)
    first = asyncio.create_task(scheduler.run("a", 0, "input", build))
    await entered.wait()
    second = asyncio.create_task(scheduler.run("a", 0, "input", build))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not scheduler.pool.cancelled
    release.set()
    await second
    assert len(calls) == 1
    assert scheduler.registry.list()[0].state == "published"


@pytest.mark.asyncio
async def test_newer_build_cancels_old_build_without_publishing_it(tmp_path):
    scheduler = BuildScheduler(tmp_path, FakePool())
    entered = asyncio.Event()
    async def old():
        entered.set()
        await asyncio.Event().wait()
    async def latest():
        scheduler.progress("published", artifact_id="new")
        return ToolResult(ok=True), GateReport(model_id="a", ir_version=1, passed=True)
    first = asyncio.create_task(scheduler.run("a", 0, "old", old))
    await entered.wait()
    await scheduler.run("a", 1, "new", latest)
    with pytest.raises(asyncio.CancelledError):
        await first
    jobs = scheduler.registry.list()
    assert [j.state for j in jobs] == ["published", "cancelled"]


def test_pool_cancellation_isolated_to_owner_and_worker_stays_warm():
    class Handle:
        def __init__(self):
            self.started = False
            self.start_count = 0
            self.entered, self.release = threading.Event(), threading.Event()
            self.aborts = 0
        def start(self):
            if not self.started:
                self.start_count += 1
                self.started = True
        def request_sync(self, method, params, timeout_s):
            self.entered.set()
            assert self.release.wait(2)
            return {"ok": True}
        def abort_inflight(self, reason):
            self.aborts += 1
            self.release.set()
            return True
    a, b = Handle(), Handle()
    pool = WarmWorkerPool([a, b])
    def run(owner):
        token = worker_owner.set(owner)
        try:
            return pool.request_sync("build", timeout_s=2)
        finally:
            worker_owner.reset(token)
    with ThreadPoolExecutor(2) as executor:
        fa, fb = executor.submit(run, "a"), executor.submit(run, "b")
        assert a.entered.wait(2) and b.entered.wait(2)
        pool.cancel_owner("a")
        assert a.aborts == 1 and b.aborts == 0
        b.release.set()
        fa.result(), fb.result()
        run("c")
    assert a.start_count == b.start_count == 1


def test_cache_locks_do_not_alias_parent_and_component_digests(tmp_path):
    cache = GeometryCache(tmp_path)
    parent = "sha256:" + "0" * 64
    child = "sha256:" + "0" * 63 + "1"
    with ThreadPoolExecutor(1) as executor:
        with cache.lock(parent):
            def compile_child():
                with cache.lock(child):
                    return True
            assert executor.submit(compile_child).result(timeout=1)


def test_cancelled_cache_wait_exits_before_other_build_releases_lock(tmp_path):
    cache = GeometryCache(tmp_path)
    digest = "sha256:" + "0" * 64
    signal = threading.Event()
    entered = threading.Event()
    def waiting():
        token = worker_cancellation.set(signal)
        try:
            entered.set()
            with cache.lock(digest):
                pytest.fail("cancelled build entered cache")
        finally:
            worker_cancellation.reset(token)
    with ThreadPoolExecutor(1) as executor:
        with cache.lock(digest):
            future = executor.submit(waiting)
            assert entered.wait(1)
            signal.set()
            with pytest.raises(WorkerAborted):
                future.result(timeout=1)


async def test_quick_edits_only_execute_latest_queued_job(tmp_path):
    scheduler = BuildScheduler(tmp_path, FakePool(), debounce_s=0.03)
    calls = []
    async def build(version):
        calls.append(version)
        scheduler.progress("published", artifact_id="artifact")
        return ToolResult(ok=True), GateReport(model_id="a", ir_version=version, passed=True)
    tasks = []
    for version in range(3):
        tasks.append(asyncio.create_task(scheduler.run("a", version, str(version), lambda v=version: build(v))))
        await asyncio.sleep(0)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert calls == [2]
    assert all(isinstance(value, asyncio.CancelledError) for value in results[:2])
    assert [job.state for job in scheduler.registry.list()] == ["published", "cancelled", "cancelled"]


async def test_cancel_is_idempotent_and_publication_checks_the_stop(tmp_path):
    scheduler = BuildScheduler(tmp_path, FakePool(), debounce_s=0)
    entered, stopped = asyncio.Event(), asyncio.Event()
    published = []
    async def build():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            with pytest.raises(WorkerAborted):
                scheduler.publish(lambda: published.append(True))
            stopped.set()
            raise
    task = asyncio.create_task(scheduler.run("a", 0, "input", build))
    await entered.wait()
    job = scheduler.registry.list()[0]
    assert scheduler.cancel(job.job_id)
    assert not scheduler.cancel(job.job_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and not published
    assert scheduler.registry.get(job.job_id).state == "cancelled"


async def test_completed_publication_cannot_be_relabelled_cancelled(tmp_path):
    scheduler = BuildScheduler(tmp_path, FakePool(), debounce_s=0)
    async def build():
        assert scheduler.publish(lambda: True)
        job = scheduler.registry.list()[0]
        assert not scheduler.cancel(job.job_id)
        scheduler.progress("published", artifact_id="artifact")
        return ToolResult(ok=True), GateReport(model_id="a", ir_version=0, passed=True)
    await scheduler.run("a", 0, "input", build)
    assert scheduler.registry.list()[0].state == "published"


async def test_build_timeout_drains_blocking_work_and_records_failure(tmp_path):
    from tcad.build.pool import check_cancelled
    scheduler = BuildScheduler(tmp_path, FakePool(), debounce_s=0, timeout_s=0.05)
    finished = threading.Event()
    def compile():
        try:
            while True:
                check_cancelled()
                finished.wait(0.01)
        finally:
            finished.set()
    async def build():
        return await run_blocking(SimpleNamespace(), compile, label="compile")
    with pytest.raises(TimeoutError):
        await scheduler.run("a", 0, "input", build)
    assert finished.is_set()
    job = scheduler.registry.list()[0]
    assert job.state == "failed" and job.phase == "timeout"


async def test_observer_failure_does_not_turn_published_build_into_failure(tmp_path):
    from tcad.build.scheduler import build_observer
    scheduler = BuildScheduler(tmp_path, FakePool(), debounce_s=0)
    def broken(payload):
        raise RuntimeError("disconnected viewer")
    async def build():
        scheduler.progress("published", artifact_id="artifact")
        return ToolResult(ok=True), GateReport(model_id="a", ir_version=0, passed=True)
    token = build_observer.set(broken)
    try:
        await scheduler.run("a", 0, "input", build)
    finally:
        build_observer.reset(token)
    assert scheduler.registry.list()[0].state == "published"


def test_partref_digest_uses_document_bytes_and_body_identity():
    kwargs = dict(compiler="c", freecad=["1"], exports=["fcstd"], assets={"b": "document"})
    body = {"id": "b", "part_ref": {"model_id": "source", "artifact_id": "first", "body_id": "part"}}
    ir = {"bodies": [body]}
    changed = {"bodies": [{**body, "part_ref": {**body["part_ref"], "artifact_id": "retry"}}]}
    assert build_digest(ir, **kwargs) == build_digest(changed, **kwargs)
    changed["bodies"][0]["part_ref"]["body_id"] = "another"
    assert build_digest(ir, **kwargs) != build_digest(changed, **kwargs)


def test_geometry_cache_rebinds_model_and_version_and_rejects_corruption(tmp_path):
    from tests.fixtures.artifact_scene import publish_scene
    import json
    _, source = publish_scene(tmp_path)
    (source / "part.FCStd").write_bytes(b"document bytes")
    cache = GeometryCache(tmp_path)
    digest = "sha256:" + "a" * 64
    cache.store(digest, source, "part")
    target = tmp_path / "attempt"
    target.mkdir()
    assert cache.restore(digest, target, "duplicate", 12)
    measured = json.loads((target / "digest.json").read_bytes())
    assert (measured["model_id"], measured["ir_version"]) == ("duplicate", 12)
    assert (target / "duplicate.FCStd").read_bytes() == b"document bytes"
    (cache.directory(digest) / "scene.json").write_bytes(b"corrupt")
    untouched = tmp_path / "fresh"
    untouched.mkdir()
    assert not cache.restore(digest, untouched, "another", 3)
    assert not list(untouched.iterdir())


def test_invalid_geometry_cache_metadata_cannot_partially_write_attempt(tmp_path):
    from tests.fixtures.artifact_scene import publish_scene
    import json
    _, source = publish_scene(tmp_path)
    (source / "part.FCStd").write_bytes(b"document")
    cache = GeometryCache(tmp_path)
    digest = "sha256:" + "b" * 64
    cache.store(digest, source, "part")
    path = cache.directory(digest) / "cache.json"
    index = json.loads(path.read_bytes())
    index["model_id"] = []
    path.write_text(json.dumps(index))
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    assert not cache.restore(digest, attempt, "part", 1)
    assert not list(attempt.iterdir())


@pytest.mark.parametrize("nodes", [
    [{"id": "part", "digest": "d", "dependencies": ["missing"]}],
    [{"id": "a", "digest": "d", "dependencies": ["b"]},
     {"id": "b", "digest": "d", "dependencies": ["a"]}],
    [{"id": "a", "digest": "d"}, {"id": "a", "digest": "e"}],
])
def test_component_graph_rejects_missing_cycles_and_duplicate_nodes(nodes):
    from tcad.build.graph import BuildGraph
    with pytest.raises(ValueError):
        BuildGraph(nodes=nodes)


def test_pool_crash_recovery_does_not_interrupt_other_owner():
    from tests.unit.test_worker_abort import make_handle
    from tcad.build.worker_client import WorkerCrashed
    import time
    handles = [make_handle(), make_handle()]
    for handle in handles:
        handle.request_sync = handle._request_raw  # fake transport probe verbs
    pool = WarmWorkerPool(handles)
    pool.start()
    def request(owner, method, params=None):
        token = worker_owner.set(owner)
        try:
            return pool.request_sync(method, params, timeout_s=10)
        finally:
            worker_owner.reset(token)
    try:
        with ThreadPoolExecutor(2) as executor:
            other = executor.submit(request, "other", "slow", {"seconds": 8})
            deadline = time.monotonic() + 2
            while not handles[0]._responses and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert handles[0]._responses
            crashed = executor.submit(request, "crashing", "die")
            with pytest.raises(WorkerCrashed):
                crashed.result(timeout=3)
            assert not other.done()
            assert request("recovered", "ping")["pong"]
            assert not other.done()
            pool.cancel_owner("other")
            with pytest.raises(WorkerAborted):
                other.result(timeout=3)
    finally:
        pool.close()


def test_registry_recovers_publication_and_removes_only_its_staging(tmp_path):
    from tests.fixtures.artifact_scene import publish_scene
    from tcad.store.artifacts import ArtifactStore
    registry = JobRegistry(tmp_path)
    manifest, _ = publish_scene(tmp_path)
    delivered = registry.save(BuildJob(model_id="part", ir_version=0, digest="d",
        state="verifying", attempt_id=manifest.attempt_id))
    staging = ArtifactStore(tmp_path).staging_dir("part", 1, "interrupted")
    staging.mkdir(parents=True)
    (staging / "scratch").write_text("unfinished")
    interrupted = registry.save(BuildJob(model_id="part", ir_version=1, digest="d",
        state="running", attempt_id="interrupted", artifact_dir=str(staging)))
    recovered = JobRegistry(tmp_path)
    assert recovered.get(delivered.job_id).state == "published"
    assert recovered.get(delivered.job_id).artifact_id == manifest.artifact_id
    assert recovered.get(interrupted.job_id).state == "interrupted"
    assert not staging.exists()


def test_build_job_http_queries_do_not_initialize_geometry_backend(tmp_path):
    from fastapi.testclient import TestClient
    from tcad.config.schema import Config
    from tcad.server.app import create_app
    registry = JobRegistry(tmp_path)
    job = registry.save(BuildJob(model_id="part", ir_version=0, digest="d", state="failed"))
    config = Config()
    config.storage.data_dir = str(tmp_path)
    app = create_app(config=config)
    with TestClient(app) as client:
        assert client.get("/build-jobs", params={"model_id": "part"}).json()["jobs"][0]["job_id"] == job.job_id
        assert client.get("/build-jobs/" + job.job_id).json()["state"] == "failed"
        assert client.post("/build-jobs/" + job.job_id + "/cancel").json() == {"cancelled": False}
        assert client.get("/build-jobs/missing").status_code == 404
        assert app.state.services is None
