"""Supervisor-side handle to a FreeCADCmd worker process.

This is the linchpin between the two processes. It owns:
  * spawning ``FreeCADCmd --console -P <root> tcad/worker/bootstrap.py``
  * a JSONL request/response client over the worker's stdin/stdout
  * per-request timeouts, crash detection, restart
  * ``abort_inflight``: ending a call that is already running, for a stop that
    has to reach the build rather than just the caller waiting for it

Why a background reader thread instead of asyncio streams: the worker can emit
its own banner/log noise on stdout at any moment (FreeCAD prints one on start-up),
so we need a single place that filters non-JSON lines and fans responses out by
request id. A daemon thread doing blocking reads is the simplest thing that is
actually correct here; every public entry point is async and hops to it via
``asyncio.to_thread`` so the event loop is never blocked.

Verified interface facts (review/history/02-架构设计.md 附录 B):
  * CLI options: ``-c/--console``, ``-P/--python-path``, positional script
    (src/App/Application.cpp:2429-2460, executed via processFiles() -> runFile())
  * ``tcad/worker/**`` runs on FreeCAD's interpreter: stdlib + FreeCAD only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from tcad.core.types import RpcError, RpcRequest, RpcResponse, ToolErrorKind
from tcad.worker.protocol import (
    DEFAULT_REQUEST_TIMEOUT_S,
    ERROR_KINDS,
    M_API_SELFTEST,
    M_PING,
    WORKER_METHODS,
    encode_line,
)

log = logging.getLogger("tcad.worker_client")

BOOTSTRAP_RELPATH = "tcad/worker/bootstrap.py"
DEFAULT_STARTUP_TIMEOUT_S = 120.0
"""FreeCADCmd takes tens of seconds to import FreeCAD + Part + Sketcher."""


class WorkerError(RuntimeError):
    """Raised for transport-level failures (dead process, timeout, bad framing)."""

    def __init__(self, message: str, kind: ToolErrorKind = ToolErrorKind.RUNTIME):
        super().__init__(message)
        self.kind = kind


class WorkerCrashed(WorkerError):
    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = ""):
        super().__init__(message, ToolErrorKind.RUNTIME)
        self.returncode = returncode
        self.stderr = stderr


class WorkerCallFailed(WorkerError):
    """The worker answered, but the call itself failed. Keeps the worker's own
    error classification (schema/compile/solver/...) so callers can act on it.

    An unrecognised kind is normalised to RUNTIME here rather than being rejected:
    the worker is the authority on what went wrong, but it must not be able to
    smuggle an unknown value into the caller's dispatch logic.
    """

    def __init__(self, error: RpcError):
        kind = error.kind if error.kind in ERROR_KINDS else ToolErrorKind.RUNTIME.value
        super().__init__(error.message, ToolErrorKind(kind))
        self.rpc_error = error
        self.feature_id = error.feature_id
        self.traceback = error.traceback


class WorkerAborted(WorkerError):
    """The call was ended from this side because someone stopped the work.

    Kept apart from ``WorkerCrashed`` on purpose: a crash is a fault to report,
    an abort is an instruction that was carried out. A caller that conflates them
    tells the user "the geometry backend died" every time they press stop — or,
    worse, treats a deliberate stop as a transient error worth retrying.
    """

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(message, ToolErrorKind.CANCELLED)
        self.reason = reason


def build_worker_command(
    freecad_cmd: str, repo_root: str | Path, worker_id: str = "w0"
) -> list[str]:
    """Exact argv for launching the worker.

    ⚠️ ``--pass`` is REQUIRED and its position matters. FreeCADCmd parses its own
    option set (src/App/Application.cpp:2429-2460) and rejects anything it does not
    recognise *before* running the script. Running::

        FreeCADCmd --console -P <root> <bootstrap.py> --worker-id=w0

    resulted in the script never executing at all (verified by instrumenting
    sys.argv). The documented escape hatch is the ``pass`` option —
    "Ignores the following arguments and pass them through to be used by a script"
    — so the script's own flags must come *after* ``--pass``::

        FreeCADCmd --console -P <root> <bootstrap.py> --pass --worker-id=w0

    Verified: with ``--pass`` the script runs and ``sys.argv`` contains
    ``--pass`` plus the passed-through flags.
    """
    root = str(Path(repo_root).resolve())
    return [
        freecad_cmd,
        "--console",
        "-P",
        root,
        str(Path(root) / BOOTSTRAP_RELPATH),
        "--pass",
        f"--worker-id={worker_id}",
    ]


class WorkerHandle:
    """One FreeCADCmd process, one request in flight at a time.

    Serialisation is deliberate: FreeCAD's document/global state is not
    thread-safe, so concurrent calls would be a latent corruption bug. Use a
    pool of handles for parallelism instead.
    """

    def __init__(
        self,
        freecad_cmd: str,
        repo_root: str | Path,
        *,
        worker_id: str = "w0",
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
        extra_env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        command_override: list[str] | None = None,
        restart_on_failure: bool = True,
    ) -> None:
        self.freecad_cmd = freecad_cmd
        self.repo_root = Path(repo_root).resolve()
        self.worker_id = worker_id
        self.request_timeout_s = request_timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.extra_env = extra_env or {}
        self.cwd = Path(cwd) if cwd else self.repo_root
        self.command_override = command_override
        """Test seam: when set, this argv is executed verbatim instead of building
        the FreeCADCmd command line. Lets the transport be tested without FreeCAD."""

        self.restart_on_failure = restart_on_failure
        """Whether a dead or wedged worker is replaced instead of being left there.

        Both failure modes are real and neither is recoverable in place: FreeCAD
        segfaults on some malformed input (``Sketcher.Constraint`` in particular),
        and a hung OCCT boolean cannot be interrupted — the process stays there,
        answering nothing. Without this, one bad call wedges the geometry backend
        for the life of the server and every later turn fails, which is how a
        single unbuildable part turned into "一直有报错"."""

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._responses: dict[int, queue.Queue[RpcResponse]] = {}
        self._lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._next_id = 0
        self._stdout_lines: list[str] = []  # non-JSON noise, kept for diagnostics
        self._stderr_lines: list[str] = []
        self._closed = False
        self._abort_epoch = 0
        self._abort_reason = ""

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, *, wait_ready: bool = True) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self.command_override is None and not Path(self.freecad_cmd).exists():
            raise WorkerError(
                f"FreeCADCmd not found at {self.freecad_cmd!r}. "
                "Build it first: cd free-cad/FreeCAD && pixi run configure && pixi run build",
                ToolErrorKind.RUNTIME,
            )
        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        # FreeCAD must not try to reach a display in headless mode.
        env.setdefault("QT_QPA_PLATFORM", "offscreen")

        cmd = self.command_override or build_worker_command(
            self.freecad_cmd, self.repo_root, self.worker_id
        )
        log.debug("spawning worker: %s", " ".join(cmd))
        self._proc = subprocess.Popen(  # noqa: S603 - argv is built from config, not user text
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.cwd),
            env=env,
            bufsize=0,
        )
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name=f"tcad-worker-reader-{self.worker_id}", daemon=True
        )
        self._reader.start()
        if wait_ready:
            self.wait_ready()

    def wait_ready(self) -> None:
        """Block until the worker answers ``ping``.

        FreeCADCmd can spend a long time importing its modules before it reads a
        single byte of stdin, so a plain 'is the pipe open' check would lie.
        """
        deadline = time.monotonic() + self.startup_timeout_s
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise WorkerCrashed(
                    f"worker exited during start-up (rc={self._proc.returncode if self._proc else 'n/a'})",
                    returncode=self._proc.returncode if self._proc else None,
                    stderr=self.stderr_text(),
                )
            try:
                self.request_sync(M_PING, {}, timeout_s=5.0)
                log.info("worker %s ready", self.worker_id)
                return
            except (WorkerError, queue.Empty) as exc:
                last_exc = exc
                time.sleep(0.25)
        raise WorkerError(
            f"worker {self.worker_id} not ready within {self.startup_timeout_s}s "
            f"(last error: {last_exc}); stderr tail: {self.stderr_text()[-500:]}",
            ToolErrorKind.TIMEOUT,
        )

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._closed

    def restart(self) -> None:
        self.close()
        self.start()

    def recover(self, reason: str) -> bool:
        """Replace a dead or wedged worker. Returns whether one is now usable.

        Called after a transport failure. A crashed process and a hung one need
        the same treatment — ``close()`` kills it, because a process stuck inside
        an OCCT call will not act on a closed stdin — and the only difference is
        the message.
        """
        if not self.restart_on_failure or self._closed:
            return False
        with self._restart_lock:
            # Another caller may have recovered it while we waited for the lock.
            try:
                if self._proc is not None and self._proc.poll() is None:
                    log.warning("worker %s: replacing a wedged process (%s)", self.worker_id, reason)
                else:
                    log.warning("worker %s: restarting after %s", self.worker_id, reason)
                self.restart()
                return True
            except Exception as exc:  # noqa: BLE001 — recovery must not mask the original error
                log.error("worker %s: could not restart (%s)", self.worker_id, exc)
                return False

    def close(self, *, timeout_s: float = 5.0) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()  # EOF ends the worker's read loop
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                log.warning("worker %s refused to die", self.worker_id)
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)

    def abort_inflight(self, reason: str = "stopped") -> bool:
        """End the call that is in flight right now, by killing the process.

        This is the difference between stopping the *waiting* and stopping the
        *work*. A caller blocked in ``_request_raw`` cannot be interrupted from
        outside — and the thread inside it is usually parked in an OCCT operation
        where nothing short of process death ends the call, which is exactly why
        the worker has to go. Killing it closes stdout, the reader thread sees
        EOF and fails every pending box, so the blocked caller wakes immediately
        with :class:`WorkerAborted` instead of sitting out its timeout.

        Deliberately not ``close()``: the handle stays open and usable, and the
        next call restarts the worker lazily (``_request_raw`` recovers a dead
        process). So a stop costs the rest of the cancelled call, nothing more.
        Returns whether a running process was actually killed.

        Safe from any thread; a no-op when nothing is running or after close().
        """
        with self._lock:
            # Bumped even when there is nothing to kill: a call that is one
            # instruction away from writing its request must be able to tell
            # that the answer it then waits for belongs to an aborted world.
            self._abort_epoch += 1
            self._abort_reason = reason
        proc = self._proc
        if proc is None or proc.poll() is not None or self._closed:
            return False
        log.warning("worker %s: aborting the in-flight call (%s)", self.worker_id, reason)
        try:
            proc.kill()
        except OSError as exc:  # pragma: no cover - the process died underneath us
            log.debug("worker %s: kill failed: %s", self.worker_id, exc)
            return False
        try:
            # Reap it here, so "the build is stopped" is true when this returns —
            # not merely "a signal was sent" — and the caller (or the next build)
            # cannot leave a zombie behind.
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
            log.warning("worker %s survived SIGKILL", self.worker_id)
        # Do not wait for the reader thread to notice the pipe closing: wake the
        # caller now, so a stop is bounded by the kill and not by EOF timing.
        self._fail_all_pending(f"worker call aborted: {reason}")
        return True

    def __enter__(self) -> WorkerHandle:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── transport ─────────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()
        buf = b""
        try:
            while True:
                chunk = proc.stdout.readline()
                if not chunk:
                    break
                buf += chunk
                if not buf.endswith(b"\n"):
                    continue  # partial line; keep accumulating
                line, buf = buf.decode("utf-8", errors="replace").strip(), b""
                if not line:
                    continue
                if not line.startswith("{"):
                    # FreeCAD banner / console noise — keep for diagnostics, skip.
                    self._stdout_lines.append(line)
                    if len(self._stdout_lines) > 2000:
                        del self._stdout_lines[:1000]
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self._stdout_lines.append(line)
                    continue
                self._dispatch(payload)
        except (OSError, ValueError) as exc:  # pragma: no cover - pipe teardown races
            log.debug("worker %s reader stopped: %s", self.worker_id, exc)
        finally:
            self._fail_all_pending("worker stdout closed")

    def _dispatch(self, payload: dict[str, Any]) -> None:
        try:
            resp = RpcResponse.model_validate(payload)
        except Exception:  # noqa: BLE001 - malformed frame must not kill the reader
            log.warning("worker %s sent an unparseable frame: %r", self.worker_id, payload)
            return
        with self._lock:
            box = self._responses.get(resp.id)
        if box is None:
            log.debug("worker %s answered unknown request id %s", self.worker_id, resp.id)
            return
        try:
            # Never block the reader thread on a caller that already walked away
            # (timed out, or was aborted a microsecond before this frame landed).
            # A blocked reader would stop delivering *every* later response, which
            # turns one lost frame into a permanently wedged worker.
            box.put_nowait(resp)
        except queue.Full:  # pragma: no cover - caller gone in a race window
            log.debug("worker %s: no one waiting for id %s", self.worker_id, resp.id)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 2000:
                        del self._stderr_lines[:1000]
        except (OSError, ValueError):  # pragma: no cover
            pass

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            boxes = list(self._responses.values())
        for box in boxes:
            try:
                box.put_nowait(
                    RpcResponse(
                        id=-1,
                        ok=False,
                        error=RpcError(kind=ToolErrorKind.RUNTIME, message=reason),
                    )
                )
            except queue.Full:  # pragma: no cover
                pass

    def _write(self, req: RpcRequest) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise WorkerCrashed(
                f"worker {self.worker_id} is not running",
                returncode=proc.returncode if proc else None,
                stderr=self.stderr_text(),
            )
        try:
            proc.stdin.write(encode_line(req.model_dump()).encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerCrashed(
                f"worker {self.worker_id} pipe broke: {exc}",
                returncode=proc.returncode,
                stderr=self.stderr_text(),
            ) from exc

    # ── public API ────────────────────────────────────────────────────────

    def request_sync(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Blocking call. Safe from any thread; use ``await request()`` in async code.

        Validates the method name against the documented worker surface before
        anything touches the wire — a typo should fail here, not as a confusing
        worker-side error envelope.
        """
        if method not in WORKER_METHODS:
            raise WorkerError(
                f"unknown worker method {method!r}; known: {', '.join(WORKER_METHODS)}",
                ToolErrorKind.NOT_FOUND,
            )
        return self._request_raw(method, params, timeout_s=timeout_s)

    def _request_raw(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Transport without the method-name check. Public entry points go through
        ``request_sync``; this exists so the transport itself (framing, timeouts,
        crash detection, error envelopes) can be exercised directly."""
        if self._proc is None:
            raise WorkerCrashed(f"worker {self.worker_id} was never started")

        # A worker that died on the *previous* call is replaced here, so a crash
        # costs one failed call rather than every subsequent one.
        if self._proc.poll() is not None and not self._closed:
            self.recover(f"worker exited with rc={self._proc.returncode}")
            if self._proc is None or self._proc.poll() is not None:
                raise WorkerCrashed(
                    f"worker {self.worker_id} is not running",
                    returncode=None,
                    stderr=self.stderr_text(),
                )

        timeout = timeout_s if timeout_s is not None else self.request_timeout_s
        box: queue.Queue[RpcResponse] = queue.Queue(maxsize=1)
        with self._lock:
            self._next_id += 1
            req_id = self._next_id + 100_000 * (hash(self.worker_id) % 97)
            self._responses[req_id] = box
            # Sampled under the same lock as the abort bump, so "an abort landed
            # while this call was in flight" is decided, not guessed.
            abort_epoch = self._abort_epoch
        try:
            self._write(RpcRequest(id=req_id, method=method, params=params or {}))
            try:
                resp = box.get(timeout=timeout)
            except queue.Empty as exc:
                # A call that did not answer in time is treated as unrecoverable:
                # the process may be spinning inside OCCT, where no amount of
                # waiting helps and a later request would queue behind it forever.
                restarted = self.recover(f"call {method!r} timed out after {timeout}s")
                raise WorkerError(
                    f"worker call {method!r} timed out after {timeout}s"
                    + ("; the worker was killed and restarted" if restarted
                       else "; the worker may be wedged (restart disabled)"),
                    ToolErrorKind.TIMEOUT,
                ) from exc
        finally:
            with self._lock:
                self._responses.pop(req_id, None)

        if resp.id == -1:  # synthetic frame from _fail_all_pending
            message = resp.error.message if resp.error else "worker died"
            if abort_epoch != self._abort_epoch:
                # Someone stopped this call: the worker was killed on purpose, so
                # this is not a crash to recover from. Restarting here would make
                # a stop cost a worker start-up inside the stop itself; the next
                # call recovers lazily instead.
                raise WorkerAborted(
                    f"worker call {method!r} was aborted ({self._abort_reason or 'stopped'})",
                    reason=self._abort_reason,
                )
            returncode = self._proc.returncode if self._proc else None
            stderr = self.stderr_text()
            restarted = self.recover(f"worker crashed (rc={returncode})")
            raise WorkerCrashed(
                message + ("; the worker was restarted" if restarted else ""),
                returncode=returncode,
                stderr=stderr,
            )
        if not resp.ok:
            err = resp.error or RpcError(
                kind=ToolErrorKind.RUNTIME, message=f"{method} failed with no error detail"
            )
            if err.kind not in ERROR_KINDS:
                err = err.model_copy(update={"kind": ToolErrorKind.RUNTIME})
            raise WorkerCallFailed(err)
        return resp.result or {}

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Async wrapper — hops to a thread so the event loop stays responsive."""
        return await asyncio.to_thread(self.request_sync, method, params, timeout_s=timeout_s)

    # ── diagnostics ───────────────────────────────────────────────────────

    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def stdout_noise_text(self) -> str:
        return "\n".join(self._stdout_lines)

    async def api_selftest(self) -> dict[str, Any]:
        """Ask the worker to re-verify every FreeCAD API this project depends on.

        FreeCAD 26.3.0dev is a moving target (design §12-11); running this at
        start-up turns a mysterious mid-build failure into a clear one.
        """
        return await self.request(M_API_SELFTEST, {})


class WorkerPool:
    """A small pool of handles. Turns still use one worker each; the pool exists so
    fork_join can build N candidates in parallel without sharing FreeCAD state."""

    def __init__(
        self,
        freecad_cmd: str,
        repo_root: str | Path,
        *,
        size: int = 1,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
    ) -> None:
        self._handles = [
            WorkerHandle(
                freecad_cmd,
                repo_root,
                worker_id=f"w{i}",
                request_timeout_s=request_timeout_s,
                startup_timeout_s=startup_timeout_s,
            )
            for i in range(max(1, size))
        ]
        self._free: asyncio.Queue[WorkerHandle] | None = None

    def start(self) -> None:
        for h in self._handles:
            h.start()

    def close(self) -> None:
        for h in self._handles:
            h.close()

    async def acquire(self) -> WorkerHandle:
        if self._free is None:
            self._free = asyncio.Queue()
            for h in self._handles:
                self._free.put_nowait(h)
        return await self._free.get()

    async def release(self, handle: WorkerHandle) -> None:
        if self._free is not None:
            await self._free.put(handle)

    async def __aenter__(self) -> WorkerPool:
        await asyncio.to_thread(self.start)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self.close)
