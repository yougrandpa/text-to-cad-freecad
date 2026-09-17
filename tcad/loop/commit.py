"""The commit pipeline (design §4.1, §9).

Exact order:
    validate IR -> pre_commit hook -> compile (worker) -> export artefacts
    -> persist digest -> build CheckContext FROM DISK -> Gate.evaluate
    -> on_gate_result hook

Key invariants:
  * The Gate reads artifacts from disk (CQRS) — it never sees the loop's
    in-memory IR object. That is what makes "the generator cannot grade its own
    paper" structural, not a discipline (design §4.6).
  * A compile failure is converted into a *structured* ToolError naming the
    feature_id and including measurements, so the model can actually repair it.
    No raw traceback is ever shown to the model.
  * ir_commit itself never declares success. The returned ToolResult carries the
    GateReport; the engine decides SUCCEEDED from ``GateReport.passed``.
"""

from __future__ import annotations

import os

from tcad.core.types import (
    GateReport,
    GeometryDigest,
    HookDecision,
    HookEvent,
    Mesh,
    ToolError,
    ToolErrorKind,
    ToolResult,
)
from tcad.tools.base import ToolOutcome
from tcad.worker.protocol import M_COMPILE_IR, M_EXPORT, M_INTROSPECT

from tcad.tools.ir_tools import _err, _ok  # shared helpers


def _ekind(kind: str | None) -> ToolErrorKind:
    if kind is None:
        return ToolErrorKind.RUNTIME
    try:
        return ToolErrorKind(kind)
    except ValueError:
        return ToolErrorKind.RUNTIME


def _worker_error(payload: dict | None) -> ToolResult:
    e = payload or {}
    return _err(
        _ekind(e.get("kind")),
        e.get("message", "worker returned an error"),
        feature_id=e.get("feature_id"),
        hint="Repair the named feature and re-patch, then re-commit.",
    )


def _format_report(report: GateReport) -> str:
    """Render the gate report so the model can act on it."""
    if report.passed:
        return (
            f"GATE PASSED (ir_version={report.ir_version}). "
            f"{len(report.results)} checks run, 0 blocking failures, "
            f"{len(report.advisory_findings)} advisory finding(s). Build succeeded — you may stop."
        )
    lines = [f"GATE FAILED (ir_version={report.ir_version}). {len(report.blocking_failures)} blocking failure(s):"]
    for r in report.results:
        if r.severity.value == "blocking" and r.status.value in ("fail", "error"):
            loc = f" (feature_id={r.feature_id})" if r.feature_id else ""
            meas = ""
            if r.measurements:
                meas = " measured=" + ", ".join(f"{k}={v}" for k, v in r.measurements.items())
            lines.append(f"  - [{r.check_id}{loc}] {r.message}{meas}")
    if report.skipped_checks:
        lines.append(f"skipped checks: {', '.join(report.skipped_checks)}")
    if report.advisory_findings:
        lines.append(f"{len(report.advisory_findings)} advisory finding(s):")
        for a in report.advisory_findings:
            lines.append(f"  - {a}")
    lines.append("Repair the blocking failures (see feature_id above), then call ir_patch + ir_commit again.")
    return "\n".join(lines)


async def run_commit(
    services: "Any",
    model_id: str,
    ir_version: int,
    message: str,
    workdir: str,
    data_dir: str,
) -> tuple[ToolResult, GateReport | None]:
    """Run the commit pipeline. Returns (tool_result, gate_report|None).

    ``gate_report`` is None only when the pipeline could not reach the Gate
    (pre-commit denial, IR-invalid, compile failure, worker unreachable).
    """
    artifact_dir = os.path.join(data_dir, "artifacts", model_id, f"v{ir_version}")
    os.makedirs(artifact_dir, exist_ok=True)

    # 1. validate IR (post-patch document). Reject early with a structured error.
    ir = services.store.load(model_id, ir_version)
    doc_errors = services.store.validate_document(ir)
    if doc_errors:
        e0 = doc_errors[0]
        return (
            _err(e0.kind, f"IR validation failed: {e0.message}", feature_id=e0.feature_id, hint=e0.hint),
            None,
        )

    # 2. pre_commit hook (spec consistency hard-check).
    pre = services.hooks.dispatch(
        HookEvent.PRE_COMMIT, {"model_id": model_id, "ir_version": ir_version, "message": message}
    )
    if pre.decision == HookDecision.DENY:
        return (_err(ToolErrorKind.DENIED, pre.reason or "pre_commit denied"), None)

    # 3. compile in the worker (FreeCAD — the only place that touches FreeCAD).
    try:
        comp = services.worker.request(
            M_COMPILE_IR, {"ir": ir.model_dump(), "out_dir": artifact_dir}, timeout_s=120.0
        )
    except Exception as e:  # worker process down / transport error
        return (_err(ToolErrorKind.RUNTIME, f"worker unreachable during compile: {e}"), None)
    if not comp.get("ok"):
        return (_worker_error(comp.get("error")), None)

    # 4. export artefacts (step/stl/brep/fcstd).
    try:
        exp = services.worker.request(
            M_EXPORT,
            {"ir": ir.model_dump(), "exports": ["step", "stl"], "name": model_id, "out_dir": artifact_dir},
            timeout_s=120.0,
        )
        files = (exp.get("result") or {}).get("files", {}) if exp.get("ok") else {}
    except Exception:
        files = {}

    # 5. persist digest (best-effort; digest is advisory, never blocks the gate).
    try:
        dig = services.worker.request(
            M_INTROSPECT,
            {"ir": ir.model_dump(), "out_dir": artifact_dir, "measure": True},
            timeout_s=60.0,
        )
        if dig.get("ok"):
            digest = GeometryDigest.model_validate(dig.get("result"))
            services.store.persist_digest(model_id, ir_version, digest)
    except Exception:
        pass

    # 6/7. Gate.evaluate builds CheckContext FROM DISK and grades independently.
    report = services.gate.evaluate(model_id, ir_version)

    # 8. on_gate_result hook (notify / badcase回流).
    services.hooks.dispatch(
        HookEvent.ON_GATE_RESULT,
        {"model_id": model_id, "ir_version": ir_version, "passed": report.passed},
    )

    result = _ok(_format_report(report))
    return (result, report)
