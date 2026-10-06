"""A compact first tool table must still make native curved CAD discoverable."""
import json
from types import SimpleNamespace

import pytest

from tcad.core.types import TurnKind
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine
from tcad.tools.authoring import build_authoring_tools, help_handler
from tcad.tools.base import build_default_registry
from tcad.tools.ir_tools import _ir_patch_schema
from tcad.tools.schema_check import check


def test_first_request_advertises_curves_and_scoped_edit_discovery():
    services = SimpleNamespace()
    registry = build_default_registry(services)
    engine = LoopEngine(services, registry, BudgetLimits(), LoopConfig(require_design_review=True))
    initial = engine._authoring_surface(registry.as_openai_tools(TurnKind.CREATE))
    tools = {tool["function"]["name"]: tool["function"] for tool in initial}
    assert "ir_patch" not in tools
    # This is the sole advanced-authoring entry point the model sees initially.
    description = tools["ir_help"]["description"]
    assert all(term in description for term in ("ellipse", "bspline", "fillet", "next request"))
    assert "not a capability limit" in description


@pytest.mark.parametrize("op, parameter", [
    ("pad", "length"), ("pocket", "length"), ("fillet", "radius"), ("chamfer", "size"),
    ("additive_loft", "ruled"), ("subtractive_loft", "ruled"),
])
async def test_feature_help_demonstrates_the_requested_operation(op, parameter):
    result = await help_handler(None, {"topic": "feature", "feature_op": op}, None)
    assert result.ok
    help_data = json.loads(result.content)
    payload = help_data["example"]["payload"]
    assert payload["op"] == help_data["feature_op"] == op
    assert parameter in payload["params"] and parameter in help_data["params"]
    assert help_data["capability"] == "verified"
    assert not check({"base_version": "current", "ops": [help_data["example"]]}, _ir_patch_schema())
    if op in {"fillet", "chamfer"}:
        assert payload["base_feature"] and payload["sub_elements"]
        assert "profile_sketch" not in payload
        assert "ir_digest" in help_data["rules"] and "placeholder" in help_data["rules"]


async def test_curve_help_explains_native_profile_semantics_without_claiming_loft_support():
    result = await help_handler(None, {"topic": "sketch"}, None)
    help_data = json.loads(result.content)
    assert all(term in help_data["rules"] for term in (
        "semi-axes", "WORLD interpolation points", "periodic=true", "without repeating", "Block",
        "elevated profile", "ir_digest", "sub:FaceN", "Nonzero sketch.offset is refused",
    ))
    schema = build_authoring_tools(SimpleNamespace())["ir_help"].params_schema
    assert not check({"topic": "feature", "feature_op": "fillet"}, schema)
    assert check({"topic": "feature", "feature_op": "loft"}, schema)


async def test_other_feature_help_does_not_return_an_unrelated_pad_example():
    result = await help_handler(None, {"topic": "feature", "feature_op": "revolution"}, None)
    help_data = json.loads(result.content)
    assert help_data["feature_op"] == "revolution"
    assert "axis" in help_data["params"]
    assert "example" not in help_data
    invalid = await help_handler(None, {"topic": "feature", "feature_op": "invented"}, None)
    assert not invalid.ok and "Unknown feature_op" in invalid.error.message


async def test_patch_help_distinguishes_body_names_from_feature_renames():
    result = await help_handler(None, {"topic": "patch"}, None)
    rules = json.loads(result.content)["rules"]
    assert "update_body" in rules and "target_id=body_id" in rules
    assert "rename operation accepts sketch or feature IDs only" in rules
    assert "do not include body_id in the partial payload" in rules


async def test_workflow_catalog_and_selection_are_scoped():
    from tcad.agent.workflows import SPECIALIZED_TOOLS

    catalog = json.loads((await help_handler(None, {"topic": "workflow"}, None)).content)
    assert catalog["workflows"] and "rules" not in catalog and "tools" not in catalog
    services = SimpleNamespace()
    registry = build_default_registry(services)
    engine = LoopEngine(services, registry, BudgetLimits(), LoopConfig(require_design_review=True))
    definitions = registry.as_openai_tools(TurnKind.CREATE)
    engine._authoring_topics.add("workflow")
    names = {tool["function"]["name"] for tool in engine._authoring_surface(definitions)}
    assert not names & SPECIALIZED_TOOLS

    selected = json.loads((await help_handler(None, {"topic": "workflow", "workflow": "radial_wheel"}, None)).content)
    assert selected["rules"] and "example" not in selected
    engine._authoring_workflows.add(selected["workflow"])
    names = {tool["function"]["name"] for tool in engine._authoring_surface(definitions)}
    assert "cad_wheel_support" in names
    assert not {"cad_cabins", "assembly_motion"} & names
    invalid = await help_handler(None, {"topic": "workflow", "workflow": "unknown"}, None)
    assert not invalid.ok


async def test_loft_help_exposes_ordered_sections_and_datum_world_placement():
    loft = json.loads((await help_handler(None, {"topic": "feature", "feature_op": "additive_loft"}, None)).content)
    assert loft["example"]["payload"]["sections"] == ["section_middle", "section_end"]
    assert "WORLD coordinates" in loft["rules"] and "closed=true loops" in loft["rules"]
    plane = json.loads((await help_handler(None, {"topic": "feature", "feature_op": "datum_plane"}, None)).content)
    assert plane["capability"] == "verified" and "XZ cross-sections" in plane["rules"]
    assert not check({"base_version": "current", "ops": [plane["example"]]}, _ir_patch_schema())


@pytest.mark.parametrize('topic', ['sketch', 'feature', 'requirements', 'assembly'])
async def test_scoped_help_keeps_usage_guidance_without_repeating_tool_schemas(topic):
    data = json.loads((await help_handler(None, {'topic': topic}, None)).content)
    assert 'schema' not in data
    assert data['operations'] and data['unlocks']
    assert data.get('rules') or data.get('example')
    assert len(json.dumps(data)) < 4000


async def test_assembly_discovery_names_unlock_and_demonstrates_valid_native_driver():
    tools = build_default_registry(SimpleNamespace())
    description = tools.get('ir_help').description
    assert 'topic=assembly unlocks assembly_configure' in description
    data = json.loads((await help_handler(None, {'topic': 'assembly'}, None)).content)
    assert 'assembly_configure' in data['unlocks'] and 'next model request' in data['unlocks']
    assert 'WORLD coordinates' in data['coordinates'] and 'radians' in data['coordinates']
    assert not check(data['example'], tools.get('assembly_configure').params_schema)


@pytest.mark.parametrize("shape, params, semantic", [
    ("box", {"length", "width", "height"}, "minimum corner"),
    ("cylinder", {"radius", "height"}, "base centre"),
    ("sphere", {"radius"}, "including below the centre"),
    ("cone", {"radius1", "radius2", "height"}, "base centre"),
])
@pytest.mark.parametrize("operation", ["additive", "subtractive"])
async def test_native_primitive_help_explains_placement_and_real_size_recipe(shape, params, semantic, operation):
    op = f"{operation}_{shape}"
    result = await help_handler(None, {"topic": "feature", "feature_op": op}, None)
    data = json.loads(result.content)
    assert set(data["params"]) == params
    assert semantic in data["rules"]
    assert "not an exhaustive" in data["params_note"]
    assert data["example"]["payload"]["op"] == op
    assert data["example"]["payload"]["placement"]["position"] == {"x": 0, "y": 0, "z": 0}
    assert not check({"base_version": "current", "ops": [data["example"]]}, _ir_patch_schema())
