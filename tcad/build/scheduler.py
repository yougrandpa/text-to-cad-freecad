"""Coalesce duplicate submissions and supersede older model builds."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading

from tcad.build.jobs import BuildJob, JobRegistry
from tcad.build.pool import worker_owner, worker_cancellation, check_cancelled
from tcad.build.worker_client import WorkerAborted

current_job = contextvars.ContextVar("tcad_build_job", default=None)
build_observer = contextvars.ContextVar("tcad_build_observer", default=None)
log = logging.getLogger(__name__)
TERMINAL_STATES = {"published", "failed", "cancelled", "interrupted"}


class BuildScheduler:
    def __init__(self, data_dir, pool, *, debounce_s=0.05, timeout_s=600):
        self.registry = JobRegistry(data_dir)
        self.pool = pool
        self._lock = threading.RLock()
        self._active = {}
        self.debounce_s = debounce_s
        self.timeout_s = timeout_s

    def progress(self, phase, **changes):
        job = current_job.get()
        if job is not None:
            with self._lock:
                if job.state in TERMINAL_STATES and not (job.state == "published" and phase == "published"):
                    return
                self.registry.update(job, phase=phase, **changes)
            self._notify(job)

    def _notify(self, job):
        observer = build_observer.get()
        if observer is not None:
            try:
                observer(job.model_dump(mode="json"))
            except Exception:
                log.exception("build observer failed")

    def publish(self, publisher):
        """Linearize cancellation with the final synchronous publication.

        Once publication begins a stop cannot claim to have prevented it. A
        stop or superseding job accepted earlier prevents the version swap.
        """
        job = current_job.get()
        with self._lock:
            check_cancelled()
            if job is not None:
                active = self._active.get(job.model_id)
                if active is None or active["job"] is not job or job.state == "cancelled":
                    raise WorkerAborted("build superseded before publication")
            published = publisher()
            if published and job is not None:
                self.registry.update(job, state="published", phase="published",
                                     artifact_id=published if isinstance(published, str) else job.artifact_id)
        return published

    def cancel(self, job_id, reason="stopped"):
        with self._lock:
            active = next((a for a in self._active.values() if a["job"].job_id == job_id), None)
            if active is None or active["job"].state in TERMINAL_STATES:
                return False
            active["cancelled"].set()
            self.registry.update(active["job"], state="cancelled", phase="cancel_requested")
            self.pool.cancel_owner(job_id, reason)
            active["task"].get_loop().call_soon_threadsafe(active["task"].cancel)
            return True

    async def run(self, model_id, version, digest, factory):
        loop = asyncio.get_running_loop()
        with self._lock:
            prior = self._active.get(model_id)
            if (prior and prior["job"].state not in TERMINAL_STATES
                    and prior["job"].ir_version == version and prior["input_digest"] == digest):
                if prior["task"].get_loop() is not loop:
                    raise RuntimeError("build belongs to another event loop")
                active = prior
                active["waiters"] += 1
            else:
                if prior:
                    if version < prior["job"].ir_version:
                        raise ValueError("a newer model version is already building")
                    self.cancel(prior["job"].job_id, "superseded by newer build")
                job = self.registry.save(BuildJob(model_id=model_id, ir_version=version,
                                                  digest=digest, input_digest=digest))
                active = {"job": job, "waiters": 1, "input_digest": digest,
                          "cancelled": threading.Event()}
                active["task"] = loop.create_task(self._execute(active, factory))
                self._active[model_id] = active
        try:
            return await asyncio.shield(active["task"])
        finally:
            with self._lock:
                active["waiters"] -= 1
                if not active["waiters"] and not active["task"].done():
                    self.cancel(active["job"].job_id, "all callers stopped")
                if active["task"].done() and self._active.get(model_id) is active:
                    self._active.pop(model_id, None)
                    self.pool.release_owner(active["job"].job_id)
            if not active["waiters"] and not active["task"].done():
                # Join cancellation before letting another write reuse staging.
                try:
                    await asyncio.shield(active["task"])
                except BaseException:
                    pass

    async def _execute(self, active, factory):
        job = active["job"]
        owner_token = worker_owner.set(job.job_id)
        cancellation_token = worker_cancellation.set(active["cancelled"])
        job_token = current_job.set(job)
        try:
            self._notify(job)
            await asyncio.sleep(self.debounce_s)
            self.registry.update(job, state="running", phase="validate")
            self._notify(job)
            async with asyncio.timeout(self.timeout_s):
                result, report = await factory()
            success = bool(result.ok and report and report.passed and job.artifact_id)
            self.registry.update(job, state="published" if success else "failed",
                                 phase="complete", error=None if success else
                                 (result.error.message if result.error else result.content))
            self._notify(job)
            return result, report
        except asyncio.CancelledError:
            self.pool.cancel_owner(job.job_id, "build cancelled")
            self.registry.update(job, state="cancelled", phase="cancelled")
            self._notify(job)
            raise
        except WorkerAborted as exc:
            self.registry.update(job, state="cancelled", phase="cancelled", error=str(exc))
            self._notify(job)
            raise asyncio.CancelledError() from exc
        except TimeoutError:
            self.registry.update(job, state="failed", phase="timeout",
                                 error=f"build exceeded {self.timeout_s}s")
            self._notify(job)
            raise
        except Exception as exc:
            if job.state == "published":
                # Notification/hook failures after the version swap cannot
                # rewrite the durable fact that a verified set was delivered.
                self.registry.update(job, error=str(exc))
                self._notify(job)
                raise
            self.registry.update(job, state="failed", phase="failed", error=str(exc))
            self._notify(job)
            raise
        finally:
            self.pool.release_owner(job.job_id)
            current_job.reset(job_token)
            worker_cancellation.reset(cancellation_token)
            worker_owner.reset(owner_token)
            with self._lock:
                if self._active.get(job.model_id) is active:
                    self._active.pop(job.model_id, None)
