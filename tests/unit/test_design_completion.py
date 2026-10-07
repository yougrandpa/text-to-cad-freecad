"""A base solid must not terminate a functional design request."""
import json

import pytest

from tcad.core.types import CheckResult, GateReport, Thread, TurnState
from tcad.ir.schema import ConstraintExpr, IrDocument, RequirementSpec
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.completion import DesignReview, validate_review
from tcad.loop.engine import UserMessage
from tcad.core.wiring import StoreAdapter
from tcad.tools.ir_tools import ir_patch_handler
from tcad.tools.schema_check import check
from tests.unit.test_loop_engine import make_services, make_engine, ScriptedLlm


def review(source="创建一个削铅笔工具箱", ids=None, remaining=None):
    return dict(summary="已生成草稿，实际削铅笔功能待验收", checklist=[
        dict(source_text=source, check_ids=ids or [])], remaining_work=remaining or [])


@pytest.mark.parametrize('key', ['constraints', 'constraints_append'])
@pytest.mark.parametrize('source,value', [
    ('装配动画需要两个旋转关节', 2),
    ('创建一个带装配动画的直升机。', 2),
])
async def test_inferred_confirmed_counts_are_rejected_before_any_edit(key, source, value):
    from tcad.core.types import ToolContext
    from tests.unit.test_loop_engine import make_ir

    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    ctx = ToolContext(thread_id='t', turn_id='r', model_id='m1',
                      request_text='创建一个带装配动画的直升机。')
    result = await ir_patch_handler(services, {'base_version': 'current', 'ops': [
        {'op': 'rename', 'target_id': 'f1', 'payload': {'name': 'changed'}, 'reason': 'Rename'},
        {'op': 'update_requirement', 'payload': {key: [
            {'kind': 'feature_count', 'target': 'revolute_joints', 'value': value,
             'source_text': source, 'confirmed': True}]}, 'reason': 'Record'}]}, ctx)
    assert not result.ok and 'explicit numeric' in result.error.message
    assert services.store.applied == []
    assert services.store.current_version('m1') == 1


@pytest.mark.parametrize('confirmed,source,value', [
    (False, 'assumed joints', 2),
    (True, '孔径8 mm', 8),
])
async def test_assumptions_and_actual_user_dimensions_can_be_recorded(confirmed, source, value):
    from tcad.core.types import ToolContext
    from tests.unit.test_loop_engine import make_ir

    services = make_services(make_ir(), ScriptedLlm([]), gate_passed=True)
    ctx = ToolContext(thread_id='t', turn_id='r', model_id='m1', request_text='制作支架，孔径8 mm')
    result = await ir_patch_handler(services, {'base_version': 'current', 'ops': [
        {'op': 'update_requirement', 'payload': {'constraints_append': [
            {'kind': 'hole_diameter', 'value': value, 'source_text': source, 'confirmed': confirmed}]},
         'reason': 'Record'}]}, ctx)
    assert result.ok, result.error
    assert len(services.store.applied) == 1


def report(version=1, status="pass", confidence="deterministic", severity="blocking", check_id="bbox_spec"):
    return GateReport(model_id="m1", ir_version=version, passed=True, results=[
        CheckResult(check_id=check_id, status=status, severity=severity, confidence=confidence)])


def sized_ir():
    source="90×45×35 mm"
    return IrDocument(model_id="m1", requirements=RequirementSpec(raw_text=source, constraints=[
        ConstraintExpr(kind="bbox", value=dict(x=90, y=45, z=35), source_text=source, confirmed=True)]))


def test_verified_review_requires_user_sourced_measured_constraints():
    ir = sized_ir()
    result = validate_review(DesignReview(**review(ir.requirements.raw_text, ["bbox_spec"])),
                             ir, report(), ir.requirements.raw_text)
    assert result["verified"] and result["scope"] == "recorded_constraints"
    assert "实际机械功能" in result["note"]


@pytest.mark.parametrize("changes", [dict(status="skip"), dict(status="fail"), dict(status="error"),
                                     dict(confidence="approximate"), dict(severity="advisory"),
                                     dict(check_id="solid_validity")])
