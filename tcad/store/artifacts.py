"""Artifact addressing (design §4.4, L4).

The digest and the exported STEP/STL are the artefacts the verification Gate
reads back *from disk* through its independent read path (CQRS, design §4.6).
This module is what makes that path work: it assigns **deterministic**,
**documented** paths so the Gate can find an artefact by ``(model_id, version,
fmt)`` without any shared in-memory state with the loop.

Path layout
-----------
    <data_dir>/artifacts/<model_id>/v<version>/
        model.step          # fmt="step"
        model.stl           # fmt="stl"
        model.brep          # fmt="brep"
        model.FCStd         # fmt="fcstd"
        digest.json         # fmt="digest"  (GeometryDigest)
        view_<name>.png     # fmt="view:<name>"

All paths are pure functions of the arguments — no hidden state, no UUIDs — so
two processes (the compiler writing, the Gate reading) agree on the location.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tcad.core.ids import contained_path, ensure_safe_id
from tcad.core.types import BuildStamp, GeometryDigest

_DEFAULT_DATA_DIR = "data"

# fmt -> file extension (no dot)
_EXT: dict[str, str] = {
    "step": "step",
    "stl": "stl",
    "brep": "brep",
    "fcstd": "FCStd",
    "digest": "json",
}


class ArtifactStore:
    def __init__(self, data_dir: str | os.PathLike[str] = _DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    # ── directories ───────────────────────────────────────────────────────────────

    def dir_for(self, model_id: str, version: int) -> Path:
        """Directory that holds every artefact for ``(model_id, version)``.

        Both components are constrained: the id by shape (it can never name
        anything but a child of ``artifacts/``), the version by coercion to int
        (``"v0/../../etc"`` is rejected by ``int()`` before it is a path).
        """
        ensure_safe_id(model_id, kind="model_id")
        return contained_path(self.data_dir, "artifacts", model_id, f"v{int(version)}")

    def ensure(self, model_id: str, version: int) -> Path:
        """Create the artefact directory if needed; return it."""
        d = self.dir_for(model_id, version)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── path resolution ────────────────────────────────────────────────────────────

    def path_for(self, model_id: str, version: int, fmt: str) -> Path:
        """Deterministic absolute path for an artefact.

        ``fmt`` is one of ``step|stl|brep|fcstd|digest`` or ``view:<name>`` for a
        rendered PNG. The base filename is always ``model`` (or ``view_<name>``).
        """
        d = self.dir_for(model_id, version)
        if fmt.startswith("view:"):
            name = fmt.split(":", 1)[1] or "iso"
            return d / f"view_{name}.png"
        ext = _EXT.get(fmt)
        if ext is None:
            raise ValueError(
                f"unknown artifact format '{fmt}'; "
                f"expected one of {sorted(_EXT)} or 'view:<name>'")
        return d / f"model.{ext}"

    # ── exports (STEP/STL/BREP/FCStd) ──────────────────────────────────────────────

    def list_exports(self, model_id: str, version: int) -> list[str]:
        """Return the export formats present on disk for this version."""
        d = self.dir_for(model_id, version)
        if not d.is_dir():
            return []
        present: list[str] = []
        for fmt, ext in _EXT.items():
            if fmt == "digest":
                continue
            if (d / f"model.{ext}").exists():
                present.append(fmt)
        return present

    # ── digest (the Gate's independent read) ──────────────────────────────────────

    def write_digest(self, model_id: str, version: int,
                     digest: GeometryDigest | dict) -> Path:
        """Persist a :class:`GeometryDigest` (or its dict form) as ``digest.json``.

        The filename is ``digest.json`` — this module's own layout comment, the
        design doc (§4.4) and ``tcad/verify/context.py::_load_digest`` all agree
        on it. The code previously wrote ``model.json`` while documenting
        ``digest.json``, so the Gate's disk reader never found the digest and
        silently fell back to "cannot measure". One name, one reader.
        """
        return self.write_digest_at(self.ensure(model_id, version), digest)

    def write_digest_at(
        self, artifact_dir: str | Path, digest: GeometryDigest | dict
    ) -> Path:
        """Write ``digest.json`` into a specific directory.

        Used by a staged build, where the digest has to land in the directory the
        Gate is about to grade (the private attempt directory) rather than in the
        version's published one. Same filename, same reader.
        """
        d = Path(artifact_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "digest.json"
        if isinstance(digest, GeometryDigest):
            data = digest.model_dump_json()
        else:
            data = json.dumps(digest, ensure_ascii=False, indent=2)
        path.write_text(data)
        return path

    def read_digest(self, model_id: str, version: int) -> GeometryDigest | None:
        """Load ``digest.json`` if present, else ``None``."""
        path = self.dir_for(model_id, version) / "digest.json"
        if not path.exists():
            return None
        return GeometryDigest.model_validate_json(path.read_text())

    # ── staging and atomic publish ─────────────────────────────────────────────
    #
    # A build is written to a private per-attempt directory and only becomes the
    # version's artifacts after the Gate has verified it. Two properties follow,
    # and they are the reason this exists at all:
    #
    #   * the Gate grades a directory that contains *only* this attempt's files,
    #     so a leftover from a failed attempt can never answer for a missing
    #     export — the guarantee is structural, not a timestamp heuristic;
    #   * a failed attempt leaves the last verified build untouched, so "recover
    #     the last good version" is just "do nothing".

    def staging_dir(self, model_id: str, version: int, attempt_id: str) -> Path:
        """Private build directory for one attempt.

        Sibling of the version directory (not inside it) so no listing, glob or
        ``rglob`` over the version directory can ever see a half-built attempt.

        Also performs publish recovery first: a crash between the two renames of
        :meth:`publish` can leave the version directory absent with its contents
        parked in ``.v<N>.previous``. Restoring there is the only correct thing
        to do, and doing it here means every new attempt repairs the tree before
        it starts.
        """
        self.recover_publish(model_id, version)
        canonical = self.dir_for(model_id, version)
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in attempt_id)
        return canonical.parent / f"{canonical.name}.staging-{safe}"

    def discard_staging(self, staging_dir: str | Path) -> None:
        """Drop a failed attempt. Its files were never published."""
        shutil.rmtree(Path(staging_dir), ignore_errors=True)

    def _previous_dir(self, model_id: str, version: int) -> Path:
        canonical = self.dir_for(model_id, version)
        return canonical.parent / f"{canonical.name}.previous"

    def recover_publish(self, model_id: str, version: int) -> None:
        """Restore ``.previous`` when a publish was interrupted mid-swap."""
        canonical = self.dir_for(model_id, version)
        previous = self._previous_dir(model_id, version)
        if not canonical.exists() and previous.exists():
            os.replace(previous, canonical)

    def write_manifest(
        self, staging_dir: str | Path, *, model_id: str, version: int,
        attempt_id: str, ir_sha256: str, extra: dict[str, Any] | None = None,
        status: str = "draft",
    ) -> Path:
        """Write the build's artifact list — every file with its hash and size.

        Written into the staging directory *before* publishing, so the manifest
        travels with the files it describes and is never ahead of them.
        """
        d = Path(staging_dir)
        files: dict[str, dict[str, Any]] = {}
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name == "manifest.json":
                continue
            data = p.read_bytes()
            files[p.name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        from tcad.artifacts.manifest import ArtifactSet, artifact_identity

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "artifact_id": artifact_identity(model_id, int(version), attempt_id, ir_sha256, files),
            "status": status,
            "model_id": model_id,
            "ir_version": int(version),
            "attempt_id": attempt_id,
            "ir_sha256": ir_sha256,
            "files": files,
        }
        if extra:
            manifest.update(extra)
        manifest = ArtifactSet.model_validate(manifest).model_dump(mode="json")
        path = d / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _retain_artifact_set(self, staging: Path) -> None:
        """Retain a verified set independently of the mutable version alias.

        Existing manifest-less fixtures/legacy builds keep their old layout.
        New sets are copied before the version swap, so artifact_id queries
        survive a retry or a later consumer writing derived files to the alias.
        """
        manifest_path = staging / "manifest.json"
        if not manifest_path.is_file():
            return
        from tcad.artifacts.manifest import PublishedArtifact
        from tcad.inspect.artifact import ArtifactReader

        manifest = PublishedArtifact.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        reader = ArtifactReader(self.data_dir)
        reader.gate_report(manifest, staging)
        source = reader.read_file(manifest, staging, "ir.json")
        if hashlib.sha256(source).hexdigest() != manifest.ir_sha256:
            raise ValueError("artifact source does not match its build input hash")
        root = reader.object_dir(manifest.artifact_id)
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.exists():
            existing, _ = reader.resolve(manifest.model_id, artifact_id=manifest.artifact_id)
            for name in existing.files:
                reader.read_file(existing, root, name)
            return
        with tempfile.TemporaryDirectory(prefix=".set-", dir=root.parent) as scratch:
            candidate = Path(scratch) / "artifact"
            candidate.mkdir()
            for name in manifest.files:
                (candidate / name).write_bytes(reader.read_file(manifest, staging, name))
            shutil.copyfile(manifest_path, candidate / "manifest.json")
            try:
                os.rename(candidate, root)
            except OSError:
                # Another publisher may have installed this exact identity.
                if not root.is_dir():
                    raise
                existing, _ = reader.resolve(manifest.model_id, artifact_id=manifest.artifact_id)
                for name in existing.files:
                    reader.read_file(existing, root, name)

    def publish(self, model_id: str, version: int, staging_dir: str | Path) -> Path:
        """Make a verified attempt the version's artifacts.

        Two ``os.replace`` directory renames, so a reader never observes a mix of
        old and new files: the old tree is moved aside, the new one moved in, and
        only then is the old one deleted. ``recover_publish`` handles the (tiny)
        window between the two renames.

        Refuses to publish an empty staging directory: "nothing was built" must
        never read as "published".
        """
        staging = Path(staging_dir)
        if not staging.is_dir() or not any(staging.iterdir()):
            raise FileNotFoundError(f"nothing to publish: {staging} is missing or empty")

        self._retain_artifact_set(staging)

        canonical = self.dir_for(model_id, version)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        previous = self._previous_dir(model_id, version)
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        if canonical.exists():
            os.replace(canonical, previous)
        try:
            os.replace(staging, canonical)
        except Exception:
            # Put the last verified build back rather than leaving a hole.
            if not canonical.exists() and previous.exists():
                os.replace(previous, canonical)
            raise
        shutil.rmtree(previous, ignore_errors=True)
        return canonical


# ══════════════════════════════════════════════════════════════════════════
# Build stamp — which attempt the files in a directory came from
# ══════════════════════════════════════════════════════════════════════════

BUILD_STAMP_FILE = "build_stamp.json"


def write_build_stamp(artifact_dir: str | Path, stamp: BuildStamp) -> Path:
    """Record the attempt that is about to write into ``artifact_dir``.

    Call this *before* the compiler runs. The whole point is that anything
    already in the directory when the stamp lands belongs to an earlier —
    possibly failed — attempt, and must not be counted as this build's output.
    """
    d = Path(artifact_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / BUILD_STAMP_FILE
    path.write_text(stamp.model_dump_json(), encoding="utf-8")
    return path


def read_build_stamp(artifact_dir: str | Path) -> BuildStamp | None:
    """The attempt that owns this directory, or ``None`` if it predates stamping."""
    path = Path(artifact_dir) / BUILD_STAMP_FILE
    if not path.is_file():
        return None
    return BuildStamp.model_validate_json(path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════
# Last gate report — what the previous turn proved (or failed to prove)
# ══════════════════════════════════════════════════════════════════════════
#
# Kept OUT of the artifact directory on purpose. That directory's file set is
# itself part of what the Gate audits (``_discover_exports`` plus the provenance
# check), and adding a bookkeeping file there would change what "the delivered
# files" means. This is conversation state, not a deliverable, so it lives beside
# the IR snapshots instead.

GATE_REPORT_DIR = "gate_reports"


def gate_report_path(
    data_dir: str | Path, model_id: str, version: int
) -> Path:
    ensure_safe_id(model_id, kind="model_id")
    return contained_path(data_dir, GATE_REPORT_DIR, model_id, f"v{int(version)}.json")


def write_gate_report(
    data_dir: str | Path, model_id: str, version: int, report: Any
) -> Path:
    """Persist one GateReport so a *later process* can see the previous result.

    The server builds a fresh ``LoopEngine`` per request, so anything the engine
    remembers in memory is gone by the next turn. "The last error" therefore has
    to be on disk for multi-turn repair to be possible at all.
    """
    path = gate_report_path(data_dir, model_id, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(report, "model_dump_json"):
        data = report.model_dump_json()
    else:
        data = json.dumps(report, ensure_ascii=False)
    path.write_text(data, encoding="utf-8")
    return path


def read_gate_report(
    data_dir: str | Path, model_id: str, version: int
) -> dict | None:
    """Read a persisted GateReport as a plain dict, or ``None``."""
    path = gate_report_path(data_dir, model_id, version)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable report is "no report"
        return None


# ══════════════════════════════════════════════════════════════════════════
# The current verdict — is THIS version verified, on disk, right now?
# ══════════════════════════════════════════════════════════════════════════
#
# A persisted GateReport answers "was version N graded, and how?". It does not
# answer the question a person actually asks, which is "is the thing I am looking
# at verified?". Those differ as soon as a write happens after a pass: the report
# for N still says passed (true — N *was* verified), while the model has moved on
# to N+1, which nothing has graded. Reading the old report as "current" is how a
# stale success gets displayed.
#
# So the current verdict is computed from two facts that must BOTH hold:
#   * the persisted report is FOR the current version and says passed, and
#   * that version's artifacts were actually published (Round-2's publish gate
#     writes the version directory only after a passing Gate).
# Either one alone is not "verified", and the returned ``reason`` says which.

_DELIVERABLE_EXTS = frozenset({".step", ".stl", ".brep", ".fcstd"})


def _published(artifact_dir: Path) -> tuple[bool, bool]:
    """``(has a deliverable, has a manifest)`` for a version directory."""
    if not artifact_dir.is_dir():
        return False, False
    manifest = (artifact_dir / "manifest.json").is_file()
    has_deliverable = any(
        p.is_file() and p.suffix.lower() in _DELIVERABLE_EXTS
        for p in artifact_dir.iterdir()
    )
    return has_deliverable, manifest


def build_verdict(
    data_dir: str | Path, model_id: str, current_version: int
) -> dict[str, Any]:
    """Whether ``current_version`` is verified, and why (or why not)."""
    report = read_gate_report(data_dir, model_id, current_version)
    published, has_manifest = _published(
        ArtifactStore(data_dir).dir_for(model_id, current_version))

    graded_version = None
    passed: bool | None = None
    blocking: list = []
    advisory: list = []
    attempt_id = ""
    if report is not None:
        graded_version = report.get("ir_version")
        passed = bool(report.get("passed"))
        blocking = list(report.get("blocking_failures") or [])
        advisory = list(report.get("advisory_findings") or [])
        attempt_id = str(report.get("attempt_id") or "")

    is_current = graded_version == current_version
    verified = bool(is_current and passed and published)

    if verified:
        reason = f"version {current_version} passed the Gate and its artifacts are published"
    elif report is None:
        reason = (f"version {current_version} has never been graded; "
                  "there is no GateReport for it")
    elif not is_current:
        reason = (f"the last GateReport is for version {graded_version}, not the "
                  f"current version {current_version} — it is a stale verdict")
    elif not passed:
        reason = (f"version {current_version} did NOT pass the Gate: "
                  + (", ".join(blocking) if blocking else "no blocking check passed"))
    else:
        reason = (f"version {current_version} passed the Gate but no artifacts are "
                  "published for it (a passing report alone is not a delivery)")

    return {
        "model_id": model_id,
        "ir_version": int(current_version),
        "verified": verified,
        "passed": passed,
        "graded_version": graded_version,
        "verdict_is_current": is_current,
        "blocking_failures": blocking,
        "advisory_findings": advisory,
        "attempt_id": attempt_id,
        "artifacts_published": published,
        "artifact_manifest": has_manifest,
        "reason": reason,
    }
