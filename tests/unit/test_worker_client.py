"""Transport tests for the supervisor-side worker handle.

These use a stand-in process (tests/fixtures/fake_freecad_cmd.py) rather than a
real FreeCADCmd, so they run fast and in CI. The real end-to-end check lives in
tests/contract/test_worker_smoke.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from tcad.core.types import ToolErrorKind
from tcad.core.worker_client import (
    WorkerCallFailed,
    WorkerCrashed,
    WorkerError,
    WorkerHandle,
    build_worker_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_freecad_cmd.py"


def make_handle(**kwargs) -> WorkerHandle:
    return WorkerHandle(
        freecad_cmd=str(FAKE),
        repo_root=REPO_ROOT,
        worker_id="test",
        startup_timeout_s=15.0,
        request_timeout_s=10.0,
        command_override=[sys.executable, str(FAKE), "--worker-id=test"],
        **kwargs,
    )


@pytest.fixture()
def handle():
    h = make_handle()
    h.start()
    try:
        yield h
    finally:
        h.close()


# ── argv construction ─────────────────────────────────────────────────────


def test_build_worker_command_matches_documented_form():
    cmd = build_worker_command("/path/to/FreeCADCmd", REPO_ROOT, "w3")
    assert cmd[0] == "/path/to/FreeCADCmd"
    assert "--console" in cmd
    assert "-P" in cmd
    assert cmd[cmd.index("-P") + 1] == str(REPO_ROOT)
    assert cmd[-3].endswith("tcad/worker/bootstrap.py")
    # `--pass` MUST sit between the script path and the script's own flags.
    # Without it FreeCADCmd rejects unknown options before running the script at
    # all, and the worker silently never starts.
    assert cmd[-2] == "--pass"
    assert cmd[-1] == "--worker-id=w3"
    assert cmd.index("--pass") > cmd.index(cmd[-3])


def test_worker_actually_receives_its_flags_through_freecadcmd(tmp_path):
    """The `--pass` contract, proven against the real FreeCADCmd binary.

    A regression here means the worker never starts, which is exactly the kind of
    failure that is painful to diagnose from the supervisor side.
    """
    freecad = REPO_ROOT / "free-cad" / "FreeCAD" / "build" / "debug" / "bin" / "FreeCADCmd"
    if not freecad.exists():
        pytest.skip("FreeCADCmd not built")

    import subprocess

    probe = tmp_path / "argv_probe.py"
    probe.write_text(
        "import json,sys\n"
        "sys.stderr.write('###ARGV###'+json.dumps(sys.argv)+'\\n')\n",
        encoding="utf-8",
    )
    # Build argv with the same --pass placement the real launcher uses.
    cmd = build_worker_command(str(freecad), REPO_ROOT, "w9")
    cmd[cmd.index(cmd[-3])] = str(probe)  # swap bootstrap.py for the probe script
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT)
    )
    out = proc.stderr + proc.stdout
    assert "###ARGV###" in out, (
        "script never ran — `--pass` placement is broken.\n"
        f"stdout={proc.stdout[:400]}\nstderr={proc.stderr[:400]}"
    )
    argv = json.loads(out.split("###ARGV###", 1)[1].splitlines()[0])
    assert "--worker-id=w9" in argv, f"flag was not passed through: {argv}"


def test_missing_freecad_binary_is_a_clear_error(tmp_path):
    h = WorkerHandle("/definitely/not/here/FreeCADCmd", tmp_path, startup_timeout_s=1.0)
    with pytest.raises(WorkerError) as ei:
        h.start(wait_ready=False)
    assert "pixi run configure" in str(ei.value)  # tells the operator what to do


# ── happy path ────────────────────────────────────────────────────────────


def test_ping_round_trip(handle):
    assert handle.request_sync("ping")["pong"] is True


def test_async_request_works(handle):
    result = asyncio.run(handle.request("ping"))
    assert result["worker_id"] == "test"


def test_start_is_idempotent(handle):
    first = handle._proc
    handle.start()
    assert handle._proc is first


def test_is_alive_and_ready(handle):
    assert handle.is_alive() is True


# ── noise tolerance ───────────────────────────────────────────────────────


def test_banner_on_stdout_is_ignored_not_fatal(handle):
    # The fake prints 3 banner lines before any protocol traffic; if the reader
    # mishandled them, ping above would already have failed. Assert we captured
    # them for diagnostics rather than crashing.
    assert "FreeCAD 26.3.0" in handle.stdout_noise_text()


def test_non_json_interleaved_frame_does_not_break_the_stream(handle):
    assert handle._request_raw("nonsense_frame")["survived"] is True
    # and the transport still works afterwards
    assert handle.request_sync("ping")["pong"] is True


def test_stderr_is_captured(handle):
    handle.request_sync("ping")
    assert "fake worker" in handle.stderr_text()


# ── error envelopes ───────────────────────────────────────────────────────


def test_worker_side_failure_keeps_its_classification(handle):
    with pytest.raises(WorkerCallFailed) as ei:
        handle._request_raw(
            "fail", {"kind": "solver", "message": "conflicting", "feature_id": "pad_1"}
        )
    assert ei.value.kind == ToolErrorKind.SOLVER
    assert ei.value.feature_id == "pad_1"
    assert "conflicting" in str(ei.value)


def test_unknown_method_is_rejected_before_hitting_the_wire(handle):
    """The whitelist is the production entry point's job — a typo must fail here,
    not as a confusing worker-side error."""
    with pytest.raises(WorkerError) as ei:
        handle.request_sync("not_a_worker_method")
    assert ei.value.kind == ToolErrorKind.NOT_FOUND
    assert "not_a_worker_method" in str(ei.value)


def test_worker_reported_not_found_survives_as_error_envelope(handle):
    with pytest.raises(WorkerCallFailed) as ei:
        handle._request_raw("fail", {"kind": "not_found", "message": "unknown method 'zzz'"})
    assert ei.value.kind == ToolErrorKind.NOT_FOUND


def test_bogus_error_kind_is_coerced_to_runtime(handle):
    """A worker that invents an error kind must not corrupt the caller's dispatch."""
    with pytest.raises(WorkerCallFailed) as ei:
        handle._request_raw("fail", {"kind": "totally-made-up", "message": "x"})
    assert ei.value.kind == ToolErrorKind.RUNTIME


