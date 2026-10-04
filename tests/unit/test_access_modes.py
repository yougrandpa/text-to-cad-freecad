from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcad.config.schema import Config
from tcad.core.access import AccessMode, READ_ONLY_TOOLS
from tcad.core.types import HookDecision, HookEvent, Thread, ToolContext, ToolTier, TurnState
from tcad.hooks.access import AccessHooks
from tcad.hooks.approval import JsonFileApprovalStore
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.engine import UserMessage
from tcad.server.app import ChatRequest, HookEventTap, create_app, run_turn_request
from tcad.tools.base import build_default_registry, execute_tool
from tests.unit.test_loop_engine import FakeHooks, ScriptedLlm, make_ir, make_services, make_engine


@pytest.mark.parametrize("decision,expected", [(HookDecision.ASK, HookDecision.ALLOW),
                                             (HookDecision.DENY, HookDecision.DENY)])
def test_auto_approves_requests_but_preserves_denials(decision, expected):
    hooks = AccessHooks(FakeHooks(decisions={HookEvent.PRE_TOOL_USE: decision}), AccessMode.AUTO)
    assert hooks.dispatch(HookEvent.PRE_TOOL_USE, {"tier":"write", "tool_name":"ir_patch"}).decision == expected


def test_full_only_bypasses_tool_policy_not_build_checks():
    hooks = AccessHooks(FakeHooks(decisions={HookEvent.PRE_TOOL_USE: HookDecision.DENY,
                                            HookEvent.PRE_COMMIT: HookDecision.DENY}), AccessMode.FULL)
    assert hooks.dispatch(HookEvent.PRE_TOOL_USE, {"tool_name":"raw_python"}).decision == HookDecision.ALLOW
    assert hooks.dispatch(HookEvent.PRE_COMMIT, {}).decision == HookDecision.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ir_patch", "ir_commit", "asset_export", "asset_import", "raw_python"])
async def test_readonly_refuses_writes_even_when_model_calls_unoffered_tools(name):
    services = make_services(make_ir(), ScriptedLlm([]), True)
    spec = build_default_registry(services, enable_privileged=True).get(name)
    ctx = ToolContext(thread_id="th", turn_id="t", model_id="m1", access_mode="read_only")
    outcome = await execute_tool(spec, {}, ctx, allowed_tiers=set(ToolTier))
    assert not outcome.result.ok and outcome.result.error.kind.value == "denied"
    assert services.store.applied == []


@pytest.mark.asyncio
async def test_readonly_engine_uses_inspection_tools_and_does_not_backfill_ir():
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="get", name="ir_get", args={})]), LlmReply(text="这是一个底板。")])
    services = make_services(make_ir(), llm, True)
    engine = make_engine(services, enable_privileged=True)
    engine.config.require_design_review = True
    result = await engine.run_turn(Thread(thread_id="th", model_id="m1"),
                                  UserMessage(text="查看尺寸", access_mode="read_only", privileged_requested=True))
    assert result.state == TurnState.INSPECTED and result.gate_report is None
    assert services.store.applied == []
    assert {tool["function"]["name"] for tool in llm.last_tools} == READ_ONLY_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,available", [("auto", False), ("read_only", False), ("full", True)])
async def test_http_runner_only_exposes_python_in_full_mode(mode, available, tmp_path):
    llm = ScriptedLlm([LlmReply(text="查看结果")])
    services = make_services(make_ir(), llm, True)
    services.config = Config()
    services.config.storage.data_dir = str(tmp_path)
    services.config.loop.max_steps_per_turn = 1
    services.loop_config = make_engine(services).config
    await run_turn_request(services, ChatRequest(model_id="m1", text="查看", access_mode=mode, privileged_requested=True))
    assert ("raw_python" in {tool["function"]["name"] for tool in llm.last_tools}) == available
    assert services.config.policy.allow_privileged is False


@pytest.mark.asyncio
async def test_full_python_uses_operator_mode_and_auto_cannot_run_it(monkeypatch):
    calls = []
    async def subprocess(code, **kwargs):
        calls.append(kwargs)
        return "1", "", 0
    monkeypatch.setattr("tcad.tools.privileged._run_subprocess", subprocess)
    services = make_services(make_ir(), ScriptedLlm([]), True)
    spec = build_default_registry(services, enable_privileged=True).get("raw_python")
    for mode in (AccessMode.AUTO, AccessMode.FULL):
        ctx = ToolContext(thread_id="th", turn_id="t", model_id="m1", access_mode=mode,
                          hooks=AccessHooks(FakeHooks(deny_tool="raw_python"), mode))
        outcome = await execute_tool(spec, {"code":"print(1)"}, ctx, allowed_tiers=set(ToolTier))
        assert outcome.result.ok == (mode == AccessMode.FULL)
    assert len(calls) == 1 and calls[0]["sandbox"] is False


def test_approvals_endpoint_filters_current_thread(tmp_path):
    config = Config()
    config.storage.data_dir = str(tmp_path)
    approvals = JsonFileApprovalStore(str(tmp_path / "approvals.json"))
    approvals.request("raw_python", thread_id="test-thread")
    own = approvals.request("ir_patch", thread_id="my-thread")
    services = SimpleNamespace(config=config, approvals=approvals)
    with TestClient(create_app(services, config=config)) as client:
        result = client.get("/approvals", params={"thread_id":"my-thread"}).json()
        assert [record["id"] for record in result["pending"]] == [own.id]
        assert client.get("/approvals", params={"thread_id":"new-thread"}).json() == {"pending": []}


def test_unknown_modes_are_rejected():
    with pytest.raises(ValueError):
        ChatRequest(model_id="m1", text="x", access_mode="typo")


def test_observer_shows_effective_auto_approval():
    tap = HookEventTap(FakeHooks(decisions={HookEvent.PRE_TOOL_USE: HookDecision.ASK}))
    result = AccessHooks(tap, AccessMode.AUTO).dispatch(HookEvent.PRE_TOOL_USE,
                                                     {"tier":"write", "tool_name":"ir_patch"})
    assert result.decision == HookDecision.ALLOW
    assert tap.drain()[0]["decision"] == "allow"


@pytest.mark.asyncio
async def test_full_mode_runs_real_python_without_legacy_sandbox():
    services = make_services(make_ir(), ScriptedLlm([]), True)
    services.config = Config()
    services.config.sandbox.backend = "bwrap"  # legacy path must fail closed
    spec = build_default_registry(services, enable_privileged=True).get("raw_python")
    ctx = ToolContext(thread_id="th", turn_id="t", model_id="m1", access_mode="full",
                      hooks=AccessHooks(FakeHooks(deny_tool="raw_python"), AccessMode.FULL))
    outcome = await execute_tool(spec, {"code":"print(1)"}, ctx, allowed_tiers=set(ToolTier))
    assert outcome.result.ok and outcome.result.content == "1"
