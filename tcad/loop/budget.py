"""Loop budget — the optional safeties.

All limit logic lives here (design §4.1 item 3). Business code never decides
whether the turn is exhausted; it only asks :class:`Budget` and reacts to
:class:`BudgetExhausted`.

The single success condition is a green Gate, *not* budget survival. The budget
exists only to stop a turn from spinning forever when the model cannot reach a
green Gate; when it trips the turn becomes ``EXHAUSTED``, which is explicitly
**not** success (§4.1 decision table).

``None`` means *no limit* for that dimension
--------------------------------------------

Every limit is optional and a value of ``None`` — not a magic large number —
means "do not cap this". That distinction is deliberate and load-bearing:

* ``0`` or ``-1`` or ``10**9`` as "unlimited" are conventions a future reader
  has to be told about, and they fail in opposite directions. A mistyped
  ``10**9`` silently caps; a mistyped ``0`` silently uncaps.
* ``None`` says the thing it means, and ``is None`` cannot be got wrong by
  arithmetic.

The defaults are all ``None``: this harness does not impose a work ceiling on a
turn. What still bounds a turn is *liveness*, which is a different concern and
is enforced elsewhere, independently of this module:

* a hung LLM call dies on the transport timeout (``llm.request_timeout_s``);
* a hung tool call dies on ``ToolSpec.timeout_s`` (:mod:`tcad.tools.base`);
* a dead worker dies on ``WorkerHandle.request_timeout_s``.

Those are per-request liveness guards. Removing the budget does **not** remove
them, and it should not: a cap on *how much work* is a policy choice, whereas a
cap on *how long a single request may hang* is what keeps the loop from wedging.

With no limits set, a turn ends only on a green Gate, ``FAILED``, or
``AWAITING_APPROVAL``. :meth:`Budget.unlimited` exists so callers can say that
out loud instead of implying a bounded run that is not bounded.
"""

from __future__ import annotations

import time
from typing import Callable

from pydantic import BaseModel

#: Names of the limit fields, in the order they are reported.
LIMIT_NAMES = (
    "max_steps_per_turn",
    "max_tokens_per_turn",
    "step_timeout_s",
    "turn_wall_clock_s",
    "max_compile_retries",
)


class BudgetLimits(BaseModel):
    """Work ceilings for a single turn. ``None`` on any field means no ceiling.

    ``max_steps_per_turn``    bounds the number of LLM+tool steps;
    ``max_tokens_per_turn``   bounds total token spend;
    ``step_timeout_s``        bounds wall-clock time for one step;
    ``turn_wall_clock_s``     bounds wall-clock time for the whole turn;
    ``max_compile_retries``   bounds consecutive compile failures before the
                              turn is declared ``FAILED``.

    All default to ``None`` (unbounded) — see the module docstring. A bounded
    profile is available as an overlay: ``configs/policies/strict.yaml``.
    """

    max_steps_per_turn: int | None = None
    max_tokens_per_turn: int | None = None
    step_timeout_s: float | None = None
    turn_wall_clock_s: float | None = None
    max_compile_retries: int | None = None

    def is_unlimited(self, name: str) -> bool:
        """True when *name* carries no ceiling. Unknown names raise."""
        if name not in LIMIT_NAMES:
            raise KeyError(f"unknown budget limit {name!r}; known: {', '.join(LIMIT_NAMES)}")
        return getattr(self, name) is None

    def unlimited(self) -> list[str]:
        """The limits that are switched off, in :data:`LIMIT_NAMES` order."""
        return [n for n in LIMIT_NAMES if getattr(self, n) is None]

    def bounded(self) -> list[str]:
        """The limits that are in force, in :data:`LIMIT_NAMES` order."""
        return [n for n in LIMIT_NAMES if getattr(self, n) is not None]

    def describe(self) -> str:
        """One-line, human-readable summary. Used by logs and ``/health``."""
        if not self.bounded():
            return "unbounded (no step, token, time or compile-retry ceiling)"
        parts = [f"{n}={getattr(self, n)}" for n in self.bounded()]
        return ", ".join(parts)


class BudgetExhausted(Exception):
    """Raised when a budget limit is tripped.

    Carries *which* limit tripped (``limit``) and the value that was exceeded
    (``value``) so the engine can build a precise ``EXHAUSTED`` result and so
    tests can assert the exact trip cause.

    Never raised for an unlimited dimension — there is nothing to trip.
    """

    def __init__(self, limit: str, value: float | int):
        self.limit = limit
        self.value = value
        super().__init__(f"budget exhausted: {limit} (limit={value})")


class Budget:
    """Per-turn counters.

    Counters are *always* maintained, whether or not a matching limit is set:
    step and token counts are also the turn's observable telemetry, and a
    counter that only advances when someone is watching would make the reported
    numbers depend on configuration. The limits only decide when to trip.

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

        Trips on the wall-clock cap first, then on the step cap. Always
        increments the step counter, so a turn that is allowed to run is still
        counted.
        """
        cap = self.limits.turn_wall_clock_s
        if cap is not None and self.elapsed() > cap:
            raise BudgetExhausted("turn_wall_clock_s", cap)
        cap = self.limits.max_steps_per_turn
        if cap is not None and self.steps >= cap:
            raise BudgetExhausted("max_steps_per_turn", cap)
        self.steps += 1

    def add_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Record token usage; trips when the per-turn ceiling is set and passed."""
        self.tokens_in += int(prompt_tokens)
        self.tokens_out += int(completion_tokens)
        cap = self.limits.max_tokens_per_turn
        if cap is not None and self.tokens_in + self.tokens_out > cap:
            raise BudgetExhausted("max_tokens_per_turn", cap)

    def check_step_timeout(self, step_start: float) -> None:
        """Call after a step's work to enforce the per-step wall clock, if set."""
        cap = self.limits.step_timeout_s
        if cap is not None and self._clock() - step_start > cap:
            raise BudgetExhausted("step_timeout_s", cap)

    def allow_more_compile_retries(self, failures: int) -> bool:
        """True when *failures* consecutive compile failures are still within budget.

        Lives here so the engine never compares against a ``None`` cap by
        accident — ``n > None`` is a ``TypeError``, and a ``TypeError`` raised
        from the middle of the step loop would surface as a mystifying
        ``FAILED`` rather than as configuration.

        The count itself stays with the engine: it is per-turn loop state, not a
        budget counter, and ``Budget`` should not hold state it never updates.
        """
        cap = self.limits.max_compile_retries
        return cap is None or failures <= cap

    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def snapshot(self) -> dict[str, float | int | None]:
        """Usage plus the caps in force, for telemetry and the UI."""
        return {
            "steps": self.steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "elapsed_s": round(self.elapsed(), 3),
            **{n: getattr(self.limits, n) for n in LIMIT_NAMES},
        }