# ── timeout / crash ───────────────────────────────────────────────────────


def test_request_timeout_is_reported_as_timeout(handle):
    with pytest.raises(WorkerError) as ei:
        handle._request_raw("slow", {"seconds": 3}, timeout_s=0.5)
    assert ei.value.kind == ToolErrorKind.TIMEOUT


def test_worker_crash_is_detected(handle):
    with pytest.raises(WorkerCrashed):
        handle._request_raw("die")


def test_a_crashed_worker_is_replaced_so_the_next_call_works(handle):
    """A native crash must cost one call, not the rest of the session.

    FreeCAD segfaults on some malformed input and there is no way to catch that
    from Python. Without replacement, every later request would go to a dead
    process — which is how one unbuildable part turned a whole session into
    "一直有报错".
    """
    with pytest.raises(WorkerCrashed):
        handle._request_raw("die")

    assert handle.is_alive() is True, "崩溃后没有换一个新 worker"
    assert handle.request_sync("ping")["pong"] is True


def test_a_wedged_worker_is_replaced_on_timeout(handle):
    """A timeout is treated as unrecoverable, because it usually is.

    A process spinning inside an OCCT call answers nothing and cannot be
    interrupted, so leaving it in place would make every later call time out too.
    """
    with pytest.raises(WorkerError) as ei:
        handle._request_raw("slow", {"seconds": 30}, timeout_s=0.4)
    assert ei.value.kind == ToolErrorKind.TIMEOUT
    assert "restart" in str(ei.value).lower(), "超时没有说明 worker 已被替换"

    assert handle.request_sync("ping")["pong"] is True, "替换后的 worker 仍不可用"


def test_automatic_replacement_can_be_turned_off():
    """`worker_restart_on_crash` is a real setting, not decoration."""
    h = make_handle(restart_on_failure=False)
    h.start()
    try:
        with pytest.raises(WorkerCrashed):
            h._request_raw("die")
        # give the process a moment to be reaped
        for _ in range(50):
            if not h.is_alive():
                break
            import time

            time.sleep(0.02)
        assert h.is_alive() is False
        with pytest.raises(WorkerCrashed):
            h.request_sync("ping")
    finally:
        h.close()


def test_restart_recovers_after_crash():
    h = make_handle()
    h.start()
    try:
        with pytest.raises(WorkerCrashed):
            h._request_raw("die")
        h.restart()
        assert h.is_alive() is True
        assert h.request_sync("ping")["pong"] is True
    finally:
        h.close()


def test_close_is_idempotent():
    h = make_handle()
    h.start()
    h.close()
    h.close()
    assert h.is_alive() is False


def test_request_before_start_raises():
    h = make_handle()
    with pytest.raises(WorkerCrashed):
        h.request_sync("ping")


def test_context_manager_closes():
    with make_handle() as h:
        assert h.request_sync("ping")["pong"] is True
    assert h.is_alive() is False


# ── pool ──────────────────────────────────────────────────────────────────


def test_pool_hands_out_and_reclaims_handles():
    from tcad.core.worker_client import WorkerPool

    pool = WorkerPool(str(FAKE), REPO_ROOT, size=2)
    pool._handles = [
        WorkerHandle(
            str(FAKE), REPO_ROOT, worker_id=f"p{i}",
            startup_timeout_s=15.0,
            command_override=[sys.executable, str(FAKE), f"--worker-id=p{i}"],
        )
        for i in range(2)
    ]

    async def scenario():
        pool.start()
        try:
            a = await pool.acquire()
            b = await pool.acquire()
            assert {a.worker_id, b.worker_id} == {"p0", "p1"}
            assert (await a.request("ping"))["pong"] is True
            await pool.release(a)
            c = await pool.acquire()
            assert c.worker_id == a.worker_id  # reclaimed
        finally:
            pool.close()

    asyncio.run(scenario())
