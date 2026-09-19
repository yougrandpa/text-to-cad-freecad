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
import json
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


# Transient errors worth retrying (openai >= 1.40).
def _transient_errors():
    from openai import (
        APIConnectionError,
        APIError,
        APITimeoutError,
        RateLimitError,
        InternalServerError,
    )

    return (APIError, APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


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
        transient = _transient_errors()
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
                return self._parse(resp)
            except transient as e:  # type: ignore[misc]
                last_err = e
                if attempt < self.max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    raise
            except Exception as e:
                # Non-transient (e.g. auth, bad request) — do not retry.
                raise
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
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
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
