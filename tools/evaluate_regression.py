#!/usr/bin/env python3
"""Fixed small-model regression evaluation (task P2-6).

Freeze this runner + the task set + the model-facing contract, then repeat each
task N times under the SAME code version, prompt and configuration. The report
records, per task and overall:

  * first-build success rate (did the FIRST ir_commit pass?)
  * parameter-error count (schema rejections, the small-model failure class)
  * recovery steps (first failure -> first passing build)
  * complete delivery rate (verified, published, served artifacts)

Design degradation ("was key design sacrificed?") is read from the recorded
part plan (tcad.loop.intent): yes / no / unknown. Comfort and load-bearing
performance are NOT scored here and remain independent acceptance items.

--freeze and --check make no provider calls. --run uses the currently saved
provider configuration and requires explicit authorization; credentials are
never written to the output.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcad.agent.evaluation import FrozenContractError, contract_snapshot, fingerprint, require_frozen_contract
from tcad.agent.regression import ACCEPTANCE_BOUNDARY, aggregate, load_events, run_metrics

DEFAULT_CASES = REPO_ROOT / "review" / "regression" / "cases.json"
DEFAULT_BASELINE = REPO_ROOT / "review" / "regression" / "baseline.json"


def load_cases(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    ids = []
    for case in cases:
        cid = case.get("id", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", cid) or cid in ids:
            raise ValueError("Regression case IDs must be unique safe directory names.")
        if not case.get("request", "").strip():
            raise ValueError("Each regression case needs a non-empty request.")
        if not case.get("key_design") or not case.get("manual"):
            raise ValueError("Each case must declare key_design and manual acceptance items.")
        ids.append(cid)
    if len(cases) < 4:
        raise ValueError("The fixed regression set must keep at least four tasks.")
    return data


def runner_fingerprint() -> str:
    paths = [Path(__file__), REPO_ROOT / "tools/run_model_e2e.py",
             REPO_ROOT / "tcad/agent/regression.py"]
    return fingerprint({str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in paths})


def freeze(path: Path, cases: dict, *, repeats: int, max_steps: int, timeout: float) -> dict:
    from tools.evaluate_generalization import configured_services

    with configured_services() as (services, _):
        baseline = {"version": 1, "contract": contract_snapshot(services),
                    "cases_sha256": fingerprint(cases), "runner_sha256": runner_fingerprint(),
                    "limits": {"repeats": repeats, "max_steps": max_steps, "timeout": timeout}}
    baseline["baseline_sha256"] = fingerprint(baseline)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(baseline, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return baseline


def validate_baseline(baseline: dict, cases: dict, services) -> None:
    if baseline.get("baseline_sha256") != fingerprint({k: v for k, v in baseline.items()
                                                        if k != "baseline_sha256"}):
        raise FrozenContractError("Frozen regression baseline was modified; create a new version.")
    if baseline.get("version") != 1 or baseline.get("cases_sha256") != fingerprint(cases):
        raise FrozenContractError("Regression tasks changed; create a new baseline.")
    if baseline.get("runner_sha256") != runner_fingerprint():
        raise FrozenContractError("Regression runner changed; create a new baseline.")
    require_frozen_contract(services, baseline["contract"])


async def evaluate(args, cases: dict, baseline: dict) -> int:
    from tools.evaluate_generalization import configured_services, public_settings
    from tools.run_model_e2e import run

    selected = set(args.case or [case["id"] for case in cases["cases"]])
    if selected - {case["id"] for case in cases["cases"]}:
        raise ValueError("Unknown regression case requested.")
    with configured_services() as (services, settings):
        validate_baseline(baseline, cases, services)
        if settings.llm.needs_key() and not settings.llm.resolved_api_key():
            raise ValueError("The selected provider requires explicitly configured credentials.")
    settings = settings.model_copy(deep=True)
    root = Path(args.output_dir or (REPO_ROOT / ".tcad_eval" /
                                    ("regression-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")))).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    limits = baseline["limits"]
    repeats = limits["repeats"]
    report = {"contract_sha256": baseline["contract"]["contract_sha256"],
              "cases_sha256": baseline["cases_sha256"], "settings": public_settings(settings),
              "limits": limits, "acceptance_boundary": ACCEPTANCE_BOUNDARY, "cases": []}
    for case in cases["cases"]:
        if case["id"] not in selected:
            continue
        runs = []
        for index in range(repeats):
            run_dir = root / case["id"] / f"run-{index + 1}"
            run_args = SimpleNamespace(request=case["request"], request_file=None,
                                       output_dir=str(run_dir),
                                       max_steps=limits["max_steps"], timeout=limits["timeout"],
                                       require_animation=False)
            try:
                await run(run_args, settings_override=settings,
                          contract_check=lambda svc: validate_baseline(baseline, cases, svc))
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                metrics = run_metrics(load_events(run_dir / "events.jsonl"), summary)
            except FrozenContractError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                if runner_fingerprint() != baseline["runner_sha256"]:
                    raise ValueError("Regression code changed during the run; results cannot be pooled.") from exc
                secret = settings.llm.resolved_api_key()
                message = str(exc).replace(secret, "[REDACTED]") if secret else str(exc)
                metrics = {"run_error": message, "first_build_passed": False, "build_passed": False,
                           "key_design_sacrificed": "unknown"}
            metrics["run_index"] = index + 1
            runs.append(metrics)
            print("case:", case["id"], "run:", index + 1, "->",
                  {k: metrics.get(k) for k in ("first_build_passed", "param_errors",
                                               "recovery_steps", "state")}, flush=True)
        entry = {"id": case["id"], "key_design": case["key_design"],
                 "manual_review_required": case["manual"], "automated": case.get("automated", {}),
                 "aggregate": aggregate(runs), "runs": runs}
        report["cases"].append(entry)
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print("report:", root / "report.json", flush=True)
    return 0


def render_markdown(report: dict) -> str:
    lines = ["# 小模型固定回归评测", "",
             f"- 契约: `{report['contract_sha256']}`",
             f"- 任务集: `{report['cases_sha256']}`",
             f"- 模型: {report['settings'].get('model')} · 重复次数: {report['limits']['repeats']}",
             f"- {report['acceptance_boundary']}", ""]
    header = "| 任务 | 首次构建成功 | 参数错误(均) | 恢复步数(均) | 完整交付 | 关键设计牺牲 |"
    lines += [header, "| --- | --- | --- | --- | --- | --- |"]
    for case in report["cases"]:
        agg = case["aggregate"]
        first = agg["first_build"]
        delivery = agg["complete_delivery"]
        sacrificed = agg["key_design_sacrificed"]
        lines.append(
            f"| {case['id']} | {first['count']}/{first['total']} | {agg['param_errors']['mean']} | "
            f"{agg['recovery_steps']['mean']} | {delivery['count']}/{delivery['total']} | "
            f"{', '.join(f'{k}:{v}' for k, v in sorted(sacrificed.items())) or 'none'} |")
    lines += ["", "## 人工验收（独立于自动指标）", ""]
    for case in report["cases"]:
        lines.append(f"- **{case['id']}**: " + "；".join(case["manual_review_required"]))
    lines += ["", "关键设计检查依据记录在案的部件计划（ir_plan）与设计退化记录；"
                  "未记录计划时记 unknown，需人工按 key_design 复核。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--freeze", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", action="append")
    parser.add_argument("--output-dir")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args(argv)
    if (args.repeats is not None and args.repeats < 1) or \
            (args.max_steps is not None and args.max_steps < 1) or \
            (args.timeout is not None and args.timeout <= 0):
        parser.error("Positive regression limits are required.")
    cases = load_cases(args.cases)
    if args.freeze:
        baseline = freeze(args.baseline, cases, repeats=args.repeats or 3,
                          max_steps=args.max_steps or 30, timeout=args.timeout or 600)
        print("frozen contract:", baseline["contract"]["contract_sha256"])
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if any(value is not None and value != baseline["limits"][name]
           for name, value in [("repeats", args.repeats), ("max_steps", args.max_steps),
                               ("timeout", args.timeout)]):
        parser.error("Limits differ from the frozen baseline.")
    if args.check:
        from tools.evaluate_generalization import configured_services
        with configured_services() as (services, _):
            validate_baseline(baseline, cases, services)
        print("frozen contract matches:", baseline["contract"]["contract_sha256"])
        return 0
    return asyncio.run(evaluate(args, cases, baseline))


if __name__ == "__main__":
    raise SystemExit(main())
