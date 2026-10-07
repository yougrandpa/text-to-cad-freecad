"""Fixed-set regression metrics for small-model runs (task P2-6).

The same code version, prompt and configuration are run repeatedly against a
fixed task set; these functions turn one run's event log into the numbers the
review asks for, and the task set's runs into rates:

  * first-build success — did the FIRST ``ir_commit`` pass the Gate?
  * parameter-error count — schema-level rejections (the class a smaller model
    produces most), with the other error kinds broken out;
  * recovery steps — how much work it took to get from the first failure to
    the first passing build;
  * complete delivery — Gate passed, review passed, artifacts served;
  * whether key design was sacrificed — design degradations recorded through
    the part plan (``tcad.loop.intent``); when no plan was recorded the honest
    answer is "unknown", not "no".

Human comfort and load-bearing performance are NOT metrics here — they stay
independent acceptance items and the report says so.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

#: Event-log phrases that record a design degradation. They come from the
#: intent machinery (commit notes and the completion review), never from
#: heuristic guessing about geometry.
DEGRADATION_MARKERS = ("USER-REQUIRED PARTS DEGRADED", "设计退化")
#: Presence of this phrase means a part plan was recorded, so an absence of
#: degradations is meaningful rather than unknown.
PLAN_MARKERS = ("DESIGN INTENT CHECK",)
#: Error kinds that count as PARAMETER errors (what the user named). Everything
#: else is reported separately, never folded into this number.
PARAM_ERROR_KINDS = frozenset({"schema"})


def load_events(path) -> list[dict]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        import json
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _tool_failed(event: dict) -> bool:
    if not event.get("ok"):
        return True
    gate = event.get("gate")
    return bool(gate) and not gate.get("passed")


def run_metrics(events: list[dict], summary: dict | None = None) -> dict:
    """One run's regression metrics, from its event log (+ optional summary)."""
    tools = [e for e in events if e.get("kind") == "tool"]
    commits = [e for e in tools if e.get("name") == "ir_commit"]
    passing = [e for e in commits if e.get("ok") and (e.get("gate") or {}).get("passed")]

    failures = [e for e in tools if _tool_failed(e)]
    first_failure_step = min((e.get("step", 0) or 0) for e in failures) if failures else None
    first_pass_step = passing[0].get("step") if passing else None

    kinds = Counter()
    for event in failures:
        kind = ((event.get("error") or {}).get("kind")) or (
            "gate" if event.get("name") == "ir_commit" else "unknown")
        kinds[kind] += 1

    if first_pass_step is None or first_failure_step is None \
            or first_failure_step >= first_pass_step:
        recovery_steps = 0 if first_pass_step is not None or first_failure_step is None else None
    else:
        recovery_steps = int(first_pass_step) - int(first_failure_step)

    contents = "\n".join(str(e.get("content") or "") for e in tools)
    if any(marker in contents for marker in DEGRADATION_MARKERS):
        sacrificed = "yes"
    elif any(marker in contents for marker in PLAN_MARKERS):
        sacrificed = "no"
    else:
        sacrificed = "unknown"

    metrics = {
        "first_build_passed": bool(commits and commits[0].get("ok")
                                   and (commits[0].get("gate") or {}).get("passed")),
        "first_build_step": commits[0].get("step") if commits else None,
        "build_passed": bool(passing),
        "first_pass_step": first_pass_step,
        "param_errors": int(kinds.get("schema", 0)),
        "error_kinds": dict(kinds),
        "recovery_steps": recovery_steps,
        "key_design_sacrificed": sacrificed,
        "commits": len(commits),
    }
    if summary is not None:
        state = summary.get("state")
        delivered = bool(summary.get("delivery_passed"))
        metrics.update({
            "state": state,
            "delivery_passed": delivered,
            "complete_delivery": bool(delivered and state == "succeeded"),
            "draft_delivery": bool(delivered and state == "draft"),
            "steps": summary.get("steps"),
            "turn_error": summary.get("turn_error"),
        })
    return metrics


def _rate(count: int, total: int):
    return {"count": count, "total": total, "rate": round(count / total, 4) if total else None}


def aggregate(runs: list[dict]) -> dict:
    """The fixed-set rates across repeated runs of one task."""
    total = len(runs)
    recovery = [r["recovery_steps"] for r in runs if r.get("recovery_steps") is not None]
    kinds = Counter()
    for run in runs:
        kinds.update(run.get("error_kinds") or {})
    sacrificed = Counter(run.get("key_design_sacrificed", "unknown") for run in runs)
    states = Counter(run.get("state") for run in runs if run.get("state"))
    return {
        "runs": total,
        "first_build": _rate(sum(1 for r in runs if r["first_build_passed"]), total),
        "param_errors": {
            "total": sum(r.get("param_errors", 0) for r in runs),
            "mean": round(sum(r.get("param_errors", 0) for r in runs) / total, 3) if total else None,
        },
        "error_kinds": dict(kinds),
        "recovery_steps": {
            "mean": round(sum(recovery) / len(recovery), 3) if recovery else None,
            "unrecovered_runs": sum(1 for r in runs if not r.get("build_passed")),
        },
        "complete_delivery": _rate(sum(1 for r in runs if r.get("complete_delivery")), total),
        "draft_delivery": _rate(sum(1 for r in runs if r.get("draft_delivery")), total),
        "states": dict(states),
        "key_design_sacrificed": dict(sacrificed),
    }


ACCEPTANCE_BOUNDARY = (
    "Automated metrics cover pipeline behaviour only. Human comfort, ergonomic "
    "fit, load-bearing performance, fatigue and production readiness remain "
    "independent acceptance items for every task."
)
