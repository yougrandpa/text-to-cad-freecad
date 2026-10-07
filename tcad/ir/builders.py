"""Compact part declarations expanded into ordinary, auditable IR patches."""
import math
from tcad.ir.motion import rotate
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Default for the non-geometric ``reason`` field of compact recipe calls.
#: The marker ("auto:") keeps the audit log honest about which reasons the
#: model actually wrote and which a default filled in. Dimensions, axes and
#: confirmed constraints are never defaulted — only provenance text is.
AUTO_REASON = "auto: compact recipe (model supplied no reason)"


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


class AttachSpec(BaseModel):
    """Place this part against an EARLIER part in the same call.

    ``where`` names an anchor point derived from the target's *declared*
    geometry (its center/size/start/end) — deterministic arithmetic, no BRep
    measurement and no model-side coordinate math. ``offset`` nudges the
    result in world millimetres.

    Anchors (a shape refuses the ones it does not define):
      box: center, top, bottom, left, right, front, back
      cylinder/tube/beam/loft/rotor: center, start, end, top(=end), bottom(=start)
    """

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    to: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    where: Literal['center', 'top', 'bottom', 'left', 'right', 'front', 'back',
                   'start', 'end'] = 'center'
    offset: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0],
                                min_length=3, max_length=3)


_ALIGN_VECTORS = {'X': [1, 0, 0], '-X': [-1, 0, 0],
                  'Y': [0, 1, 0], '-Y': [0, -1, 0],
                  'Z': [0, 0, 1], '-Z': [0, 0, -1]}
AnchorName = Literal['center', 'top', 'bottom', 'left', 'right', 'front', 'back',
                     'start', 'end']


class PartRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    body_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,40}$')
    shape: Literal['box', 'cylinder', 'tube', 'beam', 'loft', 'rotor']
    operation: Literal['add', 'cut'] = 'add'
    extend_existing: bool = Field(default=False,
        description='Explicitly append new connected material to an existing local body. Existing features are preserved; ir_commit verifies one solid. Cuts already edit existing material without this flag.')
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
    # ── relation expressions the builder resolves for the model ──────────
    # Positions/axes stop being hand-computed coordinates: `attach` puts this
    # part against an earlier part's anchor, `align`+`length` gives a
    # cylinder/beam its direction without a second endpoint.
    attach: AttachSpec | None = None
    anchor: AnchorName | None = None
    align: Literal['X', '-X', 'Y', '-Y', 'Z', '-Z'] | None = None
    length: float | None = Field(default=None, gt=0)

    @model_validator(mode='before')
    @classmethod
    def attach_origin_default(cls, data):
        """A part placed by ``attach`` supplies its own local origin.

        The anchor arithmetic is translation-invariant: the final position
        depends only on the anchor pair, so the model does not need to invent
        a nominal coordinate for the fields the anchors are computed from.
        """
        if isinstance(data, dict) and data.get('attach') is not None:
            shape = data.get('shape')
            defaults = {}
            if shape == 'box' and data.get('center') is None:
                defaults['center'] = [0.0, 0.0, 0.0]
            if shape in {'cylinder', 'tube', 'beam'} and data.get('start') is None:
                defaults['start'] = [0.0, 0.0, 0.0]
            if defaults:
                return {**data, **defaults}
        return data

    @model_validator(mode='after')
    def dimensions(self):
        used = {'box': {'center', 'size'}, 'cylinder': {'start', 'end', 'radius'},
                'tube': {'start', 'end', 'radius', 'inner_radius'},
                'beam': {'start', 'end', 'width', 'depth'},
                'loft': {'sections', 'section_axis'},
                'rotor': {'center', 'axis', 'radius', 'blade_count', 'blade_width', 'thickness', 'hub_radius'}}[self.shape]
        if self.align is not None:
            if self.shape not in {'cylinder', 'tube', 'beam'}:
                raise ValueError('align applies to cylinder/tube/beam; box/loft/rotor carry their own orientation')
            if self.length is None:
                raise ValueError('align needs length: a direction alone does not define the solid')
            if self.end is not None:
                raise ValueError('use either end or align+length, not both')
            used = (used - {'end'}) | {'align', 'length'}
        elif self.length is not None:
            raise ValueError('length only applies together with align')
        fields = {'center','size','start','end','radius','inner_radius','width','depth',
                  'sections','section_axis','axis','blade_count','blade_width','thickness','hub_radius',
                  'align','length'}
        for field in fields:
            if (getattr(self, field) is not None) != (field in used):
                raise ValueError(f'{self.shape} requires exactly {sorted(used)}; invalid/missing {field}')
        if self.size and min(self.size) <= 0:
            raise ValueError('box size must be positive')
        if self.start and self.end and math.dist(self.start, self.end) <= 1e-8:
            raise ValueError('start and end must differ')
        if self.anchor is not None and self.attach is None:
            raise ValueError('anchor only applies together with attach')
        if self.attach is not None and self.copies is not None:
            raise ValueError('attach cannot combine with copies; attach places one part, copies repeat it')
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
    reason: str = Field(default=AUTO_REASON)
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


# ─── relation resolution: deterministic anchor arithmetic ─────────────────
#
# `attach`/`align` remove coordinate arithmetic from the model's job. The model
# says "put the lid on the base"; the builder computes the translation from the
# parts' DECLARED geometry — the same numbers that generate the IR — so nothing
# here measures or guesses, and the result is an ordinary IR patch.


