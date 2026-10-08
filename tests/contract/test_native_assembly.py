"""Proof that animations use FreeCAD's actual Assembly solver."""
from pathlib import Path
import pytest
from tcad.ir.assembly import AssemblySpec
from tests.contract.test_primitive_placement import worker, FREECAD_CMD

pytestmark = [pytest.mark.contract, pytest.mark.skipif(not Path(FREECAD_CMD).exists(), reason='FreeCAD unavailable')]


def native_ir(kind='Revolute', drivers=None):
    assembly = AssemblySpec.model_validate({'grounded':['base'], 'joints':[
        {'id':'joint', 'type':kind, 'side1':{'body_id':'base'}, 'side2':{'body_id':'arm'}}],
        'drivers': drivers or [{'joint_id':'joint','type':'Angular','formula':'pi/2*time'}], 'step':0.1})
    return {'model_id':'native', 'bodies':[
        {'id':'base','name':'base','features':[{'id':'base_box','op':'additive_box','params':{'length':1,'width':1,'height':1}}]},
        {'id':'arm','name':'arm','features':[{'id':'arm_box','op':'additive_box','params':{'length':6,'width':2,'height':1}}]}],
        'assembly': assembly.model_dump()}


def test_native_revolute_frames_and_grounded_part(worker,tmp_path):
    result = worker.request_sync('simulate_assembly', {'ir': native_ir(), 'out_dir':str(tmp_path)}, timeout_s=180)
    assert result['ok'], result
    assert len(result['frames']) >= 10
    assert result['frames'][0]['base'] == pytest.approx(result['frames'][-1]['base'])
    assert result['frames'][0]['arm'] != pytest.approx(result['frames'][-1]['arm'])
    assert Path(result['export']).is_file()


@pytest.mark.parametrize('method,first_z,last_z', [
    ('solve_assembly', -10, -10),
    ('simulate_assembly', 10, 20),
])
def test_native_preview_applies_solved_pose_once(worker, tmp_path, method, first_z, last_z):
    from tcad.ir.animation import pose_frame

    ir = native_ir()
    if method == 'solve_assembly':
        ir['assembly']['drivers'] = []
        ir['assembly']['joints'][0]['type'] = 'Fixed'
        ir['assembly']['joints'][0]['side2']['position'] = [0, 0, 10]
    else:
        ir['assembly']['joints'][0]['type'] = 'Slider'
        ir['assembly']['drivers'] = [
            {'joint_id': 'joint', 'type': 'Linear', 'formula': '10+10*time'}]
    result = worker.request_sync(method, {'ir': ir, 'out_dir': str(tmp_path)}, timeout_s=180)
    assert result['ok'], result
    arm = next(part for part in result['parts'] if part['body_id'] == 'arm')
    start, end = arm['vertex_start'], arm['vertex_start'] + arm['vertex_count']
    for frame, expected_z in ((result['frames'][0], first_z), (result['frames'][-1], last_z)):
        vertices = pose_frame(result['mesh']['vertices'], result['parts'], frame)[start:end]
        assert min(point[2] for point in vertices) == pytest.approx(expected_z, abs=1e-5)
        assert max(point[2] for point in vertices) == pytest.approx(expected_z + 1, abs=1e-5)


@pytest.mark.parametrize('kind,drivers', [
    ('Slider',[{'joint_id':'joint','type':'Linear','formula':'10*time'}]),
    ('Cylindrical',[{'joint_id':'joint','type':'Angular','formula':'pi/2*time'},
                    {'joint_id':'joint','type':'Linear','formula':'10*time'}]),
])
def test_native_linear_and_dual_axis_drivers(worker,tmp_path,kind,drivers):
    result = worker.request_sync('simulate_assembly', {'ir':native_ir(kind,drivers), 'out_dir':str(tmp_path)},timeout_s=180)
    assert result['ok'],result
    final=result['frames'][-1]['arm']
    assert abs(final[11]) == pytest.approx(10,abs=1e-5)
    if kind=='Cylindrical':
        assert abs(final[0]) < 1e-5


