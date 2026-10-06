import math
from types import SimpleNamespace
import pytest
from tcad.ir.builders import BuildParts, parts_patch
from tcad.ir.rotary import gravity_frames, RotaryRig
from tcad.tools.ir_tools import _ir_patch_schema
from tcad.tools.schema_check import check
from tcad.tools.authoring import build_authoring_tools


def rig(speed=0, initial=0, damping=0):
    return {'start':0,'end':2,'step':0.01,'rotation':{'rotating_body_ids':['wheel'],
        'center':[0,0,100],'speed_deg_s':speed,'suspensions':[{'body_ids':['cab'],
        'pivot':[60,0,100],'com_distance_mm':20,'inertia_factor':1,
        'damping_per_s':damping,'initial_angle_deg':initial}]}}


def test_stationary_gravity_equilibrium_and_full_rotation():
    result=gravity_frames(rig(),['base','wheel','cab'])
    assert result['max_swing_deg']==0
    result=gravity_frames({**rig(30),'end':12,'step':0.1},['base','wheel','cab'])
    assert 0 < result['max_swing_deg'] < 1
    assert result['frames'][-1]['wheel']==pytest.approx(result['frames'][0]['wheel'],abs=1e-10)
    for f in result['frames']:
        m=f['cab']; hinge=[sum(m[i*4+j]*p for j,p in enumerate([60,0,100]))+m[i*4+3] for i in range(3)]
        assert math.dist(hinge,[0,0,100])==pytest.approx(60)
        assert f['base']==result['frames'][0]['base']


def test_pendulum_energy_conservation_and_damping():
    free=gravity_frames(rig(initial=12),['base','wheel','cab'])
    damped=gravity_frames(rig(initial=12,damping=2),['base','wheel','cab'])
    # Point-mass pendulum crosses equilibrium and retains amplitude without damping.
    angles=[f['cab'] for f in free['suspension_angles_deg']]
    assert min(angles)<-11.9 and max(angles)==pytest.approx(12)
    assert max(abs(f['cab']) for f in damped['suspension_angles_deg'][-30:]) < 3


def test_copy_anchor_keeps_cabins_below_pins_and_atomic_ids():
    request=BuildParts(parts=[{'id':'cab_box','body_id':'cab','shape':'box',
        'center':[60,0,80],'size':[10,10,20],
        'copies':{'count':4,'center':[0,0,100],'axis':[0,1,0],'anchor':[60,0,100],'rotate':False}}],reason='Cabins')
    ops,ids=parts_patch(request,[])
    assert ids==['cab_0','cab_1','cab_2','cab_3']
    boxes=[op['payload'] for op in ops if op['op']=='add_feature']
    assert list(boxes[1]['placement']['position'].values())==pytest.approx([-5,-5,10])
    assert all(b['placement']['angle']==0 for b in boxes)


def test_tube_bore_stays_within_declared_endpoints():
    request=BuildParts(parts=[{'id':'tube','body_id':'part','shape':'tube',
        'start':[4,5,8],'end':[4,5,28],'radius':5,'inner_radius':3}],reason='Tube')
    ops,_=parts_patch(request,[])
    outer,bore=[op['payload'] for op in ops if op['op']=='add_feature']
    assert bore['placement']==outer['placement']
    assert bore['params']=={'radius':3,'height':20}


@pytest.mark.parametrize('direction',[[0,0,20],[10,20,30],[0,0,-20]])
@pytest.mark.parametrize('copy_axis',[[0,0,1],[0,1,0],[1,2,3]])
def test_beam_copies_rotate_the_whole_cross_section(direction,copy_axis):
    from tcad.ir.motion import rotate
    start=[60,0,10]
    request=BuildParts(parts=[{'id':'beam','body_id':'part','shape':'beam',
        'start':start,'end':[start[i]+direction[i] for i in range(3)],'width':2,'depth':6,
        'copies':{'count':4,'center':[0,0,0],'axis':copy_axis}}],reason='Beam copies')
    ops,_=parts_patch(request,[])
    beams=[op['payload'] for op in ops if op['op']=='add_feature']
    def world_point(placement,point):
        turned=rotate(point,list(placement['axis'].values()),math.radians(placement['angle']))
        return [turned[i]+placement['position'][key] for i,key in enumerate('xyz')]
    corners=[[x,y,z] for x in (0,2) for y in (0,6) for z in (0,math.hypot(*direction))]
    for index,beam in enumerate(beams):
        for corner in corners:
            expected=rotate(world_point(beams[0]['placement'],corner),copy_axis,index*math.pi/2)
            assert world_point(beam['placement'],corner)==pytest.approx(expected,abs=1e-10)


