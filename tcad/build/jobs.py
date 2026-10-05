"""Persistent build records; interrupted processes cannot leave running jobs."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tcad.core.ids import contained_path, ensure_safe_id


class BuildJob(BaseModel):
    job_id: str = Field(default_factory=lambda: "job-" + uuid.uuid4().hex)
    model_id: str
    ir_version: int
    digest: str
    input_digest: str | None = None
    state: Literal["queued", "running", "verifying", "published", "failed", "cancelled", "interrupted"] = "queued"
    phase: str = "queued"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    artifact_id: str | None = None
    attempt_id: str | None = None
    artifact_dir: str | None = None
    error: str | None = None


class JobRegistry:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "build_jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        for path in self.root.glob("job-*.json"):
            try:
                job = BuildJob.model_validate_json(path.read_bytes())
            except (ValueError, OSError):
                continue
            if job.state in {"queued", "running", "verifying"} or (
                    job.state == "published" and job.attempt_id and not job.artifact_id):
                self._recover(job, data_dir)

    def _recover(self, job, data_dir):
        from tcad.store.artifacts import ArtifactStore
        from tcad.inspect.artifact import ArtifactReader
        try:
            store = ArtifactStore(data_dir)
            store.recover_publish(job.model_id, job.ir_version)
            manifest, _ = ArtifactReader(data_dir).resolve(job.model_id, job.ir_version)
            if job.attempt_id and manifest.attempt_id == job.attempt_id:
                self.update(job, state="published", phase="recovered_publication",
                            artifact_id=manifest.artifact_id, error=None)
                return
        except (OSError, ValueError):
            pass
        if job.attempt_id:
            try:
                expected = ArtifactStore(data_dir).staging_dir(job.model_id, job.ir_version, job.attempt_id)
                if job.artifact_dir and expected.resolve() == Path(job.artifact_dir).resolve():
                    ArtifactStore(data_dir).discard_staging(expected)
            except (OSError, ValueError):
                pass
        self.update(job, state="interrupted", phase="recovery",
                    error="supervisor restarted; submit the build again")

    def save(self, job):
        with self._lock:
            path = contained_path(self.root, ensure_safe_id(job.job_id, kind="job_id") + ".json")
            candidate = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            candidate.write_text(job.model_dump_json(), encoding="utf-8")
            candidate.replace(path)
        return job

    def update(self, job, **changes):
        with self._lock:
            for name, value in {**changes, "updated_at": time.time()}.items():
                setattr(job, name, value)
            return self.save(job)

    def get(self, job_id):
        path = contained_path(self.root, ensure_safe_id(job_id, kind="job_id") + ".json")
        return BuildJob.model_validate_json(path.read_bytes())

    def list(self, model_id=None):
        jobs = []
        for path in self.root.glob("job-*.json"):
            try:
                job = BuildJob.model_validate_json(path.read_bytes())
            except (ValueError, OSError):
                continue
            if model_id is None or job.model_id == model_id:
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)