def _axis_unit(part):
    """(unit axis, end point) for an axis-shaped part; align-aware."""
    if part.shape in {'cylinder', 'tube', 'beam'}:
        end = part.end
        if end is None:
            direction = _ALIGN_VECTORS[part.align]
            end = [part.start[i] + direction[i]*part.length for i in range(3)]
        delta = [end[i]-part.start[i] for i in range(3)]
        norm = math.hypot(*delta)
        return [d/norm for d in delta], end
    if part.shape == 'rotor':
        norm = math.hypot(*part.axis)
        return [a/norm for a in part.axis], None
    if part.shape == 'loft':
        index = 'XYZ'.index(part.section_axis)
        delta = part.sections[-1].center[index] - part.sections[0].center[index]
        direction = [0.0, 0.0, 0.0]
        direction[index] = 1.0 if delta >= 0 else -1.0
        return direction, None
    return [0.0, 0.0, 1.0], None


def _anchor_point(part, name):
    if part.shape == 'box':
        c, s = part.center, part.size
        points = {
            'center': c,
            'top': [c[0], c[1], c[2]+s[2]/2], 'bottom': [c[0], c[1], c[2]-s[2]/2],
            'right': [c[0]+s[0]/2, c[1], c[2]], 'left': [c[0]-s[0]/2, c[1], c[2]],
            'back': [c[0], c[1]+s[1]/2, c[2]], 'front': [c[0], c[1]-s[1]/2, c[2]],
        }
    elif part.shape in {'cylinder', 'tube', 'beam'}:
        _, end = _axis_unit(part)
        mid = [part.start[i]+(end[i]-part.start[i])/2 for i in range(3)]
        points = {'center': mid, 'start': list(part.start), 'end': list(end),
                  'bottom': list(part.start), 'top': list(end)}
    elif part.shape == 'loft':
        first, last = part.sections[0].center, part.sections[-1].center
        mid = [(first[i]+last[i])/2 for i in range(3)]
        points = {'center': mid, 'start': list(first), 'end': list(last),
                  'bottom': list(first), 'top': list(last)}
    else:
        c = part.center
        u, _ = _axis_unit(part)
        top = [c[i]+u[i]*part.thickness/2 for i in range(3)]
        bottom = [c[i]-u[i]*part.thickness/2 for i in range(3)]
        points = {'center': list(c), 'top': top, 'end': top, 'bottom': bottom, 'start': bottom}
    if name not in points:
        raise ValueError(f'{part.shape} {part.id!r} has no {name!r} anchor; use {sorted(points)}')
    return points[name]


def _default_anchor(part):
    return 'start' if part.shape in {'cylinder', 'tube', 'beam'} else 'center'


def _shift(value, delta):
    return [value[i]+delta[i] for i in range(3)]


def _translated(part, delta):
    if part.shape in {'box', 'rotor'}:
        return part.model_copy(update={'center': _shift(part.center, delta)})
    if part.shape in {'cylinder', 'tube', 'beam'}:
        update = {'start': _shift(part.start, delta)}
        if part.end is not None:
            update['end'] = _shift(part.end, delta)
        return part.model_copy(update=update)
    sections = [section.model_copy(update={'center': _shift(section.center, delta)})
                for section in part.sections]
    return part.model_copy(update={'sections': sections})


def resolve_attachments(parts):
    """Normalize ``align`` into an endpoint, then place every ``attach`` part.

    Targets must be declared EARLIER in the same call: a deterministic builder
    can only anchor to geometry whose numbers it already knows, and "the part
    I mention below" is not knowable. Attaching to a repeated part is refused
    for the same reason — there is no single anchor to mean.

    Idempotent: a resolved part carries ``attach=None`` and a concrete
    ``end``, so calling this again (the handler resolves once, then each
    per-part ``parts_patch`` pass repeats it) changes nothing.
    """
    resolved, placed = [], {}
    for part in parts:
        if part.attach is not None:
            target = placed.get(part.attach.to)
            if target is None:
                raise ValueError(
                    f'attach target {part.attach.to!r} must be an EARLIER part in the same call; '
                    f'declared so far: {sorted(placed) or "none"}')
            if target.copies is not None:
                raise ValueError(f'attach target {part.attach.to!r} is a repeated part; attach to a single part')
            where = _anchor_point(target, part.attach.where)
            own = _anchor_point(part, part.anchor or _default_anchor(part))
            delta = [where[i]-own[i]+part.attach.offset[i] for i in range(3)]
            # The relation is spent: the resolved part carries ordinary
            # coordinates, so a second pass over it resolves nothing further.
            part = _translated(part, delta).model_copy(update={'attach': None, 'anchor': None})
        if part.align is not None:
            direction = _ALIGN_VECTORS[part.align]
            end = [part.start[i]+direction[i]*part.length for i in range(3)]
            part = part.model_copy(update={'end': end, 'align': None, 'length': None})
        placed[part.id] = part
        resolved.append(part)
    return resolved


def parts_patch(request: BuildParts, existing):
    bodies = set(existing); ops = []; created = []
    def feature(body, id, op, params, placement):
        ops.append({'op':'add_feature', 'payload':{'body_id':body,'id':id,'name':id,
            'op':op,'params':params,'placement':placement,'recipe_id':part.id}, 'reason':request.reason})
    for part in resolve_attachments(request.parts):
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
                    geometry = {'id':'profile','points':[dict(zip('xyz', section.center))]}
                    if u == v:
                        geometry.update(kind='circle', radius=u)
                    else:
                        geometry.update(kind='ellipse', major_radius=max(u,v),
                                        minor_radius=min(u,v), rotation=0 if u >= v else 90)
                    ops.append({'op':'add_sketch','payload':{'id':sketch,'name':sketch,'body_id':body,
                        'plane':{'kind':'datum_plane','feature_id':plane},'geometry':[geometry],
                        'constraints':[{'type':'Block','refs':[0]}]},'reason':request.reason})
                    sketches.append(sketch)
                ops.append({'op':'add_feature','payload':{'id':part.id,'name':part.id,'body_id':body,
                    'recipe_id':part.id,
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
