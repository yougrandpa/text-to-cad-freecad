"""Async OpenAI-compatible LLM client (design §3, §6).

The client is *injectable*: the engine depends only on the :class:`LlmClient`
Protocol, so it can be tested with a scripted fake. The concrete
:class:`OpenAIClient` talks to any OpenAI-compatible endpoint (OpenAI, vLLM,
Ollama, …) via ``base_url`` — that is what makes the whole harness runnable
offline / private (design §1 hard constraint).

It is deliberately thin: retry-with-backoff on transient errors and a hard
per-request timeout. No agent logic lives here.
"""

from __future__ import annotations

import asyncio
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ToolCall(BaseModel):
    """One parsed function call from the model."""

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)

    #: Why ``args`` could not be parsed, when they could not. ``None`` means the
    #: provider sent valid JSON.
    #:
    #: This field exists because ``args`` alone cannot tell the two failure modes
    #: apart, and they have opposite fixes:
    #:
    #:  * **empty arguments** (``{}``) — the model emitted a bare tool call. Its
    #:    own mistake; re-sending with the required keys is the fix.
    #:  * **unparsable arguments** — the model was *writing* the arguments and
    #:    the JSON was cut off. Observed live: ``finish_reason="length"`` with a
    #:    1359-character ``arguments`` string ending mid-array
    #:    (``"direction": [0.0``). The fix is a smaller call, not a retry of the
    #:    same size.
    #:
    #: Both used to arrive downstream as ``{}`` — so a truncation was reported to
    #: the model as "you forgot to pass base_version and ops", which is false and
    #: sends it into a loop repeating the same too-large patch.
    args_error: str | None = None
    #: How many characters of arguments the provider actually sent. Distinguishes
    #: "sent nothing" from "sent a lot and it was truncated".
    args_raw_len: int = 0


class LlmReply(BaseModel):
    """Normalised model response the engine consumes."""

    text: str = ""
    reasoning_content: str | None = None
    """The model's thinking, when it emits any.

    DeepSeek's thinking mode returns it and then **requires it back** on the
    next request within the same turn; dropping it earns a 400
    ("The `reasoning_content` in the thinking mode must be passed back to the
    API"). The engine echoes it through unchanged — it is protocol state, not
    something we interpret.
    """
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str | None = None


