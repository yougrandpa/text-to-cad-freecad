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

from tcad.core.types import CheckContext, CheckResult, Confidence, Severity
from tcad.ir.schema import ConstraintExpr, ConstraintKind
from tcad.verify.checks_solid import VerifyConfig
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

    def run(self, ctx: CheckContext) -> CheckResult:
        ok, info = evaluate(self.expr, ctx.digest, ctx.ir)
        feature_id = self.expr.target if self.expr.target else None
        src = self.expr.source_text or self.expr.kind

        if ok is None:
            return CheckResult(
                check_id=self.id, status="skip",
                severity=self.severity, confidence=self.confidence,
                message=f"requirement '{src}' could not be measured",
                feature_id=feature_id,
            )

        if not self.expr.confirmed:
            # Advisory only — never blocks the build.
            if ok:
                return CheckResult(
                    check_id=self.id, status="pass",
                    severity=Severity.ADVISORY, confidence=self.confidence,
                    message=f"unconfirmed requirement '{src}' satisfied (advisory)",
                    measurements=info.get("measured", {}), expected=info.get("expected"),
                    feature_id=feature_id,
                )
            return CheckResult(
                check_id=self.id, status="fail",
                severity=Severity.ADVISORY, confidence=self.confidence,
                message=f"unconfirmed requirement '{src}' NOT met — advisory only, "
                        f"does not block the build",
                measurements=info.get("measured", {}), expected=info.get("expected"),
                feature_id=feature_id,
            )

        if ok:
            return CheckResult(
                check_id=self.id, status="pass",
                severity=self.severity, confidence=self.confidence,
                message=f"requirement '{src}' satisfied",
                measurements=info.get("measured", {}), expected=info.get("expected"),
                feature_id=feature_id,
            )
        return CheckResult(
            check_id=self.id, status="fail",
            severity=self.severity, confidence=self.confidence,
            message=f"requirement '{src}' NOT met",
            measurements=info.get("measured", {}), expected=info.get("expected"),
            feature_id=feature_id,
        )


def build_spec_checks(ir, config: VerifyConfig | None = None) -> list[SpecCheck]:
    """One ``SpecCheck`` per ``ConstraintExpr`` of a handled kind.

    Both confirmed and unconfirmed expressions get a check; the ``confirmed``
    flag only governs whether the result is allowed to block (see ``SpecCheck``).
    """
    config = config or VerifyConfig()
    checks: list[SpecCheck] = []
    idx = 0
    for expr in ir.requirements.constraints:
        if expr.kind in HANDLED_KINDS:
            checks.append(SpecCheck(expr, idx, config))
            idx += 1
    return checks
