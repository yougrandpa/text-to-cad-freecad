import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from tcad.ir.assembly import AssemblySpec, JOINT_TYPES
from tcad.ir.patch import apply_patch, PatchError
from tcad.ir.schema import IrDocument, BodySpec, IrPatch, IrPatchOp
from tcad.server.mesh import _checked_animation
from tcad.render.animation import export_animation

IDENTITY = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]


def spec(kind='Revolute'):
    return {'grounded':['base'],'joints':[{'id':'joint','type':kind,'side1':{'body_id':'base'},'side2':{'body_id':'arm'},'distance':2,'distance2':4}]}


@pytest.mark.parametrize('kind', JOINT_TYPES)
def test_all_native_joint_contracts(kind):
    assembly=AssemblySpec.model_validate(spec(kind))
    assembly.validate_bodies(['base','arm'])


@pytest.mark.parametrize('update', [
    {'grounded':['missing']}, {'grounded':['base','base']}, {'step':0}, {'end':0}, {'step':0.0001},
    {'drivers':[{'joint_id':'missing','type':'Angular','formula':'time'}]},
    {'drivers':[{'joint_id':'joint','type':'Linear','formula':'time'}]},
    {'drivers':[{'joint_id':'joint','type':'Angular','formula':'__import__("os")'}]},
])
def test_invalid_assembly_declarations(update):
    with pytest.raises(ValueError):
        assembly=AssemblySpec.model_validate({**spec(),**update})
        assembly.validate_bodies(['base','arm'])


def test_assembly_patch_persists_and_rejects_dangling_bodies():
    ir=IrDocument(model_id='assembly',bodies=[BodySpec(id='base',name='base'),BodySpec(id='arm',name='arm')])
    patch=IrPatch(base_version=0,ops=[IrPatchOp(op='set_assembly',payload={'assembly':spec()},reason='attach native joints')])
    result=apply_patch(ir,patch).ir
    assert result.assembly.joints[0].type=='Revolute'
    assert ir.assembly is None
    bad=spec();bad['joints'][0]['side2']['body_id']='missing'
    with pytest.raises(PatchError):
        apply_patch(ir,IrPatch(base_version=0,ops=[IrPatchOp(op='set_assembly',payload={'assembly':bad},reason='invalid')]))


def animation():
    matrix=IDENTITY.copy();matrix[3]=2
    return {'mesh':{'vertices':[[0,0,0],[1,0,0],[0,1,0]],'facets':[[0,1,2]]},
            'parts':[{'body_id':'arm','vertex_start':0,'vertex_count':3}],
            'frames':[{'arm':IDENTITY},{'arm':matrix}],'start':0,'step':0.1}


def test_animation_validation_requires_exact_bodies_and_finite_transforms():
    result=animation();ir={'bodies':[{'id':'arm'}],'assembly':{'start':0,'step':0.1}}
    assert _checked_animation(result,ir,3)['frames']==result['frames']
    for change in ('missing','nan','range'):
        bad=copy.deepcopy(result)
        if change=='missing': bad['frames'][0]={}
        if change=='nan': bad['frames'][0]['arm'][0]=float('nan')
        if change=='range': bad['parts'][0]['vertex_count']=4
        with pytest.raises(HTTPException): _checked_animation(bad,ir,3)


@pytest.mark.parametrize('format',['gif','mp4','avi','webm'])
def test_native_frame_media_export(tmp_path,format):
    if format!='gif':
        pytest.importorskip('av')
    path=tmp_path / ('motion.'+format)
    result=export_animation(animation(),path,width=128,height=128,stride=1)
    assert result['frames']==2
    assert path.stat().st_size>100
