"""Compact part declarations expanded into ordinary, auditable IR patches."""
import math
from tcad.ir.motion import rotate
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolarCopies(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    count: int = Field(ge=2, le=32)
    center: list[float] = Field(min_length=3, max_length=3)
    axis: list[float] = Field(default_factory=lambda: [0, 1, 0], min_length=3, max_length=3)
    step_deg: float | None = None
    rotate: bool = True
    separate_bodies: bool = True
    anchor: list[float] | None = Field(default=None, min_length=3, max_length=3)


class LoftSection(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    center: list[float] = Field(min_length=3, max_length=3)
    radii: list[float] = Field(min_length=2, max_length=2)

    @model_validator(mode='after')
    def positive_radii(self):
        if min(self.radii) <= 0:
            raise ValueError('section radii must be positive semi-axes')
        return self


class PartRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    body_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    shape: Literal['box', 'cylinder', 'tube', 'beam', 'loft', 'rotor']
    operation: Literal['add', 'cut'] = 'add'
    center: list[float] | None = Field(default=None, min_length=3, max_length=3)
    size: list[float] | None = Field(default=None, min_length=3, max_length=3)
    start: list[float] | None = Field(default=None, min_length=3, max_length=3)
    end: list[float] | None = Field(default=None, min_length=3, max_length=3)
    radius: float | None = Field(default=None, gt=0)
    inner_radius: float | None = Field(default=None, gt=0)
    width: float | None = Field(default=None, gt=0)
    depth: float | None = Field(default=None, gt=0)
    sections: list[LoftSection] | None = Field(default=None, min_length=2, max_length=12)
    section_axis: Literal['X', 'Y', 'Z'] | None = None
    ruled: bool = False
    axis: list[float] | None = Field(default=None, min_length=3, max_length=3)
    blade_count: int | None = Field(default=None, ge=2, le=8)
    blade_width: float | None = Field(default=None, gt=0)
    thickness: float | None = Field(default=None, gt=0)
    hub_radius: float | None = Field(default=None, gt=0)
    copies: PolarCopies | None = None

    @model_validator(mode='after')
    def dimensions(self):
        used = {'box': {'center', 'size'}, 'cylinder': {'start', 'end', 'radius'},
                'tube': {'start', 'end', 'radius', 'inner_radius'},
                'beam': {'start', 'end', 'width', 'depth'},
                'loft': {'sections', 'section_axis'},
                'rotor': {'center', 'axis', 'radius', 'blade_count', 'blade_width', 'thickness', 'hub_radius'}}[self.shape]
        fields = {'center','size','start','end','radius','inner_radius','width','depth',
                  'sections','section_axis','axis','blade_count','blade_width','thickness','hub_radius'}
        for field in fields:
            if (getattr(self, field) is not None) != (field in used):
                raise ValueError(f'{self.shape} requires exactly {sorted(used)}; invalid/missing {field}')
        if self.size and min(self.size) <= 0:
            raise ValueError('box size must be positive')
        if self.start and math.dist(self.start, self.end) <= 1e-8:
            raise ValueError('start and end must differ')
        if self.shape == 'tube' and (self.inner_radius >= self.radius or self.operation == 'cut'):
            raise ValueError('tube requires inner_radius < radius and operation=add')
        if self.copies and math.hypot(*self.copies.axis) <= 1e-12:
            raise ValueError('copy axis must be nonzero')
        if self.shape in {'loft', 'rotor'} and self.copies:
            raise ValueError('loft/rotor use one recipe per body, not polar copies')
        if self.ruled and self.shape != 'loft':
            raise ValueError('ruled applies only to loft')
        if self.shape == 'loft':
            axis = 'XYZ'.index(self.section_axis)
            positions = [section.center[axis] for section in self.sections]
            steps = [b-a for a,b in zip(positions, positions[1:])]
            if not (all(step > 1e-6 for step in steps) or all(step < -1e-6 for step in steps)):
                raise ValueError('loft section centers must be strictly ordered along section_axis')
        if self.shape == 'rotor':
            if self.operation != 'add' or math.hypot(*self.axis) <= 1e-12:
                raise ValueError('rotor requires operation=add and a nonzero axis')
            if self.hub_radius >= self.radius:
                raise ValueError('rotor hub_radius must be smaller than blade tip radius')
        return self


class BuildParts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parts: list[PartRecipe] = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1)
    base_version: int | Literal['current'] = 'current'


def _placement(position, direction):
    norm = math.hypot(*direction); u = [v/norm for v in direction]
    axis = [-u[1], u[0], 0]
    angle = math.degrees(math.acos(max(-1, min(1, u[2]))))
    if math.hypot(*axis) < 1e-12:
        axis = [1, 0, 0]
    return {'position': dict(zip('xyz', position)), 'axis': dict(zip('xyz', axis)), 'angle': angle}


def _composed_rotation(placement: dict, axis: list[float], angle: float) -> dict:
    """Apply a world rotation after the primitive's original orientation."""
    def quaternion(direction, theta):
        norm = math.hypot(*direction)
        scale = math.sin(theta/2)/norm
        return math.cos(theta/2), [v*scale for v in direction]

    a, u = quaternion(axis, angle)
    b, v = quaternion(list(placement['axis'].values()), math.radians(placement['angle']))
    cross = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]]
    scalar = a*b-sum(x*y for x, y in zip(u, v))
    vector = [a*v[i]+b*u[i]+cross[i] for i in range(3)]
    norm = math.hypot(*vector)
    direction = [x/norm for x in vector] if norm > 1e-12 else [1, 0, 0]
    theta = math.degrees(2*math.atan2(norm, scalar)) if norm > 1e-12 else 0
    return {'axis': dict(zip('xyz', direction)), 'angle': theta}


