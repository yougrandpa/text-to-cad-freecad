"""The commit pipeline (design §4.1, §9).

Exact order:
    validate IR -> pre_commit hook -> open a private staging directory
    -> stamp it -> compile (worker) -> export artefacts -> persist digest
    -> build CheckContext FROM DISK (the staging dir) -> Gate.evaluate
    -> publish to the version directory only if the Gate passed
    -> on_gate_result hook

Key invariants:
  * The Gate reads artifacts from disk (CQRS) — it never sees the loop's
    in-memory IR object. That is what makes "the generator cannot grade its own
    paper" structural, not a discipline (design §4.6).
  * A build is written to a private per-attempt directory and becomes the
    version's artifacts only after it passes. The Gate therefore grades a
    directory containing *only* this attempt's files: a leftover from a failed
    attempt cannot answer for a missing export, because it is not in the
    directory being graded at all. A failed attempt leaves the last verified
    build exactly where it was.
  * Every artefact directory says which attempt wrote it before anything in it
    is graded, so a retry cannot be certified by the previous attempt's files.
    The stamp travels with the build through the publish.
  * A compile failure is converted into a *structured* ToolError naming the
    feature_id and including measurements, so the model can actually repair it.
    No raw traceback is ever shown to the model.
  * ir_commit itself never declares success. The returned ToolResult carries the
    GateReport; the engine decides SUCCEEDED from ``GateReport.passed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
import uuid

from tcad.core.types import (
    BuildStamp,
    GateReport,
    GeometryDigest,
    HookDecision,
    HookEvent,
    Mesh,
    ToolError,
    ToolErrorKind,
    ToolResult,
)
from tcad.store.artifacts import write_build_stamp
from tcad.tools.base import ToolOutcome
from tcad.worker.protocol import M_COMPILE_IR, M_EXPORT, M_INTROSPECT

from tcad.tools.ir_tools import _err, _ok  # shared helpers

log = logging.getLogger("tcad.commit")


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


async def _worker_call(
    services: "Any", method: str, params: dict, *, timeout_s: float
) -> dict:
    """A worker RPC that never runs on the event loop.

    ``Worker.request`` is synchronous by contract (``tcad/tools/base.py``) while
    ``run_commit`` is awaited. Compiling can take minutes; on the loop that
    freezes every other session in the process and leaves the turn with no
    cancellation point. To a worker thread it goes.

    Cancelling the awaiting task does not stop the thread — nothing can pull it
    back out of a blocking read — so the worker call is ended the only way it can
    be: ``abort_inflight`` kills the process, the reader thread sees EOF and the
    caller wakes with a cancellation instead of the answer. Without this a stop
    only stopped the *waiting*: the build kept grinding inside OCCT, holding the
    worker, and the next turn queued behind it.
    """
    return await _off_loop(
        services, services.worker.request, method, params, timeout_s=timeout_s,
        label=method,
    )


async def _off_loop(
    services: "Any", fn, /, *args, label: str, **kwargs
) -> "Any":
    """Run a blocking call that drives the worker; end it if we are cancelled.

    Cancelling a task that is awaiting ``to_thread`` raises in the coroutine
    immediately (the thread keeps going) — so this is the one place that can
    notice "the build is still running and nobody wants it any more".
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except asyncio.CancelledError:
        _abort_worker(services, label)
        raise


def _abort_worker(services: "Any", label: str) -> None:
    """End the worker call this cancellation left running. Never raises.

    Duck-typed rather than required by the ``Services.worker`` Protocol: the
    pipeline is also driven by minimal test bundles whose worker is a plain
    object, and those have no process to kill.
    """
    abort = getattr(getattr(services, "worker", None), "abort_inflight", None)
    if not callable(abort):
        return
    try:
        aborted = abort(f"turn stopped during {label}")
        log.warning("aborted the running worker call (%s): %s", label,
                    "process killed" if aborted else "nothing was running")
    except Exception as exc:  # noqa: BLE001 — a stop must not fail on cleanup
        log.warning("could not abort the worker call (%s): %s", label, exc)


