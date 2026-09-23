"""The previous Gate verdict, rendered for the model (task book §5-D).

"Last turn's error" is one of the blocks the model needs to repair without
starting over. The verdict is produced by the Gate and persisted beside the IR
snapshot (``tcad/store/artifacts.py``); this module only turns it back into text.

Two shapes are accepted because both occur in practice:

  * a live :class:`tcad.core.types.GateReport` during the turn that produced it,
  * the persisted JSON dict a later process reads back.

Rendering is deterministic and program-generated.
"""

from __future__ import annotations

from typing import Any

#: Blocking statuses, matching ``CheckStatus``. Compared as strings so this
#: module stays usable on the persisted dict form without importing pydantic
#: enums back out of JSON.
_BLOCKING_STATUSES = frozenset({"fail", "error"})


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value)
    return str(value)


def _get(report: Any, key: str, default: Any = None) -> Any:
    if isinstance(report, dict):
        return report.get(key, default)
    return getattr(report, key, default)


def _results(report: Any) -> list[Any]:
    results = _get(report, "results", None) or []
    return list(results)


def _severity(result: Any) -> str:
    sev = _get(result, "severity", "")
    return getattr(sev, "value", sev) or ""


def _status(result: Any) -> str:
    status = _get(result, "status", "")
    return getattr(status, "value", status) or ""


def render_verdict_text(report: Any) -> str:
    """Compact text description of the previous Gate result.

    Returns ``""`` for ``None`` — callers then simply omit the block rather than
    telling the model about a run that never happened.
    """
    if report is None:
        return ""

    passed = bool(_get(report, "passed", False))
    version = _get(report, "ir_version", None)
    header = "# Previous Gate result"
    if version is not None:
        header += f" (ir_version={version})"

    if passed:
        return (
            f"{header}\npassed: true — the last attempt satisfied every blocking "
            "check. Any change since then invalidates it; re-run ir_commit."
        )

    out = [header, "passed: false"]
    failures = list(_get(report, "blocking_failures", None) or [])
    if failures:
        out.append("blocking_failures: " + ", ".join(str(f) for f in failures))

    # The per-check detail is what makes a repair possible: a bare check id says
    # *that* it failed, not what the measured and expected values were.
    for r in _results(report):
        if _severity(r) != "blocking" or _status(r) not in _BLOCKING_STATUSES:
            continue
        check_id = _get(r, "check_id", "?")
        feature_id = _get(r, "feature_id", None)
        loc = f" feature_id={feature_id}" if feature_id else ""
        out.append(f"  - [{check_id}]{loc} {_get(r, 'message', '') or ''}")
        measured = _get(r, "measurements", None) or {}
        expected = _get(r, "expected", None) or {}
        if measured:
            out.append("      measured: " + _fmt(measured))
        if expected:
            out.append("      expected: " + _fmt(expected))

    advisory = list(_get(report, "advisory_findings", None) or [])
    if advisory:
        out.append("advisory_findings (do not block): " + ", ".join(str(a) for a in advisory))
    skipped = list(_get(report, "skipped_checks", None) or [])
    if skipped:
        out.append("skipped_checks (not verified): " + ", ".join(str(s) for s in skipped))

    out.append(
        "Repair the blocking failure(s) named above with ir_patch, then call ir_commit again."
    )
    return "\n".join(out)
