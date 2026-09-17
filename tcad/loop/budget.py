"""Loop budget — the three safeties.

All limit logic lives here (design §4.1 item 3). Business code never decides
whether the turn is exhausted; it only asks :class:`Budget` and reacts to
:class:`BudgetExhausted`.

The single success condition is a green Gate, *not* budget survival — but the
budget is what stops a turn from running forever when the model cannot reach a
green Gate. When the budget trips the turn becomes ``EXHAUSTED``, which is
explicitly **not** success (§4.1 decision table).
"""

from __future__ import annotations

import time
from typing import Callable

from pydantic import BaseModel, Field


class BudgetLimits(BaseModel):
    """The three safeties (plus the compile-retry ceiling).

    Every value is a hard cap. ``max_steps_per_turn`` bounds the number of
    LLM+tool steps; ``max_tokens_per_turn`` bounds total token spend;
    ``step_timeout_s`` / ``turn_wall_clock_s`` bound wall-clock time per step
    and per turn; ``max_compile_retries`` bounds consecutive compile failures
    before the turn is declared ``FAILED``.
    """

    max_steps_per_turn: int = 24
    max_tokens_per_turn: int = 180_000
    step_timeout_s: float = 120.0
    turn_wall_clock_s: float = 600.0
    max_compile_retries: int = 3


class BudgetExhausted(Exception):
    """Raised when any single budget limit is tripped.

    Carries *which* limit tripped (``limit``) and the value that was exceeded
    (``value``) so the engine can build a precise ``EXHAUSTED`` result and so
    tests can assert the exact trip cause.
    """

    def __init__(self, limit: str, value: float | int):
        self.limit = limit
        self.value = value
        super().__init__(f"budget exhausted: {limit} (limit={value})")


class Budget:
    """Per-turn counter for the three safeties.

    ``clock`` is injectable so tests can drive wall-clock without sleeping.
    """

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] | None = None,
        start_time: float | None = None,
    ):
        self.limits = limits
        self._clock = clock or time.monotonic
        self._start = start_time if start_time is not None else self._clock()
        self.steps = 0
        self.tokens_in = 0
        self.tokens_out = 0

    # ── public surface (the only methods business code should call) ──────

    def elapsed(self) -> float:
        """Seconds since the turn started (wall clock)."""
        return self._clock() - self._start

    def check_step(self) -> None:
        """Call once at the top of every step.

        Trips on the wall-clock cap first, then on the step cap. On success it
        increments the step counter.
        """
        if self.elapsed() > self.limits.turn_wall_clock_s:
            raise BudgetExhausted("turn_wall_clock_s", self.limits.turn_wall_clock_s)
        if self.steps >= self.limits.max_steps_per_turn:
            raise BudgetExhausted("max_steps_per_turn", self.limits.max_steps_per_turn)
        self.steps += 1

    def add_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Record token usage; trips when the per-turn ceiling is exceeded."""
        self.tokens_in += int(prompt_tokens)
        self.tokens_out += int(completion_tokens)
        if self.tokens_in + self.tokens_out > self.limits.max_tokens_per_turn:
            raise BudgetExhausted("max_tokens_per_turn", self.limits.max_tokens_per_turn)

    def check_step_timeout(self, step_start: float) -> None:
        """Call after a step's tool work to enforce the per-step wall clock."""
        if self._clock() - step_start > self.limits.step_timeout_s:
            raise BudgetExhausted("step_timeout_s", self.limits.step_timeout_s)
