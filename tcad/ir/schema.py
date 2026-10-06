"""IR (intermediate representation) — the single semantic source of truth.

FROZEN CONTRACT. Every module in this project imports these types; nobody
redefines them. Changing this file changes the wire format between supervisor
and worker, so treat it as a public API.

Design rule (see review/history/02-架构设计.md §2): the model NEVER touches a FreeCAD
document directly. It only mutates this IR; FreeCAD is a compiler backend.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

# ══════════════════════════════════════════════════════════════════════════
# Primitives
# ══════════════════════════════════════════════════════════════════════════


class Vec3(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: float
    y: float
    z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


OriginPlane = Literal["XY", "XZ", "YZ"]
# Verified mapping (src/App/Datums.cpp:265-275) — index into body.Origin.OriginFeatures:
ORIGIN_FEATURE_INDEX: dict[str, int] = {
    "X_Axis": 0, "Y_Axis": 1, "Z_Axis": 2,
    "XY": 3, "XZ": 4, "YZ": 5, "Origin": 6,
}


class PlaneRef(BaseModel):
    """Where a sketch is attached.

    kind="origin_plane" -> self.plane must be set (XY/XZ/YZ)
    kind="datum_plane"  -> self.feature_id points at a datum_plane feature
    kind="face"         -> self.feature_id + self.sub (e.g. "Face5") of any feature

    Verified usage (Mod/PartDesign/PartDesignTests/TestPad.py:46):
        sk.AttachmentSupport = (doc.XY_Plane, [""])
        sk.MapMode = "FlatFace"
    Verified sub-element form (SketcherTests/TestPlacementUpdate.py:50):
        sk.AttachmentSupport = (pad, ["Face5"])
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["origin_plane", "datum_plane", "face"]
    plane: OriginPlane | None = None
    feature_id: str | None = None
    sub: str = ""


class PlacementSpec(BaseModel):
    """Where an origin-placed feature sits, in WORLD coordinates (mm).

    The primitives (``additive_*`` / ``subtractive_*``) carry their own size but
    have no sketch to take a position from, so without this field they can only
    be built at the origin — which for a part spanning x∈[0,80] means a boss that
    cannot be put on the part at all.

    Measured (FreeCAD 26.3.0 / rev 48708, ``/tmp/probe_c31.py``): a PartDesign
    primitive's ``AttachmentOffset`` is inert while ``MapMode`` is Deactivated
    (setting it left the shape at x=[0,80] unchanged), while its ``Placement``
    moves the solid. So ``position``/``axis``/``angle`` land on ``Placement``.
    The rotation is about ``axis`` through ``position``, counter-clockwise,
    degrees.

    Only the ops in ``validate._PLACEMENT_OPS`` may carry one: for every other op
    the position comes from the sketch or the reference, and a placement would be
    a second, contradictory answer to the same question.
    """

    model_config = ConfigDict(frozen=True)
    position: Vec3
    axis: Vec3 | None = None      # rotation axis; required if angle != 0
    angle: float = 0.0            # degrees, about `axis` through `position`


# ══════════════════════════════════════════════════════════════════════════
# Sketch
# ══════════════════════════════════════════════════════════════════════════

GeomKind = Literal["line", "circle", "arc", "point", "ellipse", "bspline"]