def test_geometry_or_unmeasured_results_cannot_attest_function(changes):
    ir = sized_ir()
    result = validate_review(DesignReview(**review(ir.requirements.raw_text, ["bbox_spec"])),
                             ir, report(**changes), ir.requirements.raw_text)
    assert not result["verified"] and result["remaining_work"]


def test_invented_dimensions_cannot_be_confirmed_by_copying_vague_user_words():
    ir = sized_ir()
    ir.requirements.constraints[0].source_text = "创建一个削铅笔工具箱"
    result = validate_review(DesignReview(**review(ids=["bbox_spec"])), ir, report(), "创建一个削铅笔工具箱")
    assert not result["verified"]


def test_report_does_not_hide_another_confirmed_constraint_or_remaining_work():
    ir = sized_ir()
    ir.requirements.constraints.append(ConstraintExpr(kind="hole_diameter", value=8,
        source_text="孔径8 mm", confirmed=True))
    request = ir.requirements.raw_text + " 孔径8 mm"
    result = validate_review(DesignReview(**review(ir.requirements.raw_text, ["bbox_spec"])), ir, report(), request)
    assert not result["verified"]
    assert any("spec_hole_diameter" in item for item in result["remaining_work"])
    ir.requirements.constraints.pop()
    result = validate_review(DesignReview(**review(ir.requirements.raw_text, ["bbox_spec"], ["切削性能待实物测试"])),
                             ir, report(), request)
    assert not result["verified"]


@pytest.mark.parametrize("strategy", ["loop_until_done", "fork_join", "adversarial"])
async def test_first_green_commit_continues_geometry_then_reviews_as_draft(tmp_path, monkeypatch, strategy):
    calls = [
        ToolCall(id="base", name="ir_commit", args={"message": "base only"}),
        ToolCall(id="cavity", name="ir_patch", args={"base_version": "current", "ops": [{
            "op": "add_feature", "reason": "Add the missing cavity", "payload": {
                "id": "ft_cavity", "op": "subtractive_box", "params": {"length": 80, "width": 35, "height": 30}}}]}),
        ToolCall(id="final", name="ir_commit", args={"message": "cavity build"}),
        ToolCall(id="review", name="design_review", args=review()),
    ]
    svc = make_services(IrDocument(model_id="m1"), ScriptedLlm([LlmReply(tool_calls=[c]) for c in calls]), True)
    svc.store = StoreAdapter(tmp_path)
    svc.store.create("m1", IrDocument(model_id="m1"))
    built = []
    async def commit(services, model_id, version, *args, **kwargs):
        from tcad.core.types import ToolResult
        built.append(version)
        return ToolResult(ok=True, content="Build passes; review the request"), report(version)
    monkeypatch.setattr("tcad.loop.commit.run_commit", commit)
    engine = make_engine(svc, strategy=strategy)
    engine.config.require_design_review = True
    result = await engine.run_turn(Thread(thread_id="th1", model_id="m1"), UserMessage(text="创建一个削铅笔工具箱"))
    assert built == [1, 2], result.error  # request is recorded before tools
    assert result.state == TurnState.DRAFT and result.steps == 4
    assert result.gate_report.passed and not result.completion_review["verified"]
    assert svc.store.load("m1").requirements.raw_text == "创建一个削铅笔工具箱"
    assert svc.store.load("m1").find_feature("ft_cavity") is not None


async def test_review_rejects_stale_gate_and_requires_new_commit(tmp_path, monkeypatch):
    svc = make_services(sized_ir(), ScriptedLlm([]), True)
    svc.store = StoreAdapter(tmp_path); svc.store.create("m1", sized_ir())
    async def commit(services, model_id, version, *args, **kwargs):
        from tcad.core.types import ToolResult
        return ToolResult(ok=True), report(version)
    monkeypatch.setattr("tcad.loop.commit.run_commit", commit)
    source = sized_ir().requirements.raw_text
    svc.llm = ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c", name="ir_commit", args={"message":"build"})]),
        LlmReply(tool_calls=[ToolCall(id="p", name="ir_patch", args={"base_version":"current","ops":[
            {"op":"update_requirement","reason":"new constraint","payload":{"constraints_append":[]}}]})]),
        LlmReply(tool_calls=[ToolCall(id="r", name="design_review", args=review(source,["bbox_spec"]))]),
    ])
    engine = make_engine(svc, max_steps=3); engine.config.require_design_review=True
    result = await engine.run_turn(Thread(thread_id="th1", model_id="m1"), UserMessage(text=source))
    assert result.state != TurnState.SUCCEEDED
    assert "old Gate evidence is invalid" in json.dumps(svc.llm.last_messages)


