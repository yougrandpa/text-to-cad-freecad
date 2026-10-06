from types import SimpleNamespace

import pytest

from tcad.agent.evaluation import FrozenContractError, assess_case, contract_snapshot, fingerprint
from tcad.config.loader import load_default_config
from tcad.loop.engine import LoopConfig
from tools.evaluate_generalization import load_cases, validate_baseline


def services():
    return SimpleNamespace(config=load_default_config(),
                           loop_config=LoopConfig(require_design_review=True))


def baseline_for(svc, cases, runner):
    baseline = {"version": 1, "contract": contract_snapshot(svc),
                "cases_sha256": fingerprint(cases), "runner_sha256": runner,
                "limits": {"max_steps": 40, "timeout": 600}}
    return {**baseline, "baseline_sha256": fingerprint(baseline)}


def test_frozen_contract_rejects_prompt_task_and_limit_drift(monkeypatch):
    monkeypatch.setattr("tools.evaluate_generalization.runner_fingerprint", lambda: "runner")
    svc = services()
    cases = {"cases": [{"id": "unseen"}]}
    baseline = baseline_for(svc, cases, "runner")
    validate_baseline(baseline, cases, svc)
    svc.loop_config.system_prompt += " Extra instruction."
    with pytest.raises(FrozenContractError, match="Model-facing"):
        validate_baseline(baseline, cases, svc)
    with pytest.raises(FrozenContractError, match="tasks changed"):
        validate_baseline(baseline, {"cases": []}, services())
    baseline["limits"]["max_steps"] += 1
    with pytest.raises(FrozenContractError, match="baseline was modified"):
        validate_baseline(baseline, cases, services())


def test_production_prompt_is_task_neutral_and_keeps_completion_evidence():
    prompt = contract_snapshot(services())["system_prompt"].lower()
    assert all(word not in prompt for word in ("fuselage", "helicopter", "ferris", "tapered boom"))
    assert "design_review" in prompt and "do not prove functionality" in prompt


def test_artifact_checks_cannot_promote_model_claims_to_task_acceptance():
    case = {"id": "part", "role": "holdout", "automated": {"solids": 1, "volume": 20},
            "manual": ["Requested shape is present"]}
    summary = {"state": "succeeded", "delivery_passed": True, "current_build": True,
               "measurements": {"is_valid": True, "solids": 1, "volume": 20}}
    assessment = assess_case(case, summary)
    assert assessment["automated_passed"] and not assessment["task_verified"]
    assert assessment["manual_review_required"] == case["manual"]
    summary["current_build"] = False
    assert not assess_case(case, summary)["automated_passed"]
    summary["current_build"] = True
    summary["measurements"]["volume"] = float("nan")
    assert not assess_case(case, summary)["automated_passed"]
    assert not assess_case(case, {"state": "succeeded"})["automated_passed"]


def test_cases_separate_diagnostic_and_do_not_include_solver_guidance():
    from tools.evaluate_generalization import DEFAULT_CASES
    cases = load_cases(DEFAULT_CASES)["cases"]
    assert len([case for case in cases if case["role"] == "holdout"]) == 3
    assert next(case for case in cases if case["id"] == "helicopter")["role"] == "diagnostic"
    assert all("cad_build_parts" not in case["request"] for case in cases)