class SketchGeom(BaseModel):
    """One piece of sketch geometry.

    line   -> points = [p0, p1]
    circle -> points = [center], radius required
    arc    -> points = [center], radius + theta1/theta2 required (radians)
    point  -> points = [p]
    ellipse -> points = [center], major_radius/minor_radius are semi-axes;
               rotation is degrees in the sketch's local (u, v) frame
    bspline -> points are WORLD interpolation points, not control poles;
               periodic closes the curve smoothly (do not repeat the first point)

    Verified constructors (Mod/Part/App/AppPart.cpp):
        Part.LineSegment(App.Vector(...), App.Vector(...))
        Part.Circle(center, axis, radius)
        Part.ArcOfCircle(Part.Circle(...), startAngle, endAngle)
    """

    id: str
    kind: GeomKind
    points: list[Vec3]
    radius: float | None = None
    theta1: float | None = None
    theta2: float | None = None
    construction: bool = False
    major_radius: float | None = None
    minor_radius: float | None = None
    rotation: float = 0.0
    periodic: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_curve_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("kind") in {"ellipse", "bspline"}:
            unknown = set(data) - set(cls.model_fields)
            if unknown:
                raise ValueError(f"unknown curve fields: {', '.join(sorted(unknown))}")
        return data

    @model_validator(mode="after")
    def validate_curve(self) -> "SketchGeom":
        if self.kind not in {"ellipse", "bspline"}:
            return self
        if any(not math.isfinite(v) for p in self.points for v in p.as_tuple()):
            raise ValueError("curve points must be finite")
        if not math.isfinite(self.rotation):
            raise ValueError("rotation must be finite")
        if any(v is not None for v in (self.radius, self.theta1, self.theta2)):
            raise ValueError("ellipse/bspline do not use radius or theta1/theta2")
        if self.kind == "ellipse":
            if len(self.points) != 1:
                raise ValueError("ellipse requires exactly one center point")
            if (self.major_radius is None or self.minor_radius is None
                    or not math.isfinite(self.major_radius)
                    or not math.isfinite(self.minor_radius)
                    or not self.major_radius >= self.minor_radius > 0):
                raise ValueError("ellipse requires finite major_radius >= minor_radius > 0")
            if self.periodic:
                raise ValueError("ellipse is already closed; periodic is for bspline")
        else:
            if self.major_radius is not None or self.minor_radius is not None or self.rotation:
                raise ValueError("bspline uses world interpolation points, not ellipse parameters")
            minimum = 3 if self.periodic else 2
            if not minimum <= len(self.points) <= 128:
                raise ValueError(f"bspline requires {minimum}..128 interpolation points")
            pairs = list(zip(self.points, self.points[1:]))
            if self.periodic:
                pairs.append((self.points[-1], self.points[0]))
            if any(math.dist(a.as_tuple(), b.as_tuple()) <= 1e-7 for a, b in pairs):
                raise ValueError("bspline consecutive points must differ; periodic curves must not repeat the first point")
        return self


class SketchConstraint(BaseModel):
    """A sketcher constraint.

    `type` must be one of the strings accepted by Sketcher.Constraint(...).
    Verified full list (Mod/Sketcher/App/Constraint.h:190-212):
        None, Coincident, Horizontal, Vertical, Parallel, Tangent, Distance,
        DistanceX, DistanceY, Angle, Perpendicular, Radius, Equal,
        PointOnObject, Symmetric, InternalAlignment, SnellsLaw, Block,
        Diameter, Weight, Group, Text
    Plus constructor-only forms: TangentViaPoint, PerpendicularViaPoint,
    AngleViaPoint, InternalAlignment:<Name>.

    `refs` are passed positionally into Sketcher.Constraint(type, *refs).
    `value` (if given) is applied via setDatum AFTER the constraint is added.

    Ordering rule learned the hard way (see docs 附录 B-2): bind the sketch to
    the origin FIRST, then dimension free endpoints. Dimensioning a point that
    is already constrained to the origin produces a solver conflict whose
    ValueError misleadingly says "Invalid constraint index".
    """

    type: str
    refs: list[int | float] = Field(default_factory=list)
    value: float | None = None
    name: str | None = None


class SketchSpec(BaseModel):
    id: str
    name: str  # stable, human readable, e.g. "base_plate_outline"
    plane: PlaneRef
    map_mode: str = "FlatFace"
    reversed: bool = False
    offset: Vec3 | None = None
    geometry: list[SketchGeom] = Field(default_factory=list)
    constraints: list[SketchConstraint] = Field(default_factory=list)
    require_fully_constrained: bool = True