@pytest.mark.parametrize('kind,expected_sign', [('Gears',-1),('Belt',1)])
def test_native_gear_and_belt_coupling(worker,tmp_path,kind,expected_sign):
    ir=native_ir()
    ir['bodies'].append({'id':'follower','name':'follower','features':[{'id':'follow_box','op':'additive_box', 'params':{'length':4,'width':1,'height':1}}]})
    ir['assembly']['joints'].extend([
        {'id':'follower_axis','type':'Revolute','side1':{'body_id':'base','position':[5,0,0]},'side2':{'body_id':'follower','position':[5,0,0]}},
        {'id':'coupling','type':kind,'side1':{'body_id':'arm'},'side2':{'body_id':'follower','position':[5,0,0]},'distance':2,'distance2':4},
    ])
    ir['assembly']=AssemblySpec.model_validate(ir['assembly']).model_dump()
    result=worker.request_sync('simulate_assembly',{'ir':ir,'out_dir':str(tmp_path)},timeout_s=180)
    assert result['ok'],result
    final=result['frames'][-1]['follower']
    assert final[0] == pytest.approx(2**-0.5,abs=1e-5)
    assert final[4] == pytest.approx(expected_sign*2**-0.5,abs=1e-5)


def test_saved_motion_summary_observes_closed_harmonic_cycle_and_passive_follower(worker, tmp_path):
    import math
    from tcad.inspect.motion import assembly_definition, measure_saved_motion

    ir = native_ir(drivers=[{'joint_id': 'joint', 'type': 'Angular',
                            'formula': '0.35*sin(2*pi*time)'}])
    ir['bodies'].append({'id': 'follower', 'name': 'follower', 'features': [
        {'id': 'follow_box', 'op': 'additive_box', 'params': {'length': 4, 'width': 1, 'height': 1}}]})
    ir['assembly']['joints'].extend([
        {'id': 'follower_axis', 'type': 'Revolute', 'side1': {'body_id': 'base', 'position': [5, 0, 0]},
         'side2': {'body_id': 'follower', 'position': [5, 0, 0]}},
        {'id': 'coupling', 'type': 'Gears', 'side1': {'body_id': 'arm'},
         'side2': {'body_id': 'follower', 'position': [5, 0, 0]}, 'distance': 2, 'distance2': 2},
    ])
    ir['assembly'].update(end=2, step=0.05)
    ir['assembly'] = AssemblySpec.model_validate(ir['assembly']).model_dump()
    result = worker.request_sync('simulate_assembly', {'ir': ir, 'out_dir': str(tmp_path)}, timeout_s=180)
    assert result['ok'], result
    summary = measure_saved_motion(result, result['mesh']['vertices'],
        track_points=[{'name': 'tip', 'body_id': 'arm', 'point': [6, 0, 0]}], sample_frames=[0, 5, 15, 40])
    assert summary['frames_examined'] == 41
    assert set(summary['moving_bodies']) == {'arm', 'follower'}
    assert assembly_definition(ir['assembly'])['prescribed_driver_count'] == 1
    measured = {b['body_id']: b for b in summary['bodies']}
    assert measured['base']['max_rotation_from_first_deg'] == 0
    assert measured['arm']['max_rotation_from_first_deg'] == pytest.approx(math.degrees(0.35), abs=1e-5)
    assert measured['follower']['max_rotation_from_first_deg'] == pytest.approx(math.degrees(0.35), abs=1e-5)
    assert result['frames'][0]['arm'] == pytest.approx(result['frames'][-1]['arm'], abs=1e-5)
    assert result['frames'][5]['arm'][4] == pytest.approx(math.sin(0.35), abs=1e-5)
    assert result['frames'][5]['follower'][4] == pytest.approx(-math.sin(0.35), abs=1e-5)
    samples = summary['point_tracks'][0]['samples']
    assert samples[1]['world_mm'] == pytest.approx([6 * math.cos(0.35), 6 * math.sin(0.35), 0], abs=1e-5)
    assert samples[2]['world_mm'] == pytest.approx([6 * math.cos(0.35), -6 * math.sin(0.35), 0], abs=1e-5)


@pytest.mark.parametrize('kind', ['Screw','RackPinion'])
def test_native_rotation_translation_coupling(worker,tmp_path,kind):
    ir=native_ir()
    ir['bodies'].append({'id':'slide','name':'slide','features':[{'id':'slide_box','op':'additive_box','params':{'length':2,'width':2,'height':2}}]})
    ir['assembly']['joints'].extend([
        {'id':'slide_axis','type':'Slider','side1':{'body_id':'base'},'side2':{'body_id':'slide'}},
        {'id':'coupling','type':kind,'side1':{'body_id':'slide'},'side2':{'body_id':'arm'},'distance':2},
    ])
    if kind == 'RackPinion':
        ir['assembly']['joints'][-2]['side1']['axis'] = [1,0,0]
        ir['assembly']['joints'][-2]['side2']['axis'] = [1,0,0]
        ir['assembly']['joints'][-1]['side1']['axis'] = [1,0,0]
    ir['assembly']=AssemblySpec.model_validate(ir['assembly']).model_dump()
    result=worker.request_sync('simulate_assembly',{'ir':ir,'out_dir':str(tmp_path)},timeout_s=180)
    assert result['ok'],result
    start,end=result['frames'][0]['slide'],result['frames'][-1]['slide']
    assert start != pytest.approx(end)
    # Analytical coupling is checked separately for native pitch conventions.
    assert all(abs(end[i]-start[i]) < 1e-6 for i in (0,1,2,4,5,6,8,9,10))


