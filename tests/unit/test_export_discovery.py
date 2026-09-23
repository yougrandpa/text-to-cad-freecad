"""Which files on disk count as *this build's deliverables* (objective §5-C).

The Gate used to answer that with a ``glob`` first-match over the artifact
directory. Two ways that lied:

  * ``compile_ir`` writes ``roundtrip.step`` to diagnose its own build — every
    time it runs, including when the exporter then failed to write
    ``<model>.step``. Sorting by name put the diagnostic first, so the STEP
    deliverable was graded from a file nobody asked for.
  * FreeCAD saves ``<model_id>.FCStd`` with capitals. ``*.fcstd`` matches
    nothing on a case-sensitive filesystem, so the reopenable document — the
    artifact this product exists to deliver — went unchecked on Linux while
    looking green on macOS.

And a check that grades only whatever happens to be on disk cannot notice a
missing file at all: with no exports present, ``exportability`` said "nothing
here to verify" and the build passed without delivering anything.
"""

from __future__ import annotations

import os

import pytest

from tcad.core.types import CheckStatus
from tcad.verify.checks_solid import ExportabilityCheck, VerifyConfig
from tcad.verify.context import _discover_exports, build_check_context
from tests.fixtures.gate_fixtures import FakeWorker, make_digest, make_ir, write_artefacts

MODEL = "bracket"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _touch(dirpath, name: str, content: str = "DATA") -> str:
    path = os.path.join(str(dirpath), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ── discovery: which file answers for which format ──────────────────────────


def test_a_build_diagnostic_is_not_a_deliverable(tmp_path):
    """``roundtrip.step`` alone must not stand in for the STEP export."""
    _touch(tmp_path, "roundtrip.step")
    assert "step" not in _discover_exports(str(tmp_path), MODEL)


def test_the_models_own_step_wins_over_the_diagnostic(tmp_path):
    _touch(tmp_path, "roundtrip.step")
    ours = _touch(tmp_path, f"{MODEL}.step")
    assert _discover_exports(str(tmp_path), MODEL)["step"] == ours


def test_an_export_under_another_name_is_still_reported(tmp_path):
    """Discovery must not hide geometry because of a naming surprise — the check
    that reads the map then cites the path, so the wrong name stays visible."""
    other = _touch(tmp_path, "legacy_name.step")
    assert _discover_exports(str(tmp_path), MODEL)["step"] == other


@pytest.mark.parametrize("name", ["bracket.FCStd", "bracket.fcstd"])
def test_the_reopenable_document_is_found_whatever_its_case(tmp_path, name):
    d = tmp_path / name.replace(".", "_")
    d.mkdir()
    path = _touch(d, name)
    assert _discover_exports(str(d), MODEL).get("fcstd") == path


def test_the_freecad_backup_of_the_previous_build_is_never_chosen(tmp_path):
    """``saveAs`` over an existing file leaves a ``.FCBak`` beside it: that is
    the *old* model, and old files must not vouch for a failed build."""
    _touch(tmp_path, f"{MODEL}.FCBak")
    assert "fcstd" not in _discover_exports(str(tmp_path), MODEL)


def test_a_missing_directory_yields_nothing_rather_than_raising(tmp_path):
    assert _discover_exports(str(tmp_path / "never_written"), MODEL) == {}


def test_discovered_paths_are_absolute(tmp_path):
    _touch(tmp_path, f"{MODEL}.stl")
    assert os.path.isabs(_discover_exports(str(tmp_path), MODEL)["stl"])


def test_the_check_context_carries_the_discovery_result(tmp_path):
    """The Gate does not glob again — it reads ``ctx.exports``. Prove the fixed
    discovery is what actually reaches a check."""
    ir_path, artifact_dir = write_artefacts(
        tmp_path, ir=make_ir(MODEL), digest=make_digest(MODEL), with_exports=False
    )
    _touch(artifact_dir, "roundtrip.step")
    ours = _touch(artifact_dir, f"{MODEL}.step")
    doc = _touch(artifact_dir, f"{MODEL}.FCStd")
    ctx = build_check_context(
        model_id=MODEL, ir_version=1,
        artifact_dir=str(artifact_dir), ir_path=str(ir_path),
        worker=FakeWorker(),
    )
    assert ctx.exports == {"step": ours, "fcstd": doc}
    assert ExportabilityCheck(VerifyConfig(required_exports=("step", "stl", "fcstd"))
                               ).run(ctx).status == CheckStatus.FAIL


# ── required formats: absence must block ────────────────────────────────────


class _Ctx:
    """Only the field this check reads."""

    def __init__(self, exports):
        self.exports = exports


def _grade(exports: dict, required: tuple[str, ...]):
    return ExportabilityCheck(VerifyConfig(required_exports=required)).run(_Ctx(exports))


def test_a_required_format_that_was_never_written_blocks(tmp_path):
    stl = _touch(tmp_path, "m.stl")
    r = _grade({"stl": stl}, ("step", "stl", "fcstd"))
    assert r.status == CheckStatus.FAIL
    assert r.measurements["missing_formats"] == "step, fcstd"
    assert "step" in r.message and "fcstd" in r.message


def test_no_exports_at_all_cannot_pass_when_something_is_required(tmp_path):
    r = _grade({}, ("step",))
    assert r.status == CheckStatus.FAIL
    assert r.measurements["missing_formats"] == "step"


def test_a_required_format_written_as_an_empty_file_blocks(tmp_path):
    good = _touch(tmp_path, "m.step")
    zero = os.path.join(str(tmp_path), "m.stl")
    open(zero, "w").close()
    r = _grade({"step": good, "stl": zero}, ("step", "stl"))
    assert r.status == CheckStatus.FAIL
    assert r.measurements["missing_formats"] == "stl"


def test_every_required_format_present_passes(tmp_path):
    exports = {fmt: _touch(tmp_path, f"m.{fmt}") for fmt in ("step", "stl", "fcstd")}
    r = _grade(exports, ("step", "stl", "fcstd"))
    assert r.status == CheckStatus.PASS
    assert set(r.evidence) == set(exports.values())


def test_without_requirements_the_old_grade_what_is_present_rule_holds(tmp_path):
    """Compatibility: a bare ``VerifyConfig()`` — what the unit tests and any
    caller with no storage config get — keeps its previous behaviour: SKIP on an
    empty directory, FAIL only for a file that exists but is empty."""
    assert _grade({}, ()).status == CheckStatus.SKIP
    assert _grade({"step": _touch(tmp_path, "m.step")}, ()).status == CheckStatus.PASS


# ── the wiring must actually turn the contract on ───────────────────────────


def test_reopenable_document_is_required_even_when_config_forgets_it():
    from tcad.core.wiring import required_export_formats

    assert required_export_formats(["step", "stl"]) == ("step", "stl", "fcstd")
    assert required_export_formats(["STEP"]) == ("step", "fcstd")
    assert required_export_formats(["fcstd"]) == ("fcstd",)
    assert required_export_formats([]) == ("fcstd",)


def test_every_required_format_is_one_the_exporter_can_produce():
    """A Gate that demands a format nothing writes would block every build."""
    from tcad.config.loader import load_config
    from tcad.core.wiring import required_export_formats
    from tcad.worker.protocol import EXPORT_FORMATS

    cfg = load_config(f"{REPO_ROOT}/configs/default.yaml")
    required = required_export_formats(cfg.storage.artifact_exports)
    assert set(required) <= set(EXPORT_FORMATS), required
    assert "fcstd" in required