# ══════════════════════════════════════════════════════════════════════════
# Features
# ══════════════════════════════════════════════════════════════════════════

# IR op -> FreeCAD type string. Verified in docs 附录 A-1.
FEATURE_TYPE_MAP: dict[str, str] = {
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "revolution": "PartDesign::Revolution",
    "groove": "PartDesign::Groove",
    "additive_loft": "PartDesign::AdditiveLoft",
    "subtractive_loft": "PartDesign::SubtractiveLoft",
    "fillet": "PartDesign::Fillet",
    "chamfer": "PartDesign::Chamfer",
    "draft": "PartDesign::Draft",
    "thickness": "PartDesign::Thickness",
    "hole": "PartDesign::Hole",
    "mirrored": "PartDesign::Mirrored",
    "linear_pattern": "PartDesign::LinearPattern",
    "circular_pattern": "PartDesign::CircularPattern",
    "polar_pattern": "PartDesign::PolarPattern",
    "multi_transform": "PartDesign::MultiTransform",
    "datum_plane": "PartDesign::Plane",
    "additive_box": "PartDesign::AdditiveBox",
    "additive_cylinder": "PartDesign::AdditiveCylinder",
    "additive_sphere": "PartDesign::AdditiveSphere",
    "additive_cone": "PartDesign::AdditiveCone",
    "subtractive_box": "PartDesign::SubtractiveBox",
    "subtractive_cylinder": "PartDesign::SubtractiveCylinder",
    "subtractive_sphere": "PartDesign::SubtractiveSphere",
    "subtractive_cone": "PartDesign::SubtractiveCone",
}

FeatureOp = Literal[
    "pad", "pocket", "revolution", "groove", "additive_loft", "subtractive_loft", "fillet", "chamfer", "draft",
    "thickness", "hole", "mirrored", "linear_pattern", "circular_pattern",
    "polar_pattern", "multi_transform", "datum_plane",
    "additive_box", "additive_cylinder", "additive_sphere", "additive_cone",
    "subtractive_box", "subtractive_cylinder", "subtractive_sphere", "subtractive_cone",
]


class FeatureSpec(BaseModel):
    id: str
    name: str  # stable, human readable, e.g. "mounting_hole_1"
    op: FeatureOp
    profile_sketch: str | None = None  # -> SketchSpec.id
    sections: list[str] = Field(default_factory=list)  # ordered additional loft SketchSpec IDs
    params: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)  # -> FeatureSpec.id, forms a DAG
    suppress: bool = False

    # ── sub-element references (fillet/chamfer) ──────────────────────────
    #
    # ``PartDesign::Fillet.Base`` / ``Chamfer.Base`` are an ``App::PropertyLinkSub``:
    # a (feature, [sub-element names]) pair. A scalar param cannot express that, so
    # it gets its own typed fields rather than a magic ``params["base"]`` — that key
    # was refused precisely because a string there dies inside FreeCAD.
    #
    # Additive and defaulted, so an older document parses unchanged.
    #: Which feature the names in ``sub_elements`` belong to (``FeatureSpec.id``).
    base_feature: str | None = None
    #: Sub-element names on that feature, e.g. ``["Edge1", "Edge5"]``. The names
    #: come from the geometry digest, which lists them with their length and kind
    #: so a model can pick by intent instead of guessing.
    sub_elements: list[str] = Field(default_factory=list)

    # ── plane references ────────────────────────────────────────────────
    #
    # ``PartDesign::Mirrored.MirrorPlane`` is another ``PropertyLinkSub`` — it
    # names a plane, not a scalar — so it gets the *same* typed shape a sketch
    # uses for its attachment (origin plane / datum plane / a face of a feature).
    # One mental model for "a plane reference in this IR", two places that need it.
    plane: PlaneRef | None = None

    # ── world placement (primitives and unattached datum planes) ────────
    #
    # Everything else takes its position from a sketch or a reference. The
    # primitives carry their own size and nothing else, so they need somewhere
    # to say *where* — otherwise they can only ever build at the origin.
    placement: PlacementSpec | None = None


