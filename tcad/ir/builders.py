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


class PartRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    body_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    shape: Literal['box', 'cylinder', 'tube', 'beam']
    operation: Literal['add', 'cut'] = 'add'
    center: list[float] | None = Field(default=None, min_length=3, max_length=3)
    size: list[float] | None = Field(default=None, min_length=3, max_length=3)
    start: list[float] | None = Field(default=None, min_length=3, max_length=3)
    end: list[float] | None = Field(default=None, min_length=3, max_length=3)
    radius: float | None = Field(default=None, gt=0)
    inner_radius: float | None = Field(default=None, gt=0)
    width: float | None = Field(default=None, gt=0)
    depth: float | None = Field(default=None, gt=0)
    copies: PolarCopies | None = None

    @model_validator(mode='after')
    def dimensions(self):
        used = {'box': {'center', 'size'}, 'cylinder': {'start', 'end', 'radius'},
                'tube': {'start', 'end', 'radius', 'inner_radius'},
                'beam': {'start', 'end', 'width', 'depth'}}[self.shape]
        fields = {'center','size','start','end','radius','inner_radius','width','depth'}
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
