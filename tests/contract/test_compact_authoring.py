"""Real editable wheel geometry and gravity frames share the saved artifact."""
import asyncio
import math
from pathlib import Path
import pytest
from tcad.core.types import ToolContext
from tcad.ir.schema import IrDocument
from tcad.tools.authoring import build_parts_handler, radial_wheel_handler, rotation_handler
from tcad.inspect.artifact import ArtifactReader
from tests.contract.test_build_runtime import services, commit
from tests.contract.test_primitive_placement import FREECAD_CMD

pytestmark=[pytest.mark.contract,pytest.mark.skipif(not Path(FREECAD_CMD).exists(),reason='FreeCAD unavailable')]


def test_editable_wheel_gravity_cabins_and_saved_collision_frames(services):
    from tcad.tools.geo_tools import assembly_simulate_handler, assembly_export_handler
    services.store.create('gravity-wheel',IrDocument(model_id='gravity-wheel'))
    ctx=ToolContext(model_id='gravity-wheel',thread_id='t',turn_id='turn',data_dir=services.config.storage.data_dir)
    async def create():
        result=await radial_wheel_handler(services,{'body_id':'wheel','center':[0,0,100],
            'radius':60,'thickness':8,'rim_width':5,'hub_radius':10,'spoke_width':4,
            'bore_radius':3,'reason':'Connected wheel'},ctx)
        assert result.ok,result.error
        result=await build_parts_handler(services,{'parts':[
            {'id':'base','body_id':'base','shape':'box','center':[0,0,0],'size':[180,100,10]},
            {'id':'seat','body_id':'cab','shape':'box','center':[60,20,84],'size':[12,12,20],
             'copies':{'count':8,'center':[0,0,100],'axis':[0,1,0],'anchor':[60,20,100],'rotate':False,'separate_bodies':True}},
            {'id':'hanger','body_id':'cab','shape':'cylinder','start':[60,20,92],'end':[60,20,100],'radius':1,
             'copies':{'count':8,'center':[0,0,100],'axis':[0,1,0],'anchor':[60,20,100],'rotate':False,'separate_bodies':True}}],
            'reason':'Upright cabins'},ctx)
        assert result.ok,result.error
        suspensions=[]
        from tcad.ir.motion import rotate
        for i in range(8):
            p=rotate([60,0,0],[0,1,0],i*math.pi/4)
            suspensions.append({'body_ids':[f'cab_{i}'],'pivot':[p[0],20,p[2]+100]})
        result=await rotation_handler(services,{'rotating_body_ids':['wheel'],'center':[0,0,100],
            'duration_s':12,'frames':61,'suspensions':suspensions,'reason':'Gravity suspension'},ctx)
        assert result.ok,result.error
    asyncio.run(create())
    version=services.store.current_version('gravity-wheel')
    result,report=commit(services,'gravity-wheel',version)
    assert report and report.passed,(result.error,result.content)
    reader=ArtifactReader(ctx.data_dir); manifest,root=reader.resolve(ctx.model_id)
    scene=reader.scene(manifest,root)
    assert scene.animation['solver']=='Planar gravity pendulum / fixed-step RK4'
    assert 0 < scene.animation['max_swing_deg'] < 1
    assert len(scene.animation['frames'])==61
    assert len(scene.animation['parts'])==10
    first,last=scene.animation['frames'][0],scene.animation['frames'][-1]
    assert last['wheel']==pytest.approx(first['wheel'],abs=1e-9)
    for frame in scene.animation['frames']:
        assert frame['base']==first['base']
        for i in range(8):
            # Upright direction follows the physical small swing, not wheel angle.
            matrix=frame[f'cab_{i}']
            assert matrix[10]>math.cos(math.radians(1))
    checked=asyncio.run(assembly_simulate_handler(services,{'check_pairs':[['wheel','cab_0'],['base','cab_0']], 'check_stride':6},ctx))
    assert checked.ok and '"interference_count":0' in checked.content,checked
    exported=asyncio.run(assembly_export_handler(services,{'format':'gif','width':160,'height':160,'stride':10},ctx))
    assert exported.ok,exported.error
    import json
    from PIL import Image
    path=Path(json.loads(exported.content)['path'])
    with Image.open(path) as gif:
        assert gif.n_frames >= 6


