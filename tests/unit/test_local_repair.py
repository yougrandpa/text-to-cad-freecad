"""Rejected calls are retained; a partial reply completes them (task P0-2).

The failure this pins: a long tool call is rejected for a missing field, and
the model's only recorded remedy was to re-send the whole payload — the exact
act of rewriting a long batch in which field values migrate (observed: a
compact-recipe batch came back with its `parts` value written into `topic`).
"""

from __future__ import annotations

import tempfile

import pytest

from tcad.core.types import HookDecision, HookResult, HookEvent, ToolResult, ToolSpec, ToolTier, Thread, TurnKind
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.tools.base import build_default_registry
from tests.unit.test_loop_engine import FakeHooks, ScriptedLlm, make_ir, make_services


class Demo:
    def __init__(self):
        self.calls = []

    async def handler(self, args, ctx):
        self.calls.append(dict(args))
        return ToolResult(ok=True, content="applied demo_write")


def demo_spec(demo: Demo) -> ToolSpec:
    return ToolSpec(
        name="demo_write", tier=ToolTier.WRITE, description="demo",
        params_schema={
            "type": "object", "additionalProperties": False,
            "required": ["alpha", "beta"],
            "properties": {"alpha": {"type": "integer"}, "beta": {"type": "string"}},
        },
        handler=demo.handler,
    )


def engine_with_demo(services, demo, *, max_steps=8):
    reg = build_default_registry(services)
    reg.register_tool(demo_spec(demo))
    cfg = LoopConfig(max_steps_per_turn=max_steps, data_dir=tempfile.mkdtemp())
    return LoopEngine(services, reg, BudgetLimits(max_steps_per_turn=max_steps), cfg)


def tool_texts(messages):
    return [m["content"] for m in messages if m.get("role") == "tool"]


@pytest.mark.asyncio
async def test_partial_retry_completes_the_retained_call():
    demo = Demo()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="demo_write", args={"alpha": 3})]),
        LlmReply(tool_calls=[ToolCall(id="c2", name="demo_write", args={"beta": "x"})]),
        LlmReply(text="done"),
    ])
    hooks = FakeHooks()
    services = make_services(make_ir(), llm, gate_passed=False, hooks=hooks)
    engine = engine_with_demo(services, demo)
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="demo"))

    # First call: rejected but retained, and told to fill only the missing field.
    first = [t for t in tool_texts(llm.last_messages) if "RETAINED" in t]
    assert first and "beta" in first[0]
    # Second call: only `beta` was supplied; the retained `alpha` completed it.
    assert demo.calls == [{"alpha": 3, "beta": "x"}]
    assert any("completed the retained call" in t for t in tool_texts(llm.last_messages))
    assert engine._retained_calls == {}
    # The hook saw the MERGED payload — permission checks run on what executes.
    tool_events = [p for e, p in hooks.events if e == HookEvent.PRE_TOOL_USE]
    assert tool_events[-1]["args"] == {"alpha": 3, "beta": "x"}


@pytest.mark.asyncio
async def test_a_complete_call_supersedes_the_retained_one():
    demo = Demo()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="demo_write", args={"alpha": 3})]),
        LlmReply(tool_calls=[ToolCall(id="c2", name="demo_write", args={"alpha": 9, "beta": "y"})]),
        LlmReply(text="done"),
    ])
    services = make_services(make_ir(), llm, gate_passed=False)
    engine = engine_with_demo(services, demo)
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="demo"))
    assert demo.calls == [{"alpha": 9, "beta": "y"}]
    assert engine._retained_calls == {}


@pytest.mark.asyncio
async def test_denied_merged_call_still_runs_the_permission_path():
    class DenySecondCall(FakeHooks):
        def __init__(self):
            super().__init__()
            self.tool_calls = 0
            self.seen = []

        def dispatch(self, event, payload):
            if event == HookEvent.PRE_TOOL_USE and payload.get("tool_name") == "demo_write":
                self.tool_calls += 1
                self.seen.append(dict(payload["args"]))
                if self.tool_calls == 2:
                    return HookResult(decision=HookDecision.DENY, hook_name="test", reason="policy")
            return super().dispatch(event, payload)

    demo = Demo()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="demo_write", args={"alpha": 1})]),
        LlmReply(tool_calls=[ToolCall(id="c2", name="demo_write", args={"beta": "z"})]),
        LlmReply(text="done"),
    ])
    hooks = DenySecondCall()
    services = make_services(make_ir(), llm, gate_passed=False, hooks=hooks)
    engine = engine_with_demo(services, demo)
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="demo"))
    # The merged payload reached the permission check, which refused it — the
    # handler never ran and the attempt did not survive as a retained call.
    assert demo.calls == []
    assert any("[tool error: denied]" in t for t in tool_texts(llm.last_messages))
    assert engine._retained_calls == {}
    assert hooks.seen == [{"alpha": 1}, {"alpha": 1, "beta": "z"}]


