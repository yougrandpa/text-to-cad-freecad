"""Shape discovery offers executable contracts without hiding mixed batches."""
import copy
import json
from types import SimpleNamespace

import pytest

from tcad.agent.shape_guidance import SHAPE_CHOICES, SHAPE_REVIEW
from tcad.core.types import TurnKind
from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.patch import apply_patch
from tcad.ir.schema import IrDocument, IrPatch
from tcad.ir.validate import validate_ir
from tcad.loop.budget import BudgetLimits
from tcad.loop.engine import LoopConfig, LoopEngine
from tcad.tools.authoring import build_authoring_tools, help_handler
from tcad.tools.base import build_default_registry
from tcad.tools.schema_check import check


@pytest.mark.parametrize('shape', list(SHAPE_CHOICES))
async def test_shape_help_example_matches_both_scoped_and_executable_contract(shape):
    tools = build_authoring_tools(SimpleNamespace())
    assert not check({'topic': 'shape', 'shape': shape}, tools['ir_help'].params_schema)
    result = await help_handler(None, {'topic': 'shape', 'shape': shape}, None)
    assert result.ok
    data = json.loads(result.content)
    assert not check(data['example'], data['schema'])
    assert not check(data['example'], tools['cad_build_parts'].params_schema)
    # Verify actual IR expansion, including planes/sketches for the loft example.
    request = BuildParts.model_validate(data['example'])
    ops, _ = parts_patch(request, [])
    outcome = apply_patch(IrDocument(model_id='shape-help'), IrPatch(base_version=0, ops=ops))
    assert not [issue for issue in validate_ir(outcome.ir) if issue.severity == 'error']
    assert {branch['properties']['shape']['enum'][0]
            for branch in data['schema']['properties']['parts']['items']['anyOf']} == {shape}
    other = copy.deepcopy(data['example'])
    other['parts'][0]['shape'] = 'box' if shape != 'box' else 'loft'
    assert check(other, data['schema'])


async def test_shape_catalog_is_small_and_does_not_unlock_unrequested_features():
    result = await help_handler(None, {'topic': 'shape'}, None)
    data = json.loads(result.content)
    assert set(data['choices']) == set(SHAPE_CHOICES)
    assert 'schema' not in data and 'operations' not in data
    assert len(result.content) < 3000
    services = SimpleNamespace()
    registry = build_default_registry(services)
    engine = LoopEngine(services, registry, BudgetLimits(), LoopConfig(require_design_review=True))
    engine._authoring_topics.add('shape')
    initial = engine._authoring_surface(registry.as_openai_tools(TurnKind.CREATE))
    tools = {tool['function']['name']: tool['function'] for tool in initial}
    assert 'ir_patch' not in tools
    # Querying loft help must leave ordinary boxes/mixed batches usable.
    schema = tools['cad_build_parts']['parameters']
    box = json.loads((await help_handler(None, {'topic': 'shape', 'shape': 'box'}, None)).content)['example']
    loft = json.loads((await help_handler(None, {'topic': 'shape', 'shape': 'loft'}, None)).content)['example']
    assert not check({'parts': box['parts'] + loft['parts']}, schema)


async def test_unknown_shape_returns_actionable_error():
    result = await help_handler(None, {'topic': 'shape', 'shape': 'capsule'}, None)
    assert not result.ok and 'recipe catalog' in result.error.hint


def test_visual_guidance_names_a_real_public_tool_and_supported_views():
    registry = build_default_registry(SimpleNamespace())
    assert 'geo_view' in SHAPE_REVIEW
    tool = registry.get('geo_view')
    assert not check({'views': ['iso', 'front']}, tool.params_schema)


def test_circular_loft_sections_use_exact_circles_and_preserve_other_ellipses():
    request = BuildParts(parts=[{'id': 'blend', 'body_id': 'part', 'shape': 'loft',
        'section_axis': 'X', 'sections': [
            {'center': [0, 0, 0], 'radii': [9, 9]},
            {'center': [10, 0, 0], 'radii': [5, 4]},
        ]}])
    ops, _ = parts_patch(request, [])
    profiles = [op['payload']['geometry'][0] for op in ops if op['op'] == 'add_sketch']
    assert profiles[0]['kind'] == 'circle' and profiles[0]['radius'] == 9
    assert 'major_radius' not in profiles[0]
    assert profiles[1]['kind'] == 'ellipse'
    assert profiles[1]['major_radius'] == 5 and profiles[1]['minor_radius'] == 4
