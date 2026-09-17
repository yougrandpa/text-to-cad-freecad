"""Context assembler (design §4.3): budgeted context build + three-level
degradation.

It turns the fixed context blocks (system prefix, geometry digest, latest gate
report) and the rolling conversation history + render images into an ordered
list of :class:`Message` objects that fits a token budget.

Budget table (design §4.3): system 6k / digest 2k / gate report 2k / images
2.4k / history = remainder. All numbers come from :class:`ContextBudget` so
config drives them.

Degradation thresholds (design §4.3.2):
  * used/limit < 0.70           -> FULL      (keep all history)
  * 0.70 <= used/limit < 0.85   -> SUMMARIZED (keep last N turns verbatim +
                                    a single summary of the older ones)
  * used/limit >= 0.85          -> MINIMAL   (system + digest + gate only; drop
                                    history & images, keep current turn)

The historical summary is produced by an injected ``summarize`` callable so the
assembler stays LLM-agnostic and unit-testable with a fake (design §4.3 / the
compactor module owns the same contract).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from tcad.context.compactor import Message, summarize_history
from tcad.core.types import ContextLevel, ImageRef


class ContextBudget(BaseModel):
    """Token budget. Mirrors design §8 ``context.budget`` / ``degrade_thresholds``."""

    window_tokens: int = 128_000
    system_prefix: int = 6000
    digest: int = 2000
    gate_report: int = 2000
    images: int = 2400
    summarize_keep_last_turns: int = 6
    degrade_summarized: float = 0.70
    degrade_minimal: float = 0.85


class AssembleContext(BaseModel):
    """The inputs the assembler needs (the loop layer fills this in)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    system_prompt: str = ""
    digest_text: str = ""
    gate_report_text: str = ""
    history: list[Message] = Field(default_factory=list)


def _est(text: str) -> int:
    # conservative ~4 chars/token estimate
    return max(1, len(text) // 4)


class ContextAssembler:
    def __init__(
        self,
        budget: ContextBudget | None = None,
        *,
        summarize: Callable[[list[Message]], Awaitable[str]] | None = None,
    ):
        self.budget = budget or ContextBudget()
        self.summarize = summarize

    # ── public ─────────────────────────────────────────────────────────────

    async def build(self, ctx: AssembleContext, images: list[ImageRef]) -> list[Message]:
        b = self.budget
        sys_tokens = _est(ctx.system_prompt)
        digest_tokens = _est(ctx.digest_text)
        gate_tokens = _est(ctx.gate_report_text)
        image_tokens = sum(im.tokens_estimate for im in images) or (b.images if images else 0)
        history_tokens = sum(m.tokens_estimate or _est(m.content) for m in ctx.history)

        fixed = sys_tokens + digest_tokens + gate_tokens + image_tokens
        used = fixed + history_tokens
        level = self._level(used, b.window_tokens)

        # fixed prefix (present at every level)
        out: list[Message] = [
            Message(role="system", content=ctx.system_prompt, kind="system",
                    tokens_estimate=sys_tokens),
            Message(role="system", content=ctx.digest_text, kind="digest",
                    tokens_estimate=digest_tokens),
            Message(role="system", content=ctx.gate_report_text, kind="gate",
                    tokens_estimate=gate_tokens),
        ]

        if level == ContextLevel.FULL:
            out.extend(ctx.history)
            out.extend(self._image_messages(images, image_tokens))

        elif level == ContextLevel.SUMMARIZED:
            keep = b.summarize_keep_last_turns
            recent = ctx.history[-keep:] if keep else []
            earlier = ctx.history[:-keep] if keep else ctx.history
            if earlier:
                # summarize_history returns a compacted *list* (a summary Message
                # followed by whatever was kept verbatim) — its signature and
                # docstring are explicit about that. Wrapping that list into
                # Message.content raised a ValidationError and killed the whole
                # assembly, so extend rather than append.
                out.extend(
                    await summarize_history(earlier, keep_last=0, summarize=self.summarize)
                )
            out.extend(recent)
            out.extend(self._image_messages(images, image_tokens))

        else:  # MINIMAL — drop history & images, keep current turn only
            if ctx.history:
                cur = ctx.history[-1]
                out.append(Message(role=cur.role, content=cur.content,
                                    kind="history", tokens_estimate=cur.tokens_estimate or _est(cur.content)))

        return out

    # ── internal ───────────────────────────────────────────────────────────

    def _level(self, used: int, limit: int) -> ContextLevel:
        if limit <= 0:
            return ContextLevel.MINIMAL
        ratio = used / limit
        if ratio >= self.budget.degrade_minimal:
            return ContextLevel.MINIMAL
        if ratio >= self.budget.degrade_summarized:
            return ContextLevel.SUMMARIZED
        return ContextLevel.FULL

    @staticmethod
    def _image_messages(images: list[ImageRef], total_tokens: int) -> list[Message]:
        if not images:
            return []
        msgs = [
            Message(role="user", content=f"[render image: {im.view} {im.width}x{im.height}]",
                    kind="image", tokens_estimate=im.tokens_estimate)
            for im in images
        ]
        # ensure the budgeted image token cost is represented on the first one
        if msgs:
            msgs[0].tokens_estimate = total_tokens
        return msgs
