"""The Gate — the single, deterministic definition of "done" (design §4.1, §4.6).

The LLM saying "I'm finished" is NOT a termination condition. Only a green
``GateReport.passed`` is. This module assembles every registered check, runs
them against an independently-built :class:`CheckContext`, and aggregates the
results under explicit, fail-closed rules:

  * ``passed`` == no blocking FAIL and no blocking ERROR, AND not every blocking
    check was SKIPped (design §9: if the Gate cannot actually verify anything it
    must not masquerade as success — the loop turns that into EXHAUSTED).
  * A check that *raises* becomes ``status=ERROR`` carrying its own declared
    severity (fail-closed: a blocking check that errors blocks the build).
  * A check that *cannot run* returns ``SKIP`` and is listed in
    ``skipped_checks`` — silent skipping is forbidden (design §4.6 close).
  * ``feature_id`` is attached wherever the failure is attributable to a feature
    so the model knows precisely what to change.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from tcad.core.types import (
    CheckContext,
    CheckResult,
    CheckStatus,
    GateReport,
    Severity,
)
from tcad.verify.checks_solid import ALL_SOLID_CHECKS, VerifyConfig
from tcad.verify.checks_spec import build_spec_checks
from tcad.verify.context import ContextLoader


def _loader_accepts_artifact_dir(loader) -> bool:
    """Whether ``loader`` can be asked to grade a specific directory.

    A build grades its private staging directory, not the version's published
    artifacts (see ``tcad/store/artifacts.py``). Loaders written against the
    original two-argument contract must keep working, so the extra argument is
    passed only when the callable actually declares it.
    """
    try:
        params = inspect.signature(loader).parameters
    except (TypeError, ValueError):
        return False
    if "artifact_dir" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class Gate:
    def __init__(
        self,
        context_loader: ContextLoader,
        config: VerifyConfig | None = None,
        solid_checks: list | None = None,
    ):
        """``context_loader`` is ``(model_id, ir_version[, artifact_dir]) -> CheckContext``
        and is the ONLY way the Gate obtains inputs — it must read from disk (see
        ``tcad.verify.context.build_check_context``)."""
        self._load = context_loader
        self.config = config or VerifyConfig()
        self._solid_checks = solid_checks
        self._loader_takes_dir = _loader_accepts_artifact_dir(context_loader)

    def evaluate_artifact(self, artifact_dir: str | Path) -> GateReport:
        """Grade a manifested attempt without consulting a current IR version.

        The version-addressed entry point remains for old stored builds and
        embedders. New commits use this artifact-only entry point.
        """
        from tcad.artifacts.manifest import ArtifactSet

        if not self._loader_takes_dir:
            raise TypeError("artifact evaluation requires an artifact-aware context loader")
        root = Path(artifact_dir)
        manifest = ArtifactSet.model_validate_json((root / "manifest.json").read_text(encoding="utf-8"))
        return self.evaluate(manifest.model_id, manifest.ir_version, artifact_dir=str(root))

    def evaluate(
        self, model_id: str, ir_version: int, *, artifact_dir: str | None = None
    ) -> GateReport:
        """Grade a version.

        ``artifact_dir`` grades a specific directory instead of the version's
        published one — how a build is judged *before* it is allowed to become
        the version's artifacts. When the injected loader cannot take it, the
        request is ignored rather than crashed on: an embedder with a simpler
        loader still gets a verdict.
        """
        if artifact_dir is not None and self._loader_takes_dir:
            ctx = self._load(model_id, ir_version, artifact_dir=artifact_dir)
        else:
            ctx = self._load(model_id, ir_version)
        checks = self._assemble(ctx)
        results = [self._safe_run(chk, ctx) for chk in checks]

        blocking = [r for r in results if r.severity == Severity.BLOCKING]
        has_blocking_fail = any(r.status == CheckStatus.FAIL for r in blocking)
        has_blocking_error = any(r.status == CheckStatus.ERROR for r in blocking)
        has_blocking_pass = any(r.status == CheckStatus.PASS for r in blocking)
        all_blocking_skipped = bool(blocking) and all(
            r.status == CheckStatus.SKIP for r in blocking
        )

        # "Cannot attest" is not "verified". Two independent ways the Gate can be
        # unable to say anything meaningful about the geometry:
        #   (a) no digest / the digest carries no measurements (design §4.6: a
        #       digest with measurements_available=False is structure-only), or
        #   (b) not a single blocking check actually PASSED — everything either
        #       skipped or was absent.
        # Both mean the honest answer is "not done", never a silent green. This is
        # the structural guard behind design §9 "the Gate must not degrade into the
        # model grading itself".
        no_measurements = ctx.digest is None or not ctx.digest.measurements_available
        cannot_attest = no_measurements or not has_blocking_pass

        passed = not (
            has_blocking_fail or has_blocking_error or all_blocking_skipped or cannot_attest
        )

        blocking_failures = [
            r.check_id for r in blocking if r.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]
        if cannot_attest:
            # A separate reason, reported alongside the failing checks rather
            # than only when nothing else failed. "The bbox is wrong" and "there
            # are no measurements to judge anything against" are both true of a
            # broken build, and dropping the second hides the more serious one.
            blocking_failures.append(
                "gate:cannot_attest_no_measurements"
                if no_measurements
                else "gate:cannot_attest_no_blocking_check_passed"
            )

        advisory_findings = [
            r.check_id for r in results
            if r.severity == Severity.ADVISORY and r.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]
        skipped_checks = [r.check_id for r in results if r.status == CheckStatus.SKIP]

        return GateReport(
            model_id=model_id,
            ir_version=ir_version,
            passed=passed,
            results=results,
            blocking_failures=blocking_failures,
            advisory_findings=advisory_findings,
            skipped_checks=skipped_checks,
            attempt_id=ctx.build_stamp.attempt_id if ctx.build_stamp else "",
            ir_sha256=ctx.build_stamp.ir_sha256 if ctx.build_stamp else "",
        )

    # ── internal ──────────────────────────────────────────────────────────

    def _assemble(self, ctx: CheckContext) -> list:
        if self._solid_checks is not None:
            solid = list(self._solid_checks)
        else:
            solid = [c(self.config) for c in ALL_SOLID_CHECKS]
        spec = build_spec_checks(ctx.ir, self.config)
        return solid + spec

    @staticmethod
    def _safe_run(check, ctx: CheckContext) -> CheckResult:
        try:
            res = check.run(ctx)
        except Exception as exc:  # any check raising -> ERROR, severity inherited
            return CheckResult(
                check_id=check.id,
                status=CheckStatus.ERROR,
                severity=check.severity,
                confidence=check.confidence,
                message=f"check raised {type(exc).__name__}: {exc}",
            )
        if not isinstance(res, CheckResult):
            return CheckResult(
                check_id=check.id,
                status=CheckStatus.ERROR,
                severity=check.severity,
                confidence=check.confidence,
                message="check returned a non-CheckResult value",
            )
        return res
