"""History compaction — LLM-agnostic (design §4.3).

The compactor never calls a model itself. It accepts a ``summarize`` callable
``(list[Message]) -> Awaitable[str]`` supplied by the loop layer, which owns the
model choice. That keeps this module pure and trivially testable with a fake
(see ``tests/unit/test_context_assembler.py``).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """Minimal message model for the assembled context.

    ``tcad.core.types`` has no Message (verified), so it lives here — the only
    shared symbol between the assembler and the compactor. ``kind`` is a hint for
    the loop layer (system / digest / gate / history / summary / image).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: str = "user"
    content: str = ""
    images: list[dict] = Field(default_factory=list)
    files: list[dict] = Field(default_factory=list)
    kind: str = "history"
    tokens_estimate: int = 0


async def summarize_history(
    messages: list[Message],
    *,
    keep_last: int,
    summarize: Callable[[list[Message]], Awaitable[str]] | None,
) -> list[Message]:
    """Keep the last ``keep_last`` messages verbatim; replace everything older
    with a single summary ``Message`` produced by ``summarize``.

    When ``summarize`` is ``None`` (no model wired yet) a neutral placeholder is
    used instead of dropping the history silently.
    """
    if not messages:
        return []
    if keep_last > 0 and len(messages) <= keep_last:
        return list(messages)

    to_summarize = messages[:-keep_last] if keep_last > 0 else messages
    recent = messages[-keep_last:] if keep_last > 0 else []

    if to_summarize:
        if summarize is not None:
            summary_text = await summarize(to_summarize)
        else:
            summary_text = "[earlier turns summarized — no summarizer configured]"
        summary_msg = Message(
            role="system", content=summary_text, kind="summary",
            tokens_estimate=max(1, len(summary_text) // 4),
        )
        return [summary_msg, *recent]
    return list(recent)