@runtime_checkable
class LlmClient(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> LlmReply: ...


_MAX_RETRY_DELAY_S = 30.0


def _is_retryable(error: Exception) -> bool:
    """Retry transport failures and the SDK's transient HTTP status categories.

    ``APIError`` itself is deliberately not retryable: it also covers auth,
    invalid requests, and successful responses that failed SDK validation.
    ``APITimeoutError`` is a subclass of ``APIConnectionError``.
    """
    from openai import APIConnectionError, APIStatusError

    if isinstance(error, APIConnectionError):
        return True
    if isinstance(error, APIStatusError):
        # Some compatible servers misreport an oversized prompt as a 5xx;
        # OpenAI also uses 429 for exhausted billing quota. Neither is fixed
        # by waiting and repeating the same request. Match only known codes.
        permanent_codes = (
            "context_length_exceeded", "context_window_exceeded", "insufficient_quota",
        )
        if error.code in permanent_codes:
            return False
        if isinstance(error.body, dict):
            nested_error = error.body.get("error")
            if isinstance(nested_error, dict) and nested_error.get("code") in permanent_codes:
                return False
        return error.status_code in (408, 409, 429) or 500 <= error.status_code < 600
    return False


def _retry_after_delay(error: Exception) -> float | None:
    """Read provider retry hints without letting malformed headers stall a turn.

    Match the SDK's preference for milliseconds, then seconds, then an HTTP
    date. Invalid hints fall through to the next format or local backoff.
    Zero is a valid immediate retry; negative and non-finite values are not.
    """
    from openai import APIStatusError

    if not isinstance(error, APIStatusError):
        return None
    headers = error.response.headers
    for header, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            value = float(headers[header])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0:
            return min(value / scale, _MAX_RETRY_DELAY_S)

    try:
        date = parsedate_to_datetime(headers.get("retry-after"))
        # Obsolete HTTP dates may omit GMT; they still denote UTC.
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        delay = date.timestamp() - time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if math.isfinite(delay) and delay >= 0:
        return min(delay, _MAX_RETRY_DELAY_S)
    return None


class OpenAIClient:
    """Concrete OpenAI-compatible client with retry + backoff + hard timeout."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "EMPTY",
        model: str,
        request_timeout_s: float = 120.0,
        max_retries: int = 2,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        http_client: Any = None,
    ):
        from openai import AsyncOpenAI

        # We do our own retry loop, so disable the SDK's built-in one.
        #
        # ``http_client`` exists so a caller can hand us a transport that
        # ignores HTTP(S)_PROXY. That matters more than it sounds: an ambient
        # proxy that cannot reach the endpoint turns a direct-connect failure
        # into an error message naming the wrong culprit, which is the most
        # expensive kind of bug to chase.
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=request_timeout_s,
            max_retries=0,
            **({"http_client": http_client} if http_client is not None else {}),
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.request_timeout_s = request_timeout_s

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> LlmReply:
        temp = temperature if temperature is not None else self.temperature
        last_err: Exception | None = None
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temp,
                    max_tokens=self.max_tokens,
                )
            except Exception as e:
                if not _is_retryable(e):
                    raise
                last_err = e
                if attempt < self.max_retries:
                    delay = _retry_after_delay(e)
                    # asyncio cancellation propagates through both the request
                    # and this wait; it must never become another attempt.
                    await asyncio.sleep(backoff if delay is None else delay)
                    backoff = min(backoff * 2, _MAX_RETRY_DELAY_S)
                else:
                    raise
            else:
                # Parsing a successful response cannot be fixed by transport
                # retries, which could also duplicate a billed request.
                return self._parse(resp)
        # Should be unreachable; satisfies the type checker.
        assert last_err is not None
        raise last_err

    @staticmethod
    def _parse(resp: Any) -> LlmReply:
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_calls: list[ToolCall] = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                raw = getattr(tc.function, "arguments", None) or ""
                args: dict[str, Any] = {}
                args_error: str | None = None
                try:
                    parsed = json.loads(raw or "{}")
                except json.JSONDecodeError as exc:
                    # Survive it (a raise here would kill the turn) but *record*
                    # it, so the engine can name the real cause. An empty string
                    # is NOT an error: a tool with no required parameters
                    # legitimately takes `{}`, and calling that "unparsable"
                    # would send the model chasing a truncation that never
                    # happened.
                    args_error = f"{exc.msg} at character {exc.pos}"
                    if raw:
                        args_error += f" of {len(raw)} characters of arguments"
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        # Valid JSON, wrong shape — a list or scalar where an
                        # object belongs. Same reasoning: record it rather than
                        # letting the schema check blame a missing key.
                        args_error = (f"arguments were valid JSON but a "
                                      f"{type(parsed).__name__}, not an object")
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args,
                                           args_error=args_error, args_raw_len=len(raw)))
        usage = resp.usage
        pu = getattr(usage, "prompt_tokens", 0) if usage else 0
        co = getattr(usage, "completion_tokens", 0) if usage else 0
        return LlmReply(
            text=text,
            # `reasoning_content` is DeepSeek's name; `reasoning` is what several
            # other OpenAI-compatible servers use. Either way we keep it, because
            # a provider that emits it may also require it echoed back.
            reasoning_content=getattr(msg, "reasoning_content", None)
            or getattr(msg, "reasoning", None),
            tool_calls=tool_calls,
            usage=TokenUsage(prompt_tokens=pu, completion_tokens=co),
            finish_reason=getattr(choice, "finish_reason", None),
        )
