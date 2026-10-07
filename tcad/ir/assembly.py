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
    position: list[float] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3,
                                  description='World position in mm.')
    axis: list[float] = Field(default_factory=lambda: [0, 0, 1], min_length=3, max_length=3,
                              description='World direction; use the same connector on both joint sides to keep the assembled pose.')
    angle_deg: float = Field(default=0, description='Roll about the connector axis, in degrees (after Z is aligned with axis).')

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
    distance: float = Field(default=0, description='Native Distance: length in mm / gear radius1 / pinion pitch radius / screw pitch.')
    distance2: float = Field(default=0, description='Gear/belt radius2 in mm; positive for Gears/Belt.')
    angle: float = Field(default=0, description='Joint angle used by the Angle joint, in degrees.')
    length_min: float | None = Field(default=None, description='Lower length limit in mm (nullable).')
    length_max: float | None = Field(default=None, description='Upper length limit in mm (nullable).')
    angle_min: float | None = Field(default=None, description='Lower angle limit in degrees (nullable).')
    angle_max: float | None = Field(default=None, description='Upper angle limit in degrees (nullable).')
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
    formula: str = Field(min_length=1, max_length=512,
                         description='Native math expression of time in seconds; radians for Angular, mm for Linear (e.g. "2*pi*time").')

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
    start: float = Field(default=0, description='Saved-frame start time in seconds.')
    end: float = Field(default=1, description='Saved-frame end time in seconds; (end-start)/step must be at most 600 frames.')
    step: float = Field(default=0.025, ge=1e-6, description='Frame step in seconds.')

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
