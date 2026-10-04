"""Execution strategies (design §4.1 decision 1).

  M1 loop_until_done : the default main path — step loop until a green Gate.
  M2 fork_join        : records candidate attempts; current-version verification
                        remains the authority (no historical candidate restore).
  M3 adversarial       : an isolated critic step after the main loop; advisory
                        output only — never changes the success decision.

A strategy raising must degrade to M1 and never kill the turn (the engine wraps
strategy.run in a fallback). All three share the engine's invariant: a Turn only
SUCCEEDS with a current passed build and, in production, a validated design review.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tcad.core.types import GateReport, TurnState
from tcad.loop.budget import BudgetExhausted


def score_candidate(report: GateReport) -> tuple[int, int]:
    """Lower is better: (blocking_failures, advisory_findings)."""
    return (len(report.blocking_failures), len(report.advisory_findings))


def best_candidate(reports: list[GateReport]) -> GateReport | None:
    if not reports:
        return None
    return min(reports, key=score_candidate)


@runtime_checkable
class Strategy(Protocol):
    async def run(
        self, engine: Any, turn: Any, messages: list[dict], allowed: set
    ) -> Any: ...


async def _drive(engine: Any, turn: Any, messages: list[dict], allowed: set) -> GateReport | None:
    """Shared main loop. Mutates ``turn.state`` on terminal; returns last report.

    Returns the most recent GateReport seen (even on a non-success terminal), so
    wrap-up strategies (M2/M3) can act on it.
    """
    last_report: GateReport | None = None
    try:
        while turn.state == TurnState.RUNNING:
            y = await engine._step(turn, messages, allowed)
            if y.gate_report is not None:
                last_report = y.gate_report
                if engine.gate_is_current(turn, y.gate_report):
                    turn.state = TurnState.SUCCEEDED
                    return last_report
            # if turn.state changed (ASK -> AWAITING_APPROVAL) the loop exits
    except BudgetExhausted as e:
        turn.state = TurnState.EXHAUSTED
        turn.error = str(e)
    except Exception as e:  # any unexpected failure -> FAILED, never crash the turn
        turn.state = TurnState.FAILED
        turn.error = f"{type(e).__name__}: {e}"
    return last_report


class LoopUntilDoneStrategy:
    """M1 — the default. Loop until a green Gate (or a hard terminal state)."""

    async def run(self, engine: Any, turn: Any, messages: list[dict], allowed: set) -> Any:
        last_report = await _drive(engine, turn, messages, allowed)
        return engine._finalize(turn, messages, last_report)


class ForkJoinStrategy:
    """M2 — fork & join.

    Runs the same main loop, but records every candidate GateReport the model
    produced for diagnostics. A candidate is not a branch checkout: no earlier
    report may be promoted without restoring its IR and verifying it again.
    Success therefore uses the same current-version predicate as M1.
    """

    def __init__(self, candidates: int = 3) -> None:
        self.candidates = candidates

    async def run(self, engine: Any, turn: Any, messages: list[dict], allowed: set) -> Any:
        last_report = await _drive(engine, turn, messages, allowed)
        if turn.state == TurnState.SUCCEEDED:
            return engine._finalize(turn, messages, last_report)
        # Scoring a historical candidate does not restore its IR/artifacts.
        # Only _drive's current-version Gate may succeed; in particular do not
        # turn FAILED/AWAITING_APPROVAL into success with an older candidate.
        return engine._finalize(turn, messages, last_report)


class AdversarialStrategy:
    """M3 — adversarial verification.

    Runs the main loop, then an isolated critic step (read-only, advisory only)
    that looks for issues the generator missed. The critic can NEVER flip the
    success decision — only a green Gate can.
    """

    async def run(self, engine: Any, turn: Any, messages: list[dict], allowed: set) -> Any:
        last_report = await _drive(engine, turn, messages, allowed)
        if last_report is not None and turn.state != TurnState.AWAITING_APPROVAL:
            await engine._critic_step(turn, messages, last_report)
        return engine._finalize(turn, messages, last_report)


def make_strategy(name: str, **kwargs: Any) -> Strategy:
    name = (name or "loop_until_done").lower()
    if name == "fork_join":
        return ForkJoinStrategy(candidates=kwargs.get("candidates", 3))
    if name == "adversarial":
        return AdversarialStrategy()
    return LoopUntilDoneStrategy()
