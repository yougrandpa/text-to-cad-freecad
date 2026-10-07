"""Primary outline evidence must survive repairs without being masked by details."""
import json
from types import SimpleNamespace

import pytest

from tcad.context.assembler import ContextAssembler, ContextBudget
from tcad.core.types import ToolContext, TurnKind
from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.patch import apply_patch
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument, IrPatch, SketchSpec
from tcad.loop.engine import UserMessage
from tcad.loop.intent import PlannedPart, plan_context, read_plan, review_intent, write_plan
from tcad.tools.authoring import build_authoring_tools, ir_plan_handler
from tcad.tools.schema_check import check
from tests.unit.test_context_wiring import _engine, _Store
from tests.unit.test_design_intent import plan


def outline_plan(form="tapered", outline_id="cabin"):
    return plan(PlannedPart(id="fuselage", goal="helicopter airframe", form=form,
                            outline_id=outline_id))


def airframe(op="additive_box", **kwargs):
    return IrDocument(model_id="m1", bodies=[BodySpec(id="fuselage", name="fuselage", features=[
        FeatureSpec(id="cabin", name="cabin", op=op, **kwargs),
        FeatureSpec(id="mast", name="mast", op="additive_cylinder"),
        FeatureSpec(id="hole", name="hole", op="subtractive_cylinder")])])


def test_mast_and_hole_do_not_satisfy_primary_tapered_outline():
    assert review_intent(outline_plan(), airframe())["advisories"]
    assert not review_intent(outline_plan(), airframe("additive_loft"))["advisories"]
    assert not review_intent(outline_plan(), airframe("additive_cone"))["advisories"]
    assert review_intent(outline_plan(), airframe("additive_cylinder"))["advisories"]


def test_body_level_shape_intent_requires_binding_among_multiple_features():
    notes = review_intent(outline_plan(outline_id=None), airframe("additive_loft"))["advisories"]
    assert "bind outline_id" in notes[0]


@pytest.mark.parametrize("goal", ["tapered cabin", "变截面机身", "向尾部收细"])
def test_legacy_tapered_goal_is_recognized(goal):
    intent = plan(PlannedPart(id="cabin", goal=goal))
    assert review_intent(intent, airframe())["advisories"]
    assert not review_intent(intent, airframe("additive_loft"))["advisories"]


def test_suppressed_outline_and_unrelated_loft_do_not_hide_degradation():
    ir = airframe("additive_loft", suppress=True)
    ir.bodies[0].features.append(FeatureSpec(id="fin", name="fin", op="additive_loft"))
    assert review_intent(outline_plan(), ir)["advisories"]


def test_missing_or_foreign_outline_is_reported():
    assert "missing" in review_intent(outline_plan(outline_id="removed"), airframe())["advisories"][0]
    ir = airframe()
    ir.bodies.append(BodySpec(id="other", name="other", features=[
        FeatureSpec(id="other_outline", name="other_outline", op="additive_loft")]))
    assert "another body" in review_intent(outline_plan(outline_id="other_outline"), ir)["advisories"][0]


@pytest.mark.parametrize("used,construction,op,suppressed,expected", [
    (False, False, "pad", False, True),
    (True, True, "pad", False, True),
    (True, False, "pocket", False, True),
    (True, False, "pad", True, True),
    (True, False, "pad", False, False),
])
def test_only_active_additive_profile_curves_count(used, construction, op, suppressed, expected):
    ir = airframe(op, profile_sketch="circle" if used else None, suppress=suppressed)
    ir.bodies[0].sketches.append(SketchSpec(id="circle", name="circle", plane={"kind": "origin_plane", "plane": "XY"}, geometry=[
        {"id": "g1", "kind": "circle", "points": [{"x": 0, "y": 0, "z": 0}], "radius": 10, "construction": construction}]))
    assert bool(review_intent(outline_plan("round"), ir)["advisories"]) is expected


def test_prismatic_blades_do_not_require_curvature():
    assert not review_intent(outline_plan("prismatic"), airframe())["advisories"]


