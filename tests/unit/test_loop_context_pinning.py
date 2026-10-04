"""Mid-turn compaction refreshes CAD state without rereading live chat history."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tcad.context.assembler import ContextAssembler
from tcad.context.history import messages_from_rows
from tcad.core.types import Thread, TurnKind
from tcad.loop.engine import UserMessage
from tests.unit.test_loop_engine import (
    ScriptedLlm,
    make_engine,
    make_ir,
    make_services,
)


def _batch(number, size):
    return [
        {
            "role": "assistant",
            "content": f"Inspecting feature {number}.",
            "reasoning_content": f"Original reasoning {number}.",
            "tool_calls": [{
                "id": f"call{number}",
                "type": "function",
                "function": {"name": "ir_get", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": f"call{number}",
            "name": "ir_get",
            "content": "x" * size,
        },
    ]


async def test_compaction_pins_prior_constraints_and_does_not_duplicate_live_messages():
    services = make_services(make_ir(), ScriptedLlm([]), True)
    engine = make_engine(services)
    engine._context_assembler = ContextAssembler()
    rows = [
        {"role": "user", "content": "Do not alter existing holes."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Keep the slot at 40x20 mm."},
    ]
    history_reads = []

    def load_history(thread_id, current_text):
        history_reads.append(thread_id)
        return messages_from_rows(rows, current_text=current_text)

    engine._history_provider = load_history
    user = UserMessage(kind=TurnKind.MODIFY, text=rows[-1]["content"])
    turn = engine._new_turn(Thread(thread_id="th", model_id="m1"), user)
    engine._current_user_message = user
    messages = await engine._build_messages(user, turn)
    engine._trace_start = len(messages)
    assert any(message["content"] == rows[0]["content"] for message in messages)

    oldest, latest = _batch(0, 10000), _batch(1, 10)
    for batch in (oldest, latest):
        messages.extend(batch)
        # The HTTP server persists nonempty model narration during the turn.
        # A fresh history read would duplicate it and the current user request.
        rows.append({"role": "assistant", "content": batch[0]["content"]})

    # A second assembler build would now degrade to MINIMAL and erase the
    # earlier constraint. Compaction must use the original selected history.
    engine._context_assembler.budget.window_tokens = 1
    engine.config.context_window_tokens = 2000
    engine.config.llm_max_tokens = 0
    await engine._prepare_step_context(turn, messages, [])

    assert history_reads == ["th"]
    assert sum(message["role"] == "user" and message["content"] == user.text
               for message in messages) == 1
    assert any(message["role"] == "user" and message["content"] == "Do not alter existing holes."
               for message in messages)
    assert not any(message["content"] == oldest[0]["content"] for message in messages)
    assert sum(message["content"] == latest[0]["content"] for message in messages) == 1
    assert messages[-2:] == latest


@pytest.mark.parametrize("unavailable", ["snapshot", "version", "digest"])
async def test_required_state_refresh_failure_keeps_entire_transcript(unavailable):
    services = make_services(make_ir(), ScriptedLlm([]), True)
    engine = make_engine(services)
    engine._current_user_message = UserMessage(text="Keep the exact dimensions.")
    engine._trace_start = 2
    engine.config.context_window_tokens = 2000
    engine.config.llm_max_tokens = 0
    messages = [
        {"role": "system", "content": "CAD rules"},
        {"role": "user", "content": engine._current_user_message.text},
        *_batch(0, 10000),
        *_batch(1, 10),
    ]
    before = deepcopy(messages)

    def fail(*args, **kwargs):
        raise OSError("authoritative state is unavailable")

    if unavailable == "snapshot":
        services.store.load = fail
    elif unavailable == "version":
        services.store.current_version = fail
    else:
        services.context.digest = fail

    with pytest.raises(RuntimeError, match="cannot refresh current CAD"):
        await engine._prepare_step_context(
            SimpleNamespace(model_id="m1", thread_id="th"), messages, [],
        )
    assert messages == before
    assert engine._trace_start == 2
    assert services.llm.calls == 0


async def test_compaction_without_assembler_adds_authoritative_state_before_omission():
    services = make_services(make_ir(), ScriptedLlm([]), True)
    engine = make_engine(services)
    user = UserMessage(text="Keep the slot at 40x20 mm.")
    turn = engine._new_turn(Thread(thread_id="th", model_id="m1"), user)
    engine._current_user_message = user
    messages = await engine._build_messages(user, turn)
    assert len(messages) == 2
    engine._trace_start = len(messages)
    latest = _batch(1, 10)
    messages.extend([*_batch(0, 10000), *latest])
    engine.config.context_window_tokens = 2000
    engine.config.llm_max_tokens = 0

    await engine._prepare_step_context(turn, messages, [])

    assert any("Requirement contract" in message["content"] for message in messages)
    assert any(message["role"] == "system" and message["content"] == "digest"
               for message in messages)
    assert any("Context compaction:" in message["content"] for message in messages)
    assert messages[-2:] == latest
    assert sum(message["role"] == "user" and message["content"] == user.text
               for message in messages) == 1
