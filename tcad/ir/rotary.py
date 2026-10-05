"""A horizontal driven rotor with gravity pendulums; units mm, seconds, radians."""
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from tcad.ir.builders import rotate


class Suspension(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    body_ids: list[str] = Field(min_length=1, max_length=32)
    pivot: list[float] | None = Field(default=None, min_length=3, max_length=3)
    com_distance_mm: float | None = Field(default=None, gt=0, le=10000)
    inertia_factor: float | None = Field(default=None, ge=1, le=100)
    damping_per_s: float = Field(default=0.5, ge=0, le=100)
    initial_angle_deg: float = 0
    initial_velocity_deg_s: float = 0


class RotaryRig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    rotating_body_ids: list[str] = Field(min_length=1, max_length=100)
    center: list[float] = Field(min_length=3, max_length=3)
    axis: list[float] = Field(default_factory=lambda: [0,1,0], min_length=3, max_length=3)
    speed_deg_s: float = Field(default=30, ge=-360, le=360)
    suspensions: list[Suspension] = Field(default_factory=list, max_length=32)
    gravity_mm_s2: float = Field(default=9810, gt=0, le=100000)

    @model_validator(mode='after')
    def horizontal_unique(self):
        if math.hypot(*self.axis) <= 1e-12 or abs(self.axis[2]) > 1e-8:
            raise ValueError('gravity pendulums require a nonzero horizontal axis (Z is up)')
        ids = self.rotating_body_ids + [id for s in self.suspensions for id in s.body_ids]
        if len(ids) != len(set(ids)):
            raise ValueError('each moving body belongs to exactly one rotor/suspension')
        return self

    def validate_bodies(self, ids):
        used = set(self.rotating_body_ids) | {id for s in self.suspensions for id in s.body_ids}
        if used - set(ids):
            raise ValueError(f'unknown moving bodies: {sorted(used-set(ids))}')


from tcad.ir.pendulum import gravity_frames
