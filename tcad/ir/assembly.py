"""Declarative contracts for the built-in FreeCAD Assembly workbench."""
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tcad.ir.rotary import RotaryRig

JOINT_TYPES = ('Fixed', 'Revolute', 'Cylindrical', 'Slider', 'Ball', 'Distance',
               'Parallel', 'Perpendicular', 'Angle', 'RackPinion', 'Screw', 'Gears', 'Belt')


class Connector(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    body_id: str
    element: str = ""  # optional FaceN / EdgeN / VertexN on the final body
    vertex: str = ""
    position: list[float] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    axis: list[float] = Field(default_factory=lambda: [0, 0, 1], min_length=3, max_length=3)
    angle_deg: float = 0  # roll about connector Z, after aligning Z with axis

    @model_validator(mode='after')
    def axis_nonzero(self):
        if any(value and not re.fullmatch(r'(Face|Edge|Vertex)[1-9][0-9]*', value) for value in (self.element, self.vertex)):
            raise ValueError('connector element/vertex must be a valid topology name')
        if math.hypot(*self.axis) <= 1e-12:
            raise ValueError('connector axis must be nonzero')
        return self


class AssemblyJoint(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: str
    type: Literal['Fixed', 'Revolute', 'Cylindrical', 'Slider', 'Ball', 'Distance',
                  'Parallel', 'Perpendicular', 'Angle', 'RackPinion', 'Screw', 'Gears', 'Belt']
    side1: Connector
    side2: Connector
    distance: float = 0  # native Distance: length / gear radius1 / pinion pitch radius / screw pitch
    distance2: float = 0  # gear/belt radius2
    angle: float = 0
    length_min: float | None = None
    length_max: float | None = None
    angle_min: float | None = None
    angle_max: float | None = None
    suppressed: bool = False

    @model_validator(mode='after')
    def valid_joint(self):
        if self.side1.body_id == self.side2.body_id:
            raise ValueError('joint requires two distinct bodies')
        for lo, hi in ((self.length_min, self.length_max), (self.angle_min, self.angle_max)):
            if lo is not None and hi is not None and lo > hi:
                raise ValueError('joint minimum must not exceed maximum')
        if self.type in ('Gears', 'Belt') and (self.distance <= 0 or self.distance2 <= 0):
            raise ValueError('gear/belt pitch radii must be positive')
        if self.type in ('RackPinion', 'Screw') and self.distance <= 0:
            raise ValueError('pinion radius / screw pitch must be positive')
        return self


class AssemblyDriver(BaseModel):
    model_config = ConfigDict(extra='forbid')
    joint_id: str
    type: Literal['Angular', 'Linear']
    formula: str = Field(min_length=1, max_length=512)

    @model_validator(mode='after')
    def formula_is_native(self):
        if not re.fullmatch(r'[A-Za-z0-9+*/^()., \-]+', self.formula):
            raise ValueError('use the native math formula syntax, not Python code')
        return self


class AssemblySpec(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    grounded: list[str] = Field(min_length=1, max_length=100)
    joints: list[AssemblyJoint] = Field(default_factory=list, max_length=100)
    drivers: list[AssemblyDriver] = Field(default_factory=list, max_length=100)
    rotation: RotaryRig | None = None
    start: float = 0
    end: float = 1
    step: float = Field(default=0.025, ge=1e-6)

    @model_validator(mode='after')
    def valid_simulation(self):
        if self.end <= self.start or (self.end-self.start)/self.step > 599+1e-8:
            raise ValueError('simulation requires increasing time and at most 600 frames')
        if self.rotation is not None and (self.joints or self.drivers):
            raise ValueError('rotation rig cannot be mixed with native joints/drivers')
        ids = [j.id for j in self.joints]
        if len(set(ids)) != len(ids) or len(set(self.grounded)) != len(self.grounded):
            raise ValueError('joint IDs and grounded body IDs must be unique')
        joints = {j.id: j for j in self.joints}
        seen = set()
        for driver in self.drivers:
            joint = joints.get(driver.joint_id)
            allowed = ('Revolute', 'Cylindrical') if driver.type == 'Angular' else ('Slider', 'Cylindrical')
            if not joint or joint.suppressed or joint.type not in allowed:
                raise ValueError('Angular drives Revolute/Cylindrical; Linear drives Slider/Cylindrical')
            key = (driver.joint_id, driver.type)
            if key in seen:
                raise ValueError('duplicate driver for joint degree of freedom')
            seen.add(key)
        return self

    def validate_bodies(self, body_ids):
        ids = set(body_ids)
        if self.rotation is not None:
            self.rotation.validate_bodies(ids)
            moving = set(self.rotation.rotating_body_ids) | {id for s in self.rotation.suspensions for id in s.body_ids}
            if set(self.grounded) != ids - moving:
                raise ValueError('rotation rig grounded IDs must be exactly the stationary bodies')
            return
        referenced = set(self.grounded) | {side.body_id for j in self.joints for side in (j.side1, j.side2)}
        if referenced - ids:
            raise ValueError(f'assembly references unknown bodies: {sorted(referenced-ids)}')
        connected = set(self.grounded)
        while True:
            old = len(connected)
            for joint in self.joints:
                if joint.suppressed:
                    continue
                a,b = joint.side1.body_id, joint.side2.body_id
                if a in connected or b in connected:
                    connected.update((a,b))
            if len(connected) == old:
                break
        if ids - connected:
            raise ValueError(f'ungrounded disconnected bodies: {sorted(ids-connected)}')
