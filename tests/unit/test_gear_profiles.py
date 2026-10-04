import json
import math
from types import SimpleNamespace

import pytest

from tcad.ir.gears import gear_sketch, involute_outline
from tcad.ir.schema import IrDocument, SketchSpec
from tcad.tools.ir_tools import ir_get_handler


def test_involute_dimensions_and_closed_valid_sketch():
    points = involute_outline(20, 2)
    radii = [math.hypot(*p) for p in points]
    assert min(radii) == pytest.approx(17.5)
    assert max(radii) == pytest.approx(22)
    assert len(points) < 600
    spec = SketchSpec.model_validate(gear_sketch({'id': 'gear', 'body_id': 'drive', 'teeth': 20, 'module': 2}))
    assert spec.geometry[-1].points[-1] == spec.geometry[0].points[0]
    assert len(spec.constraints) == len(spec.geometry)


@pytest.mark.parametrize('args', [dict(teeth=16, module=2), dict(teeth=20, module=0),
                                dict(teeth=20, module=2, backlash=2), dict(teeth=18, module=2, pressure_angle=15)])
def test_reject_unsupported_or_undercut_parameters(args):
    with pytest.raises(ValueError):
        involute_outline(**args)


@pytest.mark.asyncio
async def test_compact_ir_is_losslessly_reconstructible_and_targeted():
    ir = IrDocument(model_id='test')
    services = SimpleNamespace(store=SimpleNamespace(load=lambda _: ir))
    ctx = SimpleNamespace(model_id='test')
    result = await ir_get_handler(services, {}, ctx)
    assert IrDocument.model_validate_json(result.content) == ir
    assert len(result.content) < len(ir.model_dump_json(indent=2))
    result = await ir_get_handler(services, {'ids': ['missing']}, ctx)
    assert not result.ok
