"""The commit pipeline must not run the FreeCAD worker on the event loop.

``run_commit`` is awaited by the engine, but the worker handle it is wired with
is synchronous (``tcad.tools.base.Worker.request``). A compile that takes a
minute used to take the whole process with it: every other session stopped
streaming, health checks timed out, and a cancelled turn had no cancellation
point until FreeCAD answered. These tests watch a heartbeat coroutine while a
slow fake worker is busy.

The last two tests cover the other half of that story: off the loop is not the
same as interruptible. Cancelling the turn must end the *build* — the thread
cannot be pulled out of a blocking read, so the worker call has to be aborted —
and the attempt's staging directory must not be left on disk.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcad.core.types import (
    GateReport,
    GeometryDigest,
    HookDecision,
    HookResult,
)
from tcad.loop.commit import run_commit
from tests.fixtures.gate_fixtures import make_digest, make_ir

COMPILE_SLOWNESS = 0.4
GATE_SLOWNESS = 0.2
TICK_S = 0.01


class _SlowWorker:
    """A worker whose RPC blocks its calling thread, like the real one."""

    def __init__(self, digest: GeometryDigest):
        self._digest = digest
        self.calls: list[str] = []

    def request(self, method, params=None, *, timeout_s=30.0):
        self.calls.append(method)
        time.sleep(COMPILE_SLOWNESS if method == "compile_ir" else 0.01)
        if method == "compile_ir":
            return {"ok": True, "result": {"ok": True}}
        if method == "introspect_document":
            return {"ok": True, "result": self._digest.model_dump()}
        return {"ok": True, "result": {}}


class _Hooks:
    def dispatch(self, event, payload):
        return HookResult(decision=HookDecision.ALLOW, hook_name="test")


class _Store:
    def __init__(self, ir):
        self._ir = ir
        self.digests = []

    def load(self, model_id, version=None):
        return self._ir

    def validate_document(self, ir):
        return []

    def persist_digest(self, model_id, ir_version, digest):
        self.digests.append(digest)


class _Gate:
    def __init__(self, *, slow=False):
        self._slow = slow

    def evaluate(self, model_id, ir_version):
        if self._slow:
            time.sleep(GATE_SLOWNESS)
        return GateReport(model_id=model_id, ir_version=ir_version, passed=True)


def _services(**kw):
    ir = make_ir()
    return SimpleNamespace(
        store=_Store(ir),
        hooks=_Hooks(),
        worker=_SlowWorker(make_digest()),
        gate=_Gate(slow=kw.get("slow_gate", False)),
        config=None,
    ), ir


async def _heartbeat(counter):
    while True:
        await asyncio.sleep(TICK_S)
        counter["ticks"] += 1


async def _run_with_heartbeat(services, tmp_path):
    counter = {"ticks": 0}
    hb = asyncio.create_task(_heartbeat(counter))
    try:
        await run_commit(
            services, model_id="m1", ir_version=1, message="build",
            workdir=str(tmp_path), data_dir=str(tmp_path),
        )
    finally:
        hb.cancel()
    return counter["ticks"]


def test_worker_rpc_leaves_the_event_loop_free(tmp_path):
    """~60 ticks worth of headroom: a blocked loop can only manage ~1."""
    services, _ = _services()
    ticks = asyncio.run(_run_with_heartbeat(services, tmp_path))
    assert services.worker.calls, "the fake worker was never called"
    assert ticks >= 15, (
        f"the event loop only ticked {ticks} times during a "
        f"{COMPILE_SLOWNESS}s compile — the worker RPC ran on the loop"
    )


def test_gate_evaluation_leaves_the_event_loop_free(tmp_path):
    """round_trip re-imports the STEP through the same synchronous handle."""
    services, _ = _services(slow_gate=True)
    total = COMPILE_SLOWNESS + GATE_SLOWNESS
    started = time.monotonic()
    ticks = asyncio.run(_run_with_heartbeat(services, tmp_path))
    elapsed = time.monotonic() - started
    assert elapsed >= total
    # A loop blocked for `total` would tick ~elapsed/... i.e. once. Require at
    # least the number of ticks that fit in the slowest single step.
    assert ticks >= COMPILE_SLOWNESS / TICK_S / 2, (
        f"only {ticks} ticks across {elapsed:.2f}s — something ran on the loop"
    )


def test_commit_stamps_the_directory_before_it_builds(tmp_path):
    """The stamp must exist even when the build later fails."""
    services, _ = _services()
    services.worker.request = lambda method, params=None, *, timeout_s=30.0: {
        "ok": False, "error": {"kind": "compile", "message": "boom"},
    }
    result, report = asyncio.run(
        run_commit(
            services, model_id="m1", ir_version=1, message="build",
            workdir=str(tmp_path), data_dir=str(tmp_path),
        )
    )
    assert report is None and result.ok is False
    stamp_path = tmp_path / "artifacts" / "m1" / "v1" / "build_stamp.json"
    assert stamp_path.is_file(), (
        "a failed attempt must still say it was there — otherwise the next "
        "attempt cannot tell its own files from these"
    )


# ══════════════════════════════════════════════════════════════════════════
# interruption: a stop must reach the build, and leave nothing behind
# ══════════════════════════════════════════════════════════════════════════

#: How long the fake compile would block if it were never aborted. A no-op stop
#: makes this test take this long and then fail, which is the honest signal.
BUILD_SLOWNESS = 10.0


class _InterruptibleWorker:
    """A worker whose call ends when it is aborted, like the real one.

    The real mechanism is a killed process; here the same shape is produced in
    process (abort releases the blocked call), so the pipeline's behaviour around
    the abort is observable without FreeCAD.
    """

    def __init__(self, digest: GeometryDigest) -> None:
        self._digest = digest
        self.released = threading.Event()
        self.aborted: list[str] = []
        self.calls: list[str] = []

    def request(self, method, params=None, *, timeout_s=30.0):
        self.calls.append(method)
        if method == "compile_ir":
            self.released.wait(timeout=BUILD_SLOWNESS)
            return {"ok": True, "result": {"ok": True}}
        if method == "introspect_document":
            return {"ok": True, "result": self._digest.model_dump()}
        return {"ok": True, "result": {}}

    def abort_inflight(self, reason: str = "stopped") -> bool:
        self.aborted.append(reason)
        self.released.set()
        return True


class _StagingStore(_Store):
    """A store with a private per-attempt directory, like the real one."""

    def __init__(self, ir, root: Path) -> None:
        super().__init__(ir)
        self._root = root
        self.discarded: list[str] = []

    def staging_dir(self, model_id, version, attempt_id):
        d = self._root / f"v{version}.staging-{attempt_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def discard_staging(self, staging_dir):
        self.discarded.append(str(staging_dir))
        shutil.rmtree(staging_dir, ignore_errors=True)


def _interruptible_services(tmp_path):
    ir = make_ir()
    store = _StagingStore(ir, tmp_path / "artifacts" / "m1")
    services = SimpleNamespace(
        store=store,
        hooks=_Hooks(),
        worker=_InterruptibleWorker(make_digest()),
        gate=_Gate(),
        config=None,
    )
    return services, store


def test_cancelling_a_turn_aborts_the_running_build(tmp_path):
    """The cancellation stops the work, not only the wait for it."""
    services, _ = _interruptible_services(tmp_path)

    async def scenario() -> float:
        task = asyncio.create_task(
            run_commit(
                services, model_id="m1", ir_version=1, message="build",
                workdir=str(tmp_path), data_dir=str(tmp_path),
            )
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "the build finished before it could be cancelled"
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())

    assert services.worker.aborted, (
        "the turn was cancelled but the running build was left to finish — "
        "the worker call was never aborted"
    )
    assert "compile_ir" in services.worker.aborted[0], services.worker.aborted
    assert elapsed < 2.0, (
        f"the cancellation took {elapsed:.1f}s to take effect out of a "
        f"{BUILD_SLOWNESS}s build"
    )


def test_a_cancelled_build_leaves_no_staging_directory(tmp_path):
    """Staging is private to an attempt; an abandoned one is residue.

    Only a full pass publishes it, so nothing reads it again — it would just
    accumulate, one directory per stopped build, next to the version directory.
    """
    services, store = _interruptible_services(tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(
            run_commit(
                services, model_id="m1", ir_version=1, message="build",
                workdir=str(tmp_path), data_dir=str(tmp_path),
            )
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert store.discarded, "the staging directory of the cancelled attempt was kept"
    assert not Path(store.discarded[0]).exists()
    assert not list((tmp_path / "artifacts" / "m1").glob("*staging*")), (
        "a cancelled attempt left its scratch directory on disk"
    )
    # ...and the version directory must not have been created by a build that
    # never passed.
    assert not (tmp_path / "artifacts" / "m1" / "v1").exists()
