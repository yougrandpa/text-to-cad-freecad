"""The design carries a plan; recovery must not silently degrade it (P1-5)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tcad.core.wiring import StoreAdapter
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.intent import (
    IntentPlan,
    PlannedPart,
    degradation_items,
    design_notes,
    failure_history,
    intent_note,
    read_plan,
    recovery_note,
    review_intent,
    write_plan,
)
from tests.unit.test_design_completion import report, review, sized_ir
from tests.unit.test_loop_engine import ScriptedLlm, make_engine, make_services


def plan(*parts) -> IntentPlan:
    return IntentPlan(model_id="m1", recorded_version=1, parts=list(parts))


def part(id, goal, origin="model", status="planned", note=""):
    return PlannedPart(id=id, goal=goal, origin=origin, status=status, note=note)


def ir_with(*features, body="base"):
    return IrDocument(model_id="m1", bodies=[BodySpec(
        id=body, name=body,
        features=[FeatureSpec(id=f, name=f, op="additive_box") for f in features])])


def test_review_classifies_kept_lost_and_reduced():
    checked = review_intent(plan(
        part("base", "the mounting plate", origin="user"),
        part("backrest", "弧形靠背、腰部支撑", origin="user", status="simplified",
             note="本轮简化为平板"),
        part("headrest", "an optional headrest"),
    ), ir_with("base"))
    assert [e["id"] for e in checked["kept"]] == ["base"]
    assert [e["id"] for e in checked["lost"]] == ["headrest"]
    assert [e["id"] for e in checked["reduced"]] == ["backrest"]
    # Only the USER-required reduction is a degradation; the model's own
    # dropped choice is not.
    assert [e["id"] for e in checked["degraded"]] == ["backrest"]


def test_reducing_a_user_part_requires_a_note():
    with pytest.raises(ValidationError, match="note"):
        part("backrest", "curved backrest", origin="user", status="dropped")
    # The model's own part does not need one.
    part("hook", "my own hook", origin="model", status="dropped")


def test_curvature_goal_built_from_straight_primitives_is_flagged():
    checked = review_intent(plan(
        part("backrest", "弧形靠背与腰部支撑", origin="user")), ir_with("backrest"))
    assert checked["kept"] and checked["advisories"]
    assert "straight primitives" in checked["advisories"][0]
    # A curved feature clears the flag.
    curved = IrDocument(model_id="m1", bodies=[BodySpec(id="backrest", name="backrest", features=[
        FeatureSpec(id="backrest", name="backrest", op="additive_loft")])])
    assert not review_intent(plan(part("backrest", "弧形靠背", origin="user")), curved)["advisories"]


@pytest.mark.parametrize('recipe', [
    {'id': 'propeller', 'body_id': 'rotor_body', 'shape': 'rotor',
     'center': [0, 0, 0], 'axis': [0, 0, 1], 'radius': 10, 'blade_count': 2,
     'blade_width': 2, 'thickness': 1, 'hub_radius': 2},
    *[{'id': 'posts', 'body_id': 'frame', 'shape': 'cylinder',
       'start': [5, 0, 0], 'end': [5, 0, 8], 'radius': 1,
       'copies': {'count': 3, 'center': [0, 0, 0], 'separate_bodies': separate}}
      for separate in (False, True)],
])
def test_recipe_identity_survives_serialization_and_feature_renames(recipe):
    from tcad.ir.builders import BuildParts, parts_patch
    from tcad.ir.patch import apply_patch
    from tcad.ir.schema import IrPatch

    ops, _ = parts_patch(BuildParts(parts=[recipe]), [])
    ir = apply_patch(IrDocument(model_id='m1'), IrPatch(base_version=0, ops=ops)).ir
    # Reload the persisted representation, rather than relying on transient IDs.
    ir = IrDocument.model_validate_json(ir.model_dump_json())
    intent = plan(part(recipe['id'], 'required part', origin='user'))
    checked = review_intent(intent, ir)
    assert [entry['id'] for entry in checked['kept']] == [recipe['id']]
    assert not checked['degraded']
    renamed = apply_patch(ir, IrPatch(base_version=ir.version, ops=[
        {'op': 'rename', 'target_id': feature.id, 'payload': {'name': f'renamed_{i}'}, 'reason': 'Rename'}
        for i, feature in enumerate(ir.all_features())])).ir
    assert review_intent(intent, renamed)['kept']
    for body in renamed.bodies:
        body.features = []
    assert review_intent(intent, renamed)['lost']


def test_recipe_matching_does_not_guess_from_feature_prefixes():
    checked = review_intent(plan(part('propeller', 'required rotor', origin='user')),
                            ir_with('propeller_hub', 'propeller_blade_0'))
    assert checked['lost'] and not checked['kept']


def test_suppressed_recipe_features_do_not_satisfy_the_part_plan():
    ir = IrDocument(model_id='m1', bodies=[BodySpec(id='frame', name='frame', features=[
        FeatureSpec(id='post_0', name='post_0', op='additive_cylinder',
                    recipe_id='post', suppress=True)])])
    assert review_intent(plan(part('post', 'required post', origin='user')), ir)['lost']


def test_degradation_items_feed_the_completion_review(tmp_path):
    write_plan(tmp_path, "m1", plan(
        part("backrest", "弧形靠背、腰部支撑", origin="user", status="simplified", note="平板替代")))
    items = degradation_items(tmp_path, "m1", ir_with("backrest"))
    assert items and "设计退化" in items[0] and "backrest" in items[0]
    assert degradation_items(tmp_path, "other", ir_with()) == []
    assert read_plan(tmp_path, "m1").parts[0].status == "simplified"


def test_intent_note_names_lost_and_degraded_parts(tmp_path):
    write_plan(tmp_path, "m1", plan(
        part("base", "plate", origin="user"),
        part("headrest", "头枕", origin="user"),
        part("backrest", "弧线靠背", origin="user", status="simplified", note="平板替代")))
    note = intent_note(tmp_path, "m1", ir_with("base", "backrest"))
    assert "DESIGN INTENT CHECK" in note
    assert "LOST (planned, no geometry)" in note and "headrest" in note
    assert "USER-REQUIRED PARTS DEGRADED" in note and "backrest" in note
    assert "draft pending acceptance" in note


def test_failure_history_aggregates_repeated_classes_across_versions(tmp_path):
    from tcad.store.artifacts import write_gate_report

    def failed(version, classes):
        rep = report(version=version, status="fail")
        rep.passed = False
        rep.results = []
        rep.blocking_failures = [f"{cls}: broken (feature_id=f1)" for cls in classes]
        return rep

    write_gate_report(tmp_path, "m1", 1, failed(1, ["solid_validity"]))
    write_gate_report(tmp_path, "m1", 2, failed(2, ["solid_validity", "bbox_spec"]))
    write_gate_report(tmp_path, "m1", 3, report(version=3))
    history = failure_history(tmp_path, "m1")
    assert history["graded_versions"] == [1, 2, 3]
    assert history["classes"] == {"solid_validity": [1, 2], "bbox_spec": [2]}
    assert history["repeated"] == {}
    assert recovery_note(tmp_path, "m1") == ""
    # A later failure of the same class makes the recurrence actionable again.
    write_gate_report(tmp_path, "m1", 4, failed(4, ["solid_validity"]))
    assert failure_history(tmp_path, "m1")["repeated"] == {"solid_validity": [1, 2, 4]}
    note = recovery_note(tmp_path, "m1")
    assert "RECOVERY CHECK" in note and "solid_validity" in note
    assert "not evidence of progress" in note
    # A one-off class does not trigger the note.
    write_gate_report(tmp_path, "m2", 1, failed(1, ["solid_validity"]))
    assert recovery_note(tmp_path, "m2") == ""


def test_design_notes_bundle_is_empty_without_a_plan(tmp_path):
    assert design_notes(tmp_path, "m1", ir_with()) == ""
    write_plan(tmp_path, "m1", plan(part("base", "plate", origin="user")))
    assert "DESIGN INTENT CHECK" in design_notes(tmp_path, "m1", ir_with("base"))


@pytest.mark.asyncio
async def test_plan_echo_marks_unbuilt_parts_instead_of_degrading_them(tmp_path):
    """Recording a plan is not a degradation. A live session saw all user parts
    reported as degraded_user_parts the moment the plan was recorded — before
    any geometry existed — and read it as "you did something wrong"."""
    from types import SimpleNamespace
    from tcad.core.types import ToolContext
    from tcad.tools.authoring import ir_plan_handler

    services = SimpleNamespace(store=SimpleNamespace(load=lambda _model_id: ir_with()))
    ctx = ToolContext(thread_id="th", turn_id="tn", model_id="m1", data_dir=str(tmp_path))
    call = {"reason": "plan", "parts": [
        {"id": "base", "goal": "机身", "origin": "user"},
        {"id": "rotor", "goal": "主旋翼", "origin": "user"}]}
    data = json.loads((await ir_plan_handler(services, call, ctx)).content)
    against = data["against_current_ir"]
    assert against["kept"] == ["base"]
    assert against["not_built_yet"] == ["rotor"]
    assert against["degraded_user_parts"] == []
    assert "lost" not in against
    # An explicit simplified/dropped status is still a degradation.
    call["parts"][1].update({"status": "simplified", "note": "简化为十字盘"})
    against = json.loads((await ir_plan_handler(services, call, ctx)).content)["against_current_ir"]
    assert against["degraded_user_parts"] == ["rotor"]


@pytest.mark.asyncio
async def test_engine_downgrades_a_verified_review_when_the_plan_degraded(tmp_path, monkeypatch):
    from tcad.core.types import ToolResult, Thread, TurnState
    from tcad.loop.engine import UserMessage

    source = sized_ir().requirements.raw_text
    svc = make_services(sized_ir(), ScriptedLlm([
        LlmReply(tool_calls=[ToolCall(id="c", name="ir_commit", args={"message": "build"})]),
        LlmReply(tool_calls=[ToolCall(id="r", name="design_review",
                                      args=review(source, ["bbox_spec"]))]),
    ]), True)
    svc.store = StoreAdapter(tmp_path)
    svc.store.create("m1", sized_ir())

    async def commit(services, model_id, version, *args, **kwargs):
        return ToolResult(ok=True), report(version)

    monkeypatch.setattr("tcad.loop.commit.run_commit", commit)
    engine = make_engine(svc)
    engine.config.require_design_review = True
    engine.config.data_dir = str(tmp_path)
    write_plan(tmp_path, "m1", plan(
        part("cavity", "用户要求的削笔空腔", origin="user", status="simplified", note="本轮简化为浅槽")))
    result = await engine.run_turn(Thread(thread_id="th1", model_id="m1"),
                                   UserMessage(text=source))
    # The measured review would have passed on its own; the recorded
    # degradation keeps the delivery a draft pending acceptance.
    assert result.state == TurnState.DRAFT
    assert not result.completion_review["verified"]
    assert any("设计退化" in item for item in result.completion_review["remaining_work"])
    assert "设计退化已记录" in result.completion_review["note"]
