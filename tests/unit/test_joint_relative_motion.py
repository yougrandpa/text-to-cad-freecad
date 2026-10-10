"""Inherited motion cannot prove a child rotates about its own joint."""
import copy
import math

import numpy as np
import pytest

from tcad.inspect.motion import measure_joint_motion, measure_saved_motion
from tests.e2e.standing_fan import standing_fan_evidence
from types import SimpleNamespace


def rotate(axis, degrees):
    theta=math.radians(degrees)
    x,y,z=axis
    c,s=math.cos(theta),math.sin(theta)
    u=1-c
    return np.array([[c+x*x*u,x*y*u-z*s,x*z*u+y*s,0],
                     [y*x*u+z*s,c+y*y*u,y*z*u-x*s,0],
                     [z*x*u-y*s,z*y*u+x*s,c+z*z*u,0],[0,0,0,1]])


def fixture(spin=True):
    times=np.linspace(0,1,41)
    joints=[{'id':'yaw','type':'Revolute','side1':{'body_id':'stand','axis':[0,0,1]},
             'side2':{'body_id':'carrier','axis':[0,0,1]}},
            {'id':'spin','type':'Revolute','side1':{'body_id':'carrier','axis':[1,0,0]},
             'side2':{'body_id':'rotor','axis':[1,0,0]}}]
    assembly={'grounded':['stand'],'joints':joints,'drivers':[
        {'joint_id':'yaw','type':'Angular','formula':'pi/4*sin(2*pi*time)'},
        {'joint_id':'spin','type':'Angular','formula':'2*pi*time'}]}
    frames=[]
    for t in times:
        head=rotate([0,0,1],45*math.sin(2*math.pi*t))
        rotor=head@rotate([1,0,0],360*t if spin else 0)
        frames.append({k:v.ravel().tolist() for k,v in
                       [('stand',np.eye(4)),('carrier',head),('rotor',rotor)]})
    vertices=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]]*3
    animation={'frames':frames,'parts':[{'body_id':b,'vertex_start':i*4,'vertex_count':4}
        for i,b in enumerate(('stand','carrier','rotor'))],'start':0,'step':0.025}
    scene=SimpleNamespace(animation=animation,mesh=SimpleNamespace(vertices=vertices,
        bbox=SimpleNamespace(x=400,y=400,z=1200,z_min=0)))
    return assembly,scene


def test_parent_yaw_is_removed_from_rotor_spin_and_full_cycle_unwrapped():
    assembly,scene=fixture()
    original=copy.deepcopy(scene.animation)
    yaw,spin=measure_joint_motion(scene.animation,assembly)
    assert yaw['min_angle_from_first_deg'] == -45
    assert yaw['max_angle_from_first_deg'] == 45
    assert yaw['direction_reversals'] == 2
    assert spin['net_angle_deg'] == 360
    assert spin['sampled_angular_path_deg'] == 360
    assert spin['direction_reversals'] == 0
    assert scene.animation == original
    assert standing_fan_evidence({'assembly':assembly},scene)['passed']


def test_inherited_parent_motion_warns_and_fails_case_even_when_bodies_move():
    assembly,scene=fixture(spin=False)
    summary=measure_saved_motion(scene.animation,scene.mesh.vertices,assembly=assembly)
    assert 'rotor' in summary['moving_bodies']
    assert summary['joints'][1]['sampled_angular_path_deg'] == 0
    assert len(summary['warnings']) == 1 and 'spin' in summary['warnings'][0]
    assert not standing_fan_evidence({'assembly':assembly},scene)['passed']


def test_reversed_joint_sides_and_nonzero_first_pose():
    assembly,scene=fixture()
    joint=assembly['joints'][1]
    joint['side1'],joint['side2']=joint['side2'],joint['side1']
    for frame in scene.animation['frames']:
        head=np.array(frame['carrier']).reshape(4,4)
        rotor=np.array(frame['rotor']).reshape(4,4)
        frame['rotor']=(rotor@rotate([1,0,0],70)).ravel().tolist()
    spin=measure_joint_motion(scene.animation,assembly)[1]
    assert spin['net_angle_deg'] == -360
    assert spin['sampled_angular_path_deg'] == 360


def test_suppressed_joints_and_constant_drivers_do_not_raise_motion_warnings():
    assembly,scene=fixture(spin=False)
    assembly['drivers'][1]['formula']='initialValue'
    assembly['joints'][0]['suppressed']=True
    result=measure_saved_motion(scene.animation,scene.mesh.vertices,assembly=assembly)
    assert len(result['joints']) == 1
    assert result['warnings'] == []


@pytest.mark.parametrize('failure', ['base_motion','only_swing','too_fast','wrong_axis','too_short','not_standing'])
def test_case_rejects_functional_counterexamples(failure):
    assembly,scene=fixture()
    if failure=='base_motion': scene.animation['frames'][10]['stand'][3]=10
    if failure=='only_swing': assembly['joints'][1]['suppressed']=True
    if failure=='too_fast': scene.animation['frames']=scene.animation['frames'][::20]
    if failure=='wrong_axis': assembly['joints'][1]['side1']['axis']=[0,0,1]
    if failure=='too_short': scene.animation['frames']=scene.animation['frames'][:10]
    if failure=='not_standing': scene.mesh.bbox.z=100
    assert not standing_fan_evidence({'assembly':assembly},scene)['passed']


def test_oscillation_acceptance_is_independent_of_initial_phase():
    assembly,scene=fixture()
    for i,frame in enumerate(scene.animation['frames']):
        head=rotate([0,0,1],45*math.cos(2*math.pi*i/40))
        frame['carrier']=head.ravel().tolist()
        frame['rotor']=(head@rotate([1,0,0],360*i/40)).ravel().tolist()
    assert standing_fan_evidence({'assembly':assembly},scene)['passed']


def test_initial_connector_axis_alignment_keeps_child_rotation_observable():
    assembly, scene = fixture()
    joint = assembly['joints'][1]
    joint['side1']['axis'] = [0, 0, 1]
    joint['side2']['axis'] = [1, 0, 0]
    for i, frame in enumerate(scene.animation['frames']):
        head = np.array(frame['carrier']).reshape(4, 4)
        frame['rotor'] = (head @ rotate([0, 0, 1], 360*i/40)
                          @ rotate([0, 1, 0], -90)).ravel().tolist()
    spin = measure_joint_motion(scene.animation, assembly)[1]
    assert spin['sampled_angular_path_deg'] == pytest.approx(360)
    assert spin['direction_reversals'] == 0
