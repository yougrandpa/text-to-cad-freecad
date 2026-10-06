"""Compact shape recipes delegate CAD coordinate bookkeeping to validated IR."""
import copy
from types import SimpleNamespace

import pytest

from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.patch import apply_patch, PatchError
from tcad.ir.schema import BodySpec, FeatureSpec, IrDocument, IrPatch
from tcad.tools.authoring import build_authoring_tools, build_parts_handler
from tcad.tools.schema_check import check


def loft(axis='X'):
    centers = [[0,0,0], [0,0,0], [0,0,0]]
    for index, center in enumerate(centers):
        center['XYZ'.index(axis)] = index*5
    return {'id':'shell','body_id':'housing','shape':'loft','section_axis':axis,
            'sections':[{'center':c,'radii':[2+i,1+i/2]} for i,c in enumerate(centers)]}


def rotor(axis=None):
    return {'id':'propeller','body_id':'rotor','shape':'rotor','center':[1,2,3],
            'axis':axis or [0,0,1],'radius':20,'blade_count':4,'blade_width':3,
            'thickness':2,'hub_radius':4}


@pytest.fixture
def recipe_services(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services
    from tcad.core.types import ToolContext
    cfg=load_default_config(); cfg.storage.data_dir=str(tmp_path); cfg.storage.sqlite_path=''
    services=build_services(cfg,start_worker=False)
    services.store.create('m',IrDocument(model_id='m'))
    ctx=ToolContext(model_id='m',thread_id='t',turn_id='turn',data_dir=str(tmp_path))
    try:
        yield services,ctx
    finally:
        services.worker.close()


def recipe(shape):
    if shape == 'loft': part=loft()
    elif shape == 'rotor': part=rotor()
    elif shape == 'box': part={'shape':shape,'center':[0,0,0],'size':[10,10,10]}
    else:
        part={'shape':shape,'start':[0,0,0],'end':[0,0,10]}
        part.update({'width':4,'depth':2} if shape == 'beam' else {'radius':3})
        if shape == 'tube': part['inner_radius']=1
    return {**part,'id':'part','body_id':'body'}


SHAPES = ('box','beam','cylinder','tube','loft','rotor')


@pytest.mark.parametrize('before,after',[(a,b) for a in SHAPES for b in SHAPES
    if a != b and ({a,b} & {'loft','rotor'})])
async def test_composite_recipe_type_changes_are_rejected_atomically(recipe_services,before,after):
    services,ctx=recipe_services
    assert (await build_parts_handler(services,{'parts':[recipe(before)],'reason':'Create'},ctx)).ok
    original=services.store.load('m')
    result=await build_parts_handler(services,{'parts':[recipe(after)],'reason':'Change type'},ctx)
    assert not result.ok and 'another body/shape' in result.error.message
    assert services.store.load('m') == original


@pytest.mark.parametrize('shape',['loft','rotor'])
async def test_composite_recipe_ownership_changes_are_rejected_atomically(recipe_services,shape):
    services,ctx=recipe_services
    part=recipe(shape)
    assert (await build_parts_handler(services,{'parts':[part],'reason':'Create'},ctx)).ok
    original=services.store.load('m')
    result=await build_parts_handler(services,{'parts':[{**part,'body_id':'other'}],'reason':'Change owner'},ctx)
    assert not result.ok
    assert services.store.load('m') == original


async def test_recipe_identity_conflicts_within_a_batch_are_atomic(recipe_services):
    services,ctx=recipe_services
    original=services.store.load('m')
    result=await build_parts_handler(services,{'parts':[recipe('rotor'),recipe('loft')],'reason':'Conflict'},ctx)
    assert not result.ok
    assert services.store.load('m') == original


@pytest.mark.parametrize('suffix',['_hub','_shape','_0'])
async def test_independent_ids_do_not_reserve_a_composite_recipe_prefix(recipe_services,suffix):
    services,ctx=recipe_services
    independent={**recipe('box'),'id':'part'+suffix,'body_id':'independent'}
    assert (await build_parts_handler(services,{'parts':[independent],'reason':'Create'},ctx)).ok
    assert (await build_parts_handler(services,{'parts':[recipe('loft')],'reason':'Add independent recipe'},ctx)).ok


@pytest.mark.parametrize('before',['box','cylinder'])
@pytest.mark.parametrize('after',['loft','rotor'])
async def test_aliased_primitive_ids_cannot_bypass_recipe_identity(recipe_services,before,after):
    services,ctx=recipe_services
    initial={**recipe(before),'body_id':'part'}
    assert (await build_parts_handler(services,{'parts':[initial],'reason':'Create'},ctx)).ok
    original=services.store.load('m')
    changed={**recipe(after),'body_id':'other'}
    assert not (await build_parts_handler(services,{'parts':[changed],'reason':'Change recipe'},ctx)).ok
    assert services.store.load('m') == original


@pytest.mark.parametrize('count',[2,5,8])
async def test_rotor_edits_preserve_downstream_history(recipe_services,count):
    services,ctx=recipe_services
    part=rotor()
    downstream={'id':'bore','body_id':part['body_id'],'shape':'cylinder','operation':'cut',
                'start':[1,2,0],'end':[1,2,6],'radius':1}
    assert (await build_parts_handler(services,{'parts':[part,downstream],'reason':'Create'},ctx)).ok
    changed={**part,'blade_count':count}
    assert (await build_parts_handler(services,{'parts':[changed],'reason':'Edit'},ctx)).ok
    edited=services.store.load('m')
    fresh_ops,_=parts_patch(BuildParts(parts=[changed,downstream],reason='Rebuild'),[])
    rebuilt=apply_patch(IrDocument(model_id='m'),IrPatch(base_version=0,ops=fresh_ops)).ir
    assert edited.bodies == rebuilt.bodies


@pytest.mark.parametrize('anchor',['missing','foreign',None,[]])
def test_feature_insertion_rejects_invalid_or_foreign_anchors(anchor):
    ir=IrDocument(model_id='m',bodies=[
        BodySpec(id='body',name='body',features=[FeatureSpec(id='base',name='base',op='additive_box')]),
        BodySpec(id='other',name='other',features=[FeatureSpec(id='foreign',name='foreign',op='additive_box')])])
    original=ir.model_copy(deep=True)
    with pytest.raises(PatchError,match='after_feature'):
        apply_patch(ir,IrPatch(base_version=0,ops=[{'op':'add_feature','payload':{
            'id':'inserted','name':'inserted','body_id':'body','op':'additive_box',
            'after_feature':anchor},'reason':'Insert'}]))
    assert ir == original


@pytest.mark.parametrize('recipe',[loft(), rotor()])
def test_recipes_have_executable_schemas_and_only_native_declarative_operations(recipe):
    args = {'parts':[recipe],'reason':'Native shape'}
    schema = build_authoring_tools(SimpleNamespace())['cad_build_parts'].params_schema
    assert not check(args, schema)
    ops,_ = parts_patch(BuildParts.model_validate(args), [])
    result = apply_patch(IrDocument(model_id='m'), IrPatch(base_version=0,ops=ops))
    assert len(result.ir.bodies) == 1
    if recipe['shape'] == 'loft':
        body = result.ir.bodies[0]
        assert len(body.sketches) == 3
        assert all(s.constraints[0].type == 'Block' for s in body.sketches)
        assert body.features[-1].op == 'additive_loft'
    else:
        assert [f.op for f in result.ir.bodies[0].features] == ['additive_cylinder']+['additive_box']*4


@pytest.mark.parametrize('change',[
    {'sections':[{'center':[0,0,0],'radii':[1,0]}, {'center':[1,0,0],'radii':[1,1]}]},
    {'sections':[{'center':[0,0,0],'radii':[1,1]}, {'center':[0,0,5],'radii':[1,1]}]},
    {'copies':{'count':2,'center':[0,0,0]}},
])
def test_bad_loft_sections_and_unsupported_copies_are_rejected(change):
    with pytest.raises(ValueError):
        BuildParts(parts=[{**loft(),**change}],reason='Invalid')


@pytest.mark.parametrize('change',[{'axis':[0,0,0]}, {'hub_radius':20}, {'blade_count':1}, {'operation':'cut'}])
def test_invalid_rotor_cannot_emit_geometry(change):
    with pytest.raises(ValueError):
        BuildParts(parts=[{**rotor(),**change}],reason='Invalid')


async def test_recipe_updates_keep_native_section_ids_and_are_atomic(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services
    from tcad.core.types import ToolContext
    cfg=load_default_config(); cfg.storage.data_dir=str(tmp_path); cfg.storage.sqlite_path=''
    services=build_services(cfg,start_worker=False)
    services.store.create('m',IrDocument(model_id='m'))
    ctx=ToolContext(model_id='m',thread_id='t',turn_id='turn',data_dir=str(tmp_path))
    try:
        initial = {'parts':[loft(),rotor()],'reason':'Create'}
        assert (await build_parts_handler(services,initial,ctx)).ok
        before=services.store.load('m')
        changed=copy.deepcopy(initial)
        changed['parts'][0]['sections'][1]['radii']=[4,2]
        changed['parts'][1]['radius']=25
        assert (await build_parts_handler(services,changed,ctx)).ok
        after=services.store.load('m')
        assert [s.id for s in after.all_sketches()] == [s.id for s in before.all_sketches()]
        assert [f.id for f in after.all_features()] == [f.id for f in before.all_features()]
        assert after.find_sketch('shell_section_1').geometry[0].major_radius == 4
        assert after.find_feature('propeller_blade_0').params['length'] == 25
        reduced=copy.deepcopy(changed); reduced['parts'][1]['blade_count']=2
        assert (await build_parts_handler(services,reduced,ctx)).ok
        after=services.store.load('m')
        assert after.find_feature('propeller_blade_1') is not None
        assert after.find_feature('propeller_blade_2') is None
        assert after.find_feature('propeller_blade_3') is None
        invalid=copy.deepcopy(changed); invalid['parts'][0]['body_id']='other'
        assert not (await build_parts_handler(services,invalid,ctx)).ok
        assert services.store.load('m') == after
    finally:
        services.worker.close()


def test_invalid_box_parameter_is_rejected_before_persist_and_old_parameter_can_be_removed():
    ir=IrDocument(model_id='m',bodies=[BodySpec(id='b',name='b',features=[
        FeatureSpec(id='box',name='box',op='additive_box',params={'length':10,'width':5,'height':3})])])
    with pytest.raises(PatchError,match='length/width/height'):
        apply_patch(ir,IrPatch(base_version=0,ops=[{'op':'update_feature','target_id':'box',
            'payload':{'params':{'depth':5}},'reason':'Wrong property'}]))
    ir.bodies[0].features[0].params['depth']=5
    repaired=apply_patch(ir,IrPatch(base_version=0,ops=[{'op':'update_feature','target_id':'box',
        'payload':{'params_remove':['depth'],'params':{'height':4}},'reason':'Repair unsupported parameter'}]))
    assert repaired.ir.find_feature('box').params == {'length':10,'width':5,'height':4}
    assert ir.find_feature('box').params['depth'] == 5
    with pytest.raises(PatchError,match='same key'):
        apply_patch(repaired.ir,IrPatch(base_version=1,ops=[{'op':'update_feature','target_id':'box',
            'payload':{'params_remove':['height'],'params':{'height':2}},'reason':'Ambiguous'}]))