def test_cabin_recipe_records_exact_hinges_and_measures_uniform_mass(services):
    from tcad.tools.authoring import cabin_copies_handler
    import json
    services.store.create('cabins',IrDocument(model_id='cabins'))
    ctx=ToolContext(model_id='cabins',thread_id='t',turn_id='turn',data_dir=services.config.storage.data_dir)
    async def build():
        result=await radial_wheel_handler(services,{'body_id':'wheel','center':[0,0,120],
            'radius':60,'thickness':8,'rim_width':5,'hub_radius':10,'spoke_width':4,'reason':'Wheel'},ctx)
        assert result.ok,result.error
        result=await build_parts_handler(services,{'parts':[{'id':'base','body_id':'base',
            'shape':'box','center':[0,0,0],'size':[180,100,10]}],'reason':'Base'},ctx)
        assert result.ok,result.error
        result=await cabin_copies_handler(services,{'prefix':'cab','wheel_center':[0,0,120],
            'radius':60,'count':8,'box_size':[12,12,16],'hang_drop':20,'wall':1,
            'axial_offset':20,'reason':'Generated cabins'},ctx)
        assert result.ok,result.error
        result=await rotation_handler(services,{'rotating_body_ids':['wheel'],'center':[0,0,120],
            'hanging_body_ids':[f'cab_{i}' for i in range(8)],'frames':61,'reason':'Gravity'},ctx)
        assert result.ok,result.error
    asyncio.run(build())
    ir=services.store.load('cabins')
    assert ir.bodies[2].suspension_pivot.as_tuple()==pytest.approx((60,20,120))
    assert ir.assembly.rotation.suspensions[0].pivot==pytest.approx([60,20,120])
    result,report=commit(services,'cabins',ir.version)
    assert report and report.passed,(result.error,result.content)
    reader=ArtifactReader(ctx.data_dir); manifest,root=reader.resolve(ctx.model_id)
    scene=reader.scene(manifest,root)
    assert len(scene.animation['frames'])==61 and scene.animation['max_swing_deg']<1


@pytest.mark.parametrize('plate_z,start_z,end_z',[(4,8,28),(32,8,28),(4,0,-20)])
def test_tube_preserves_material_beyond_both_endpoints(services,plate_z,start_z,end_z):
    services.store.create('tube-plate',IrDocument(model_id='tube-plate'))
    ctx=ToolContext(model_id='tube-plate',thread_id='t',turn_id='turn',data_dir=services.config.storage.data_dir)
    result=asyncio.run(build_parts_handler(services,{'parts':[
        {'id':'plate','body_id':'part','shape':'box','center':[0,0,plate_z],'size':[20,20,8]},
        {'id':'tube','body_id':'part','shape':'tube','start':[0,0,start_z],
         'end':[0,0,end_z],'radius':5,'inner_radius':3}], 'reason':'Tube attached to plate'},ctx))
    assert result.ok,result.error
    result,report=commit(services,ctx.model_id,services.store.current_version(ctx.model_id))
    assert report and report.passed,(result.error,result.content)
    reader=ArtifactReader(ctx.data_dir);manifest,root=reader.resolve(ctx.model_id)
    digest=reader.digest(manifest,root)
    assert digest.topology.solids==1
    assert digest.volume==pytest.approx(20*20*8+math.pi*(5**2-3**2)*20,rel=1e-6)


