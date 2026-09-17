"""IR (intermediate representation) — the single semantic source of truth.

FROZEN CONTRACT. Every module in this project imports these types; nobody
redefines them. Changing this file changes the wire format between supervisor
and worker, so treat it as a public API.

Design rule (see docs/02-架构设计.md §2): the model NEVER touches a FreeCAD
document directly. It only mutates this IR; FreeCAD is a compiler backend.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


# ══════════════════════════════════════════════════════════════════════════
# Sketch
# ══════════════════════════════════════════════════════════════════════════

GeomKind = Literal["line", "circle", "arc", "point"]


class SketchGeom(BaseModel):
    """One piece of sketch geometry.

    line   -> points = [p0, p1]
    circle -> points = [center], radius required
    arc    -> points = [center], radius + theta1/theta2 required (radians)
    point  -> points = [p]

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
    "subtractive_box": "PartDesign::SubtractiveBox",
    "subtractive_cylinder": "PartDesign::SubtractiveCylinder",
    "subtractive_sphere": "PartDesign::SubtractiveSphere",
}

FeatureOp = Literal[
    "pad", "pocket", "revolution", "groove", "fillet", "chamfer", "draft",
    "thickness", "hole", "mirrored", "linear_pattern", "circular_pattern",
    "polar_pattern", "multi_transform", "datum_plane",
    "additive_box", "additive_cylinder", "additive_sphere",
    "subtractive_box", "subtractive_cylinder", "subtractive_sphere",
]


class FeatureSpec(BaseModel):
    id: str
    name: str  # stable, human readable, e.g. "mounting_hole_1"
    op: FeatureOp
    profile_sketch: str | None = None  # -> SketchSpec.id
    params: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)  # -> FeatureSpec.id, forms a DAG
    suppress: bool = False


class BodySpec(BaseModel):
    id: str
    name: str
    sketches: list[SketchSpec] = Field(default_factory=list)
    features: list[FeatureSpec] = Field(default_factory=list)  # list order = build order


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


class IrDocument(BaseModel):
    schema_version: int = SCHEMA_VERSION
    model_id: str
    version: int = 0
    units: Literal["mm"] = "mm"
    bodies: list[BodySpec] = Field(default_factory=list)
    requirements: RequirementSpec = Field(default_factory=RequirementSpec)
    notes: list[str] = Field(default_factory=list)

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