@pytest.mark.parametrize('payload',[
    {'constraints_append':[{'kind':'note','value':'upright'}]},
    {'key':'count','value':8,'confirmed':True}, {'raw_text':'replacement'},
    {'constraints_append':[{'value':8}]},
])
def test_reported_requirement_failures_rejected_with_field_paths(payload):
    errors=check({'base_version':'current','ops':[{'op':'update_requirement','payload':payload,'reason':'Review'}]},_ir_patch_schema())
    assert errors and 'arguments.ops[0].payload' in errors[0]


def test_schema_has_inline_copy_contract_and_no_untyped_shape_guessing():
    tools=build_authoring_tools(SimpleNamespace())
    schema=tools['cad_build_parts'].params_schema
    assert '$ref' not in str(schema) and '$defs' not in str(schema)
    assert check({'parts':[{'id':'x','body_id':'x','shape':'sphere'}],'reason':'Make'},schema)
    assert check({'parts':[{'id':'x','body_id':'x','shape':'box','size':[1,2]}],'reason':'Make'},schema)
    assert check({'parts':[],'reason':'Make'},schema)
    with pytest.raises(ValueError):
        BuildParts(parts=[{'id':'x','body_id':'x','shape':'box','radius':2}],reason='bad')
    with pytest.raises(ValueError):
        RotaryRig(rotating_body_ids=['w'],center=[0,0,0],axis=[0,0,1])


def test_remove_body_is_audited_and_referenced_bodies_cannot_disappear():
    from tcad.ir.schema import IrDocument, IrPatch, BodySpec
    from tcad.ir.patch import apply_patch, PatchError
    ir=IrDocument(model_id='m',bodies=[BodySpec(id='empty',name='empty')])
    out=apply_patch(ir,IrPatch(base_version=0,ops=[{'op':'remove_body','target_id':'empty','reason':'Delete empty draft'}]))
    assert not out.ir.bodies and ir.bodies
    assert 'remove_body empty' in out.changes[0]
    from tcad.ir.assembly import AssemblySpec
    ir.bodies.append(BodySpec(id='base',name='base'))
    ir.assembly=AssemblySpec(grounded=['base'],rotation=RotaryRig(rotating_body_ids=['empty'],center=[0,0,0]))
    with pytest.raises(PatchError,match='unknown moving bodies'):
        apply_patch(ir,IrPatch(base_version=0,ops=[{'op':'remove_body','target_id':'empty','reason':'Bad delete'}]))


def test_op_catalog_does_not_unlock_or_repeat_all_editing_schemas():
    from tcad.loop.engine import LoopEngine, LoopConfig
    from tcad.loop.budget import BudgetLimits
    from tcad.tools.base import build_default_registry
    from tcad.core.types import TurnKind
    svc=SimpleNamespace()
    registry=build_default_registry(svc)
    engine=LoopEngine(svc,registry,BudgetLimits(),LoopConfig(require_design_review=True))
    all_tools=registry.as_openai_tools(TurnKind.CREATE)
    initial=engine._authoring_surface(all_tools)
    names={t['function']['name'] for t in initial}
    assert {'cad_build_parts','cad_wheel','ir_help'}<=names
    assert not {'cad_cabins','cad_wheel_support','assembly_motion'} & names
    assert not {'ir_patch','assembly_configure'}&names
    engine._authoring_topics={'patch'}
    catalog=engine._authoring_surface(all_tools)
    patch=next(t for t in catalog if t['function']['name']=='ir_patch')
    ops={b['properties']['op']['enum'][0] for b in patch['function']['parameters']['properties']['ops']['items']['anyOf']}
    assert 'remove_body' in ops and 'add_sketch' not in ops


