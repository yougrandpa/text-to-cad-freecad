"""Against the real kernel: a stop kills the worker, and the worker comes back.

The in-flight half of this story (abort ends a call that is *running*, promptly)
is verified in ``tests/unit/test_worker_abort.py`` against a real child process,
because a real FreeCAD compile is fast and cannot be made reliably long enough
to catch mid-flight. What needs the real kernel is what happens around it:

  1. a stopping turn kills a real ``FreeCADCmd`` process — the OS-level process
     is gone, not merely detached from;
  2. the handle is still usable afterwards: the *next* build restarts the worker
     lazily and measures the same golden volume;
  3. the restarted worker is a working geometry backend, not a half-initialised
     one: it still turns a bad build into a structured error rather than a crash.

Nothing here is timing-based except the abort itself, and that one is asserted
by process death rather than by a stopwatch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tcad.core.worker_client import WorkerCallFailed, WorkerHandle
from tcad.worker.protocol import M_COMPILE_IR

from tests.contract.test_build_failures import plate_with_hole_ir
from tests.contract.test_sketch_planes import rect_ir

REPO_ROOT = Path(__file__).resolve().parents[2]

FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    str(REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"),
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not Path(FREECAD_CMD).exists(),
        reason="FreeCADCmd build not found (free-cad/FreeCAD/build/debug/bin/FreeCADCmd)",
    ),
]

VOL_ABS = 1e-6


@pytest.fixture(scope="module")
def worker():
    handle = WorkerHandle(
        FREECAD_CMD, REPO_ROOT, worker_id="interrupt",
        startup_timeout_s=180.0, request_timeout_s=180.0,
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.close()


def _pid(handle: WorkerHandle) -> int:
    """The OS pid of the worker this handle owns.

    Private on purpose: there is no public pid accessor because nothing in the
    product needs one, and this test is checking the OS-level fact that the
    process is gone.
    """
    assert handle._proc is not None
    return handle._proc.pid


def _compile(handle: WorkerHandle, ir: dict, out_dir: Path) -> dict:
    return handle.request_sync(
        M_COMPILE_IR, {"ir": ir, "out_dir": str(out_dir)}, timeout_s=180.0
    )


def test_a_real_build_measures_the_golden_volume(worker, tmp_path):
    res = _compile(worker, rect_ir("XY", 40, 20, 5, model_id="pre"), tmp_path / "pre")
    assert res["measurements"]["volume"] == pytest.approx(4000.0, abs=VOL_ABS)
    assert worker.is_alive()


def test_an_abort_kills_the_real_process(worker, tmp_path):
    """The stop reaches the process — checked at the OS level, not by is_alive()."""
    before_pid = _pid(worker)
    assert worker.abort_inflight("the user stopped the turn") is True
    assert worker.is_alive() is False
    with pytest.raises(ProcessLookupError):
        # Signal 0 only asks "does this process exist". No such process = stopped.
        os.kill(before_pid, 0)
    assert worker._proc is not None and worker._proc.poll() is not None


def test_the_next_build_restarts_the_worker_and_still_measures(worker, tmp_path):
    """A stop must cost the stopped build, not the rest of the session.

    The process was killed by the previous test; this call finds it dead and
    recovers, and the fresh worker has to produce the same geometry.
    """
    assert not worker.is_alive()
    res = _compile(worker, rect_ir("XY", 40, 20, 5, model_id="post"), tmp_path / "post")
    assert res["measurements"]["volume"] == pytest.approx(4000.0, abs=VOL_ABS)
    assert res["measurements"]["solids"] == 1
    assert worker.is_alive()


def test_the_restarted_worker_still_reports_a_bad_build_as_an_error(worker, tmp_path):
    """Not a wedged or half-built process: real errors still arrive as errors.

    A pocket whose circle misses the plate is a reproducible failure (see
    tests/contract/test_build_failures.py), and it must still come back as a
    structured error — a killed-and-restarted worker that answers everything
    with a timeout or a crash would be worse than no restart at all.
    """
    ir = plate_with_hole_ir("bad", cx=200.0, cy=200.0, radius=3.0)
    with pytest.raises(WorkerCallFailed) as info:
        _compile(worker, ir, tmp_path / "bad")
    err = info.value.rpc_error
    assert err.message, "a failed build must say why"
    assert err.feature_id, f"nothing points at what failed: {err}"
    assert worker.is_alive(), "a rejected build must not take the worker with it"