def parts_patch(request: BuildParts, existing):
    bodies = set(existing); ops = []; created = []
    def feature(body, id, op, params, placement):
        ops.append({'op':'add_feature', 'payload':{'body_id':body,'id':id,'name':id,
            'op':op,'params':params,'placement':placement}, 'reason':request.reason})
    for part in request.parts:
        if part.shape in {'loft', 'rotor'}:
            body = part.body_id
            if body not in bodies:
                if part.operation == 'cut':
                    raise ValueError(f'cut requires an existing body: {body}')
                bodies.add(body); created.append(body)
                ops.append({'op':'add_body','payload':{'id':body,'name':body},'reason':request.reason})
            if part.shape == 'loft':
                # Plane local U/V are world Y/Z, X/Z, X/Y for X, Y, Z.
                orientation = {'X': ([1, 1, 1], 120), 'Y': ([1, 0, 0], 90),
                               'Z': ([0, 0, 1], 0)}[part.section_axis]
                sketches = []
                for index, section in enumerate(part.sections):
                    plane, sketch = f'{part.id}_plane_{index}', f'{part.id}_section_{index}'
                    placement = {'position': dict(zip('xyz', section.center)),
                                 'axis': dict(zip('xyz', orientation[0])), 'angle': orientation[1]}
                    feature(body, plane, 'datum_plane', {}, placement)
                    u, v = section.radii
                    geometry = {'id':'profile','kind':'ellipse','points':[dict(zip('xyz', section.center))],
                                'major_radius':max(u,v),'minor_radius':min(u,v),'rotation':0 if u >= v else 90}
                    ops.append({'op':'add_sketch','payload':{'id':sketch,'name':sketch,'body_id':body,
                        'plane':{'kind':'datum_plane','feature_id':plane},'geometry':[geometry],
                        'constraints':[{'type':'Block','refs':[0]}]},'reason':request.reason})
                    sketches.append(sketch)
                ops.append({'op':'add_feature','payload':{'id':part.id,'name':part.id,'body_id':body,
                    'op':'additive_loft' if part.operation == 'add' else 'subtractive_loft',
                    'profile_sketch':sketches[0],'sections':sketches[1:],
                    'params':{'ruled':part.ruled,'closed':False}},'reason':request.reason})
            else:
                length = math.hypot(*part.axis)
                normal = [v/length for v in part.axis]
                bottom = [part.center[i]-normal[i]*part.thickness/2 for i in range(3)]
                feature(body, part.id+'_hub', 'additive_cylinder',
                        {'radius':part.hub_radius,'height':part.thickness}, _placement(bottom, normal))
                base = _placement(part.center, normal)
                base_axis, base_angle = list(base['axis'].values()), math.radians(base['angle'])
                corner = rotate([0,-part.blade_width/2,-part.thickness/2], base_axis, base_angle)
                for index in range(part.blade_count):
                    theta = index*2*math.pi/part.blade_count
                    offset = rotate(corner, normal, theta)
                    placement = {'position':dict(zip('xyz',[part.center[i]+offset[i] for i in range(3)])),
                                 **_composed_rotation(base, normal, theta)}
                    feature(body, f'{part.id}_blade_{index}', 'additive_box',
                            {'length':part.radius,'width':part.blade_width,'height':part.thickness}, placement)
            continue
        copies = part.copies
        for index in range(copies.count if copies else 1):
            body = f'{part.body_id}_{index}' if copies and copies.separate_bodies else part.body_id
            id = f'{part.id}_{index}' if copies else part.id
            if id == body:
                id += '_shape'
            if body not in bodies:
                if part.operation == 'cut':
                    raise ValueError(f'cut requires an existing body: {body}')
                bodies.add(body); created.append(body)
                ops.append({'op':'add_body','payload':{'id':body,'name':body},'reason':request.reason})
            theta = math.radians(index*(copies.step_deg if copies and copies.step_deg is not None else 360/copies.count)) if copies else 0
            def point(value):
                if not copies: return value
                source = copies.anchor if not copies.rotate and copies.anchor is not None else value
                delta = [source[i]-copies.center[i] for i in range(3)]
                turn = rotate(delta, copies.axis, theta)
                return [copies.center[i]+turn[i]+value[i]-source[i] for i in range(3)]
            sign = 'additive_' if part.operation == 'add' else 'subtractive_'
            if part.shape == 'box':
                center = point(part.center)
                position = [center[i]-part.size[i]/2 for i in range(3)]
                placement = _placement(position, [0,0,1])
                if copies and copies.rotate:
                    base = [part.center[i]-part.size[i]/2 for i in range(3)]
                    placement = {'position':dict(zip('xyz',point(base))), 'axis':dict(zip('xyz',copies.axis)), 'angle':math.degrees(theta)}
                feature(body,id,sign+'box',dict(zip(('length','width','height'),part.size)),placement)
            else:
                start = point(part.start)
                delta = [part.end[i]-part.start[i] for i in range(3)]
                length = math.hypot(*delta); placement = _placement(start,delta)
                if copies and copies.rotate:
                    placement.update(_composed_rotation(placement, copies.axis, theta))
                if part.shape == 'beam':
                    # Center the cross section on the endpoint-to-endpoint line.
                    axis = list(placement['axis'].values()); angle = math.radians(placement['angle'])
                    shift = rotate([-part.width/2,-part.depth/2,0],axis,angle)
                    placement['position'] = dict(zip('xyz',[start[i]+shift[i] for i in range(3)]))
                    feature(body,id,sign+'box',{'length':part.width,'width':part.depth,'height':length},placement)
                else:
                    feature(body,id,sign+'cylinder',{'radius':part.radius,'height':length},placement)
                    if part.shape == 'tube':
                        # Do not cut neighboring material beyond the tube endpoints.
                        feature(body,id+'_bore','subtractive_cylinder',{'radius':part.inner_radius,'height':length},placement)
    return ops, created