async def run_commit(
    services: "Any",
    model_id: str,
    ir_version: int,
    message: str,
    workdir: str,
    data_dir: str,
    hooks: "Any" = None,
) -> tuple[ToolResult, GateReport | None]:
    """Run the commit pipeline. Returns (tool_result, gate_report|None).

    ``hooks`` is the per-turn dispatcher the caller's tool call came through.
    It defaults to ``services.hooks`` for direct callers. Passing it matters for
    the same reason it matters everywhere else: a front end must be able to
    observe a commit's ``pre_commit`` / ``on_gate_result`` events without
    swapping a bundle that other concurrent turns are using.

    ``gate_report`` is None only when the pipeline could not reach the Gate
    (pre-commit denial, IR-invalid, compile failure, worker unreachable).
    """
    final_dir = os.path.join(data_dir, "artifacts", model_id, f"v{ir_version}")

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
    #
    # ASK is handled exactly like DENY here, and that is the point: a commit is
    # *the* write. "A human should look at this before it is built" cannot mean
    # "build it anyway and mention it afterwards" — the artefacts, the exported
    # files and the Gate verdict would all already exist. Before this, only DENY
    # was checked, so a pre_commit ASK fell through to the build.
    dispatch = hooks if hooks is not None else getattr(services, "hooks", None)
    pre = dispatch.dispatch(
        HookEvent.PRE_COMMIT, {"model_id": model_id, "ir_version": ir_version, "message": message}
    )
    if pre.decision == HookDecision.ASK:
        return (
            _err(ToolErrorKind.DENIED,
                 f"pre_commit requires approval before this build runs: "
                 f"{pre.reason or 'no reason given'} (hook '{pre.hook_name}'). "
                 f"Nothing was compiled, nothing was written and the Gate was not "
                 f"run — approve and re-commit.",
                 hint="this is a suspension, not a build failure"),
            None,
        )
    if pre.decision == HookDecision.DENY:
        return (_err(ToolErrorKind.DENIED, pre.reason or "pre_commit denied"), None)

    # 2b. Open a private staging directory and stamp it with *this* attempt
    # before anything is written. The version directory is reused across retries,
    # so a build must not be able to read — or be graded against — a file a
    # previous attempt left there. Staging makes that structural: the directory
    # the Gate sees contains only what this attempt wrote.
    #
    # A store that cannot stage (a minimal test bundle) keeps the older
    # behaviour, writing straight into the version directory; the stamp and the
    # provenance check still apply.
    attempt_id = f"{model_id}-v{ir_version}-{uuid.uuid4().hex[:8]}"
    staging_dir = _open_staging(services, model_id, ir_version, attempt_id)
    artifact_dir = staging_dir or final_dir
    os.makedirs(artifact_dir, exist_ok=True)

    stamp = BuildStamp(
        attempt_id=attempt_id,
        model_id=model_id,
        ir_version=ir_version,
        started_at=time.time(),
        ir_sha256=hashlib.sha256(ir.model_dump_json().encode("utf-8")).hexdigest(),
    )
    write_build_stamp(artifact_dir, stamp)

    try:
        return await _build_and_grade(
            services,
            ir=ir,
            model_id=model_id,
            ir_version=ir_version,
            artifact_dir=artifact_dir,
            staging_dir=staging_dir,
            attempt_id=attempt_id,
            stamp=stamp,
            dispatch=dispatch,
        )
    except asyncio.CancelledError:
        # Someone stopped this turn. Only a full pass turns staging into the
        # version's artifacts, so an abandoned attempt is pure residue — and it
        # would accumulate one directory per stopped build. Discard it here, in
        # the one place that knows this attempt owns it.
        if staging_dir:
            _discard_staging(services, staging_dir)
        raise


