"""Requested CAD details discover executable native operations with one help call."""
import json
from types import SimpleNamespace

import pytest

from tcad.agent.detail_guidance import DETAIL_ROUTES
from tcad.core.types import Thread, TurnKind
from tcad.ir.patch import apply_patch
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument, IrPatch
from tcad.ir.validate import validate_ir
from tcad.llm.client import LlmReply, ToolCall
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine, UserMessage
from tcad.tools.authoring import help_handler
from tcad.tools.base import build_default_registry
from tcad.tools.ir_tools import _ir_patch_schema
from tcad.tools.schema_check import check
from tests.unit.test_loop_engine import ScriptedLlm, make_ir, make_services


def example_base(detail):
    if detail == "arc":
        return IrDocument(model_id="details")
    feature = FeatureSpec(id="blank", name="blank", op="additive_box",
                          params={"length": 80, "width": 50, "height": 8})
    if detail == "groove":
        feature.op = "additive_cylinder"
        feature.params = {"radius": 10, "height": 20}
    return IrDocument(model_id="details", bodies=[BodySpec(id="base", name="base", features=[feature])])


async def test_catalog_names_distinct_geometries_without_unlocking_every_command(tmp_path):
    result = await help_handler(None, {"topic": "detail"}, None)
    data = json.loads(result.content)
    assert set(data["choices"]) == set(DETAIL_ROUTES)
    assert "schema" not in data and "example" not in data
    assert len(result.content) < 2000
    engine, llm = await run_help(tmp_path, {"topic": "detail"})
    assert "ir_patch" not in {t["function"]["name"] for t in llm.last_tools}


@pytest.mark.parametrize("detail", DETAIL_ROUTES)
async def test_selected_detail_unlocks_only_its_required_ops(tmp_path, detail):
    services = SimpleNamespace()
    registry = build_default_registry(services)
    assert not check({"topic": "detail", "detail": detail}, registry.get("ir_help").params_schema)
    result = await help_handler(None, {"topic": "detail", "detail": detail}, None)
    data = json.loads(result.content)
    assert data["feature_ops"] == DETAIL_ROUTES[detail]["feature_ops"]
    assert len(result.content) < 4000
    engine, llm = await run_help(tmp_path, {"topic": "detail", "detail": detail})
    schema = next(t["function"]["parameters"] for t in llm.last_tools
                  if t["function"]["name"] == "ir_patch")
    branches = schema["properties"]["ops"]["items"]["anyOf"]
    ops = {b["properties"]["op"]["enum"][0] for b in branches}
    assert ("add_sketch" in ops) == ("sketch" in DETAIL_ROUTES[detail]["topics"])
    for branch in branches:
        if branch["properties"]["op"]["enum"][0] in {"add_feature", "update_feature"}:
            assert branch["properties"]["payload"]["properties"]["op"]["enum"] == data["feature_ops"]
    example = data["example"]
    patch = example if "ops" in example else {"base_version": "current", "ops": [example]}
    assert not check(patch, schema)
    assert not check(patch, _ir_patch_schema())
    # Another turn starts with the compact discovery surface again.
    await engine.run_turn(Thread(thread_id="next", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="a different design"))
    assert "ir_patch" not in {t["function"]["name"] for t in llm.last_tools}


@pytest.mark.parametrize("detail", ["slot", "arc", "groove"])
async def test_profile_examples_are_closed_valid_native_ir(detail):
    data = json.loads((await help_handler(None, {"topic": "detail", "detail": detail}, None)).content)
    ir = apply_patch(example_base(detail), IrPatch.model_validate(data["example"] | {"base_version": 0})).ir
    errors = [issue for issue in validate_ir(ir) if issue.severity == "error"]
    assert not errors
    if detail == "slot":
        profile = next(s for s in ir.all_sketches() if s.id == "slot_profile")
        assert [g.kind for g in profile.geometry] == ["line", "arc", "line", "arc"]
        assert ir.all_features()[-1].op == "pocket"
    if detail == "arc":
        assert any(g.kind == "arc" for s in ir.all_sketches() for g in s.geometry)
        assert ir.all_features()[-1].op == "pad"


async def test_unknown_detail_neither_succeeds_nor_unlocks_authoring(tmp_path):
    result = await help_handler(None, {"topic": "detail", "detail": "invented"}, None)
    assert not result.ok
    engine, llm = await run_help(tmp_path, {"topic": "detail", "detail": "invented"})
    assert not engine._authoring_features
    assert "ir_patch" not in {t["function"]["name"] for t in llm.last_tools}


async def run_help(tmp_path, args):
    llm = ScriptedLlm([LlmReply(tool_calls=[ToolCall(id="help", name="ir_help", args=args)])])
    services = make_services(make_ir(), llm, gate_passed=False)
    engine = LoopEngine(services, build_default_registry(services), BudgetLimits(max_steps_per_turn=2),
                        LoopConfig(require_design_review=True, data_dir=str(tmp_path)))
    await engine.run_turn(Thread(thread_id="t1", model_id="m1"),
                          UserMessage(kind=TurnKind.CREATE, text="Model a native CAD detail"))
    return engine, llm
