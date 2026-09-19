"""LLM client: normalising what different providers send back.

`_parse` is the single point where a provider's wire format becomes the
harness's `LlmReply`, so it is where protocol details enter the system — and it
had no tests until the first *real* DeepSeek run failed at step 2 with

    400 "The `reasoning_content` in the thinking mode must be passed back"

The stub model emits no reasoning, so nothing offline could have produced that.
"""

from __future__ import annotations

from types import SimpleNamespace

from tcad.llm.client import LlmReply, OpenAIClient


def _response(*, content="hi", tool_calls=None, finish_reason="stop", **message_extra):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, **message_extra)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
    )


# ─── reasoning / thinking ──────────────────────────────────────────────────


def test_parse_keeps_deepseek_reasoning_content():
    reply = OpenAIClient._parse(_response(reasoning_content="step by step"))
    assert reply.reasoning_content == "step by step"


def test_parse_accepts_the_alternate_field_name():
    """Several OpenAI-compatible servers call the same thing `reasoning`."""
    reply = OpenAIClient._parse(_response(reasoning="alt name"))
    assert reply.reasoning_content == "alt name"


def test_parse_prefers_reasoning_content_when_both_are_present():
    reply = OpenAIClient._parse(
        _response(reasoning_content="primary", reasoning="secondary")
    )
    assert reply.reasoning_content == "primary"


def test_parse_leaves_reasoning_none_when_absent():
    assert OpenAIClient._parse(_response()).reasoning_content is None


def test_the_default_reply_carries_no_reasoning():
    """A fake, or a provider that emits nothing, must not produce the field —
    the engine keys its echo off it being truthy."""
    assert LlmReply().reasoning_content is None


# ─── the rest of the shape ─────────────────────────────────────────────────


def test_parse_keeps_text_tools_and_usage():
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="ir_get", arguments='{"a": 1}'))
    reply = OpenAIClient._parse(
        _response(content="", tool_calls=[call], finish_reason="tool_calls")
    )
    assert reply.text == ""
    assert reply.tool_calls[0].id == "c1"
    assert reply.tool_calls[0].name == "ir_get"
    assert reply.tool_calls[0].args == {"a": 1}
    assert reply.usage.prompt_tokens == 11
    assert reply.usage.completion_tokens == 22
    assert reply.finish_reason == "tool_calls"


def test_parse_survives_malformed_tool_arguments():
    """A truncated argument string must not take the whole turn down."""
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="x", arguments="{not json"))
    reply = OpenAIClient._parse(_response(tool_calls=[call]))
    assert reply.tool_calls[0].args == {}


def test_parse_handles_a_missing_usage_block():
    """Some local servers omit `usage` entirely."""
    resp = _response()
    resp.usage = None
    reply = OpenAIClient._parse(resp)
    assert reply.usage.prompt_tokens == 0
    assert reply.usage.completion_tokens == 0
