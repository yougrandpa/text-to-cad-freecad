"""Tests for the budget safeties (design §4.1 item 3)."""

from __future__ import annotations

import pytest

from tcad.loop.budget import Budget, BudgetExhausted, BudgetLimits


def test_max_steps_exhausted_carries_limit_and_value():
    b = Budget(BudgetLimits(max_steps_per_turn=3))
    for _ in range(3):
        b.check_step()  # ok for steps 1..3
    with pytest.raises(BudgetExhausted) as exc:
        b.check_step()  # 4th trips
    assert exc.value.limit == "max_steps_per_turn"
    assert exc.value.value == 3
    assert "max_steps_per_turn" in str(exc.value)


def test_tokens_exhausted_carries_limit_and_value():
    b = Budget(BudgetLimits(max_tokens_per_turn=100))
    b.add_tokens(60, 30)  # 90 ok
    with pytest.raises(BudgetExhausted) as exc:
        b.add_tokens(20, 0)  # 110 > 100
    assert exc.value.limit == "max_tokens_per_turn"
    assert exc.value.value == 100


def test_wall_clock_exhausted():
    clock = [0.0]
    b = Budget(BudgetLimits(turn_wall_clock_s=5.0), clock=lambda: clock[0])
    clock[0] = 6.0
    with pytest.raises(BudgetExhausted) as exc:
        b.check_step()
    assert exc.value.limit == "turn_wall_clock_s"
    assert exc.value.value == 5.0


def test_step_timeout_exhausted():
    clock = [0.0]
    b = Budget(BudgetLimits(step_timeout_s=2.0), clock=lambda: clock[0])
    clock[0] = 3.0
    with pytest.raises(BudgetExhausted) as exc:
        b.check_step_timeout(0.0)
    assert exc.value.limit == "step_timeout_s"


def test_elapsed_returns_float_and_counts():
    clock = [1.0]
    b = Budget(BudgetLimits(), clock=lambda: clock[0])
    assert isinstance(b.elapsed(), float)
    b.check_step()
    assert b.steps == 1
    assert b.tokens_in == 0 and b.tokens_out == 0
