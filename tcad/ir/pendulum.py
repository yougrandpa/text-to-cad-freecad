"""Stdlib-only gravity integration shared with the separate FreeCAD process."""
import math
from types import SimpleNamespace
from tcad.ir.motion import rotate


def _matrix(axis, angle, source, destination):
    cols = [rotate([float(i==j) for i in range(3)],axis,angle) for j in range(3)]
    mapped = rotate(source,axis,angle)
    shift = [destination[i]-mapped[i] for i in range(3)]
    return [cols[j][i] if j<3 else shift[i] for i in range(3) for j in range(4)] + [0,0,0,1]


def gravity_frames(spec, body_ids):
    raw = spec['rotation']
    rig = SimpleNamespace(**{'axis':[0,1,0], 'speed_deg_s':30, 'gravity_mm_s2':9810, **raw})
    rig.suspensions = [SimpleNamespace(**{'damping_per_s':0.5,'initial_angle_deg':0,
        'initial_velocity_deg_s':0,'com_distance_mm':None,'inertia_factor':None,**s}) for s in raw.get('suspensions',[])]
    if any(s.com_distance_mm is None or s.inertia_factor is None for s in rig.suspensions):
        raise ValueError('measure suspension COM and inertia before integration')
    u = [a/math.hypot(*rig.axis) for a in rig.axis]
    speed = math.radians(rig.speed_deg_s)
    initial = [(math.radians(s.initial_angle_deg),math.radians(s.initial_velocity_deg_s)) for s in rig.suspensions]
    states = initial[:]; frames = []; diagnostics = []; previous = 0.0
    count = int(math.floor((spec['end']-spec['start'])/spec['step']+1e-8))+1
    if not 2 <= count <= 600: raise ValueError('rotation requires 2..600 frames')
    def hinge(s,time):
        radial = [s.pivot[i]-rig.center[i] for i in range(3)]
        r = rotate(radial,u,speed*time)
        parallel = sum(r[i]*u[i] for i in range(3))
        accel = [-speed*speed*(r[i]-parallel*u[i]) for i in range(3)]
        return [rig.center[i]+r[i] for i in range(3)],accel
    def derivative(s,time,state):
        phi,velocity = state; _,acceleration = hinge(s,time)
        effective = [-acceleration[0],-acceleration[1],-rig.gravity_mm_s2-acceleration[2]]
        r = rotate([0,0,-1],u,phi)
        torque = [r[1]*effective[2]-r[2]*effective[1],r[2]*effective[0]-r[0]*effective[2],r[0]*effective[1]-r[1]*effective[0]]
        return velocity,sum(u[i]*torque[i] for i in range(3))/(s.com_distance_mm*s.inertia_factor)-s.damping_per_s*velocity
    for index in range(count):
        time = index*spec['step']
        for j,s in enumerate(rig.suspensions):
            dt_limit = min(0.005,0.05/math.sqrt(rig.gravity_mm_s2/(s.com_distance_mm*s.inertia_factor)),0.05/max(s.damping_per_s,1))
            steps = max(1,math.ceil((time-previous)/dt_limit)); dt = (time-previous)/steps
            phi,velocity = states[j]
            for substep in range(steps):
                t = previous+substep*dt; state = (phi,velocity)
                k1=derivative(s,t,state)
                k2=derivative(s,t+dt/2,[state[k]+dt*k1[k]/2 for k in range(2)])
                k3=derivative(s,t+dt/2,[state[k]+dt*k2[k]/2 for k in range(2)])
                k4=derivative(s,t+dt,[state[k]+dt*k3[k] for k in range(2)])
                phi,velocity=[state[k]+dt*(k1[k]+2*k2[k]+2*k3[k]+k4[k])/6 for k in range(2)]
            states[j]=(phi,velocity)
        poses={id:_matrix(u,0,[0,0,0],[0,0,0]) for id in body_ids}
        for id in rig.rotating_body_ids: poses[id]=_matrix(u,speed*time,rig.center,rig.center)
        angles={}
        for j,s in enumerate(rig.suspensions):
            pivot,_=hinge(s,time); phi=states[j][0]
            for id in s.body_ids:
                poses[id]=_matrix(u,phi,s.pivot,pivot); angles[id]=math.degrees(phi)
        frames.append(poses); diagnostics.append(angles); previous=time
    return {'frames':frames,'start':spec['start'],'step':spec['step'],
            'solver':'Planar gravity pendulum / fixed-step RK4',
            'scope':'Driven constant-speed rotor; rigid planar pendulums with declared COM/inertia and damping. No contact, bearing friction or structural loads.',
            'suspension_angles_deg':diagnostics,
            'max_swing_deg':max((abs(v) for f in diagnostics for v in f.values()),default=0)}
