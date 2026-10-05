"""Warm worker leases. Cancellation only targets the requesting owner."""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tcad.core.types import ToolErrorKind
from tcad.build.worker_client import WorkerAborted, WorkerError

worker_owner = contextvars.ContextVar("tcad_build_owner", default=None)
worker_cancellation = contextvars.ContextVar("tcad_build_cancellation", default=None)


def check_cancelled():
    signal = worker_cancellation.get()
    if signal is not None and signal.is_set():
        raise WorkerAborted("build cancelled")


class WarmWorkerPool:
    def __init__(self, handles):
        if not handles:
            raise ValueError("worker pool requires at least one handle")
        self.handles = list(handles)
        self._available = list(handles)
        self._active = {}
        self._cancelled = set()
        self._condition = threading.Condition()
        self._closed = False

    def start(self):
        with ThreadPoolExecutor(max_workers=len(self.handles)) as executor:
            futures = [executor.submit(h.start) for h in self.handles]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                self.close()
                raise

    def request_sync(self, method, params=None, *, timeout_s=60):
        check_cancelled()
        owner = worker_owner.get() or threading.get_ident()
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while not self._available and not self._closed and owner not in self._cancelled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("worker lease timed out", ToolErrorKind.TIMEOUT)
                self._condition.wait(remaining)
            if self._closed:
                raise WorkerError("worker pool is closed")
            if owner in self._cancelled:
                raise WorkerAborted("build cancelled before worker lease")
            handle = self._available.pop(0)
            self._active[handle] = owner
        try:
            # start() is idempotent; initialized workers remain warm. Failed
            # transports recover inside WorkerHandle, without replaying writes.
            handle.start()
            with self._condition:
                if self._closed:
                    raise WorkerError("worker pool closed during startup")
                if owner in self._cancelled:
                    raise WorkerAborted("build cancelled during worker startup")
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker lease/startup timed out", ToolErrorKind.TIMEOUT)
            return handle.request_sync(method, params or {}, timeout_s=remaining)
        finally:
            with self._condition:
                self._active.pop(handle, None)
                self._available.append(handle)
                self._condition.notify_all()
                closed = self._closed
            if closed:
                handle.close()

    def cancel_owner(self, owner, reason="stopped"):
        with self._condition:
            self._cancelled.add(owner)
            handles = [h for h, current in self._active.items() if current == owner]
            self._condition.notify_all()
            # Keep the lease until killing is complete; otherwise a finishing
            # call could lend this process to an unrelated owner before kill.
            return any([h.abort_inflight(reason) for h in handles])

    def release_owner(self, owner):
        with self._condition:
            self._cancelled.discard(owner)

    def abort_inflight(self, reason="stopped"):
        return self.cancel_owner(worker_owner.get() or threading.get_ident(), reason)

    def is_alive(self):
        return any(h.is_alive() for h in self.handles)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        for handle in self.handles:
            handle.abort_inflight("worker pool closing")
            handle.close()

    def stdout_noise_text(self):
        return "\n".join(h.stdout_noise_text() for h in self.handles)

    def stderr_text(self):
        return "\n".join(h.stderr_text() for h in self.handles)
