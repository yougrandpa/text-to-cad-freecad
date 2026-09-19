"""Engine tests — the headline invariants (design §4.1, §9, §10).

The single success condition is a green Gate; a model merely *saying* it is done
is not a termination. The model never touches FreeCAD (writes go through ir_*).
All collaborators are fakes (Protocol-based), so this runs with no FreeCAD.
"""

from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace

import pytest

from tcad.core.types import (
    CheckResult,
    CheckStatus,
    Confidence,
    GateReport,
    GeometryDigest,
    HookDecision,
    HookEvent,
    HookResult,
    ImageRef,
    Severity,
    Thread,
    TurnKind,
    TurnState,
)
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.tools.base import build_default_registry


# ─── fakes ─────────────────────────────────────────────────────────────────


def make_ir() -> IrDocument:
    return IrDocument(
        model_id="m1",
        version=1,
        bodies=[
            BodySpec(
                id="b1",
                name="body",
                features=[FeatureSpec(id="f1", name="pad1", op="pad", params={"length": 10})],
            )
        ],
    )


class FakeStore:
    def __init__(self, ir):
        self.ir = ir
        self.applied = []

    def load(self, model_id, version=None):
        return self.ir

    def current_version(self, model_id):
        return self.ir.version

    def apply_patch(self, model_id, patch):
        self.applied.append(patch)
        new = self.ir.model_copy(deep=True)
        new.version += 1
        return new, SimpleNamespace(seq=0, model_id=model_id)

    def validate_patch(self, ir, patch):
        return []

    def validate_document(self, ir):
        return []

    def persist_digest(self, model_id, ir_version, digest):
        pass


class FakeWorker:
    def request(self, method, params, timeout_s=30.0):
        if method == "compile_ir":
            return {"ok": True, "result": {"fcstd": "/tmp/x.FCStd"}}
        if method == "export_artifacts":
            return {"ok": True, "result": {"files": {"step": "/tmp/x.step"}}}
        if method == "introspect_document":
            return {"ok": True, "result": GeometryDigest(model_id="m1", ir_version=1, text="digest").model_dump()}
        if method == "tessellate":
            return {
                "ok": True,
                "result": {
                    "mesh": {"vertices": [(0, 0, 0), (1, 0, 0), (0, 1, 0)], "facets": [(0, 1, 2)]},
                    "bbox": {},
                    "volume": 1.0,
                },
            }
        if method == "import_asset":
            return {"ok": True, "result": {"shape_summary": {"faces": 6}}}
        return {"ok": True, "result": {}}


class FakeGate:
    def __init__(self, passed):
        self.passed = passed

    def evaluate(self, model_id, ir_version):
        if self.passed:
            return GateReport(model_id=model_id, ir_version=ir_version, passed=True, results=[],
                              blocking_failures=[], advisory_findings=[])
        return GateReport(
            model_id=model_id,
            ir_version=ir_version,
            passed=False,
            results=[
                CheckResult(
                    check_id="solid_validity",
                    status=CheckStatus.FAIL,
                    severity=Severity.BLOCKING,
                    confidence=Confidence.DETERMINISTIC,
                    message="body is not valid",
                    feature_id="f1",
                )
            ],
            blocking_failures=["solid_validity: body is not valid (feature_id=f1)"],
            advisory_findings=[],
        )


class FakeRenderer:
    def render(self, mesh, out_dir, views, style, width, height):
        return [ImageRef(path=f"{out_dir}/{v}.png", view=v, width=width, height=height) for v in views]


class FakeContext:
    def digest(self, model_id, ir_version):
        return GeometryDigest(model_id=model_id, ir_version=ir_version, text="digest")


class FakeHooks:
    def __init__(self, deny_tool=None):
        self.events = []
        self.deny_tool = deny_tool

    def dispatch(self, event, payload):
        self.events.append((event, payload))
        if event == HookEvent.PRE_TOOL_USE and self.deny_tool and payload.get("tool_name") == self.deny_tool:
            return HookResult(decision=HookDecision.DENY, hook_name="test",
                              reason=f"policy denies {self.deny_tool}")
        return HookResult(decision=HookDecision.ALLOW, hook_name="test")


class ScriptedLlm:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.last_messages = None
        self.last_tools = None

    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        self.calls += 1
        self.last_messages = messages
        self.last_tools = tools
        if self._replies:
            return self._replies.pop(0)
        return LlmReply(text="I'm done, the part is complete.")


class RaisingLlm:
    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        raise RuntimeError("llm is down")


