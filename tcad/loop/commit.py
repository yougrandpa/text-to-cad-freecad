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


def _confirmed_constraints(ir: Any) -> list:
    """The requirements the Gate is allowed to judge against.

    Only ``confirmed=True`` expressions may block, so anything else is not
    evidence that the part matches what the user asked for.
    """
    requirements = getattr(ir, "requirements", None)
    constraints = getattr(requirements, "constraints", None) or []
    return [c for c in constraints if getattr(c, "confirmed", False)]


def _format_report(report: GateReport, ir: Any = None) -> str:
    """Render the gate report so the model can act on it."""
    if report.passed:
        text = (
            f"GATE PASSED (ir_version={report.ir_version}). "
            f"{len(report.results)} checks run, 0 blocking failures, "
            f"{len(report.advisory_findings)} advisory finding(s)."
        )
        if ir is None:
            return text + " Build succeeded — you may stop."

        judged = len(_confirmed_constraints(ir))
        if judged:
            return (
                text
                + f" Judged against {judged} confirmed requirement(s)."
                " Build succeeded — you may stop."
            )

        # A green Gate with nothing to judge against is the most misleading
        # result this harness can produce: it reads as "your part is correct"
        # while proving only that the geometry is self-consistent.
        #
        # Observed live. Asked to put arms and legs on a cube, a model built two
        # calibration probes, never recorded what the user had asked for, and was
        # told "Build succeeded — you may stop." The Gate had run every check it
        # could; there was simply nothing to check the *request* against. Say so.
        return (
            text
            + " ⚠ NOT JUDGED AGAINST ANY REQUEST: no confirmed requirement was"
            " recorded, so the Gate verified only that the geometry is"
            " self-consistent (one solid, exportable, STEP round-trip). It has NOT"
            " verified that the part matches what was asked for. If the user named"
            " any size, count or position, record it with update_requirement"
            ' ("confirmed": true) and commit again — otherwise nothing checks it.'
        )
    lines = [f"GATE FAILED (ir_version={report.ir_version}). {len(report.blocking_failures)} blocking failure(s):"]
    for r in report.results:
        if r.severity.value == "blocking" and r.status.value in ("fail", "error"):
            loc = f" (feature_id={r.feature_id})" if r.feature_id else ""
            # Show the delta, not just the measurement. A bare "measured=value=32000"
            # (the key is literally "value" because a scalar measurement is wrapped)
            # told the model what happened but not what was wanted, so it had to
            # guess the direction and magnitude of the repair. Emitting
            # measured vs expected vs the miss is what makes one iteration enough.
            parts: list[str] = []
            measured = r.measurements or {}
            expected = r.expected or {}
            if measured:
                parts.append("measured=" + ", ".join(f"{k}={_num(v)}" for k, v in measured.items()))
            if expected:
                parts.append("expected=" + ", ".join(f"{k}={_num(v)}" for k, v in expected.items()))
            if measured and expected:
                miss = " ".join(
                    f"{k}: off by {_num(_delta(measured.get(k), expected.get(k)))}"
                    for k in expected
                    if _delta(measured.get(k), expected.get(k)) is not None
                )
                if miss:
                    parts.append(f"({miss})")
            lines.append(f"  - [{r.check_id}{loc}] {r.message}")
            if parts:
                lines.append("      " + "; ".join(parts))
    if report.skipped_checks:
        lines.append(f"skipped checks: {', '.join(report.skipped_checks)}")
    if report.advisory_findings:
        lines.append(f"{len(report.advisory_findings)} advisory finding(s):")
        for a in report.advisory_findings:
            lines.append(f"  - {a}")
    lines.append("Repair the blocking failures (see feature_id above), then call ir_patch + ir_commit again.")
    return "\n".join(lines)


def _num(value) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.6g}"
    return str(value)


def _delta(measured, expected) -> float | None:
    """Signed miss, or None when the pair is not numeric-comparable.

    Values are matched by key, and the spec checks happen to use matching keys for
    the simple cases (volume vs volume). Where they do not match, we simply omit the
    delta rather than inventing a pairing.
    """
    if isinstance(measured, bool) or isinstance(expected, bool):
        return None
    if isinstance(measured, (int, float)) and isinstance(expected, (int, float)):
        return float(measured) - float(expected)
    return None


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

    # Steps 4/5 failures used to be swallowed (`except: pass`, and an unchecked
    # inner `ok`). That turned a real problem — a sketch that will not solve, a
    # solid that will not export — into a Gate that could only report "cannot
    # attest", with no hint of the cause. The model then had nothing to repair.
    # Collect the causes and tell it.
    pipeline_notes: list[str] = []

    # Honour the configured export list when a full Config is wired; fall back to
    # the design's default otherwise (a bare service bundle in a unit test).
    storage_cfg = getattr(getattr(services, "config", None), "storage", None)
    export_formats = list(getattr(storage_cfg, "artifact_exports", None) or ["step", "stl"])

    # 4. export artefacts (step/stl/brep/fcstd).
    try:
        exp = services.worker.request(
            M_EXPORT,
            {
                "ir": ir.model_dump(),
                "exports": list(export_formats),
                "name": model_id,
                "out_dir": artifact_dir,
            },
            timeout_s=120.0,
        )
        if not exp.get("ok"):
            pipeline_notes.append(f"artefact export failed: {_describe(exp)}")
    except Exception as exc:  # noqa: BLE001
        pipeline_notes.append(f"artefact export raised: {type(exc).__name__}: {exc}")

    # 5. persist digest (advisory for the Gate's *measurements*, but its absence
    #    is exactly why the Gate would otherwise say "no measurements available").
    try:
        dig = services.worker.request(
            M_INTROSPECT,
            {"ir": ir.model_dump(), "out_dir": artifact_dir, "measure": True},
            timeout_s=60.0,
        )
        if dig.get("ok"):
            digest = GeometryDigest.model_validate(dig.get("result"))
            services.store.persist_digest(model_id, ir_version, digest)
        else:
            pipeline_notes.append(f"geometry measurement failed: {_describe(dig)}")
    except Exception as exc:  # noqa: BLE001
        pipeline_notes.append(f"geometry measurement raised: {type(exc).__name__}: {exc}")

    # 6/7. Gate.evaluate builds CheckContext FROM DISK and grades independently.
    report = services.gate.evaluate(model_id, ir_version)

    # 8. on_gate_result hook (notify / badcase回流).
    services.hooks.dispatch(
        HookEvent.ON_GATE_RESULT,
        {"model_id": model_id, "ir_version": ir_version, "passed": report.passed},
    )

    text = _format_report(report, ir)
    if pipeline_notes:
        text += "\n\nUpstream failures that hid the geometry from the Gate:\n" + "\n".join(
            f"  - {n}" for n in pipeline_notes
        )
    return (_ok(text), report)


def _describe(env: dict) -> str:
    """Readable one-liner from a worker error envelope."""
    err = env.get("error") or {}
    if not isinstance(err, dict):
        return str(err)
    msg = err.get("message") or "unknown failure"
    fid = err.get("feature_id")
    kind = err.get("kind")
    out = f"{msg}"
    if kind:
        out = f"[{kind}] {out}"
    if fid:
        out += f" (feature_id={fid})"
    return out
