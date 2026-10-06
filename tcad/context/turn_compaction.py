"""Pure, protocol-safe compaction of a current turn's provider request.

The caller refreshes the authoritative prefix (requirements, CAD snapshot, Gate,
history and current user request). Only complete older assistant/tool batches
may be omitted here. This is lossy omission, not an LLM-generated summary.

Token counts are conservative UTF-8-byte estimates, not tokenizer measurements
or provider guarantees. Tool schemas and message framing are included; output
space is reserved separately. No provider SDK or tokenization dependency is
needed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from tcad.context.tool_images import is_render_message


_REQUEST_OVERHEAD = 32
_ITEM_OVERHEAD = 16
_OMITTED_TRACE_MESSAGE = {
    "role": "system",
    "content": (
        "[Context compaction: earlier complete assistant/tool rounds were omitted "
        "to fit the context window. This is an omission notice, not a summary. "
        "The current CAD snapshot, requirements and Gate state in the refreshed "
        "context are authoritative. Read tools remain available to inspect details "
        "again before acting.]"
    ),
}


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict[str, Any]]
    dropped_batches: int
    estimated_tokens: int
    input_budget: int


class ToolProtocolError(ValueError):
    """The supplied transcript cannot be replayed as complete tool batches."""


class ContextWindowExceeded(ValueError):
    """Even the mandatory request does not fit the estimated input budget."""

    def __init__(
        self,
        *,
        estimated_tokens: int,
        input_budget: int,
        window_tokens: int,
        max_output_tokens: int,
        dropped_batches: int,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.input_budget = input_budget
        self.window_tokens = window_tokens
        self.max_output_tokens = max_output_tokens
        self.dropped_batches = dropped_batches
        reserve = max(max_output_tokens, (window_tokens + 9) // 10)
        super().__init__(
            f"Estimated request size {estimated_tokens} input tokens exceeds input "
            f"budget {input_budget} (context window {window_tokens}, output reserve "
            f"{reserve}, configured max_output_tokens {max_output_tokens}). Cannot "
            "compact further without discarding authoritative context or the latest "
            "whole tool batch. Use a larger context window or reduce supplied "
            "context/tool schemas; reducing max_output_tokens can help when it "
            "controls the output reserve. Request must not be sent to the provider."
        )


def _item_tokens(item: Mapping[str, Any]) -> int:
    # Count every wire field, including tool arguments and reasoning_content.
    # Three UTF-8 bytes/token is deliberately more cautious than four characters
    # per token, especially for CJK text. Framing overhead is small and linear.
    image_tokens = 0
    content = item.get("content")
    if isinstance(content, list):
        # Image bytes are not tokenized as base64 text. Reserve a conservative
        # 8192 tokens per full image (85 at low detail), independently of file
        # compression. This remains an estimate, not a provider token guarantee.
        item = dict(item)
        item["content"] = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "image_url":
                image = part.get("image_url", {})
                image_tokens += 85 if image.get("detail") == "low" else 8192
                item["content"].append({"type": "image_url", "image_url": {"detail": image.get("detail", "auto")}})
            else:
                item["content"].append(part)
    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return _ITEM_OVERHEAD + (len(payload.encode("utf-8")) + 2) // 3 + image_tokens


def estimate_request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
) -> int:
    """Estimate the whole request, including tool schemas and protocol framing.

    This heuristic deliberately has no exact-token-count claim. Inputs must be
    JSON-serializable provider wire dictionaries. Text and protocol fields are
    counted by bytes; image payloads receive a separate vision token allowance.
    """
    return (
        _REQUEST_OVERHEAD
        + sum(_item_tokens(message) for message in messages)
        + sum(_item_tokens(tool) for tool in (tools or ()))
    )


def input_budget(window_tokens: int, max_output_tokens: int) -> int:
    """Reserve max(configured output, ceil(10% of window)); never return < 0."""
    if isinstance(window_tokens, bool) or not isinstance(window_tokens, int) or window_tokens <= 0:
        raise ValueError("window_tokens must be a positive integer")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 0:
        raise ValueError("max_output_tokens must be a nonnegative integer")
    reserve = max(max_output_tokens, (window_tokens + 9) // 10)
    return max(0, window_tokens - reserve)


def compact_turn(
    prefix: Sequence[Mapping[str, Any]],
    trace: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    *,
    window_tokens: int,
    max_output_tokens: int,
    keep_recent_batches: int = 2,
) -> CompactionResult:
    """Omit oldest complete batches until the request fits, without slicing.

    A batch is one assistant message, *all* matching tool results and generated
    image feedback, or a standalone assistant narration. The newest ``keep_recent_batches`` are
    preferred; they may be reduced further when necessary. The latest complete
    tool-call batch and any later narration are mandatory. If there are no tool
    calls, the latest narration is mandatory. The prefix is always preserved.

    Validate all batches before dropping anything, so malformed older history
    is never silently repaired by omission. Results are independent deep copies;
    retained content, tool arguments and reasoning fields remain verbatim.
    """
    budget = input_budget(window_tokens, max_output_tokens)
    if (
        isinstance(keep_recent_batches, bool)
        or not isinstance(keep_recent_batches, int)
        or keep_recent_batches < 0
    ):
        raise ValueError("keep_recent_batches must be a nonnegative integer")

    # Prefix history may contain completed tool exchanges, but must not end
    # inside one. A compaction boundary may never split a call/result group.
    _complete_batches(prefix, label="prefix", allow_context=True)
    batches = _complete_batches(trace, label="trace")
    estimated = estimate_request_tokens([*prefix, *trace], tools)
    batch_costs = [sum(_item_tokens(message) for message in batch) for batch in batches]

    mandatory_keep = min(1, len(batches))
    for index in range(len(batches) - 1, -1, -1):
        if batches[index][0].get("tool_calls"):
            mandatory_keep = len(batches) - index
            break

    dropped = 0
    # First exhaust older batches, then compromise the recent-batch preference.
    # At neither stage may the latest complete tool round be discarded.
    for keep in (max(keep_recent_batches, mandatory_keep), mandatory_keep):
        while estimated > budget and len(batches) - dropped > keep:
            if dropped == 0:
                estimated += _item_tokens(_OMITTED_TRACE_MESSAGE)
            estimated -= batch_costs[dropped]
            dropped += 1

    if estimated > budget:
        raise ContextWindowExceeded(
            estimated_tokens=estimated,
            input_budget=budget,
            window_tokens=window_tokens,
            max_output_tokens=max_output_tokens,
            dropped_batches=dropped,
        )

    messages = [*prefix]
    if dropped:
        messages.append(_OMITTED_TRACE_MESSAGE)
    messages.extend(message for batch in batches[dropped:] for message in batch)
    return CompactionResult(
        messages=deepcopy([dict(message) for message in messages]),
        dropped_batches=dropped,
        estimated_tokens=estimated,
        input_budget=budget,
    )


def _complete_batches(
    messages: Sequence[Mapping[str, Any]],
    *,
    label: str,
    allow_context: bool = False,
) -> list[list[Mapping[str, Any]]]:
    """Parse contiguous OpenAI assistant/result groups, checking every ID."""
    batches: list[list[Mapping[str, Any]]] = []
    index = 0
    while index < len(messages):
        assistant = messages[index]
        if not isinstance(assistant, Mapping):
            raise ToolProtocolError(f"{label}[{index}] must be a message dictionary")
        role = assistant.get("role")
        if role == "tool":
            raise ToolProtocolError(f"{label}[{index}] is an orphan tool result")
        if role != "assistant":
            if allow_context and role in ("system", "developer", "user") and not assistant.get("tool_calls"):
                index += 1
                continue
            raise ToolProtocolError(f"{label}[{index}] has unexpected role {role!r}")

        calls = assistant.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            raise ToolProtocolError(f"{label}[{index}].tool_calls must be a list")
        batch = [assistant]
        batch_index = index
        index += 1
        if not calls:
            batches.append(batch)
            continue

        pending: dict[str, Mapping[str, Any]] = {}
        for call in calls:
            call_id = call.get("id") if isinstance(call, Mapping) else None
            if not isinstance(call_id, str) or not call_id:
                raise ToolProtocolError(f"{label}[{batch_index}] has a tool call without a nonempty ID")
            if call_id in pending:
                raise ToolProtocolError(f"{label}[{batch_index}] has duplicate tool call ID {call_id!r}")
            pending[call_id] = call

        while pending:
            if index >= len(messages) or not isinstance(messages[index], Mapping) or messages[index].get("role") != "tool":
                raise ToolProtocolError(
                    f"{label}[{batch_index}] has an incomplete tool batch; "
                    f"missing result(s) for {', '.join(sorted(pending))}"
                )
            result = messages[index]
            call_id = result.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ToolProtocolError(
                    f"{label}[{index}] has a mismatched or duplicate tool result ID {call_id!r}"
                )
            function = pending[call_id].get("function")
            expected_name = function.get("name") if isinstance(function, Mapping) else None
            if result.get("name") is not None and expected_name is not None and result["name"] != expected_name:
                raise ToolProtocolError(
                    f"{label}[{index}] tool result name does not match call {call_id!r}"
                )
            del pending[call_id]
            batch.append(result)
            index += 1
        # Generated render feedback belongs to this whole tool round, so it is
        # retained or omitted together with its calls and results.
        while index < len(messages) and is_render_message(messages[index]):
            batch.append(messages[index])
            index += 1
        batches.append(batch)
    return batches