def make_services(ir, llm, gate_passed, hooks=None):
    return SimpleNamespace(
        store=FakeStore(ir),
        worker=FakeWorker(),
        gate=FakeGate(gate_passed),
        renderer=FakeRenderer(),
        hooks=hooks or FakeHooks(),
        context=FakeContext(),
        llm=llm,
    )


def make_engine(services, *, max_steps=24, enable_privileged=False, strategy="loop_until_done"):
    reg = build_default_registry(services, enable_privileged=enable_privileged)
    cfg = LoopConfig(
        max_steps_per_turn=max_steps,
        allow_privileged=enable_privileged,
        data_dir=tempfile.mkdtemp(),
        default_strategy=strategy,
    )
    return LoopEngine(services, reg, BudgetLimits(max_steps_per_turn=max_steps), cfg)


def make_uncapped_engine(services, *, strategy="loop_until_done"):
    """An engine with the shipped configuration: no ceilings at all."""
    reg = build_default_registry(services)
    cfg = LoopConfig(data_dir=tempfile.mkdtemp(), default_strategy=strategy)
    return LoopEngine(services, reg, BudgetLimits(), cfg)


# ─── headline: green gate required; "I'm done" is NOT a termination ────────


async def test_gate_false_means_not_succeeded_and_failure_fed_back():
    ir = make_ir()
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="ir_commit", args={"message": "go"})])])
    svc = make_services(ir, llm, gate_passed=False)
    engine = make_engine(svc, max_steps=3)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="bracket"))

    assert result.state != TurnState.SUCCEEDED
    assert result.gate_report is None or result.gate_report.passed is False
    # The gate's failure detail was fed back into the conversation.
    blob = json.dumps(llm.last_messages)
    assert "GATE FAILED" in blob
    assert "solid_validity" in blob
    assert "f1" in blob


async def test_gate_true_means_succeeded():
    ir = make_ir()
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="ir_commit", args={"message": "go"})])])
    svc = make_services(ir, llm, gate_passed=True)
    engine = make_engine(svc, max_steps=10)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="bracket"))

    assert result.state == TurnState.SUCCEEDED
    assert result.gate_report is not None and result.gate_report.passed is True


async def test_model_claiming_done_without_gate_is_not_success():
    # Model never calls ir_commit; it just keeps saying "done". Budget trips -> EXHAUSTED.
    ir = make_ir()
    llm = ScriptedLlm([])  # always returns "I'm done" text, no tool calls
    svc = make_services(ir, llm, gate_passed=True)
    engine = make_engine(svc, max_steps=3)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="bracket"))

    assert result.state == TurnState.EXHAUSTED
    assert result.state != TurnState.SUCCEEDED


# ─── inspect turns expose no write tools (assert by name) ──────────────────


async def test_inspect_turn_exposes_only_read_tools():
    ir = make_ir()
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="ir_get", args={})])])
    svc = make_services(ir, llm, gate_passed=True)
    engine = make_engine(svc, max_steps=3)
    thread = Thread(thread_id="th1", model_id="m1")
    await engine.run_turn(thread, UserMessage(kind=TurnKind.INSPECT, text="show me"))

    names = {t["function"]["name"] for t in llm.last_tools}
    assert "ir_patch" not in names
    assert "ir_commit" not in names
    assert names <= {"ir_get", "ir_digest", "ir_list_features", "geo_view", "geo_measure", "asset_export", "asset_import"}


async def test_privileged_absent_unless_enabled():
    """The escape hatch needs BOTH conditions (design §4.2): the operator must
    enable it in config AND the turn must explicitly request it. Either one alone
    must leave raw_python invisible to the model."""
    ir = make_ir()
    thread = Thread(thread_id="th1", model_id="m1")

    # 1. config off, not requested -> absent
    llm_off = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="geo_view", args={"views": ["iso"]})])])
    svc_off = make_services(ir, llm_off, gate_passed=True)
    engine_off = make_engine(svc_off, max_steps=3, enable_privileged=False)
    await engine_off.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="x"))
    assert "raw_python" not in {t["function"]["name"] for t in llm_off.last_tools}

    # 2. config ON but the turn did NOT request it -> still absent
    llm_cfg_only = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="geo_view", args={"views": ["iso"]})])])
    svc_cfg_only = make_services(ir, llm_cfg_only, gate_passed=True)
    engine_cfg_only = make_engine(svc_cfg_only, max_steps=3, enable_privileged=True)
    await engine_cfg_only.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="x"))
    assert "raw_python" not in {t["function"]["name"] for t in llm_cfg_only.last_tools}

    # 3. config ON and the turn explicitly requested it -> present
    llm_on = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="geo_view", args={"views": ["iso"]})])])
    svc_on = make_services(ir, llm_on, gate_passed=True)
    engine_on = make_engine(svc_on, max_steps=3, enable_privileged=True)
    await engine_on.run_turn(
        thread,
        UserMessage(kind=TurnKind.CREATE, text="x", privileged_requested=True),
    )
    assert "raw_python" in {t["function"]["name"] for t in llm_on.last_tools}

    # 4. requested but config OFF -> must stay absent (config is the hard stop)
    llm_req_only = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="geo_view", args={"views": ["iso"]})])])
    svc_req_only = make_services(ir, llm_req_only, gate_passed=True)
    engine_req_only = make_engine(svc_req_only, max_steps=3, enable_privileged=False)
    await engine_req_only.run_turn(
        thread,
        UserMessage(kind=TurnKind.CREATE, text="x", privileged_requested=True),
    )
    assert "raw_python" not in {t["function"]["name"] for t in llm_req_only.last_tools}


