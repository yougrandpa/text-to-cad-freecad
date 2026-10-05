"""Run blocking geometry work with an isolated cancellation owner."""

import asyncio
import logging
import threading
import uuid

from tcad.build.pool import worker_owner, worker_cancellation

log = logging.getLogger(__name__)


async def run_blocking(services, fn, /, *args, label, **kwargs):
    owner = worker_owner.get()
    owner_token = worker_owner.set(owner or "operation-" + uuid.uuid4().hex)
    signal = worker_cancellation.get() or threading.Event()
    signal_token = worker_cancellation.set(signal)
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        signal.set()
        abort = getattr(getattr(services, "worker", None), "abort_inflight", None)
        if callable(abort):
            try:
                abort(f"turn stopped during {label}")
            except Exception:
                log.exception("worker abort failed during %s", label)
        # Drain the owning thread before its scratch directory is removed or
        # its cancellation tombstone is released. Repeated stops are harmless.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
    finally:
        if owner is None:
            pool = getattr(getattr(services, "worker", None), "handle", None)
            release = getattr(pool, "release_owner", None)
            if callable(release):
                release(worker_owner.get())
        worker_cancellation.reset(signal_token)
        worker_owner.reset(owner_token)
