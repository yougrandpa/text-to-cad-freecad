"""Compact curved/rotor recipes compile and measure with the real CAD kernel."""
import math

import pytest

from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.patch import apply_patch
from tcad.ir.schema import IrDocument, IrPatch
from tests.contract.test_sketch_planes import compile_ir, pytestmark, worker
from tests.unit.test_shape_recipes import loft, rotor, recipe_services
from tcad.tools.authoring import build_parts_handler


def recipe_ir(parts):
    ops,_=parts_patch(BuildParts(parts=parts,reason='Contract'), [])
    return apply_patch(IrDocument(model_id='shape_recipe'),
                       IrPatch(base_version=0,ops=ops)).ir.model_dump(mode='json')


@pytest.mark.parametrize('axis',['X','Y','Z'])
@pytest.mark.parametrize('swap',[False,True])
def test_loft_plane_orientation_and_semi_axes_are_measured(worker,tmp_path,axis,swap):
    part=loft(axis)
    if swap:
        for section in part['sections']:
            section['radii'].reverse()
    ir=recipe_ir([part])
    result=compile_ir(worker,ir,tmp_path)
    measured=result['measurements']
    assert measured['is_valid'] and measured['solids']==1
    assert measured['bbox'][axis.lower()]==pytest.approx(10)
    # Similar elliptical sections, radii grow linearly from (2,1) to (4,2).
    assert measured['volume']==pytest.approx(math.pi*10*(2+math.sqrt(2*8)+8)/3,rel=1e-6)
    u,v={'X':('y','z'),'Y':('x','z'),'Z':('x','y')}[axis]
    # OCC bounds of a spline loft are conservative; use the analytical volume
    # above and the larger world extent to check section orientation.
    assert (measured['bbox'][u] < measured['bbox'][v]) if swap else (measured['bbox'][u] > measured['bbox'][v])
    assert result['round_trip']['ok']


@pytest.mark.parametrize('axis',[[0,0,1],[0,1,0],[1,0,0],[1,2,3]])
def test_rotor_has_one_connected_valid_body_and_no_outer_rim(worker,tmp_path,axis):
    part=rotor(axis)
    result=compile_ir(worker,recipe_ir([part]),tmp_path)
    assert result['measurements']['is_valid'] and result['measurements']['solids']==1
    assert result['round_trip']['ok']
    assert result['measurements']['volume'] > math.pi*4**2*2
    assert result['measurements']['volume'] < math.pi*20**2*2/2


def test_recipe_reopened_sections_and_blades_remain_native_editable(worker,tmp_path):
    from tcad.worker.protocol import M_REOPEN_EDIT
    result=compile_ir(worker,recipe_ir([loft(),rotor()]),tmp_path)
    reopened=worker.request_sync(M_REOPEN_EDIT,{'fcstd_path':result['fcstd'],'edits':[
        {'object':'shell','property':'Ruled','value':True},
        {'object':'propeller_blade_0','property':'Length','value':25}]})
    assert reopened['ok'], reopened
    assert reopened['feature_states']['shell']['type_id']=='PartDesign::AdditiveLoft'
    assert reopened['feature_states']['shell_plane_0']['type_id']=='PartDesign::Plane'
    assert reopened['feature_states']['propeller_blade_0']['type_id']=='PartDesign::AdditiveBox'
    assert reopened['measurements']['is_valid'] and reopened['measurements']['solids']==2


def test_circular_loft_sections_and_subtractive_recipe_measure_the_requested_bore(worker,tmp_path):
    part=loft('Z')
    part['operation']='cut'
    part['body_id']='base'
    for section in part['sections']:
        section['radii']=[section['radii'][0]]*2
    result=compile_ir(worker,recipe_ir([
        {'id':'block','body_id':'base','shape':'box','center':[0,0,5],'size':[10,10,10]},part]),tmp_path)
    assert result['measurements']['solids']==1 and result['measurements']['is_valid']
    assert result['measurements']['volume']==pytest.approx(1000-math.pi*10*28/3,rel=1e-6)
    assert result['round_trip']['ok']


@pytest.mark.parametrize('axis',['X','Y','Z'])
def test_loft_downstream_cut_preserves_the_requested_material_removal(worker,tmp_path,axis):
    part=loft(axis)
    start=[0,0,0]; end=[0,0,0]
    index='XYZ'.index(axis)
    start[index]=-1; end[index]=11
    cut={'id':'bore','body_id':part['body_id'],'shape':'cylinder','operation':'cut',
         'start':start,'end':end,'radius':0.5}
    base=compile_ir(worker,recipe_ir([part]),tmp_path/'base')
    result=compile_ir(worker,recipe_ir([part,cut]),tmp_path/'cut')
    assert result['measurements']['is_valid'] and result['measurements']['solids'] == 1
    removed=base['measurements']['volume']-result['measurements']['volume']
    assert removed == pytest.approx(math.pi*cut['radius']**2*10,rel=1e-6)
    assert result['round_trip']['ok']


@pytest.mark.parametrize('count',[2,5,8])
async def test_rotor_edits_and_rebuilds_have_equivalent_geometry(worker,tmp_path,recipe_services,count):
    services,ctx=recipe_services
    part=rotor()
    cut={'id':'bore','body_id':part['body_id'],'shape':'cylinder','operation':'cut',
         'start':[1,2,0],'end':[1,2,6],'radius':1}
    assert (await build_parts_handler(services,{'parts':[part,cut],'reason':'Create'},ctx)).ok
    changed={**part,'blade_count':count}
    assert (await build_parts_handler(services,{'parts':[changed],'reason':'Edit'},ctx)).ok
    edited=compile_ir(worker,services.store.load('m').model_dump(mode='json'),tmp_path/'edited')
    rebuilt=compile_ir(worker,recipe_ir([changed,cut]),tmp_path/'rebuilt')
    assert edited['measurements']['is_valid'] and edited['measurements']['solids'] == 1
    assert edited['measurements']['volume'] == pytest.approx(rebuilt['measurements']['volume'],rel=1e-6)
    assert edited['measurements']['bbox'] == pytest.approx(rebuilt['measurements']['bbox'],abs=1e-6)
    assert edited['round_trip']['ok']


async def test_loft_section_growth_preserves_downstream_geometry(worker,tmp_path,recipe_services):
    services,ctx=recipe_services
    part=loft('Z')
    cut={'id':'bore','body_id':part['body_id'],'shape':'cylinder','operation':'cut',
         'start':[0,0,-1],'end':[0,0,11],'radius':0.5}
    initial={**part,'sections':part['sections'][:2]}
    assert (await build_parts_handler(services,{'parts':[initial,cut],'reason':'Create'},ctx)).ok
    assert (await build_parts_handler(services,{'parts':[part],'reason':'Add section'},ctx)).ok
    edited=compile_ir(worker,services.store.load('m').model_dump(mode='json'),tmp_path/'edited')
    rebuilt=compile_ir(worker,recipe_ir([part,cut]),tmp_path/'rebuilt')
    assert edited['measurements']['is_valid'] and edited['measurements']['solids'] == 1
    assert edited['measurements']['volume'] == pytest.approx(rebuilt['measurements']['volume'],rel=1e-6)
    assert edited['round_trip']['ok']