# ─── hook DENY becomes a visible tool error and the loop continues ─────────


async def test_hook_deny_is_visible_tool_error_and_loop_continues():
    ir = make_ir()
    hooks = FakeHooks(deny_tool="ir_patch")
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="ir_patch", args={
        "base_version": 1, "ops": [{"op": "add_feature", "target_id": "f2", "payload": {"op": "pad"}}],
        "summary": "try",
    })])])
    svc = make_services(ir, llm, gate_passed=True, hooks=hooks)
    engine = make_engine(svc, max_steps=5, enable_privileged=False)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="add a hole"))

    # The denial became a visible tool error in the conversation.
    blob = json.dumps(llm.last_messages)
    assert "[tool error: denied]" in blob
    # Hooks fired on both sides; the patch was refused before execution.
    assert HookEvent.PRE_TOOL_USE in [e for e, _ in hooks.events]
    assert HookEvent.POST_TOOL_USE in [e for e, _ in hooks.events]
    assert len(svc.store.applied) == 0
    # No crash: a normal terminal state was reached.
    assert result.state in (TurnState.EXHAUSTED, TurnState.FAILED, TurnState.SUCCEEDED)


# ─── post_turn fires even when the LLM raises ──────────────────────────────


async def test_post_turn_fires_even_when_llm_raises():
    ir = make_ir()
    hooks = FakeHooks()
    llm = RaisingLlm()
    svc = make_services(ir, llm, gate_passed=True, hooks=hooks)
    engine = make_engine(svc, max_steps=5)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="bracket"))

    assert result.state == TurnState.FAILED
    assert HookEvent.PRE_TURN in [e for e, _ in hooks.events]
    assert HookEvent.POST_TURN in [e for e, _ in hooks.events]


# ─── strategy raising degrades to M1 (never kills the turn) ────────────────


async def test_unknown_tool_is_safe_error_not_crash():
    ir = make_ir()
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="c1", name="no_such_tool", args={})])])
    svc = make_services(ir, llm, gate_passed=True)
    engine = make_engine(svc, max_steps=3)
    thread = Thread(thread_id="th1", model_id="m1")
    result = await engine.run_turn(thread, UserMessage(kind=TurnKind.CREATE, text="x"))
    blob = json.dumps(llm.last_messages)
    assert "[tool error: not_found]" in blob
    assert result.state != TurnState.SUCCEEDED


# ─── thinking-mode protocol ────────────────────────────────────────────────
# Found on the first real DeepSeek run: every turn died at step 2 with
#   400 "The `reasoning_content` in the thinking mode must be passed back to
#        the API."
# Nothing in the offline suite could have caught it — the stub model emits no
# reasoning, and the requirement only exists in the live protocol.


async def test_reasoning_content_is_echoed_back_with_its_tool_calls():
    """A thinking model's reasoning must be replayed alongside the call.

    A turn is a multi-step loop, so every step after the first replays the
    previous assistant message — which is exactly why this breaks at step 2 and
    nowhere else.
    """
    ir = make_ir()
    llm = ScriptedLlm([
        LlmReply(
            text="",
            reasoning_content="let me look at the current model",
            tool_calls=[ToolCall(id="c1", name="ir_list_features", args={})],
        ),
        LlmReply(text="done", reasoning_content="nothing left to do"),
    ])
    services = make_services(ir, llm, gate_passed=True)
    engine = make_engine(services, max_steps=3)
    await engine.run_turn(
        Thread(thread_id="t1", model_id="m1"),
        UserMessage(kind=TurnKind.CREATE, text="x"),
    )

    replayed = [m for m in llm.last_messages if m.get("role") == "assistant"]
    assert replayed, "assistant 消息没有被回放"
    assert replayed[0].get("reasoning_content") == "let me look at the current model", (
        "thinking 模式下缺少 reasoning_content —— DeepSeek 会以 400 拒绝"
    )