async def _build_and_grade(
    services: "Any",
    *,
    ir,
    model_id: str,
    ir_version: int,
    artifact_dir: str,
    staging_dir: str | None,
    attempt_id: str,
    stamp: BuildStamp,
    dispatch: "Any",
) -> tuple[ToolResult, GateReport | None]:
    """Steps 3-8 of the commit pipeline (module docstring has the invariants)."""
    # 3. compile in the worker (FreeCAD — the only place that touches FreeCAD).
    try:
        comp = await _worker_call(
            services, M_COMPILE_IR,
            {"ir": ir.model_dump(), "out_dir": artifact_dir},
            timeout_s=120.0,
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
        exp = await _worker_call(
            services,
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
        dig = await _worker_call(
            services, M_INTROSPECT,
            {"ir": ir.model_dump(), "out_dir": artifact_dir, "measure": True},
            timeout_s=60.0,
        )
        if dig.get("ok"):
            digest = GeometryDigest.model_validate(dig.get("result"))
            _persist_digest(services, model_id, ir_version, digest, artifact_dir)
        else:
            pipeline_notes.append(f"geometry measurement failed: {_describe(dig)}")
    except Exception as exc:  # noqa: BLE001
        pipeline_notes.append(f"geometry measurement raised: {type(exc).__name__}: {exc}")

    # 6/7. Gate.evaluate builds CheckContext FROM DISK and grades independently.
    # It also drives the worker (round_trip re-imports the STEP), so it goes off
    # the loop for the same reason the compile steps above do.
    #
    # With staging, the Gate is pointed at *this attempt's* directory. That is
    # what makes "old files must not fill in for a failed export" structural
    # rather than a timestamp heuristic.
    if staging_dir:
        report = await _off_loop(
            services, services.gate.evaluate, model_id, ir_version,
            artifact_dir=staging_dir, label="gate.evaluate",
        )
    else:
        report = await _off_loop(
            services, services.gate.evaluate, model_id, ir_version, label="gate.evaluate"
        )

    # 7b. Publish — only a fully verified build becomes the version's artifacts.
    # A failed attempt is discarded, leaving the last verified build in place, so
    # "recover the last good version" is simply "do nothing".
    published = False
    if staging_dir:
        if report.passed:
            published = _publish(services, model_id, ir_version, staging_dir, attempt_id, stamp)
            if not published:
                # The Gate passed but the result is not the version's artifacts.
                # Saying nothing here would let a green report imply a delivery
                # that does not exist.
                pipeline_notes.append(
                    "verified build could not be published to the version "
                    "directory; the previous verified artifacts are still in place"
                )
        else:
            _discard_staging(services, staging_dir)

    # 7c. Persist the verdict beside the IR snapshot. The *next* turn may run in
    # a different process (the server builds a fresh engine per request), so this
    # is the only way "what did the last attempt fail on?" survives to be put in
    # front of the model. Best-effort: losing this record degrades context, it
    # does not change the verdict.
    _persist_gate_report(services, model_id, ir_version, report)

    # 8. on_gate_result hook (notify / badcase回流).
    dispatch.dispatch(
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


def _persist_gate_report(services: "Any", model_id: str, ir_version: int, report: GateReport) -> None:
    """Store the verdict for the *next* turn, tolerating a minimal fake store.

    The whole report is written, not just ``passed``: the next turn's context
    renders the blocking failure messages, and those live in ``results``. The
    store keeps it beside the IR snapshots (not in the artifact directory, whose
    file set the Gate audits).
    """
    writer = getattr(services.store, "write_gate_report", None)
    if not callable(writer):
        return
    try:
        writer(model_id, ir_version, report)
    except Exception:  # noqa: BLE001 — context bookkeeping must not fail a build
        pass


def _open_staging(
    services: "Any", model_id: str, ir_version: int, attempt_id: str
) -> str | None:
    """A private build directory for this attempt, or ``None`` if unsupported."""
    fn = getattr(services.store, "staging_dir", None)
    if not callable(fn):
        return None
    try:
        return str(fn(model_id, ir_version, attempt_id))
    except Exception:  # noqa: BLE001 — fall back to the version directory
        return None


def _persist_digest(
    services: "Any", model_id: str, ir_version: int, digest: GeometryDigest, artifact_dir: str
) -> None:
    """Write the digest into the directory being graded.

    The store's own signature may or may not accept ``artifact_dir``; a store
    that does not gets the original call, which writes to the version directory.
    """
    fn = getattr(services.store, "persist_digest", None)
    if not callable(fn):
        return
    try:
        accepts_dir = "artifact_dir" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts_dir = False
    if accepts_dir:
        fn(model_id, ir_version, digest, artifact_dir=artifact_dir)
    else:
        fn(model_id, ir_version, digest)


def _discard_staging(services: "Any", staging_dir: str) -> None:
    fn = getattr(services.store, "discard_staging", None)
    if callable(fn):
        try:
            fn(staging_dir)
            return
        except Exception:  # noqa: BLE001
            pass
    import shutil

    shutil.rmtree(staging_dir, ignore_errors=True)


def _publish(
    services: "Any",
    model_id: str,
    ir_version: int,
    staging_dir: str,
    attempt_id: str,
    stamp: BuildStamp,
) -> bool:
    """Write the artifact list and promote the verified build. Never raises.

    Returns whether the version directory now holds this build. A failure to
    publish is reported to the model as an upstream failure rather than silently
    producing a green Gate over artifacts that are not actually the version's.
    """
    try:
        manifest = getattr(services.store, "write_manifest", None)
        if callable(manifest):
            manifest(
                staging_dir,
                model_id=model_id,
                version=ir_version,
                attempt_id=attempt_id,
                ir_sha256=stamp.ir_sha256,
            )
        publisher = getattr(services.store, "publish", None)
        if not callable(publisher):
            # No publish capability: the "staging" dir was already promoted by
            # the store's own convention (or there is no staging at all).
            return True
        publisher(model_id, ir_version, staging_dir)
        return True
    except Exception:  # noqa: BLE001 — surfaced as a pipeline note, never a crash
        _discard_staging(services, staging_dir)
        return False
