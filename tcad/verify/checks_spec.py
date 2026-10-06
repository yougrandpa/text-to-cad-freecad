"""Second-tier checks (design §4.6 item 5): one ``SpecCheck`` per confirmed
``ConstraintExpr`` of the kinds the first tier does not already own.

Division of labour (documented for team-lead and worker-smith):
  * FIRST tier (``checks_solid.py``) owns the geometry-vs-spec checks for
    ``bbox`` (BBoxSpecCheck) and ``volume`` (MassSpecCheck), plus the
    pure-geometry checks. It also owns the approximate ``wall_thickness``
    offset heuristic. To avoid double-counting a single failure, this module
    deliberately does NOT re-check those three kinds.
  * THIS module owns the remaining confirmed constraint kinds:
    ``count``, ``hole_diameter``, ``hole_position``, ``symmetric``,
    ``feature_count``.

Load-bearing rule (design §4.6 item 5): a ``confirmed=False`` expression MUST
NOT produce a blocking result. We still evaluate it and, if it would fail, emit
it as **advisory only** (severity forced to ADVISORY) so the model sees the
gap without it ever failing the build. Unconfirmed + unmeasurable -> SKIP.

Per-kind confidence (design §4.6 item 3): ``symmetric`` is approximate
(structural heuristic) -> advisory; the rest are deterministic -> blocking when
confirmed.
"""

from __future__ import annotations

from tcad.core.types import CheckContext, CheckResult, CheckStatus, Confidence, Severity
from tcad.ir.schema import ConstraintExpr, ConstraintKind
from tcad.verify.checks_solid import VerifyConfig, _as_dict
from tcad.verify.specexpr import evaluate

# Kinds the first tier already owns — never re-checked here (avoid double FAIL).
FIRST_TIER_OWNED: frozenset[str] = frozenset({"bbox", "volume", "wall_thickness"})

APPROXIMATE_KINDS: frozenset[str] = frozenset({"symmetric"})

HANDLED_KINDS: tuple[str, ...] = tuple(
    k for k in ConstraintKind.__args__ if k not in FIRST_TIER_OWNED
)