@pytest.mark.parametrize('kind',['Fixed','Ball','Distance','Parallel','Perpendicular','Angle'])
def test_native_passive_joint_static_solve(worker,tmp_path,kind):
    ir=native_ir();ir['assembly']['drivers']=[]
    joint=ir['assembly']['joints'][0];joint['type']=kind
    if kind=='Perpendicular': joint['side2']['axis']=[1,0,0]
    if kind=='Angle':
        joint['side2']['axis']=[1,0,0];joint['angle']=90
    if kind=='Distance': joint['distance']=0
    ir['assembly']=AssemblySpec.model_validate(ir['assembly']).model_dump()
    result=worker.request_sync('solve_assembly',{'ir':ir,'out_dir':str(tmp_path)},timeout_s=180)
    assert result['ok'],result
    assert len(result['frames'])==1


@pytest.mark.parametrize('formula,step', [('2*pi*time', 0.05), ('20*pi*time', 0.01)])
def test_nested_carrier_oscillation_and_relative_rotor_spin(worker, tmp_path, formula, step):
    """A global moving-body list cannot distinguish inherited yaw from rotor spin."""
    from tcad.inspect.motion import measure_joint_motion
    ir = native_ir(drivers=[{'joint_id':'joint','type':'Angular',
                            'formula':'pi/4*sin(2*pi*time)'}])
    ir['bodies'].append({'id':'rotor','name':'rotor','features':[
        {'id':'rotor_box','op':'additive_box','params':{'length':2,'width':6,'height':1}}]})
    ir['assembly']['joints'].append({'id':'spin','type':'Revolute',
        'side1':{'body_id':'arm','axis':[1,0,0]},
        'side2':{'body_id':'rotor','axis':[1,0,0]}})
    ir['assembly']['drivers'].append({'joint_id':'spin','type':'Angular','formula':formula})
    ir['assembly']['step']=step
    ir['assembly']=AssemblySpec.model_validate(ir['assembly']).model_dump()
    result=worker.request_sync('simulate_assembly',{'ir':ir,'out_dir':str(tmp_path)},timeout_s=180)
    assert result['ok'],result
    assert len(result['frames']) == round(1/step)+1
    yaw,spin=measure_joint_motion(result,ir['assembly'])
    assert yaw['min_angle_from_first_deg'] == pytest.approx(-45, abs=1e-4)
    assert yaw['max_angle_from_first_deg'] == pytest.approx(45, abs=1e-4)
    assert yaw['direction_reversals'] == 2
    assert spin['sampled_angular_path_deg'] == pytest.approx(360 if step==0.05 else 3600, abs=1e-4)
    assert spin['direction_reversals'] == 0
    assert result['frames'][0]['base'] == pytest.approx(result['frames'][-1]['base'])


def test_native_solver_partial_success_is_not_accepted_as_completed_motion(worker, tmp_path):
    # This rapid motion makes this Ondsel build stop after t=0 despite code=0.
    ir = native_ir(drivers=[{'joint_id':'joint','type':'Angular','formula':'20*pi*time'}])
    ir['assembly'].update(step=0.025)
    from tcad.core.worker_client import WorkerCallFailed
    try:
        result = worker.request_sync('simulate_assembly', {'ir':ir,'out_dir':str(tmp_path)}, timeout_s=180)
    except WorkerCallFailed as exc:
        assert 'expected 41' in str(exc)
    else:
        # Newer native solvers may finish; a full series is then legitimate.
        assert result['ok'] and len(result['frames']) == 41


def test_relative_rotation_with_nonzero_native_initial_phase(worker, tmp_path):
    from tcad.inspect.motion import measure_joint_motion
    ir = native_ir(drivers=[{'joint_id':'joint','type':'Angular','formula':'pi/3+pi/2*time'}])
    result = worker.request_sync('simulate_assembly', {'ir':ir,'out_dir':str(tmp_path)}, timeout_s=180)
    assert result['ok'], result
    measured = measure_joint_motion(result, ir['assembly'])[0]
    assert measured['sampled_angular_path_deg'] == pytest.approx(90, abs=1e-4)
    assert measured['direction_reversals'] == 0
