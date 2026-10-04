"""Progress, authoritative completion and whole-turn context regressions."""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from tcad.core.types import HookDecision, HookEvent, HookResult, Thread, ToolResult, TurnKind, TurnState
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.engine import UserMessage
from tcad.context.assembler import ContextAssembler
from tcad.context.turn_compaction import estimate_request_tokens, input_budget
from tests.unit.test_loop_engine import (FakeGate, FakeHooks, ScriptedLlm, make_engine,
                                       make_ir, make_services, make_uncapped_engine)

STRATEGIES = ["loop_until_done", "fork_join", "adversarial"]

def call(name, ident, args=None):
    return ToolCall(id=ident, name=name, args=args if args is not None else ({"message":"verify current model"} if name=="ir_commit" else {}))

def patch_args():
    return {"base_version":"current", "ops":[{"op":"rename","target_id":"f1",
            "payload":{"name":"updated"},"reason":"requested change"}]}

async def run(engine):
    return await engine.run_turn(Thread(thread_id="th1",model_id="m1"),
                                 UserMessage(kind=TurnKind.MODIFY,text="KEEP_SLOT_40X20"))

@pytest.mark.parametrize("strategy",STRATEGIES)
async def test_approval_after_green_gate_wins_over_success(strategy):
    class AskPatch(FakeHooks):
        def dispatch(self,event,payload):
            if event == HookEvent.PRE_TOOL_USE and payload.get("tool_name")=="ir_patch":
                return HookResult(decision=HookDecision.ASK,hook_name="permission",reason="needs approval")
            return super().dispatch(event,payload)
    llm=ScriptedLlm([LlmReply(tool_calls=[call("ir_commit","a"),call("ir_patch","b",patch_args()),call("ir_commit","c")])])
    svc=make_services(make_ir(),llm,True,hooks=AskPatch())
    result=await run(make_engine(svc,strategy=strategy))
    assert result.state == TurnState.AWAITING_APPROVAL
    assert result.gate_report is None
    assert svc.store.applied == []
    assert {m["tool_call_id"] for m in llm.last_messages if m["role"]=="tool"}=={"a","b","c"}
    assert "NOT executed" in llm.last_messages[-1]["content"]

@pytest.mark.parametrize("strategy",STRATEGIES)
async def test_failed_recommit_cannot_promote_prior_green_candidate(strategy):
    class ChangingGate(FakeGate):
        calls=0
        def evaluate(self,*args):
            self.calls+=1;self.passed=self.calls==1
            return super().evaluate(*args)
    llm=ScriptedLlm([LlmReply(tool_calls=[call("ir_commit","a"),call("ir_commit","b")])])
    svc=make_services(make_ir(),llm,True);svc.gate=ChangingGate(True)
    result=await run(make_engine(svc,strategy=strategy))
    assert result.state != TurnState.SUCCEEDED
    assert result.gate_report is not None and not result.gate_report.passed

@pytest.mark.parametrize("strategy",STRATEGIES)
async def test_new_commit_after_mutation_verifies_current_version(strategy):
    llm=ScriptedLlm([LlmReply(tool_calls=[call("ir_commit","a"),call("ir_patch","b",patch_args())]),
                    LlmReply(tool_calls=[call("ir_commit","c")])])
    svc=make_services(make_ir(),llm,True)
    original=svc.store.apply_patch
    def persist(*args):
        new,event=original(*args);svc.store.ir=new;return new,event
    svc.store.apply_patch=persist
    result=await run(make_engine(svc,strategy=strategy))
    assert result.state==TurnState.SUCCEEDED
    assert result.gate_report.ir_version==svc.store.current_version("m1")==2
    assert result.steps==2

async def test_identical_failed_tools_stop_uncapped_turn_and_complete_skipped_batch():
    llm=ScriptedLlm([LlmReply(tool_calls=[call("unknown","a"),call("unknown","b"),call("unknown","c"),
                                       call("ir_patch","d",patch_args())])])
    svc=make_services(make_ir(),llm,True)
    result=await run(make_uncapped_engine(svc))
    assert result.state==TurnState.FAILED and "failed identically 3 times" in result.error
    assert svc.store.applied==[]
    assert {m["tool_call_id"] for m in llm.last_messages if m["role"]=="tool"}=={"a","b","c","d"}
    assert "NOT executed" in llm.last_messages[-1]["content"]

async def test_diagnostic_reads_do_not_reset_identical_gate_failures():
    replies=[]
    for i in range(3):
        replies.append(LlmReply(tool_calls=[call("ir_commit",f"c{i}",{"message":f"fresh narration {i}"})]))
        replies.append(LlmReply(tool_calls=[call("ir_get",f"r{i}")]))
    svc=make_services(make_ir(),ScriptedLlm(replies),False)
    result=await run(make_uncapped_engine(svc))
    assert result.state==TurnState.FAILED and result.steps==5
    assert "ir_commit failed identically 3 times" in result.error

