"""Public shape examples compile with the real kernel and round-trip STEP."""
import asyncio
import json

import pytest

from tcad.agent.shape_guidance import SHAPE_CHOICES
from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.patch import apply_patch
from tcad.ir.schema import IrDocument, IrPatch
from tcad.tools.authoring import help_handler
from tests.contract.test_sketch_planes import compile_ir, pytestmark, worker


@pytest.mark.parametrize('shape', list(SHAPE_CHOICES))
def test_public_recipe_examples_compile_native_editable_solids(worker, tmp_path, shape):
    result = asyncio.run(help_handler(None, {'topic': 'shape', 'shape': shape}, None))
    request = BuildParts.model_validate(json.loads(result.content)['example'])
    ops, _ = parts_patch(request, [])
    outcome = apply_patch(IrDocument(model_id=f'help-{shape}'), IrPatch(base_version=0, ops=ops))
    built = compile_ir(worker, outcome.ir.model_dump(mode='json'), tmp_path)
    assert built['measurements']['is_valid']
    assert built['measurements']['solids'] == 1
    assert built['measurements']['volume'] > 0
    assert built['round_trip']['ok'], built['round_trip']
    if shape == 'loft':
        assert built['measurements']['bbox']['x'] == pytest.approx(60)
        assert outcome.ir.bodies[0].features[-1].op == 'additive_loft'


def test_compact_loft_mixes_circular_and_elliptical_sections(worker, tmp_path):
    request = BuildParts(parts=[{'id': 'outline', 'body_id': 'part', 'shape': 'loft',
        'section_axis': 'X', 'sections': [
            {'center': [0, 0, 0], 'radii': [9, 9]},
            {'center': [10, 0, 0], 'radii': [8, 7]},
            {'center': [20, 0, 0], 'radii': [5, 4]},
        ]}])
    ops, _ = parts_patch(request, [])
    outcome = apply_patch(IrDocument(model_id='round-sections'), IrPatch(base_version=0, ops=ops))
    built = compile_ir(worker, outcome.ir.model_dump(mode='json'), tmp_path)
    assert built['measurements']['is_valid'] and built['measurements']['solids'] == 1
    assert built['measurements']['bbox']['x'] == pytest.approx(20)
    assert built['round_trip']['ok']
