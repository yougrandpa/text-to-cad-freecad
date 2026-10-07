"""Incremental refinement preserves existing part history and connections."""
import json
from types import SimpleNamespace

import pytest

from tcad.core.types import ToolContext
from tcad.ir.schema import BodySpec, IrDocument, IrPatch
from tcad.tools.authoring import _check_body_rules, build_authoring_tools
from tcad.tools.base import execute_tool


@pytest.fixture
def setup(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services

    cfg = load_default_config()
    cfg.storage.data_dir = str(tmp_path)
    cfg.storage.sqlite_path = ''
    services = build_services(cfg, start_worker=False)
    services.store.create('part', IrDocument(model_id='part'))
    ctx = ToolContext(model_id='part', thread_id='t', turn_id='turn', data_dir=str(tmp_path))
    yield services, ctx, build_authoring_tools(services)['cad_build_parts']
    services.worker.close()


async def test_refine_assembled_part_without_rebuilding_or_losing_history(setup):
    services, ctx, spec = setup
    base = {'id': 'base', 'body_id': 'housing', 'shape': 'box',
            'center': [0, 0, 0], 'size': [20, 20, 10]}
    first = await execute_tool(spec, {'parts': [base]}, ctx)
    assert first.result.ok, first.result.error
    services.store.apply_patch('part', IrPatch(base_version=1, ops=[
        {'op': 'set_assembly', 'payload': {'assembly': {'grounded': ['housing']}},
         'reason': 'Preserve assembled pose'},
    ]))
    before = services.store.load('part')
    cut = {'id': 'bore', 'body_id': 'housing', 'shape': 'cylinder',
           'start': [0, 0, -6], 'end': [0, 0, 6], 'radius': 2, 'operation': 'cut'}
    boss = {'id': 'boss', 'body_id': 'housing', 'shape': 'box',
            'center': [7, 0, 5], 'size': [4, 4, 4]}
    refused = await execute_tool(spec, {'parts': [cut, boss]}, ctx)
    assert not refused.result.ok and 'extend_existing=true' in refused.result.error.message
    assert services.store.load('part').model_dump() == before.model_dump()
    refined = await execute_tool(spec, {'parts': [cut, {**boss, 'extend_existing': True}]}, ctx)
    assert refined.result.ok, refined.result.error
    after = services.store.load('part')
    assert after.assembly == before.assembly
    assert [b.id for b in after.bodies] == ['housing']
    assert after.bodies[0].features[0] == before.bodies[0].features[0]
    assert [f.id for f in after.bodies[0].features] == ['base', 'bore', 'boss']
    assert after.find_feature('bore').op == 'subtractive_cylinder'
    assert after.find_feature('boss').op == 'additive_box'
    assert 'assembly connections are preserved' in json.loads(refined.result.content)['edit_hint']
    # Stable-ID updates still edit in place, preserving the appended cut/boss.
    updated = await execute_tool(spec, {'parts': [{**base, 'size': [22, 20, 10]}]}, ctx)
    assert updated.result.ok, updated.result.error
    assert [f.id for f in services.store.load('part').bodies[0].features] == ['base', 'bore', 'boss']


@pytest.mark.parametrize('operation,extend', [('cut', False), ('add', True)])
def test_refinement_cannot_mutate_a_pinned_component(operation, extend):
    body = BodySpec(id='source', name='source', part_ref={
        'model_id': 'component', 'artifact_id': 'sha256:' + 'a' * 64, 'body_id': 'part'})
    ir = IrDocument(model_id='assembly', bodies=[body])
    recipe = SimpleNamespace(id='detail', body_id='source', shape='box',
                             operation=operation, extend_existing=extend)
    with pytest.raises(ValueError, match='referenced component'):
        _check_body_rules(recipe, ir, {})


async def test_stale_refinement_is_atomic(setup):
    services, ctx, spec = setup
    base = {'id': 'base', 'body_id': 'housing', 'shape': 'box',
            'center': [0, 0, 0], 'size': [20, 20, 10]}
    assert (await execute_tool(spec, {'parts': [base]}, ctx)).result.ok
    before = services.store.load('part').model_dump()
    cut = {**base, 'id': 'opening', 'operation': 'cut', 'size': [2, 2, 12]}
    result = await execute_tool(spec, {'parts': [cut], 'base_version': 0}, ctx)
    assert not result.result.ok and 'stale base_version' in result.result.error.message
    assert services.store.load('part').model_dump() == before