class RotaryMotionSpec(BaseModel):
    """Prescribed rigid rotation for previews, not a dynamics/cutting solver."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    pivot: Vec3
    axis: Vec3
    ratio: float = 1.0  # body angle = input crank angle * ratio


class PartRef(BaseModel):
    """A verified component pinned to immutable build bytes."""
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    body_id: str
    placement: PlacementSpec | None = None


class BodySpec(BaseModel):
    id: str
    name: str
    sketches: list[SketchSpec] = Field(default_factory=list)
    features: list[FeatureSpec] = Field(default_factory=list)  # list order = build order
    motion: RotaryMotionSpec | None = None
    part_ref: PartRef | None = None
    suspension_pivot: Vec3 | None = None  # world hinge declared by a generated upright cabin

    @model_validator(mode="after")
    def reference_or_features(self):
        if self.part_ref and (self.sketches or self.features):
            raise ValueError("a referenced part cannot also declare sketches/features")
        return self


# ══════════════════════════════════════════════════════════════════════════
# Requirements (the machine-checkable half of the user's ask)
# ══════════════════════════════════════════════════════════════════════════

ConstraintKind = Literal[
    "bbox", "volume", "count", "hole_diameter", "hole_position",
    "symmetric", "wall_thickness", "feature_count",
]


class ConstraintExpr(BaseModel):
    """A requirement turned into something a program can decide.

    ``confirmed`` gates whether it may participate in the blocking gate —
    an unconfirmed expression must never fail a build (see design §4.6).
    ``source_text`` must be a verbatim span of the user's message so the
    provenance is auditable.
    """

    kind: ConstraintKind
    target: str | None = None
    value: float | dict[str, Any] | list[Any] | None = None
    tol: float = 0.05
    source_text: str = ""
    confirmed: bool = False


class RequirementSpec(BaseModel):
    raw_text: str = ""
    constraints: list[ConstraintExpr] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════
# The document
# ══════════════════════════════════════════════════════════════════════════


from tcad.ir.assembly import AssemblySpec


class IrDocument(BaseModel):
    schema_version: int = SCHEMA_VERSION
    model_id: str
    version: int = 0
    units: Literal["mm"] = "mm"
    bodies: list[BodySpec] = Field(default_factory=list)
    requirements: RequirementSpec = Field(default_factory=RequirementSpec)
    notes: list[str] = Field(default_factory=list)
    assembly: AssemblySpec | None = None

    # ── lookup helpers (used by patch validation, digest, compiler) ──

    def all_sketches(self) -> list[SketchSpec]:
        return [s for b in self.bodies for s in b.sketches]

    def all_features(self) -> list[FeatureSpec]:
        return [f for b in self.bodies for f in b.features]

    def find_sketch(self, sketch_id: str) -> SketchSpec | None:
        return next((s for s in self.all_sketches() if s.id == sketch_id), None)

    def find_feature(self, feature_id: str) -> FeatureSpec | None:
        return next((f for f in self.all_features() if f.id == feature_id), None)

    def find_body_of_feature(self, feature_id: str) -> BodySpec | None:
        return next(
            (b for b in self.bodies if any(f.id == feature_id for f in b.features)), None
        )


# ══════════════════════════════════════════════════════════════════════════
# Patches — the ONLY way the model may mutate an IrDocument
# ══════════════════════════════════════════════════════════════════════════

PatchOpName = Literal[
    "add_body", "update_body", "remove_body", "set_assembly",
    "add_sketch", "update_sketch", "add_feature", "update_feature",
    "remove_feature", "update_requirement", "rename",
]


class IrPatchOp(BaseModel):
    op: PatchOpName
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""  # model MUST justify; lands in the event log, feeds multi-turn reference resolution


class IrPatch(BaseModel):
    base_version: int  # optimistic concurrency: rejected if != current version
    ops: list[IrPatchOp] = Field(default_factory=list)
    summary: str = ""
