"""Measure uniform-density pendulum mass properties from the actual built BRep."""
import copy
import math
import FreeCAD as App


def measured_rotation(spec, results):
    spec=copy.deepcopy(spec); rig=spec['rotation']
    u=[v/math.hypot(*rig['axis']) for v in rig['axis']]
    shapes={body['id']:body['shape'] for body in results}
    for suspension in rig['suspensions']:
        selected=[]
        for id in suspension['body_ids']:
            solids=list(shapes[id].Solids)
            if len(solids)!=1:
                raise ValueError(f'suspension body {id} has {len(solids)} disconnected solids; remove misplaced duplicate features or connect them before animation')
            selected.extend(solids)
        mass=sum(s.Volume for s in selected)
        center=[sum(s.Volume*list(s.CenterOfMass)[i] for s in selected)/mass for i in range(3)]
        if suspension.get('pivot') is None:
            suspension['pivot']=[center[0],center[1],max(s.BoundBox.ZMax for s in selected)]
        radius=[center[i]-suspension['pivot'][i] for i in range(3)]
        length=-radius[2]
        lateral=[radius[i]-sum(radius[k]*u[k] for k in range(3))*u[i] for i in range(3)]
        if length <= 1e-6 or math.hypot(*lateral[:2]) > 0.05:
            raise ValueError('suspension COM must lie vertically below its hinge in the zero pose; adjust pivot or geometry')
        moment=0
        for shape in selected:
            projected=shape.MatrixOfInertia.multVec(App.Vector(*u))
            moment+=sum(u[i]*list(projected)[i] for i in range(3))
            d=[list(shape.CenterOfMass)[i]-suspension['pivot'][i] for i in range(3)]
            moment+=shape.Volume*(sum(v*v for v in d)-sum(u[i]*d[i] for i in range(3))**2)
        if suspension.get('com_distance_mm') is not None and abs(suspension['com_distance_mm']-length)>1e-4:
            raise ValueError('declared com_distance_mm disagrees with measured uniform-density COM')
        suspension['com_distance_mm']=length
        if suspension.get('inertia_factor') is None: suspension['inertia_factor']=max(1,moment/(mass*length*length))
    return spec