async def test_model_cannot_erase_the_preserved_request(tmp_path):
    svc = make_services(sized_ir(), ScriptedLlm([]), True)
    from tcad.core.types import ToolContext
    ctx = ToolContext(thread_id="t", turn_id="r", model_id="m1", request_text="原始需求")
    result = await ir_patch_handler(svc, {"base_version":1,"ops":[
        {"op":"update_requirement","reason":"erase","payload":{"raw_text":""}}]}, ctx)
    assert not result.ok and not svc.store.applied


@pytest.mark.parametrize("args", [review() | {"checklist": []}, review() | {"summary": ""},
                                  review() | {"remaining_work": ["x"] * 65}])
def test_review_schema_enforces_real_bounds(args):
    assert check(args, DesignReview.model_json_schema())

async def test_measured_review_succeeds_only_after_the_review_call(tmp_path, monkeypatch):
    from tcad.core.types import ToolResult
    source = sized_ir().requirements.raw_text
    svc = make_services(sized_ir(), ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c", name="ir_commit", args={"message":"final build"})]),
        LlmReply(tool_calls=[ToolCall(id="r", name="design_review", args=review(source,["bbox_spec"]))]),
    ]), True)
    svc.store = StoreAdapter(tmp_path);svc.store.create("m1", sized_ir())
    async def commit(services, model_id, version, *args, **kwargs):
        return ToolResult(ok=True), report(version)
    monkeypatch.setattr("tcad.loop.commit.run_commit", commit)
    engine = make_engine(svc);engine.config.require_design_review=True
    result = await engine.run_turn(Thread(thread_id="th1",model_id="m1"), UserMessage(text=source))
    assert result.state == TurnState.SUCCEEDED and result.steps == 2
    assert result.completion_review["verified"]
    assert result.completion_review["scope"] == "recorded_constraints"


async def test_old_session_request_is_backfilled_from_user_history(tmp_path):
    from tcad.context.compactor import Message
    svc = make_services(IrDocument(model_id="m1"), ScriptedLlm([]), True)
    svc.store=StoreAdapter(tmp_path);svc.store.create("m1",IrDocument(model_id="m1"))
    engine=make_engine(svc,max_steps=1);engine.config.require_design_review=True
    engine._history_provider=lambda *_: [Message(role="user",content="创建一个削铅笔工具箱"),
                                        Message(role="assistant",content="已完成，不需要孔")]
    await engine.run_turn(Thread(thread_id="th1",model_id="m1"),UserMessage(text="继续完善"))
    raw=svc.store.load("m1").requirements.raw_text
    assert "创建一个削铅笔工具箱" in raw and "继续完善" in raw
    assert "已完成，不需要孔" not in raw
    assert any(raw in message["content"] for message in svc.llm.last_messages)

async def test_model_stopping_after_build_returns_unreviewed_draft(tmp_path, monkeypatch):
    from tcad.core.types import ToolResult
    svc=make_services(sized_ir(),ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c",name="ir_commit",args={"message":"build"})])]),True)
    svc.store=StoreAdapter(tmp_path);svc.store.create("m1",sized_ir())
    async def commit(services,model_id,version,*args,**kwargs):
        return ToolResult(ok=True), report(version)
    monkeypatch.setattr("tcad.loop.commit.run_commit",commit)
    engine=make_engine(svc);engine.config.require_design_review=True
    result=await engine.run_turn(Thread(thread_id="th1",model_id="m1"),UserMessage(text=sized_ir().requirements.raw_text))
    assert result.state==TurnState.DRAFT and result.gate_report.passed
    assert not result.completion_review["verified"]
    assert "模型没有提交" in result.completion_review["summary"]