def test_recipe_scope_ignores_unrelated_additive_features():
    request = BuildParts(parts=[{"id": "cabin_recipe", "body_id": "fuselage", "shape": "loft",
        "section_axis": "X", "sections": [
            {"center": [0, 0, 0], "radii": [10, 8]},
            {"center": [30, 0, 0], "radii": [4, 3]}]}])
    ops, _ = parts_patch(request, [])
    ir = apply_patch(IrDocument(model_id="m1"), IrPatch(base_version=0, ops=ops)).ir
    intent = outline_plan(outline_id="cabin_recipe")
    assert not review_intent(intent, ir)["advisories"]
    for f in ir.bodies[0].features:
        if f.op == "additive_loft":
            f.op = "additive_box"
    ir.bodies[0].features.append(FeatureSpec(id="mast", name="mast", op="additive_cylinder"))
    assert review_intent(intent, ir)["advisories"]


async def test_plan_schema_routes_and_shape_fields_survive_partial_plan_updates(tmp_path):
    ctx = ToolContext(thread_id="th", turn_id="tn", model_id="m1", data_dir=str(tmp_path))
    services = SimpleNamespace(store=_Store(airframe()))
    args = {"parts": [p.model_dump() for p in outline_plan().parts]}
    assert not check(args, build_authoring_tools(services)["ir_plan"].params_schema)
    result = await ir_plan_handler(services, args, ctx)
    assert result.ok
    assert "shape=loft" in json.loads(result.content)["shape_routes"]["fuselage"]
    await ir_plan_handler(services, {"parts": [{"id": "fuselage", "goal": "repair connections"}]}, ctx)
    saved = read_plan(tmp_path, "m1").parts[0]
    assert saved.form == "tapered" and saved.outline_id == "cabin"
    # Explicit updates still replace an earlier outline binding.
    args["parts"][0]["outline_id"] = "new_cabin"
    await ir_plan_handler(services, args, ctx)
    assert read_plan(tmp_path, "m1").parts[0].outline_id == "new_cabin"


def test_reading_absent_plan_does_not_create_runtime_directory(tmp_path):
    assert read_plan(tmp_path, "m1") is None
    assert plan_context(tmp_path, "m1", airframe()) == ""
    assert not (tmp_path / "intents").exists()


async def test_plan_survives_history_degradation_and_state_refresh(tmp_path):
    write_plan(tmp_path, "m1", outline_plan())
    engine = _engine(store=_Store(airframe()), assembler=ContextAssembler(ContextBudget(window_tokens=100)))
    engine.config = engine.config.model_copy(update={"data_dir": str(tmp_path)})
    turn = SimpleNamespace(thread_id="th1", model_id="m1", base_ir_version=0)
    user = UserMessage(kind=TurnKind.MODIFY, text="repair the build")
    for require_state in (False, True):
        messages = await engine._build_messages(user, turn, require_state=require_state)
        text = "\n".join(m["content"] for m in messages)
        assert "PERSISTED PART PLAN" in text and '"form":"tapered"' in text
        assert '"outline_id":"cabin"' in text
        assert "lacks active outline evidence" in text
        assert messages[-1] == {"role": "user", "content": user.text}


def test_native_feature_binding_does_not_expand_to_other_recipe_features():
    ir = airframe(recipe_id="airframe_recipe")
    ir.bodies[0].features[1].recipe_id = "airframe_recipe"
    assert review_intent(outline_plan("curved"), ir)["advisories"]


async def test_compaction_reloads_plan_after_original_tool_result_is_discarded(tmp_path):
    from tests.unit.test_turn_compaction import _batch

    engine = _engine(store=_Store(airframe()), assembler=ContextAssembler())
    engine.config = engine.config.model_copy(update={
        "data_dir": str(tmp_path), "context_window_tokens": 5000, "llm_max_tokens": 0})
    turn = SimpleNamespace(thread_id="th1", model_id="m1", base_ir_version=0)
    user = UserMessage(kind=TurnKind.MODIFY, text="repair the build")
    messages = await engine._build_messages(user, turn)
    engine._current_user_message = user
    engine._trace_start = len(messages)
    # The plan was written during this turn, after the initial prefix was built.
    write_plan(tmp_path, "m1", outline_plan())
    first = _batch(0, size=40_000)
    first[0]["tool_calls"][0]["function"]["name"] = "ir_plan"
    first[1]["name"] = "ir_plan"
    messages.extend([*first, *_batch(1), *_batch(2)])
    await engine._prepare_step_context(turn, messages, [])
    text = json.dumps(messages, ensure_ascii=False)
    assert "call_0_0" not in text
    assert "PERSISTED PART PLAN" in text and "tapered" in text and "cabin" in text
    assert "lacks active outline evidence" in text
