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


# ══════════════════════════════════════════════════════════════════════════
# no ceiling: None means unlimited, and it must not trip
#
# Every limit is optional. A `None` cap has to be *skipped*, not compared
# against — `steps >= None` is a TypeError, and coming out of the step loop it
# would surface as a mystifying FAILED rather than as a configuration mistake.
# ══════════════════════════════════════════════════════════════════════════


def test_defaults_impose_no_ceiling():
    """The shipped default is "no work ceiling". If this ever changes, the UI
    badge and the docs must change with it."""
    limits = BudgetLimits()
    assert limits.bounded() == []
    assert len(limits.unlimited()) == 5
    assert "unbounded" in limits.describe()


def test_unlimited_budget_never_trips():
    clock = [0.0]
    b = Budget(BudgetLimits(), clock=lambda: clock[0])
    for i in range(1000):
        clock[0] = float(i * 10)  # way past any plausible wall clock
        b.check_step()            # never raises
        b.add_tokens(1_000_000, 1_000_000)
        b.check_step_timeout(0.0)
    assert b.steps == 1000, "steps must still be counted without a step cap"
    assert b.total_tokens() == 2_000_000_000, "tokens must still be counted"
    assert b.elapsed() > 0


def test_a_single_set_limit_trips_while_the_rest_stay_open():
    """Dimensions are independent. Setting one must not silently bound the others."""
    b = Budget(BudgetLimits(max_steps_per_turn=2))
    b.check_step()
    b.check_step()
    with pytest.raises(BudgetExhausted) as exc:
        b.check_step()
    assert exc.value.limit == "max_steps_per_turn"

    open_budget = Budget(BudgetLimits(max_steps_per_turn=2))
    open_budget.add_tokens(10**9, 10**9)          # no token cap
    open_budget.check_step_timeout(0.0)           # no step-timeout cap
    assert open_budget.limits.is_unlimited("max_tokens_per_turn")


def test_compile_retry_ceiling_is_optional_and_does_not_compare_against_none():
    unlimited = Budget(BudgetLimits())
    assert unlimited.allow_more_compile_retries(10**6) is True

    bounded = Budget(BudgetLimits(max_compile_retries=3))
    assert bounded.allow_more_compile_retries(3) is True
    assert bounded.allow_more_compile_retries(4) is False


def test_zero_is_a_real_ceiling_not_a_synonym_for_unlimited():
    """The whole reason `None` is used: 0 must mean 0, not "off"."""
    b = Budget(BudgetLimits(max_steps_per_turn=0))
    with pytest.raises(BudgetExhausted):
        b.check_step()
    assert b.limits.is_unlimited("max_steps_per_turn") is False
    assert "max_steps_per_turn" in b.limits.bounded()


def test_snapshot_reports_caps_alongside_usage():
    snap = Budget(BudgetLimits(max_steps_per_turn=5)).snapshot()
    assert snap["steps"] == 0
    assert snap["max_steps_per_turn"] == 5
    assert snap["max_tokens_per_turn"] is None, "unset caps must read as null, not 0"


def test_unknown_limit_name_is_rejected_by_name():
    with pytest.raises(KeyError, match="max_stepz"):
        BudgetLimits().is_unlimited("max_stepz")