@pytest.mark.asyncio
async def test_retention_does_not_leak_across_turns():
    demo = Demo()
    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=False)
    engine = engine_with_demo(services, demo)
    # Turn 1 ends with the partial call retained (then four idle replies end it).
    services.llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="demo_write", args={"alpha": 5})]),
        *[LlmReply(text="thinking") for _ in range(4)],
    ])
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="demo"))
    assert engine._retained_calls.get("demo_write") == {"alpha": 5}
    # Turn 2 supplied only `beta`; turn 1's retained `alpha` is gone — that is
    # still incomplete, and it is retained again for the next partial reply.
    services.llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c2", name="demo_write", args={"beta": "q"})]),
        LlmReply(text="done"),
    ])
    await engine.run_turn(Thread(thread_id="t2", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="again"))
    assert demo.calls == []
    assert engine._retained_calls.get("demo_write") == {"beta": "q"}
    assert any("missing required property 'alpha'" in t
               for t in tool_texts(services.llm.last_messages))


@pytest.mark.asyncio
async def test_type_errors_are_not_retained():
    """Only missing fields are accumulated; a wrong type cannot be completed."""
    demo = Demo()
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c1", name="demo_write", args={"alpha": "NaN", "beta": "x"})]),
        LlmReply(text="done"),
    ])
    services = make_services(make_ir(), llm, gate_passed=False)
    engine = engine_with_demo(services, demo)
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="demo"))
    assert demo.calls == []
    assert engine._retained_calls == {}
    assert any("expected integer" in t for t in tool_texts(llm.last_messages))


@pytest.mark.parametrize('mutate', [False, True])
@pytest.mark.asyncio
async def test_repaired_design_review_uses_the_executed_arguments(tmp_path, monkeypatch, mutate):
    from tcad.core.types import TurnState
    from tcad.core.wiring import StoreAdapter
    from tests.unit.test_design_completion import report, review, sized_ir
    from tests.unit.test_loop_engine import make_engine

    class ReviewHooks(FakeHooks):
        def dispatch(self, event, payload):
            if mutate and event == HookEvent.PRE_TOOL_USE and payload.get('tool_name') == 'design_review':
                return HookResult(decision=HookDecision.ALLOW, hook_name='test',
                                  mutated_args={**payload['args'], 'summary': 'Reviewed by policy'})
            return super().dispatch(event, payload)

    source = sized_ir().requirements.raw_text
    first = review(source, ['bbox_spec'])
    first.pop('remaining_work')
    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id='c', name='ir_commit', args={'message': 'build'})]),
        LlmReply(tool_calls=[ToolCall(id='r1', name='design_review', args=first)]),
        LlmReply(tool_calls=[ToolCall(id='r2', name='design_review', args={'remaining_work': []})]),
    ])
    services = make_services(sized_ir(), llm, True, hooks=ReviewHooks())
    services.store = StoreAdapter(tmp_path)
    services.store.create('m1', sized_ir())

    async def commit(services, model_id, version, *args, **kwargs):
        return ToolResult(ok=True), report(version)

    monkeypatch.setattr('tcad.loop.commit.run_commit', commit)
    engine = make_engine(services, max_steps=3)
    engine.config.require_design_review = True
    engine.config.data_dir = str(tmp_path)
    result = await engine.run_turn(Thread(thread_id='t', model_id='m1'), UserMessage(text=source))
    assert result.state == TurnState.SUCCEEDED, result.error
    assert result.completion_review['verified']
    assert result.completion_review['summary'] == ('Reviewed by policy' if mutate else first['summary'])
    assert engine._retained_calls == {}


@pytest.mark.asyncio
async def test_repaired_help_unlocks_the_retained_feature_operation(tmp_path):
    from tests.unit.test_loop_engine import make_engine

    llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id='h1', name='ir_help', args={'feature_op': 'additive_loft'})]),
        LlmReply(tool_calls=[ToolCall(id='h2', name='ir_help', args={'topic': 'feature'})]),
    ])
    services = make_services(make_ir(), llm, False)
    engine = make_engine(services, max_steps=2)
    engine.config.require_design_review = True
    engine.config.data_dir = str(tmp_path)
    await engine.run_turn(Thread(thread_id='t', model_id='m1'), UserMessage(text='make a loft'))
    assert engine._authoring_features == ['additive_loft']