async def test_compact_updates_preserve_feature_order_and_explicit_version_safety(tmp_path):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services
    from tcad.core.types import ToolContext
    from tcad.ir.schema import IrDocument
    from tcad.tools.authoring import build_parts_handler
    cfg=load_default_config();cfg.storage.data_dir=str(tmp_path);cfg.storage.sqlite_path=''
    services=build_services(cfg,start_worker=False)
    ctx=ToolContext(model_id='m',thread_id='t',turn_id='turn',data_dir=str(tmp_path))
    services.store.create('m',IrDocument(model_id='m'))
    try:
        part={'id':'base','body_id':'base','shape':'box','center':[0,0,1],'size':[10,10,2]}
        first=await build_parts_handler(services,{'parts':[part],'reason':'Create'},ctx)
        assert first.ok,first.error
        updated=await build_parts_handler(services,{'parts':[{**part,'size':[20,10,2]}], 'reason':'Widen','base_version':'1'},ctx)
        assert updated.ok,updated.error
        ir=services.store.load('m')
        assert len(ir.bodies)==1 and len(ir.bodies[0].features)==1
        assert ir.bodies[0].features[0].params['length']==20
        stale=await build_parts_handler(services,{'parts':[part],'reason':'Stale','base_version':'1'},ctx)
        assert not stale.ok and 'stale base_version' in stale.error.message
        assert services.store.load('m').version==2
    finally: services.worker.close()


@pytest.mark.parametrize('copies',[None,{'count':4,'center':[0,0,0],'axis':[0,0,1]}])
async def test_tube_to_cylinder_is_rejected_atomically_and_tube_updates_work(tmp_path,copies):
    from tcad.config.loader import load_default_config
    from tcad.core.wiring import build_services
    from tcad.core.types import ToolContext
    from tcad.ir.schema import IrDocument
    from tcad.tools.authoring import build_parts_handler
    cfg=load_default_config();cfg.storage.data_dir=str(tmp_path);cfg.storage.sqlite_path=''
    services=build_services(cfg,start_worker=False)
    ctx=ToolContext(model_id='m',thread_id='t',turn_id='turn',data_dir=str(tmp_path))
    services.store.create('m',IrDocument(model_id='m'))
    try:
        tube={'id':'tube','body_id':'part','shape':'tube','start':[20,0,0],
              'end':[20,0,20],'radius':5,'inner_radius':3,'copies':copies}
        result=await build_parts_handler(services,{'parts':[tube],'reason':'Tube'},ctx)
        assert result.ok,result.error
        before=services.store.load('m').model_dump()
        cylinder={k:v for k,v in tube.items() if k!='inner_radius'}
        cylinder['shape']='cylinder'
        result=await build_parts_handler(services,{'parts':[
            {'id':'base','body_id':'base','shape':'box','center':[0,0,0],'size':[10,10,2]},
            cylinder],'reason':'Solid cylinder'},ctx)
        assert not result.ok and 'changing tube to cylinder' in result.error.message
        assert services.store.load('m').model_dump()==before

        result=await build_parts_handler(services,{'parts':[{**tube,'inner_radius':2}],
            'reason':'Smaller tube bore'},ctx)
        assert result.ok,result.error
        updated=services.store.load('m')
        for old_body,body in zip(before['bodies'],updated.bodies):
            assert [f.id for f in body.features]==[f['id'] for f in old_body['features']]
            assert body.features[1].params['radius']==2

        # An explicit bore removal permits reusing the outer cylinder's recipe ID.
        from tcad.tools.ir_tools import ir_patch_handler
        result=await ir_patch_handler(services,{'base_version':'current','ops':[
            {'op':'remove_feature','target_id':body.features[1].id,'payload':{},
             'reason':'Remove tube bore explicitly'} for body in updated.bodies]},ctx)
        assert result.ok,result.error
        result=await build_parts_handler(services,{'parts':[cylinder],'reason':'Solid cylinder'},ctx)
        assert result.ok,result.error
        assert all(len(body.features)==1 for body in services.store.load('m').bodies)
    finally: services.worker.close()
