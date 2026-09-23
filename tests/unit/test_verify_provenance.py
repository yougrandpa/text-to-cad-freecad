"""Provenance: the Gate may only grade the files this build attempt wrote.

A retry of ``ir_commit`` reuses ``data/artifacts/<model_id>/v<N>/``, so a STEP
surviving from the attempt that just failed is indistinguishable from a fresh
one by name alone. The commit pipeline stamps the directory with an
``attempt_id`` before it touches the worker; these tests pin down what the
stamp does and does not let the Gate believe.
"""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from tcad.core.types import BuildStamp, CheckStatus
from tcad.store.artifacts import read_build_stamp, write_build_stamp
from tcad.verify.context import CheckContextError, build_check_context
from tcad.verify.gate import Gate
from tests.fixtures.gate_fixtures import (
    FakeWorker, make_digest, make_ir, write_artefacts,
)


def _graded(tmp_path, **kw):
    """Build a Gate over a directory written by ``write_artefacts``."""
    ir = kw.pop("ir", None) or make_ir()
    digest = kw.pop("digest", None)
    ir_path, artifact_dir = write_artefacts(
        tmp_path, ir=ir, digest=digest, **kw
    )
    ctx = build_check_context(
        model_id=ir.model_id, ir_version=ir.version,
        artifact_dir=str(artifact_dir), ir_path=str(ir_path),
        worker=FakeWorker(),
    )
    return Gate(lambda m, v: ctx).evaluate(ir.model_id, ir.version), artifact_dir


def _result(rep, check_id):
    return next(r for r in rep.results if r.check_id == check_id)


# ── the clean case: everything this attempt wrote ────────────────────────


def test_clean_build_provenance_passes(tmp_path):
    rep, _ = _graded(tmp_path)
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.PASS
    assert rep.passed is True


def test_gate_report_carries_the_attempt_it_graded(tmp_path):
    ir = make_ir()
    rep, artifact_dir = _graded(tmp_path, attempt_id="acc-42")
    stamp = read_build_stamp(artifact_dir)
    assert stamp is not None
    assert rep.attempt_id == "acc-42" == stamp.attempt_id
    # The report binds the exact IR handed to the compiler, so a later edit of
    # ir.json cannot be presented as the document that produced these exports.
    assert stamp.ir_sha256 == hashlib.sha256(ir.model_dump_json().encode()).hexdigest()
    assert rep.ir_sha256 == stamp.ir_sha256


# ── wrong-version / wrong-model digest: real numbers, wrong build ─────────


def test_digest_from_another_ir_version_blocks(tmp_path):
    rep, _ = _graded(tmp_path, digest=make_digest(version=7))
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "v7" in r.message and "v1" in r.message
    assert rep.passed is False
    assert "provenance" in rep.blocking_failures


def test_digest_from_another_model_blocks(tmp_path):
    rep, _ = _graded(tmp_path, digest=make_digest(model_id="other_model"))
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "other_model" in r.message
    assert "provenance" in rep.blocking_failures


def test_stamp_from_another_build_blocks(tmp_path):
    """A stamp copied in from a different model/version is not a blank cheque."""
    ir = make_ir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    write_build_stamp(artifact_dir, BuildStamp(
        attempt_id="borrowed", model_id="another", ir_version=99,
        started_at=time.time() - 1,
        ir_sha256=hashlib.sha256(ir.model_dump_json().encode()).hexdigest(),
    ))
    ir_path, _ = write_artefacts(tmp_path, ir=ir, with_stamp=False)
    ctx = build_check_context(
        model_id="m1", ir_version=1, artifact_dir=str(artifact_dir),
        ir_path=str(ir_path), worker=FakeWorker(),
    )
    rep = Gate(lambda m, v: ctx).evaluate("m1", 1)
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "another" in r.message
    assert rep.passed is False


# ── stale exports: the file is real, just not from this build ─────────────


def test_export_predating_the_attempt_blocks(tmp_path):
    """The retry's own exports must not be satisfied by leftovers."""
    ir_path, artifact_dir = write_artefacts(tmp_path)
    stale = artifact_dir / "m1.step"
    # An hour older than the attempt that "just" wrote it.
    back = time.time() - 3600
    os.utime(stale, (back, back))

    ctx = build_check_context(
        model_id="m1", ir_version=1, artifact_dir=str(artifact_dir),
        ir_path=str(ir_path), worker=FakeWorker(),
    )
    rep = Gate(lambda m, v: ctx).evaluate("m1", 1)
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "step" in r.message
    assert "leftover" in r.message
    assert r.measurements["attempt_id"] == "fixture-attempt"
    assert rep.passed is False


def test_fresh_export_alongside_a_stale_one_still_blocks(tmp_path):
    """One good file does not launder another attempt's STEP."""
    ir_path, artifact_dir = write_artefacts(tmp_path)
    (artifact_dir / "m1.brep").write_text("BREP", encoding="utf-8")
    back = time.time() - 3600
    os.utime(artifact_dir / "m1.step", (back, back))

    ctx = build_check_context(
        model_id="m1", ir_version=1, artifact_dir=str(artifact_dir),
        ir_path=str(ir_path), worker=FakeWorker(),
    )
    rep = Gate(lambda m, v: ctx).evaluate("m1", 1)
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "step" in r.message and "brep" not in r.message


# ── no stamp at all: cannot verify, and that is not a shrug ───────────────


def test_unstamped_directory_is_a_blocking_fail(tmp_path):
    """"Not enough evidence" must not degrade into an ignorable SKIP."""
    rep, _ = _graded(tmp_path, with_stamp=False)
    r = _result(rep, "provenance")
    assert r.status is CheckStatus.FAIL
    assert "build stamp" in r.message
    assert rep.passed is False
    assert "provenance" not in rep.skipped_checks


def test_unreadable_stamp_raises_instead_of_grading_blindly(tmp_path):
    """A half-written stamp file is an error the operator must see."""
    ir_path, artifact_dir = write_artefacts(tmp_path)
    (artifact_dir / "build_stamp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckContextError, match="unreadable"):
        build_check_context(
            model_id="m1", ir_version=1, artifact_dir=str(artifact_dir),
            ir_path=str(ir_path), worker=FakeWorker(),
        )
