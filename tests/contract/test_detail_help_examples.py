"""The examples served to the model must produce their analytic native CAD geometry."""
import asyncio
import json
import math

import pytest

from tcad.ir.patch import apply_patch
from tcad.ir.schema import IrPatch
from tcad.tools.authoring import help_handler
from tests.contract.test_sketch_planes import compile_ir, pytestmark, worker
from tests.unit.test_detail_discovery import example_base


@pytest.mark.parametrize("detail,volume,bounds", [
    ("slot", 80 * 50 * 8 - (20 * 6 + math.pi * 3**2) * 3,
     {"x": 80, "y": 50, "z": 8}),
    ("arc", math.pi * 8**2 / 2 * 5,
     {"x_min": 0, "y_min": -8, "z_min": 0, "x": 8, "y": 16, "z": 5}),
    ("groove", math.pi * 10**2 * 20 - math.pi * (10**2 - 8**2) * 4,
     {"x": 20, "y": 20, "z": 20}),
])
def test_served_profile_example_builds_the_claimed_shape(worker, tmp_path, detail, volume, bounds):
    data = json.loads(asyncio.run(help_handler(None, {"topic": "detail", "detail": detail}, None)).content)
    patch = IrPatch.model_validate(data["example"] | {"base_version": 0})
    ir = apply_patch(example_base(detail), patch).ir
    result = compile_ir(worker, ir.model_dump(mode="json"), tmp_path)
    measured = result["measurements"]
    assert measured["is_valid"] and measured["solids"] == 1
    assert measured["volume"] == pytest.approx(volume, rel=1e-6)
    for axis, value in bounds.items():
        assert measured["bbox"][axis] == pytest.approx(value, abs=1e-6)
