"""Fixed-set regression metrics: pipeline numbers, not a semantic verdict."""

from __future__ import annotations

import json

import pytest

from tcad.agent.regression import ACCEPTANCE_BOUNDARY, aggregate, load_events, run_metrics
from tools.evaluate_regression import load_cases as load_regression_cases


def events(*items) -> list[dict]:
    return list(items)


def model(step):
    return {"kind": "model", "step": step}


def tool(step, name, ok=True, kind=None, content="", gate=None):
    event = {"kind": "tool", "step": step, "name": name, "ok": ok, "content": content}
    if kind is not None:
        event["error"] = {"kind": kind, "message": f"{kind} failure"}
    if gate is not None:
        event["gate"] = {"passed": gate}
    return event


def summary(state="succeeded", delivered=True, steps=6):
    return {"state": state, "delivery_passed": delivered, "steps": steps}


def test_clean_run_has_zero_recovery_and_first_build_success():
    metrics = run_metrics(events(
        model(1), tool(1, "cad_build_parts"), model(2), tool(2, "ir_commit", gate=True),
        model(3), tool(3, "design_review"),
    ), summary())
    assert metrics["first_build_passed"] and metrics["first_build_step"] == 2
    assert metrics["build_passed"] and metrics["first_pass_step"] == 2
    assert metrics["recovery_steps"] == 0
    assert metrics["param_errors"] == 0
    assert metrics["complete_delivery"]
    # No plan was recorded, so "was key design sacrificed?" is honestly unknown.
    assert metrics["key_design_sacrificed"] == "unknown"


def test_recovery_steps_measure_failure_to_first_pass():
    metrics = run_metrics(events(
        model(1), tool(1, "cad_build_parts", ok=False, kind="schema"),
        model(2), tool(2, "cad_build_parts", ok=False, kind="semantic"),
        model(3), tool(3, "ir_commit", gate=False),
        model(4), tool(4, "ir_patch"),
        model(5), tool(5, "ir_commit", gate=True),
    ), summary())
    assert not metrics["error_kinds"].get("gate") is None  # failed commit recorded
    assert not metrics["first_build_passed"] and metrics["first_build_step"] == 3
    assert metrics["build_passed"] and metrics["first_pass_step"] == 5
    assert metrics["recovery_steps"] == 4
    assert metrics["param_errors"] == 1
    assert metrics["error_kinds"]["semantic"] == 1 and metrics["error_kinds"]["gate"] == 1


def test_unrecovered_run_reports_none_steps_and_failure_kinds():
    metrics = run_metrics(events(
        model(1), tool(1, "ir_commit", gate=False),
        model(2), tool(2, "ir_commit", gate=False),
    ), summary(state="failed", delivered=False))
    assert not metrics["first_build_passed"]
    assert not metrics["build_passed"] and metrics["first_pass_step"] is None
    assert metrics["recovery_steps"] is None
    assert not metrics["complete_delivery"]


def test_design_degradation_is_read_from_the_recorded_plan():
    degraded = run_metrics(events(
        tool(1, "ir_plan"), tool(2, "ir_commit", gate=True,
                                 content="DESIGN INTENT CHECK ... ⚠ USER-REQUIRED PARTS DEGRADED: backrest")),
        summary())
    assert degraded["key_design_sacrificed"] == "yes"
    planned = run_metrics(events(
        tool(1, "ir_plan"), tool(2, "ir_commit", gate=True, content="DESIGN INTENT CHECK — kept: base")),
        summary())
    assert planned["key_design_sacrificed"] == "no"


def test_aggregate_rates_and_counter_breakdown():
    runs = [
        {"first_build_passed": True, "build_passed": True, "param_errors": 0, "error_kinds": {"schema": 0},
         "recovery_steps": 0, "complete_delivery": True, "draft_delivery": False,
         "key_design_sacrificed": "no", "state": "succeeded"},
        {"first_build_passed": False, "build_passed": True, "param_errors": 2, "error_kinds": {"schema": 2, "gate": 1},
         "recovery_steps": 3, "complete_delivery": True, "draft_delivery": False,
         "key_design_sacrificed": "yes", "state": "succeeded"},
        {"first_build_passed": False, "build_passed": False, "param_errors": 1, "error_kinds": {"schema": 1},
         "recovery_steps": None, "complete_delivery": False, "draft_delivery": True,
         "key_design_sacrificed": "unknown", "state": "draft"},
    ]
    agg = aggregate(runs)
    assert agg["first_build"] == {"count": 1, "total": 3, "rate": pytest.approx(0.3333, abs=1e-3)}
    assert agg["param_errors"] == {"total": 3, "mean": 1.0}
    assert agg["recovery_steps"]["mean"] == 1.5
    assert agg["recovery_steps"]["unrecovered_runs"] == 1
    assert agg["complete_delivery"]["count"] == 2
    assert agg["key_design_sacrificed"] == {"no": 1, "yes": 1, "unknown": 1}
    assert agg["error_kinds"] == {"schema": 3, "gate": 1}
    assert "comfort" in ACCEPTANCE_BOUNDARY.lower()


@pytest.mark.parametrize('first', [
    tool(1, 'ir_commit', gate=False),
    tool(1, 'ir_commit', ok=False, kind='compile'),
])
def test_later_pass_does_not_count_as_first_build_success(first):
    metrics = run_metrics(events(first, tool(2, 'ir_commit', gate=True)))
    assert not metrics['first_build_passed']
    assert metrics['build_passed'] and metrics['first_pass_step'] == 2
    assert aggregate([metrics])['recovery_steps']['unrecovered_runs'] == 0


def test_run_without_a_commit_is_not_a_successful_or_recovered_build():
    metrics = run_metrics(events(tool(1, 'cad_build_parts')))
    assert not metrics['first_build_passed'] and not metrics['build_passed']
    assert metrics['first_build_step'] is None and metrics['first_pass_step'] is None
    assert aggregate([metrics])['recovery_steps']['unrecovered_runs'] == 1


def test_events_file_parsing_skips_garbage(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"kind": "model", "step": 1}) + "\nnot json\n\n"
                    + json.dumps({"kind": "tool", "step": 1, "name": "x", "ok": True}) + "\n",
                    encoding="utf-8")
    assert [e["kind"] for e in load_events(path)] == ["model", "tool"]


def test_regression_cases_are_fixed_and_carry_independent_acceptance():
    cases = load_regression_cases(
        __import__("tools.evaluate_regression", fromlist=["DEFAULT_CASES"]).DEFAULT_CASES)["cases"]
    ids = [case["id"] for case in cases]
    assert {"chair", "bracket", "curved_shell", "mechanism"} <= set(ids)
    for case in cases:
        assert case["key_design"] and case["manual"]
        # The request states the design need; evaluation machinery never enters it.
        assert not any(word in case["request"] for word in ("验收", "评测", "自动检查", "key_design"))
        # Independent acceptance (comfort/strength) is deliberately NOT promised
        # in the request — it stays human review.
        assert all(item not in case["request"] for item in case["manual"])
    chair = next(case for case in cases if case["id"] == "chair")
    assert any("弧度" in item or "腰部支撑" in item for item in chair["key_design"])
    assert any("舒适" in item for item in chair["manual"])
