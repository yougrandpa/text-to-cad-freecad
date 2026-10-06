"""Request compaction must preserve provider protocol and authoritative input."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from tcad.context.turn_compaction import (
    CompactionResult,
    ContextWindowExceeded,
    ToolProtocolError,
    compact_turn,
    estimate_request_tokens,
    input_budget,
)


def _prefix():
    return [
        {"role": "system", "content": "CAD assistant"},
        {"role": "system", "content": "REQUIREMENTS: four holes, diameter 8 mm"},
        {"role": "system", "content": "CURRENT CAD SNAPSHOT: version 12, body_1"},
        {"role": "system", "content": "LATEST GATE: failed, wall thickness"},
        {"role": "user", "content": "Change those holes to 8 mm; leave everything else unchanged."},
    ]


def _batch(number, size=20, count=1):
    calls = [
        {
            "id": f"call_{number}_{n}",
            "type": "function",
            "function": {
                "name": "ir_read",
                "arguments": json.dumps({"id": f"body_{number}_{n}", "detail": "all"}),
            },
        }
        for n in range(count)
    ]
    return [
        {
            "role": "assistant",
            "content": f"Inspect round {number}",
            "reasoning_content": f"Preserve provider reasoning for round {number} exactly.",
            "tool_calls": calls,
        },
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": "ir_read",
                "content": f"{number}:" + "x" * size,
            }
            for call in calls
        ],
    ]


def _compact(prefix, trace, *, budget=1000, tools=None, **kwargs):
    # With zero configured output, this integer window has exactly this input
    # budget after the mandatory 10% reserve (including rounding).
    window = (budget * 10 + 8) // 9
    assert input_budget(window, 0) == budget
    return compact_turn(
        prefix, trace, tools,
        window_tokens=window, max_output_tokens=0, **kwargs,
    )


def test_no_history_preserves_authoritative_prefix_without_marker():
    prefix = _prefix()
    result = _compact(prefix, [])
    assert isinstance(result, CompactionResult)
    assert result.messages == prefix
    assert result.dropped_batches == 0
    assert result.estimated_tokens == estimate_request_tokens(result.messages, None)
    assert result.estimated_tokens <= result.input_budget


def test_empty_context_is_a_bounded_nonzero_request_estimate():
    assert 0 < estimate_request_tokens([], []) < 100
    result = _compact([], [], budget=100)
    assert result.messages == []
    assert result.dropped_batches == 0


def test_everything_fitting_preserves_all_messages_verbatim():
    prefix = _prefix()
    trace = [*_batch(0), *_batch(1, count=3)]
    result = _compact(prefix, trace, budget=10_000)
    assert result.messages == [*prefix, *trace]
    assert result.dropped_batches == 0
    assert result.estimated_tokens == estimate_request_tokens(result.messages, [])


def test_oldest_whole_group_is_omitted_and_newest_two_are_preferred():
    prefix = _prefix()
    first, second, third = _batch(0, 8000), _batch(1), _batch(2)
    result = _compact(prefix, [*first, *second, *third], budget=1100)
    assert result.dropped_batches == 1
    assert result.messages[:len(prefix)] == prefix
    assert result.messages[len(prefix) + 1:] == [*second, *third]
    assert "call_0_0" not in json.dumps(result.messages)
    notice = result.messages[len(prefix)]
    assert notice["role"] == "system"
    assert "omitted" in notice["content"]
    assert "not a summary" in notice["content"]
    assert "authoritative" in notice["content"]
    assert "Read tools" in notice["content"]
    assert result.estimated_tokens == estimate_request_tokens(result.messages, None)
    assert result.estimated_tokens <= result.input_budget


def test_recent_preference_can_shrink_to_one_but_latest_batch_is_untouched():
    prefix = _prefix()
    latest = _batch(2, size=40)
    result = _compact(prefix, [*_batch(0, 8000), *_batch(1, 8000), *latest], budget=900)
    assert result.dropped_batches == 2
    assert result.messages[-len(latest):] == latest
    assert result.messages[-2]["reasoning_content"] == latest[0]["reasoning_content"]
    assert result.messages[-2]["tool_calls"][0]["function"]["arguments"] == latest[0]["tool_calls"][0]["function"]["arguments"]
    assert result.messages[-1]["content"] == latest[-1]["content"]


def test_multitool_batch_keeps_every_call_and_matching_result():
    latest = _batch(2, count=3)
    # Results are allowed to arrive in a different order than the calls.
    latest[1:] = reversed(latest[1:])
    result = _compact(_prefix(), [*_batch(0, 10_000, count=2), *latest], budget=1200)
    assert result.dropped_batches == 1
    assert result.messages[-len(latest):] == latest
    ids = {call["id"] for call in result.messages[-4]["tool_calls"]}
    assert ids == {message["tool_call_id"] for message in result.messages[-3:]}


def test_latest_tool_batch_and_following_narration_are_both_mandatory():
    latest = _batch(1)
    narration = {"role": "assistant", "content": "Checking whether another read is needed."}
    result = _compact(_prefix(), [*_batch(0, 8000), *latest, narration], budget=1000)
    assert result.messages[-3:] == [*latest, narration]
    # A tiny budget cannot drop the latest tool round just to retain narration.
    with pytest.raises(ContextWindowExceeded):
        _compact(_prefix(), [*_batch(0, 8000), *latest, narration], budget=500)


def test_standalone_assistant_narration_can_be_compacted_as_whole_batches():
    trace = [
        {"role": "assistant", "content": "old narration " * 2000},
        {"role": "assistant", "content": "latest narration", "reasoning_content": "retain"},
    ]
    result = _compact(_prefix(), trace, budget=800, keep_recent_batches=0)
    assert result.dropped_batches == 1
    assert result.messages[-1] == trace[-1]


def test_requirements_current_user_and_all_prefix_history_are_never_shortened():
    prefix = _prefix()
    prefix.insert(-1, {"role": "assistant", "content": "Prior design decision: 15 mm walls."})
    result = _compact(prefix, [*_batch(0, 8000), *_batch(1)], budget=900)
    assert result.messages[:len(prefix)] == prefix
    assert sum(message == prefix[-1] for message in result.messages) == 1
    too_big = deepcopy(prefix)
    too_big[-1]["content"] += "This requirement must survive. " * 1000
    with pytest.raises(ContextWindowExceeded):
        _compact(too_big, [*_batch(0, 8000), *_batch(1)], budget=900)


def test_latest_batch_overflow_is_explicit_numerical_and_actionable():
    with pytest.raises(ContextWindowExceeded) as exc:
        _compact(_prefix(), _batch(1, 9000), budget=700)
    error = exc.value
    assert error.estimated_tokens > error.input_budget == 700
    assert error.dropped_batches == 0
    assert str(error.estimated_tokens) in str(error)
    assert str(error.window_tokens) in str(error)
    assert "larger context window" in str(error)
    assert "tool schemas" in str(error)
    assert "must not be sent" in str(error)


def test_oversized_prefix_fails_without_history_to_drop():
    with pytest.raises(ContextWindowExceeded):
        _compact([{"role": "user", "content": "x" * 5000}], [], budget=100)


def test_tools_are_counted_even_with_no_trace_and_can_block_request():
    prefix = _prefix()
    tools = [{"type": "function", "function": {"name": "large_tool", "description": "x" * 9000, "parameters": {"type": "object"}}}]
    assert estimate_request_tokens(prefix, tools) > estimate_request_tokens(prefix, []) + 3000
    with pytest.raises(ContextWindowExceeded):
        _compact(prefix, [], tools=tools, budget=1000)


def test_tool_schema_and_message_framing_are_bounded_and_counted():
    message = {"role": "user", "content": ""}
    empty = estimate_request_tokens([], None)
    one = estimate_request_tokens([message], [])
    two = estimate_request_tokens([message, message], [])
    assert 0 < one - empty < 100
    assert two - one == one - empty
    assert estimate_request_tokens([], [{"type": "function"}]) > empty


def test_utf8_estimation_counts_bytes_not_python_character_length():
    ascii_estimate = estimate_request_tokens([{"role": "user", "content": "x" * 300}], [])
    cjk_estimate = estimate_request_tokens([{"role": "user", "content": "孔" * 300}], [])
    emoji_estimate = estimate_request_tokens([{"role": "user", "content": "🔩" * 300}], [])
    assert cjk_estimate - ascii_estimate == 200
    assert emoji_estimate - ascii_estimate == 300
    reasoning = {"role": "assistant", "content": "", "reasoning_content": "孔" * 300}
    assert estimate_request_tokens([reasoning], []) > cjk_estimate


def test_success_and_failure_leave_all_inputs_unchanged_and_outputs_independent():
    prefix = _prefix()
    trace = [*_batch(0, 8000), *_batch(1)]
    tools = [{"type": "function", "function": {"name": "ir_read", "parameters": {"type": "object"}}}]
    before = deepcopy((prefix, trace, tools))
    result = _compact(prefix, trace, tools=tools, budget=1000)
    assert (prefix, trace, tools) == before
    result.messages[0]["content"] = "changed output"
    result.messages[-2]["tool_calls"][0]["function"]["arguments"] = "changed nested output"
    assert (prefix, trace, tools) == before
    with pytest.raises(ContextWindowExceeded):
        _compact(prefix, trace, tools=tools, budget=50)
    assert (prefix, trace, tools) == before


@pytest.mark.parametrize(
    ("window", "output", "expected"),
    [(1000, 200, 800), (1000, 20, 900), (1001, 0, 900), (1, 0, 0), (1000, 1000, 0), (1000, 2000, 0)],
)
def test_input_budget_reserves_configured_output_or_rounded_ten_percent(window, output, expected):
    assert input_budget(window, output) == expected


@pytest.mark.parametrize(("window", "output"), [(0, 0), (-1, 0), (100, -1), (True, 0), (100, True), (100.5, 0), (100, 1.5)])
def test_invalid_budget_configuration_is_rejected(window, output):
    with pytest.raises(ValueError):
        input_budget(window, output)


def test_output_reservation_alone_can_leave_no_request_space():
    with pytest.raises(ContextWindowExceeded) as exc:
        compact_turn([], [], [], window_tokens=100, max_output_tokens=101)
    assert exc.value.input_budget == 0
    assert "output reserve 101" in str(exc.value)


def test_exact_budget_boundary_fits_without_unnecessary_compaction():
    prefix, trace = _prefix(), _batch(0)
    size = estimate_request_tokens([*prefix, *trace], [])
    result = _compact(prefix, trace, budget=size)
    assert result.estimated_tokens == result.input_budget
    assert result.dropped_batches == 0
    with pytest.raises(ContextWindowExceeded):
        _compact(prefix, trace, budget=size - 1)


@pytest.mark.parametrize("keep", [-1, 1.5, True])
def test_invalid_keep_recent_preference_is_rejected(keep):
    with pytest.raises(ValueError, match="keep_recent_batches"):
        _compact([], [], keep_recent_batches=keep)


def test_orphan_tool_result_is_rejected_even_if_everything_fits():
    with pytest.raises(ToolProtocolError, match="orphan"):
        _compact([], [_batch(0)[1]])


@pytest.mark.parametrize("cut", [1, 2, 3])
def test_incomplete_multitool_groups_are_rejected(cut):
    with pytest.raises(ToolProtocolError, match="incomplete"):
        _compact([], _batch(0, count=3)[:cut])


def test_interleaving_an_assistant_before_all_results_is_rejected():
    batch = _batch(0, count=2)
    batch.insert(2, {"role": "assistant", "content": "interleaved"})
    with pytest.raises(ToolProtocolError, match="incomplete"):
        _compact([], batch)


def test_mismatched_result_id_is_rejected_even_in_old_history_to_be_dropped():
    invalid = _batch(0, 8000)
    invalid[1]["tool_call_id"] = "unrelated_call"
    before = deepcopy(invalid)
    with pytest.raises(ToolProtocolError, match="mismatched"):
        _compact(_prefix(), [*invalid, *_batch(1)], budget=900)
    assert invalid == before


def test_duplicate_call_ids_in_one_batch_are_rejected():
    batch = _batch(0, count=2)
    batch[0]["tool_calls"][1]["id"] = batch[0]["tool_calls"][0]["id"]
    with pytest.raises(ToolProtocolError, match="duplicate tool call"):
        _compact([], batch)


def test_duplicate_result_ids_are_rejected():
    batch = _batch(0, count=2)
    batch[2]["tool_call_id"] = batch[1]["tool_call_id"]
    with pytest.raises(ToolProtocolError, match="duplicate tool result"):
        _compact([], batch)


def test_extra_result_after_complete_batch_is_rejected_as_orphan():
    batch = _batch(0)
    with pytest.raises(ToolProtocolError, match="orphan"):
        _compact([], [*batch, batch[-1]])


def test_tool_result_name_mismatch_is_rejected():
    batch = _batch(0)
    batch[1]["name"] = "different_tool"
    with pytest.raises(ToolProtocolError, match="name does not match"):
        _compact([], batch)


def test_optional_result_name_can_be_omitted():
    batch = _batch(0)
    del batch[1]["name"]
    assert _compact([], batch).messages == batch


@pytest.mark.parametrize("calls", ["invalid", [{}], [{"id": ""}], [{"id": 42}]])
def test_malformed_tool_call_identifiers_fail_explicitly(calls):
    with pytest.raises(ToolProtocolError):
        _compact([], [{"role": "assistant", "tool_calls": calls}])


def test_trace_rejects_user_context_instead_of_accidentally_dropping_it():
    with pytest.raises(ToolProtocolError, match="unexpected role"):
        _compact([], [{"role": "user", "content": "important"}])


def _render_feedback(payload="pixels"):
    return {"role": "user", "content": [
        {"type": "text", "text": "[Tool render images] geo_view, artifact_id=v4"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + payload, "detail": "high"}},
    ]}


def test_image_estimate_counts_vision_budget_instead_of_base64_characters():
    short = estimate_request_tokens([_render_feedback("x")], [])
    huge = estimate_request_tokens([_render_feedback("x" * 2_000_000)], [])
    assert huge == short
    assert 8192 < short < 8500


def test_compaction_keeps_or_drops_render_pixels_with_their_complete_tool_round():
    first = [*_batch(0), _render_feedback("first")]
    last = [*_batch(1), _render_feedback("last")]
    result = _compact(_prefix(), [*first, *last], budget=10_000)
    assert result.dropped_batches == 1
    assert result.messages[-len(last):] == last
    assert _render_feedback("first") not in result.messages
    assert result.estimated_tokens == estimate_request_tokens(result.messages, [])


def test_orphan_render_feedback_is_not_a_valid_tool_round():
    with pytest.raises(ToolProtocolError):
        _compact([], [_render_feedback()], budget=10_000)


def test_prefix_can_contain_completed_tool_history_but_cannot_split_a_batch():
    history = _batch(0)
    prefix = [*_prefix(), *history, {"role": "user", "content": "now change it"}]
    assert _compact(prefix, []).messages == prefix
    with pytest.raises(ToolProtocolError, match="prefix.*incomplete"):
        _compact(history[:1], history[1:])