async def test_successful_mutation_resets_identical_failures():
    replies=[LlmReply(tool_calls=[call("unknown","a"),call("unknown","b")]),
             LlmReply(tool_calls=[call("ir_patch","p",patch_args())]),
             LlmReply(tool_calls=[call("unknown","c"),call("unknown","d")]),
             LlmReply(tool_calls=[call("ir_commit","e")])]
    svc=make_services(make_ir(),ScriptedLlm(replies),True)
    result=await run(make_uncapped_engine(svc))
    assert result.state==TurnState.SUCCEEDED and result.steps==4

async def test_recovery_state_does_not_leak_across_turns():
    svc=make_services(make_ir(),ScriptedLlm([LlmReply(tool_calls=[call("unknown",str(i))]) for i in range(3)]),True)
    engine=make_uncapped_engine(svc)
    assert (await run(engine)).state==TurnState.FAILED
    svc.llm=ScriptedLlm([LlmReply(tool_calls=[call("unknown","new1"),call("unknown","new2"),call("ir_commit","new3")])])
    assert (await run(engine)).state==TurnState.SUCCEEDED

async def test_operator_can_disable_stall_guard_without_changing_work_budget():
    svc=make_services(make_ir(),ScriptedLlm([LlmReply(tool_calls=[call("unknown",str(i))]) for i in range(5)]),True)
    engine=make_engine(svc,max_steps=4);engine.config.repeated_tool_failure_limit=None
    result=await run(engine)
    assert result.state==TurnState.EXHAUSTED

async def test_oversize_request_fails_before_contacting_provider():
    svc=make_services(make_ir(),ScriptedLlm([]),True)
    engine=make_engine(svc);engine.config.context_window_tokens=100
    result=await run(engine)
    assert result.state==TurnState.FAILED
    assert "ContextWindowExceeded" in result.error
    assert svc.llm.calls==0 and svc.store.applied==[]

async def test_long_turn_compacts_complete_rounds_and_preserves_current_request():
    class RecordingLlm(ScriptedLlm):
        def __init__(self,replies):super().__init__(replies);self.requests=[]
        async def chat(self,**kw):
            self.requests.append(deepcopy(kw));return await super().chat(**kw)
    llm=RecordingLlm([LlmReply(reasoning_content=f"reasoning{i}",tool_calls=[call("ir_get",f"read{i}")]) for i in range(7)]
                     +[LlmReply(tool_calls=[call("ir_commit","finish")])])
    svc=make_services(make_ir(),llm,True)
    engine=make_engine(svc);engine._context_assembler=ContextAssembler()
    async def large_read(args,ctx):return ToolResult(ok=True,content="LATEST_DETAILS_"+"x"*9000)
    engine.registry.get("ir_get").handler=large_read
    tools=engine.registry.as_openai_tools(TurnKind.MODIFY,include_privileged=False)
    base=estimate_request_tokens([{"role":"system","content":engine.config.system_prompt}],tools)
    engine.config.context_window_tokens=base+engine.config.llm_max_tokens+6500
    events=[];engine._observer=lambda kind,data:events.append((kind,data))
    result=await run(engine)
    assert result.state==TurnState.SUCCEEDED, result.error
    assert any(kind=="context" and data["dropped_batches"]>0 for kind,data in events)
    assert llm.calls==8
    for request in llm.requests:
        messages=request["messages"]
        assert estimate_request_tokens(messages,request["tools"])<=input_budget(engine.config.context_window_tokens,engine.config.llm_max_tokens)
        assert any(m["role"]=="user" and m["content"]=="KEEP_SLOT_40X20" for m in messages)
        calls={c["id"] for m in messages for c in m.get("tool_calls",[])}
        results={m["tool_call_id"] for m in messages if m["role"]=="tool"}
        assert calls==results
        for m in messages:
            if m.get("tool_calls") and m["tool_calls"][0]["function"]["name"]=="ir_get":
                assert m["reasoning_content"].startswith("reasoning")
    assert any("Context compaction:" in m.get("content","") for m in llm.requests[-1]["messages"])

async def test_compaction_cannot_discard_history_when_snapshot_refresh_fails():
    svc=make_services(make_ir(),ScriptedLlm([]),True)
    engine=make_engine(svc)
    engine._current_user_message=UserMessage(text="keep the exact dimensions")
    engine._trace_start=2
    engine.config.context_window_tokens=100
    original=[{"role":"system","content":"rules"},{"role":"user","content":"keep the exact dimensions"},
              {"role":"assistant","content":"x"*3000}]
    saved=deepcopy(original)
    svc.store.load=lambda *a,**kw: (_ for _ in ()).throw(OSError("snapshot unreadable"))
    with pytest.raises(RuntimeError,match="cannot refresh current CAD"):
        await engine._prepare_step_context(SimpleNamespace(model_id="m1",thread_id="th1"),original,[])
    assert original==saved
    assert svc.llm.calls==0
