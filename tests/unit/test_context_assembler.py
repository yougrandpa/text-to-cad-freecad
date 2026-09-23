"""Tests for the context assembler + three-level degradation (design §4.3)."""

from __future__ import annotations

import asyncio

from tcad.context.assembler import (
    AssembleContext, ContextAssembler, ContextBudget, Message,
)
from tcad.core.types import ContextLevel, ImageRef


async def _fake_summarize(msgs):
    return "SUMMARY of %d turns" % len(msgs)


def test_level_thresholds():
    a = ContextAssembler(ContextBudget())
    assert a._level(500, 1000) == ContextLevel.FULL
    assert a._level(750, 1000) == ContextLevel.SUMMARIZED
    assert a._level(900, 1000) == ContextLevel.MINIMAL
    # boundary exactly on 0.85 -> MINIMAL
    assert a._level(850, 1000) == ContextLevel.MINIMAL
    # boundary exactly on 0.70 -> SUMMARIZED
    assert a._level(700, 1000) == ContextLevel.SUMMARIZED


def test_build_full_keeps_all_history():
    budget = ContextBudget(window_tokens=100_000)
    history = [Message(role="user", content=f"u{i}", tokens_estimate=10) for i in range(3)]
    ctx = AssembleContext(system_prompt="sys", digest_text="dig",
                          gate_report_text="gate", history=history)
    out = asyncio.run(ContextAssembler(budget).build(ctx, []))
    assert len(out) == 4 + 3  # 4 fixed (system/requirements/digest/gate) + 3 history
    assert sum(1 for m in out if m.kind == "history") == 3


def test_build_summarized_keeps_last_and_summary():
    budget = ContextBudget(window_tokens=1300, summarize_keep_last_turns=2)
    history = [Message(role="user", content=f"turn {i}", tokens_estimate=100)
               for i in range(10)]
    ctx = AssembleContext(system_prompt="", digest_text="", gate_report_text="",
                          history=history)
    out = asyncio.run(ContextAssembler(budget, summarize=_fake_summarize).build(ctx, []))
    # 4 fixed + 1 summary + 2 recent == 7
    assert len(out) == 7
    assert any(m.kind == "summary" for m in out)
    assert sum(1 for m in out if m.kind == "history") == 2


def test_build_minimal_drops_history_and_images():
    budget = ContextBudget(window_tokens=500, summarize_keep_last_turns=2)
    history = [Message(role="user", content=f"turn {i}", tokens_estimate=100)
               for i in range(10)]
    images = [ImageRef(path="v.png", view="iso", width=768, height=576)]
    ctx = AssembleContext(system_prompt="", digest_text="", gate_report_text="",
                          history=history)
    out = asyncio.run(ContextAssembler(budget, summarize=_fake_summarize).build(ctx, images))
    # 4 fixed + 1 current turn == 5; no summary, no image
    assert len(out) == 5
    assert not any(m.kind == "summary" for m in out)
    assert not any(m.kind == "image" for m in out)
    assert sum(1 for m in out if m.kind == "history") == 1


def test_requirement_contract_survives_every_degradation_level():
    """The block that says what the part is judged against is never dropped.

    MINIMAL exists to shed conversation when the window is tight — exactly when
    the model is most likely to lose the ask. Dropping the contract there would
    trade "the model forgot the conversation" for "the model forgot the
    requirement", which is strictly worse.
    """
    for window in (100_000, 1300, 500):
        budget = ContextBudget(window_tokens=window, summarize_keep_last_turns=2)
        history = [Message(role="user", content=f"turn {i}", tokens_estimate=100)
                   for i in range(10)]
        ctx = AssembleContext(system_prompt="sys", requirements_text="REQUIREMENTS",
                              digest_text="dig", gate_report_text="gate", history=history)
        out = asyncio.run(ContextAssembler(budget, summarize=_fake_summarize).build(ctx, []))
        assert any(m.kind == "requirements" and m.content == "REQUIREMENTS" for m in out), window


def test_to_openai_drops_blank_blocks_but_keeps_requirements():
    from tcad.context.assembler import to_openai_messages

    msgs = [
        Message(role="system", content="   ", kind="system"),
        Message(role="system", content="REQS", kind="requirements"),
        Message(role="user", content="hi", kind="history"),
    ]
    out = to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "REQS"
    # `kind` is a loop-layer hint; it must not leak onto the wire.
    assert all("kind" not in m for m in out)


def test_build_includes_images_at_full():
    budget = ContextBudget(window_tokens=100_000)
    images = [ImageRef(path="v.png", view="iso", width=768, height=576,
                       tokens_estimate=800)]
    ctx = AssembleContext(system_prompt="s", digest_text="d", gate_report_text="g",
                          history=[])
    out = asyncio.run(ContextAssembler(budget).build(ctx, images))
    img = [m for m in out if m.kind == "image"]
    assert img and img[0].tokens_estimate == 800
