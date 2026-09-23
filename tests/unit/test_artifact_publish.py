"""Staging + verified-only publish (task book §5-C).

A build writes into a private per-attempt directory; only a build the Gate
passed becomes the version's artifacts. These tests are deterministic (no
FreeCAD): they exercise the store primitives, the Gate's directory override, and
the one seam that could quietly reintroduce the bug — a fallback that borrows the
published digest while grading a staged build.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcad.core.types import (
    CheckContext,
    CheckResult,
    CheckStatus,
    Confidence,
    GateReport,
    Severity,
)
from tcad.core.wiring import ContextServiceAdapter, StoreAdapter
from tcad.ir.schema import IrDocument
from tcad.store.artifacts import ArtifactStore
from tcad.verify.gate import Gate


# ══════════════════════════════════════════════════════════════════════════
# 1. the store primitives
# ══════════════════════════════════════════════════════════════════════════


def _staging_with_files(store: ArtifactStore, model="m", version=0, attempt="a1", **files):
    d = store.staging_dir(model, version, attempt)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (d / name).write_text(data, encoding="utf-8")
    return d


def test_publish_promotes_the_staging_directory(tmp_path):
    store = ArtifactStore(tmp_path)
    staging = _staging_with_files(store, step="SOLID", manifest_hint="x")

    canonical = store.publish("m", 0, staging)

    assert canonical == store.dir_for("m", 0)
    assert (canonical / "step").read_text() == "SOLID"
    assert not staging.exists(), "the staging directory must not survive a publish"


def test_staging_is_a_sibling_so_no_listing_can_see_a_half_built_attempt(tmp_path):
    store = ArtifactStore(tmp_path)
    staging = store.staging_dir("m", 3, "attempt-1")
    assert staging.parent == store.dir_for("m", 3).parent
    assert store.dir_for("m", 3) not in staging.parents


def test_publish_refuses_an_empty_staging_directory(tmp_path):
    store = ArtifactStore(tmp_path)
    staging = store.staging_dir("m", 0, "empty")
    staging.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        store.publish("m", 0, staging)
    assert not store.dir_for("m", 0).exists(), "nothing was built; nothing may appear"


def test_publish_replaces_a_previous_build_wholesale(tmp_path):
    """A stale file from the last build must not survive into the new one."""
    store = ArtifactStore(tmp_path)
    store.write_digest_at(store.ensure("m", 0), {"a": 1})
    (store.dir_for("m", 0) / "leftover.step").write_text("OLD", encoding="utf-8")

    staging = _staging_with_files(store, step="NEW")
    store.publish("m", 0, staging)

    assert (store.dir_for("m", 0) / "step").read_text() == "NEW"
    assert not (store.dir_for("m", 0) / "leftover.step").exists()


def test_an_interrupted_publish_is_recovered(tmp_path):
    """Crash between the two renames: the old build is parked, not lost."""
    store = ArtifactStore(tmp_path)
    canonical = store.dir_for("m", 0)
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / "step").write_text("VERIFIED", encoding="utf-8")
    previous = canonical.parent / f"{canonical.name}.previous"
    canonical.rename(previous)  # the crash: canonical moved aside, staging not moved in

    assert not canonical.exists()
    store.recover_publish("m", 0)
    assert (canonical / "step").read_text() == "VERIFIED"


def test_discard_removes_a_failed_attempt(tmp_path):
    store = ArtifactStore(tmp_path)
    staging = _staging_with_files(store, step="HALF BUILT")
    store.discard_staging(staging)
    assert not staging.exists()


def test_manifest_lists_every_file_with_hash_and_size(tmp_path):
    import hashlib

    store = ArtifactStore(tmp_path)
    staging = _staging_with_files(store, **{"m.step": "GEOMETRY", "digest.json": "{}"})
    store.write_manifest(
        staging, model_id="m", version=0, attempt_id="att-9", ir_sha256="f" * 64
    )
    manifest = json.loads((staging / "manifest.json").read_text())

    assert manifest["attempt_id"] == "att-9"
    assert manifest["ir_sha256"] == "f" * 64
    assert set(manifest["files"]) == {"m.step", "digest.json"}
    assert manifest["files"]["m.step"]["bytes"] == len("GEOMETRY")
    assert manifest["files"]["m.step"]["sha256"] == hashlib.sha256(b"GEOMETRY").hexdigest()
    # The manifest never lists itself — it is the index, not an entry.
    assert "manifest.json" not in manifest["files"]


# ══════════════════════════════════════════════════════════════════════════
# 2. the Gate's directory override
# ══════════════════════════════════════════════════════════════════════════


class _Check:
    id = "always"
    severity = Severity.BLOCKING
    confidence = Confidence.DETERMINISTIC

    def run(self, ctx):
        return CheckResult(
            check_id=self.id, status=CheckStatus.PASS, severity=self.severity,
            confidence=self.confidence, message=f"graded {ctx.artifact_dir}",
        )


def _ctx(model_id, ir_version, artifact_dir):
    return CheckContext(
        model_id=model_id, ir_version=ir_version,
        ir=IrDocument(model_id=model_id, version=ir_version),
        artifact_dir=str(artifact_dir), exports={}, digest=None,
    )


def test_gate_grades_the_requested_directory_when_the_loader_supports_it(tmp_path):
    seen = {}

    def loader(model_id, ir_version, artifact_dir=None):
        seen["artifact_dir"] = artifact_dir
        return _ctx(model_id, ir_version, artifact_dir or tmp_path)

    gate = Gate(loader, solid_checks=[_Check()])
    gate.evaluate("m", 1, artifact_dir=str(tmp_path / "staged"))

    assert seen["artifact_dir"] == str(tmp_path / "staged")


def test_gate_still_works_with_a_two_argument_loader(tmp_path):
    """The original loader contract must not break because of the new kwarg."""
    calls = []

    def loader(model_id, ir_version):
        calls.append((model_id, ir_version))
        return _ctx(model_id, ir_version, tmp_path)

    gate = Gate(loader, solid_checks=[_Check()])
    report = gate.evaluate("m", 1, artifact_dir=str(tmp_path / "staged"))

    assert calls == [("m", 1)]
    assert isinstance(report, GateReport)


# ══════════════════════════════════════════════════════════════════════════
# 3. the stale-evidence seam
# ══════════════════════════════════════════════════════════════════════════


def test_grading_staging_never_borrows_the_published_digest(tmp_path):
    """A staged attempt with no digest must read as "cannot measure".

    Falling back to the version's *published* digest would hand the Gate the
    previous build's measurements for an IR that was just rewritten. That is a
    stale verdict attesting a fresh build — the exact failure staging exists to
    remove — and it would live in the "cannot attest" fallback path, where it is
    least likely to be noticed.
    """
    store = StoreAdapter(tmp_path)
    ir = IrDocument(model_id="m", version=0)
    store.create("m", ir)

    published = store.artifact_dir("m", 0)
    published.mkdir(parents=True, exist_ok=True)
    store.artifacts.write_digest_at(
        published, {"model_id": "m", "ir_version": 0, "volume": 12345.0,
                    "measurements_available": True}
    )

    staging = store.staging_dir("m", 0, "attempt-x")
    staging.mkdir(parents=True, exist_ok=True)

    digest = ContextServiceAdapter(store).digest("m", 0, artifact_dir=str(staging))
    assert digest.measurements_available is False, (
        "the staged attempt produced no measurements; the published ones are not its evidence")
    assert digest.volume == 0.0


def test_grading_the_published_directory_still_reads_its_digest(tmp_path):
    store = StoreAdapter(tmp_path)
    store.create("m", IrDocument(model_id="m", version=0))
    store.artifacts.write_digest_at(
        store.artifact_dir("m", 0),
        {"model_id": "m", "ir_version": 0, "volume": 42.0, "measurements_available": True},
    )
    digest = ContextServiceAdapter(store).digest("m", 0)
    assert digest.measurements_available is True and digest.volume == 42.0