def test_beam_quarter_turn_copies_swap_measured_cross_section(services):
    services.store.create('beam-copies',IrDocument(model_id='beam-copies'))
    ctx=ToolContext(model_id='beam-copies',thread_id='t',turn_id='turn',data_dir=services.config.storage.data_dir)
    result=asyncio.run(build_parts_handler(services,{'parts':[
        {'id':'beam','body_id':'part','shape':'beam','start':[60,0,0],'end':[60,0,20],
         'width':2,'depth':6,'copies':{'count':4,'center':[0,0,0],'axis':[0,0,1]}}],
        'reason':'Rotate full beam cross sections'},ctx))
    assert result.ok,result.error
    result,report=commit(services,ctx.model_id,services.store.current_version(ctx.model_id))
    assert report and report.passed,(result.error,result.content)
    reader=ArtifactReader(ctx.data_dir);manifest,root=reader.resolve(ctx.model_id)
    scene=reader.scene(manifest,root)
    # Tessellation reads the exported document, so these are actual BRep bounds.
    expected_bounds=[((59,-3,0),(61,3,20)),((-3,59,0),(3,61,20)),
                     ((-61,-3,0),(-59,3,20)),((-3,-61,0),(3,-59,20))]
    ir=services.store.load(ctx.model_id)
    for body,(low,high) in zip(ir.bodies,expected_bounds):
        part=services.worker.request('read_artifact_scene',{'fcstd_path':str(root/(ctx.model_id+'.FCStd')),
            'bodies':[{'id':body.id,'name':body.name}]})
        assert part['ok'],part
        vertices=part['result']['mesh']['vertices']
        assert [min(p[i] for p in vertices) for i in range(3)]==pytest.approx(low,abs=1e-6)
        assert [max(p[i] for p in vertices) for i in range(3)]==pytest.approx(high,abs=1e-6)
    assert scene.mesh.volume==pytest.approx(4*2*6*20,rel=1e-6)


def test_incremental_cut_and_connected_boss_preserve_assembly_and_measured_volume(services):
    from tcad.tools.ir_tools import assembly_configure_handler, ir_digest_handler
    services.store.create('refined', IrDocument(model_id='refined'))
    ctx = ToolContext(model_id='refined', thread_id='t', turn_id='turn',
                      data_dir=services.config.storage.data_dir)

    async def create():
        result = await build_parts_handler(services, {'parts': [
            {'id': 'plate', 'body_id': 'housing', 'shape': 'box',
             'center': [0, 0, 5], 'size': [40, 30, 10]}]}, ctx)
        assert result.ok, result.error
        result = await assembly_configure_handler(services,
            {'assembly': {'grounded': ['housing']}, 'reason': 'Keep assembled pose'}, ctx)
        assert result.ok, result.error

    asyncio.run(create())
    result, report = commit(services, ctx.model_id, services.store.current_version(ctx.model_id))
    assert report and report.passed, (result.error, result.content)
    reader = ArtifactReader(ctx.data_dir)
    before, _ = reader.resolve(ctx.model_id)
    initial = services.store.load(ctx.model_id)
    result = asyncio.run(build_parts_handler(services, {'parts': [
        {'id': 'bore', 'body_id': 'housing', 'shape': 'cylinder', 'operation': 'cut',
         'start': [0, 0, -1], 'end': [0, 0, 11], 'radius': 2},
        {'id': 'boss', 'body_id': 'housing', 'shape': 'cylinder', 'extend_existing': True,
         'start': [12, 0, 8], 'end': [12, 0, 16], 'radius': 3}]}, ctx))
    assert result.ok, result.error
    edited = services.store.load(ctx.model_id)
    assert edited.assembly == initial.assembly
    assert edited.bodies[0].features[0] == initial.bodies[0].features[0]
    result, report = commit(services, ctx.model_id, edited.version)
    assert report and report.passed, (result.error, result.content)
    manifest, root = reader.resolve(ctx.model_id)
    digest = reader.digest(manifest, root)
    assert digest.topology.solids == 1 and digest.is_valid
    assert digest.volume == pytest.approx(12000 - 40*math.pi + 54*math.pi, rel=1e-6)
    assert digest.body_measurements['housing'].volume == pytest.approx(digest.volume)
    animation = reader.scene(manifest, root).animation
    assert animation['solver'] == 'FreeCAD Assembly / OndselSolver'
    assert len(animation['frames']) == 1 and 'housing' in animation['frames'][0]
    # Historical topology stays accessible for a repair while newer IR is pending.
    historical = asyncio.run(ir_digest_handler(services,
        {'artifact_id': before.artifact_id, 'feature_id': 'plate'}, ctx))
    assert historical.ok, historical.error
    assert 'volume(mm^3): 12000' in historical.content and 'historical evidence' in historical.content