class SpecCheck:
    """A single machine-checkable requirement turned into a gate check."""

    def __init__(self, expr: ConstraintExpr, index: int, config: VerifyConfig | None = None):
        self.expr = expr
        self.config = config or VerifyConfig()
        target = expr.target or "all"
        self.id = f"spec_{expr.kind}_{target}_{index}"
        # Unconfirmed expressions can NEVER block (design §4.6 item 5).
        if not expr.confirmed:
            self.severity = Severity.ADVISORY
            self.confidence = Confidence.APPROXIMATE if expr.kind in APPROXIMATE_KINDS else Confidence.DETERMINISTIC
        else:
            self.severity = (
                Severity.ADVISORY if expr.kind in APPROXIMATE_KINDS else Severity.BLOCKING
            )
            self.confidence = (
                Confidence.APPROXIMATE if expr.kind in APPROXIMATE_KINDS else Confidence.DETERMINISTIC
            )

    def _result(self, status, message: str, info: dict, feature_id) -> CheckResult:
        """Build the result **through the same coercion the first tier uses**.

        ``specexpr.evaluate`` returns ``measured``/``expected`` as whatever shape
        is natural for the kind — a bare scalar for ``count``/``feature_count``/
        ``wall_thickness``, a ``{holeName: [x, y, z]}`` map for
        ``hole_position``, a ``{counterpart: <id>}`` map for ``symmetric``.
        ``CheckResult.measurements`` is ``dict[str, float | str | bool]``, so
        those raw shapes are not constructible: the ones that happened to be
        dicts were stringified into the report, and the scalars *raised* — which
        the Gate records as an ERROR carrying a pydantic traceback, losing the
        verdict entirely.

        The four kinds that raised were ``count``, ``feature_count``,
        ``symmetric`` and ``wall_thickness`` — i.e. recording the user's "there
        should be one solid" or "the wall is 3 mm" as a confirmed requirement
        made the build unpassable and the reason unreadable. ``_as_dict`` is the
        one place that knows how to fit a measurement into the field type; using
        it here is what keeps "a confirmed requirement can always produce a
        verdict" true in the second tier as well.
        """
        return CheckResult(
            check_id=self.id, status=status,
            severity=self.severity, confidence=self.confidence,
            message=message,
            measurements=_as_dict(info.get("measured")) or {},
            expected=_as_dict(info.get("expected"), floats_only=True),
            feature_id=feature_id,
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        ok, info = evaluate(self.expr, ctx.digest, ctx.ir)
        feature_id = self.expr.target if self.expr.target else None
        src = self.expr.source_text or self.expr.kind

        if ok is None:
            reason = info.get("reason")
            detail = f" ({reason})" if reason else ""
            if self.expr.confirmed and self.severity is Severity.BLOCKING:
                # A confirmed requirement the harness cannot verify is NOT a
                # ignorable SKIP: the build cannot be called accepted while the
                # thing the user asked for sits unmeasured.
                return self._result(
                    "error",
                    f"required_but_unverified: confirmed requirement "
                    f"'{src}' has no measurement to judge it by{detail}",
                    info, feature_id,
                )
            return self._result(
                "skip", f"requirement '{src}' could not be measured{detail}",
                info, feature_id,
            )

        if not self.expr.confirmed:
            # Advisory only — never blocks the build.
            if ok:
                return self._result(
                    "pass",
                    f"unconfirmed requirement '{src}' satisfied (advisory)",
                    info, feature_id,
                )
            return self._result(
                "fail",
                f"unconfirmed requirement '{src}' NOT met — advisory only, "
                f"does not block the build",
                info, feature_id,
            )

        if ok:
            return self._result("pass", f"requirement '{src}' satisfied", info, feature_id)
        return self._result("fail", f"requirement '{src}' NOT met", info, feature_id)


class RequirementCoverageCheck:
    """Was anything at all checked *against the request*?

    A green Gate with no confirmed requirement is not evidence that the part is
    right — it is evidence that the geometry is self-consistent (one solid,
    exportable, STEP round-trip). Those are different claims, and the difference
    is exactly what the person who asked for the part cares about.

    Observed live: asked to put arms and legs on a cube, a model built two
    calibration probes, recorded no requirement, and the turn ended green. Every
    check the Gate could run had passed; there was simply nothing to check the
    *request* against, and nothing said so.

    ADVISORY on purpose: a prose-only draft can still be valid geometry and
    exportable. Production completion separately requires design_review against
    measured, user-sourced constraints. Without that evidence the turn is a
    draft pending acceptance, never verified functionality.
    """

    id = "requirement_coverage"
    severity = Severity.ADVISORY
    confidence = Confidence.DETERMINISTIC

    def run(self, ctx: CheckContext) -> CheckResult:
        requirements = getattr(ctx.ir, "requirements", None)
        constraints = list(getattr(requirements, "constraints", None) or [])
        confirmed = [c for c in constraints if getattr(c, "confirmed", False)]

        if confirmed:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                severity=self.severity,
                confidence=self.confidence,
                message=f"judged against {len(confirmed)} confirmed requirement(s)",
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            severity=self.severity,
            confidence=self.confidence,
            message=(
                "no confirmed requirement — only self-consistency was verified, "
                "NOT that the part matches the request. If the user supplied explicit "
                "measurable values, record them via update_requirement (confirmed=true). "
                "If none were supplied, no requirement write is needed: keep chosen dimensions "
                "unconfirmed, complete the design and call design_review as a draft. "
                "Never invent confirmed values to clear this advisory."
            ),
        )


def build_spec_checks(ir, config: VerifyConfig | None = None) -> list:
    """One ``SpecCheck`` per ``ConstraintExpr`` of a handled kind.

    Both confirmed and unconfirmed expressions get a check; the ``confirmed``
    flag only governs whether the result is allowed to block (see ``SpecCheck``).
    """
    config = config or VerifyConfig()
    checks: list = []
    idx = 0
    for expr in ir.requirements.constraints:
        if expr.kind in HANDLED_KINDS:
            checks.append(SpecCheck(expr, idx, config))
            idx += 1
    # Always report whether anything was judged against the request — including,
    # and especially, when the answer is "nothing".
    checks.append(RequirementCoverageCheck())
    return checks