async def test_models_without_reasoning_gain_no_stray_field():
    """The echo must be conditional: an ordinary model's assistant message must
    not acquire a `reasoning_content: null`."""
    ir = make_ir()
    llm = ScriptedLlm([
        LlmReply(text="", tool_calls=[ToolCall(id="c1", name="ir_list_features", args={})]),
        LlmReply(text="done"),
    ])
    services = make_services(ir, llm, gate_passed=True)
    engine = make_engine(services, max_steps=3)
    await engine.run_turn(
        Thread(thread_id="t1", model_id="m1"),
        UserMessage(kind=TurnKind.CREATE, text="x"),
    )

    replayed = [m for m in llm.last_messages if m.get("role") == "assistant"]
    assert replayed
    assert "reasoning_content" not in replayed[0]


# ══════════════════════════════════════════════════════════════════════════
# termination with no work ceiling
#
# The shipped budget has no limits, so nothing bounds a turn except reaching a
# terminal state. A model that answers with text and no tool call cannot change
# anything, and repeating the request would repeat the answer — that used to be
# caught by `max_steps_per_turn`, and without it the loop would spin forever,
# spending tokens and never ending.
# ══════════════════════════════════════════════════════════════════════════


class NarrationOnlyLlm:
    """Always answers with prose and never calls a tool."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *, messages, tools=None, tool_choice=None, temperature=None):
        self.calls += 1
        return LlmReply(text=f"thinking about it… ({self.calls})")


async def test_a_narrating_model_cannot_spin_forever_with_no_ceiling():
    """Bounded, and NOT reported as success — nothing was verified."""
    import asyncio

    from tcad.loop.engine import MAX_IDLE_STEPS

    ir = make_ir()
    llm = NarrationOnlyLlm()
    services = make_services(ir, llm, gate_passed=True)
    engine = make_uncapped_engine(services)     # exactly the shipped configuration

    # Wrapped in a timeout as a *best-effort* safety net, not a guarantee.
    #
    # It only helps if the loop actually yields: an `async def` that never awaits
    # runs to completion without ever giving the event loop control, and then no
    # external timer can fire — not this one, and not a `turn_wall_clock_s`
    # either. (Measured: with the guard removed and a resolve-immediately fake
    # LLM, the loop becomes one long synchronous run that starves the loop and is
    # killed before any timeout gets a chance.) Which is the point: the stall
    # detector has to live *inside* the loop, because from outside there is
    # nothing to interrupt.
    result = await asyncio.wait_for(
        engine.run_turn(
            Thread(thread_id="t1", model_id="m1"),
            UserMessage(kind=TurnKind.CREATE, text="x"),
        ),
        timeout=5.0,
    )

    assert result.state is TurnState.FAILED
    assert result.state is not TurnState.SUCCEEDED
    assert "no progress" in (result.error or "")
    # Two assertions, and both are needed.
    #
    # The equality pins the *mechanism*: it stops exactly when the counter says,
    # not later. On its own it is self-referential — it compares against the very
    # constant under test, so it would pass for any value, including one so large
    # the guard never fires in practice.
    assert llm.calls == MAX_IDLE_STEPS + 1, llm.calls
    # ...so the magnitude is pinned separately, against a literal.
    assert llm.calls <= 5, f"停滞检测太宽松了：{llm.calls} 次空转才停"


async def test_any_tool_call_resets_the_stall_counter():
    """A slow turn must never be cut off by the stall detector — only a turn
    that has stopped doing anything."""
    ir = make_ir()
    llm = ScriptedLlm([
        LlmReply(text="hmm"),
        LlmReply(text="let me look", tool_calls=[ToolCall(id="c1", name="ir_get", args={})]),
        LlmReply(text="still looking"),
        LlmReply(text="and again", tool_calls=[ToolCall(id="c2", name="ir_get", args={})]),
        LlmReply(text="done"),
        LlmReply(text="really done"),
    ])
    services = make_services(ir, llm, gate_passed=True)
    engine = make_uncapped_engine(services)

    result = await engine.run_turn(
        Thread(thread_id="t1", model_id="m1"),
        UserMessage(kind=TurnKind.CREATE, text="x"),
    )
    # It ran past MAX_IDLE_STEPS total no-op steps, which is only possible if the
    # tool calls in between reset the counter.
    assert llm.calls > 4, llm.calls
    assert "no progress" in (result.error or ""), result.error
