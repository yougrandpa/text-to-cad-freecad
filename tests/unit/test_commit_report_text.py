"""What the model is told after a commit.

`_format_report` is the model's only summary of whether the part is finished, so
a misleading sentence here costs a whole session. The one this file exists for:

    a green Gate with nothing to judge against used to say
    "Build succeeded — you may stop."

That happened live. Asked to put arms and legs on a cube, a model built two
calibration probes, never recorded what the user had asked for, and was told the
build had succeeded. Every check the Gate could run had passed — there was simply
nothing to check the *request* against, and the report did not say so.
"""

from __future__ import annotations

from tcad.core.types import (
    CheckResult,
    CheckStatus,
    Confidence,
    GateReport,
    Severity,
)
from tcad.ir.schema import ConstraintExpr, IrDocument, RequirementSpec
from tcad.loop.commit import _confirmed_constraints, _format_report


def _report(*, passed: bool = True) -> GateReport:
    result = CheckResult(
        check_id="solid_validity",
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        severity=Severity.BLOCKING,
        confidence=Confidence.DETERMINISTIC,
        message="a single valid solid" if passed else "not a solid",
    )
    return GateReport(
        model_id="m1",
        ir_version=1,
        passed=passed,
        results=[result],
        blocking_failures=[] if passed else ["solid_validity"],
    )


def _ir(*, constraints: list[ConstraintExpr] | None = None) -> IrDocument:
    return IrDocument(
        model_id="m1",
        requirements=RequirementSpec(
            raw_text="一个 60x60x60 的立方体",
            constraints=constraints or [],
        ),
    )


def _bbox(confirmed: bool = True) -> ConstraintExpr:
    return ConstraintExpr(
        kind="bbox",
        value={"x": 60.0, "y": 60.0, "z": 60.0},
        source_text="60x60x60",
        confirmed=confirmed,
    )


# ─── the misleading case ───────────────────────────────────────────────────


def test_a_pass_with_no_recorded_requirement_says_so_loudly():
    """The single most important sentence in this file.

    A green Gate with no requirement is not evidence that the part is right; it
    is evidence that the geometry is self-consistent. The model must not be told
    to stop on the strength of it.
    """
    text = _format_report(_report(passed=True), _ir())
    assert "NOT JUDGED AGAINST ANY REQUEST" in text
    assert "update_requirement" in text, "没有告诉模型该怎么补救"
    assert "self-consistent" in text, "没有说明 Gate 实际证明了什么"
    # and it must NOT read as an unqualified success
    assert "Build succeeded — you may stop." not in text


def test_a_pass_with_a_confirmed_requirement_reports_what_it_was_judged_on():
    text = _format_report(_report(passed=True), _ir(constraints=[_bbox()]))
    assert "Judged against 1 confirmed requirement(s)" in text
    assert "NOT JUDGED" not in text
    assert "you may stop" not in text
    assert "design_review" in text


def test_an_unconfirmed_requirement_does_not_count_as_a_judgement():
    """`confirmed=False` must never block, so it cannot count as evidence
    either — otherwise the two rules would contradict each other."""
    text = _format_report(_report(passed=True), _ir(constraints=[_bbox(confirmed=False)]))
    assert "NOT JUDGED AGAINST ANY REQUEST" in text


def test_several_confirmed_requirements_are_counted():
    text = _format_report(
        _report(passed=True), _ir(constraints=[_bbox(), _bbox(), _bbox(confirmed=False)])
    )
    assert "Judged against 2 confirmed requirement(s)" in text


def test_a_failure_report_is_unchanged_by_the_new_wording():
    text = _format_report(_report(passed=False), _ir())
    assert "GATE FAILED" in text
    assert "NOT JUDGED" not in text


def test_the_report_still_works_without_an_ir():
    """`_format_report` is called from tests and tooling with just a report."""
    text = _format_report(_report(passed=True))
    assert "GATE PASSED" in text


# ─── the helper ────────────────────────────────────────────────────────────


def test_confirmed_constraints_filters_correctly():
    ir = _ir(constraints=[_bbox(), _bbox(confirmed=False)])
    assert len(_confirmed_constraints(ir)) == 1


def test_confirmed_constraints_tolerates_a_bare_object():
    """It is called with whatever the caller has; it must not explode."""
    assert _confirmed_constraints(None) == []
    assert _confirmed_constraints(object()) == []
