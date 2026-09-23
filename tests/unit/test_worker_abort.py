"""Stopping a turn must end the *build*, not just the waiting for it.

A worker call in flight is a thread parked on ``box.get`` while the child process
is inside FreeCAD/OCCT, where nothing can interrupt it from the inside — killing
the process is the only thing that ends the call. Before ``abort_inflight``, a
user stop (and the cancellation the server sends with it) only stopped the
*waiting*: the compile kept running to its own timeout, holding the worker, and
the next turn queued behind it.

These tests drive a real subprocess (``tests/fixtures/fake_freecad_cmd.py``), so
the kill, the closed pipe and the reader thread's EOF are the real ones — only
FreeCAD itself is a stand-in. The real-kernel counterpart lives in
``tests/contract/test_worker_interrupt.py``.

``_request_raw`` is used for the fake's ``slow``/``die`` verbs: they are
transport probes, not worker methods, so they are deliberately not in
``WORKER_METHODS`` (see tests/unit/test_worker_client.py).

Time budget: the calls here would need 30 s to finish on their own; each test
asserts it finished within a few seconds, which is the whole point.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from tcad.core.types import RpcError, ToolErrorKind
from tcad.core.worker_client import WorkerAborted, WorkerCrashed, WorkerHandle
from tcad.core.wiring import SyncWorkerClient

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_freecad_cmd.py"

#: What the fake worker would sleep for if nothing stopped it.
SLEEP_S = 30.0
#: Well below SLEEP_S and the request timeout, so a pass cannot be the sleep
#: finishing or the timeout arriving — it has to be the abort.
QUICK_S = 5.0


def make_handle(**kwargs) -> WorkerHandle:
    return WorkerHandle(
        freecad_cmd=str(FAKE),
        repo_root=REPO_ROOT,
        worker_id="abort",
        startup_timeout_s=15.0,
        request_timeout_s=SLEEP_S + 10.0,
        command_override=[sys.executable, str(FAKE), "--worker-id=abort"],
        **kwargs,
    )


def _call_in_thread(handle: WorkerHandle, method: str, params: dict):
    """Start a call in a thread; return (thread, result-holder)."""
    box: list = []

    def run() -> None:
        try:
            box.append(handle._request_raw(method, params, timeout_s=SLEEP_S))
        except Exception as exc:  # noqa: BLE001 — the exception is the result here
            box.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


@pytest.fixture()
def handle():
    h = make_handle()
    h.start()
    try:
        yield h
    finally:
        h.close()


def test_abort_ends_the_in_flight_call_before_its_timeout(handle):
    thread, box = _call_in_thread(handle, "slow", {"seconds": SLEEP_S})
    time.sleep(0.3)  # let the request reach the worker
    started = time.monotonic()
    assert handle.abort_inflight("user pressed stop") is True
    thread.join(timeout=QUICK_S)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), (
        "the call was still running after an abort; the stop did not reach it"
    )
    assert elapsed < QUICK_S, f"the abort took {elapsed:.1f}s"
    assert box, "the call never returned"
    exc = box[0]
    assert isinstance(exc, WorkerAborted), f"got {exc!r} instead of a cancellation"
    assert exc.kind is ToolErrorKind.CANCELLED
    assert "abort" in str(exc).lower()
    assert not handle.is_alive(), "the worker process survived the abort"


def test_the_aborted_handle_restarts_lazily_and_the_next_call_works(handle):
    """A stop must not leave the geometry backend dead for the rest of the day."""
    thread, _ = _call_in_thread(handle, "slow", {"seconds": SLEEP_S})
    time.sleep(0.3)
    handle.abort_inflight("stopped")
    thread.join(timeout=QUICK_S)
    assert not handle.is_alive()

    result = handle.request_sync("ping", {}, timeout_s=30.0)
    assert result.get("pong") is True, result
    assert handle.is_alive()


def test_a_crash_after_an_abort_is_still_reported_as_a_crash(handle):
    """The abort must not permanently relabel every later failure as a stop."""
    thread, _ = _call_in_thread(handle, "slow", {"seconds": SLEEP_S})
    time.sleep(0.3)
    handle.abort_inflight("stopped")
    thread.join(timeout=QUICK_S)

    with pytest.raises(WorkerCrashed) as info:
        handle._request_raw("die", {}, timeout_s=30.0)
    assert not isinstance(info.value, WorkerAborted)
    assert info.value.kind is ToolErrorKind.RUNTIME


def test_aborting_nothing_is_a_no_op():
    never_started = make_handle()
    assert never_started.abort_inflight("stopped") is False

    closed = make_handle()
    closed.start()
    closed.close()
    assert closed.abort_inflight("stopped") is False


class _AbortedHandle:
    """A handle whose call is ended from outside — all the shim needs to see."""

    def __init__(self) -> None:
        self.aborted: str | None = None

    def request_sync(self, method, params=None, *, timeout_s=None):
        raise WorkerAborted(
            f"worker call {method!r} was aborted (the user stopped the turn)",
            reason="the user stopped the turn",
        )

    def abort_inflight(self, reason: str = "stopped") -> bool:
        self.aborted = reason
        return True

    def is_alive(self) -> bool:
        return False

    def close(self) -> None:
        pass


def test_the_cancellation_reaches_the_tool_layer_as_its_own_kind():
    """Tools read ``{"ok": False, "error": {"kind": ...}}``. A stop must be
    distinguishable there from a crash or a timeout — otherwise a deliberate
    stop is retried as a transient failure, or reported as a broken backend."""
    client = SyncWorkerClient(_AbortedHandle())
    envelope = client.request("compile_ir", {}, timeout_s=SLEEP_S)

    assert envelope["ok"] is False, envelope
    assert envelope["error"]["kind"] == ToolErrorKind.CANCELLED.value, envelope
    assert ToolErrorKind.CANCELLED.value not in {
        ToolErrorKind.RUNTIME.value,
        ToolErrorKind.TIMEOUT.value,
        ToolErrorKind.DENIED.value,
    }
    # This change did not touch the wire contract: ``kind`` stays a plain string
    # with the same default, and the fake worker's unknown-kind error keeps its
    # own classification.
    assert RpcError().kind == ToolErrorKind.RUNTIME.value
    assert ToolErrorKind.CANCELLED.value in {k.value for k in ToolErrorKind}
