"""Shared runtime types — FROZEN CONTRACT.

Every subsystem (loop / tools / context / store / hooks / verify / worker /
render / server) imports from here. Do not redefine these elsewhere; do not
change field names without updating the affected consumers and contract tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tcad.ir.schema import IrDocument, IrPatch
from tcad.worker.protocol import WORKER_METHODS as _WORKER_METHODS

# ══════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════


class TurnKind(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    INSPECT = "inspect"


class TurnState(str, Enum):
    INSPECTED = "inspected"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    DRAFT = "draft"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    ABORTED = "aborted"
    CONFIRMED = "confirmed"


class ToolTier(str, Enum):
    READ = "read"
    WRITE = "write"
    PRIVILEGED = "privileged"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class Severity(str, Enum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


class Confidence(str, Enum):
    DETERMINISTIC = "deterministic"
    APPROXIMATE = "approximate"
    UNVERIFIED = "unverified"


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    PRE_TURN = "pre_turn"
    PRE_STEP = "pre_step"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_COMMIT = "pre_commit"
    ON_GATE_RESULT = "on_gate_result"
    POST_TURN = "post_turn"
    SESSION_END = "session_end"


class HookDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolErrorKind(str, Enum):
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    COMPILE = "compile"
    SOLVER = "solver"
    RUNTIME = "runtime"
    DENIED = "denied"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"


class ContextLevel(str, Enum):
    FULL = "full"
    SUMMARIZED = "summarized"
    MINIMAL = "minimal"


IrEventKind = Literal[
    "thread_created", "turn_started", "step_started", "llm_message",
    "tool_called", "tool_result", "patch_proposed", "patch_applied",
    "patch_rejected", "compile_started", "compile_finished", "gate_evaluated",
    "turn_finished", "approval_requested", "approval_resolved", "hook_failed",
]


# ══════════════════════════════════════════════════════════════════════════
# Events (append-only log is the single source of truth)
# ══════════════════════════════════════════════════════════════════════════


class IrEvent(BaseModel):
    seq: int = 0
    model_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: IrEventKind
    thread_id: str | None = None
    turn_id: str | None = None
    ir_version_before: int | None = None
    ir_version_after: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    tokens: dict[str, int] | None = None
    actor: Literal["user", "model", "kernel", "hook"] = "model"


# ══════════════════════════════════════════════════════════════════════════
# Thread / Turn / Step
# ══════════════════════════════════════════════════════════════════════════


class Thread(BaseModel):
    thread_id: str
    model_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    turns: list[str] = Field(default_factory=list)
    context_state: ContextLevel = ContextLevel.FULL


class Turn(BaseModel):
    """One unit of work: a user message → until the Gate goes green, a human
    intervenes, or the budget trips.

    ``model_id`` is denormalised from the owning :class:`Thread` on purpose.
    Every collaborator a turn touches — hook payloads, ``ToolContext``, the
    store, the compiler — needs to know which model is being built, and they are
    all handed a ``Turn`` and nothing else (see ``Strategy.run``). Carrying the
    id here makes a Turn self-describing and removes a whole class of
    plumbing-through-``thread`` bugs.
    """

    turn_id: str
    thread_id: str
    model_id: str
    kind: TurnKind
    state: TurnState = TurnState.RUNNING
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    base_ir_version: int
    error: str | None = None


# ══════════════════════════════════════════════════════════════════════════
# Tools
# ══════════════════════════════════════════════════════════════════════════


class ToolError(BaseModel):
    kind: ToolErrorKind
    message: str
    feature_id: str | None = None  # so the model knows WHAT to change
    hint: str = ""


class ImageRef(BaseModel):
    path: str
    view: str
    width: int
    height: int
    tokens_estimate: int = 0


class ToolResult(BaseModel):
    ok: bool
    content: str = ""  # text fed back to the model; must be self-explanatory
    images: list[ImageRef] = Field(default_factory=list)
    patch_applied: IrPatch | None = None
    error: ToolError | None = None


class HookContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thread_id: str
    turn_id: str
    model_id: str
    tier: ToolTier | None = None
    tool_name: str | None = None
    approved: bool = False


class ToolContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thread_id: str
    turn_id: str
    model_id: str
    ir: IrDocument | None = None
    workdir: str = "."
    data_dir: str = "data"
    worker: Any = None  # WorkerHandle; typed loosely to avoid a circular import
    hook_ctx: HookContext | None = None

    #: The dispatcher this turn's tool calls must go through. Set by the engine
    #: from the per-turn override, and preferred by handlers over
    #: ``services.hooks``. Without it a front end had to *replace*
    #: ``services.hooks`` for the duration of a request, which is shared mutable
    #: state: two concurrent requests clobber each other's tap, and whichever
    #: finishes first restores the dispatcher out from under the other.
    hooks: Any = None

    #: Whether ``geo_view`` is permitted *for this step*. Also per-turn state: it
    #: used to live on the shared services bundle, so one session's commit could
    #: open the visual checkpoint for another session's inspect step.
    visual_ok: bool = False
    request_text: str | None = None  # authoritative user text; model cannot rewrite it
    access_mode: str | None = None  # operator selection, never model-controlled
    edit_precondition: Any = None  # server-owned EditPrecondition; not model input


ToolHandler = Any  # Callable[[dict, ToolContext], Awaitable[ToolResult]]


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    tier: ToolTier
    description: str
    params_schema: dict[str, Any] = Field(default_factory=dict)
    handler: ToolHandler
    timeout_s: float = 30.0
    concurrency_safe: bool = False
    selection_capability: str | None = None

    def as_openai_tool(self) -> dict[str, Any]:
        # Some compatible gateways require an explicit required list, even
        # when every argument is optional. Do not mutate the validation schema.
        parameters = dict(self.params_schema)
        if parameters.get("type") == "object":
            parameters.setdefault("required", [])
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


# ══════════════════════════════════════════════════════════════════════════
# Hooks
# ══════════════════════════════════════════════════════════════════════════


class HookResult(BaseModel):
    decision: HookDecision
    hook_name: str
    reason: str = ""
    mutated_args: dict[str, Any] | None = None


class HookSpec(BaseModel):
    name: str
    events: list[HookEvent]
    kind: Literal["policy", "command"] = "policy"
    command: str | None = None
    timeout_s: float = 5.0
    module: str | None = None  # "pkg.mod:Factory" for kind="policy"


# ══════════════════════════════════════════════════════════════════════════
# Verification
# ══════════════════════════════════════════════════════════════════════════


class CheckResult(BaseModel):
    check_id: str
    status: CheckStatus
    severity: Severity
    confidence: Confidence
    message: str = ""
    measurements: dict[str, float | str | bool] = Field(default_factory=dict)
    expected: dict[str, float] | None = None
    evidence: list[str] = Field(default_factory=list)
    feature_id: str | None = None


class GateReport(BaseModel):
    model_id: str
    ir_version: int
    passed: bool = False
    results: list[CheckResult] = Field(default_factory=list)
    blocking_failures: list[str] = Field(default_factory=list)
    advisory_findings: list[str] = Field(default_factory=list)
    skipped_checks: list[str] = Field(default_factory=list)
    # Which build attempt the graded files came from. Filled from the artefact
    # directory's build stamp, so a "passed" report can be traced to the writes
    # it actually measured rather than to whatever sat in the directory.
    attempt_id: str = ""
    ir_sha256: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BuildStamp(BaseModel):
    """Which build attempt the files in an artefact directory came from.

    Written by the commit pipeline *before* it touches the worker, so anything
    already in the directory when it lands belongs to an older attempt. Without
    it, a retry into the same ``v<N>`` directory could be graded against the
    exports of the attempt that failed — the STEP would be real, just not real
    *for this build*.
    """

    attempt_id: str
    model_id: str
    ir_version: int
    started_at: float  # unix seconds, taken before any of this attempt's writes
    ir_sha256: str = ""  # hash of the IR document handed to the compiler


class CheckContext(BaseModel):
    """Read-only view of artefacts. Gate only ever sees this — never the live
    objects held by the loop. That is the structural guarantee behind
    "the generator must not grade its own paper" (design §4.6)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    model_id: str
    ir_version: int
    ir: IrDocument  # freshly loaded from disk, not the loop's in-memory object
    artifact_dir: str
    exports: dict[str, str] = Field(default_factory=dict)  # fmt -> abs path
    digest: "GeometryDigest | None" = None
    build_stamp: BuildStamp | None = None
    worker: Any = None


class Check(Protocol):
    id: str
    severity: Severity
    confidence: Confidence

    def run(self, ctx: CheckContext) -> CheckResult: ...


# ══════════════════════════════════════════════════════════════════════════
# Geometry digest — the projection that makes a BRep fit in a context window
# ══════════════════════════════════════════════════════════════════════════


class Topology(BaseModel):
    solids: int = 0
    faces: int = 0
    edges: int = 0
    vertexes: int = 0
    shells: int = 0


class BBox(BaseModel):
    """Axis-aligned box. Populated from `shape.BoundBox` (verified to exist:
    XMin/XMax/.../XLength/YLength/ZLength)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    x_min: float = 0.0
    y_min: float = 0.0
    z_min: float = 0.0

    @property
    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z,
                "x_min": self.x_min, "y_min": self.y_min, "z_min": self.z_min}


class FeatureDigest(BaseModel):
    id: str
    name: str
    op: str
    params: dict[str, Any] = Field(default_factory=dict)
    suppressed: bool = False


class MeasuredHole(BaseModel):
    """A hole read off the real BRep, never from the IR that asked for it.

    The Gate must not grade the generator's own paper: a diameter taken from
    ``FeatureSpec.params`` proves only that the model *wrote* a number. Each
    entry here comes from one concave cylindrical face group of the built
    shape, so it survives a compiler that silently cut nothing, cut the wrong
    depth, or moved the hole.
    """

    index: int
    diameter: float
    radius: float
    axis: list[float] = Field(default_factory=list)      # unit direction
    center: list[float] = Field(default_factory=list)    # a point on the axis
    depth: float = 0.0                                   # measured axial extent
    through: bool = False                                # depth spans the solid
    faces: int = 1                                       # faces merged into this hole


class FaceInfo(BaseModel):
    """One planar face, numbered locally on its owning feature.

    This exists because the tool description tells the model to attach a sketch to
    a face by name ("look the face number up with ir_digest — do not guess it")
    while ``ir_digest`` listed only a *count*. The model had no way to learn that
    ``Face6`` is the top one, so the instruction was unfollowable.

    Measured off the feature BRep: the name is FreeCAD's ``Face<N>`` index
    (1-based, matching ``shape.Faces[N-1]``), and ``normal``/``center``/``area``
    are what let a reader pick the face by *intent* ("the top face") instead of by
    a number that shifts when the model changes. Compound indices cannot be
    applied to a feature; older digests without an owner remain readable but
    cannot identify a reliable attachment target.
    """

    name: str
    feature_id: str | None = None
    body_id: str | None = None
    area: float = 0.0
    normal: list[float] = Field(default_factory=list)
    center: list[float] = Field(default_factory=list)


class EdgeInfo(BaseModel):
    """One edge, numbered locally on its owning feature.

    Same reason as :class:`FaceInfo`: ``fillet``/``chamfer`` select edges by name
    (``Edge<N>``), and a count of edges is not something a model can act on.
    ``kind``/``length``/``mid`` are what let a reader pick "the 8 mm vertical
    edge" instead of a number that moves when the part changes.
    """

    name: str
    feature_id: str | None = None
    body_id: str | None = None
    kind: str = ""                                  # "Line", "Circle", …
    length: float = 0.0
    mid: list[float] = Field(default_factory=list)  # midpoint
    direction: list[float] = Field(default_factory=list)  # tangent at mid (lines)


class FeatureGeometry(BaseModel):
    """Sub-elements measured in the owning feature's local numbering."""

    faces: list[FaceInfo] = Field(default_factory=list)
    edges: list[EdgeInfo] = Field(default_factory=list)


class GeometryDigest(BaseModel):
    model_id: str
    ir_version: int
    feature_chain: list[FeatureDigest] = Field(default_factory=list)
    topology: Topology = Field(default_factory=Topology)
    bbox: BBox = Field(default_factory=BBox)
    volume: float = 0.0
    area: float = 0.0
    shape_type: str = ""
    is_valid: bool = False
    key_dimensions: dict[str, float] = Field(default_factory=dict)
    spec_deviation: dict[str, float] = Field(default_factory=dict)
    holes: list[MeasuredHole] = Field(default_factory=list)
    #: Planar faces, so a model can name one for a ``plane: {"kind": "face"}``
    #: sketch attachment. Bounded by the worker; additive, so an older digest.json
    #: without it still parses.
    faces: list[FaceInfo] = Field(default_factory=list)
    #: Edges, so a model can name one for ``base_feature`` + ``sub_elements``.
    edges: list[EdgeInfo] = Field(default_factory=list)
    feature_geometry: dict[str, FeatureGeometry] = Field(default_factory=dict)
    measurements_available: bool = True  # False -> digest is structure-only
    body_solids: dict[str, int] = Field(default_factory=dict)
    text: str = ""  # rendered, <= ~2000 tokens


# ══════════════════════════════════════════════════════════════════════════
# Worker RPC (process boundary)
# ══════════════════════════════════════════════════════════════════════════


class RpcRequest(BaseModel):
    id: int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    # NOTE: `kind` is a plain str on purpose. The wire format must tolerate an
    # unrecognised error kind from a worker — modelling it as the ToolErrorKind
    # enum made the whole frame fail validation, so an unknown kind surfaced as a
    # 10-second timeout instead of a fast, honest error. Normalisation to
    # ToolErrorKind happens at the client boundary (see WorkerHandle.request_sync).
    kind: str = ToolErrorKind.RUNTIME.value
    message: str = ""
    feature_id: str | None = None
    traceback: str = ""


class RpcResponse(BaseModel):
    id: int
    ok: bool
    result: dict[str, Any] | None = None
    error: RpcError | None = None


# WORKER_METHODS is owned by the zero-dependency wire module (the worker runs in
# FreeCAD's interpreter, which has no pydantic/numpy). Imported or not, never
# redeclare it here.
WORKER_METHODS: tuple[str, ...] = _WORKER_METHODS


# ══════════════════════════════════════════════════════════════════════════
# Render
# ══════════════════════════════════════════════════════════════════════════

ViewName = Literal["iso", "front", "top", "right"]


class Mesh(BaseModel):
    """Triangulated geometry handed from the worker to the supervisor.

    Note: BRep objects never cross the process boundary; only this does.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    vertices: list[tuple[float, float, float]] = Field(default_factory=list)
    facets: list[tuple[int, int, int]] = Field(default_factory=list)
    bbox: BBox = Field(default_factory=BBox)
    volume: float = 0.0
    tolerance: float = 0.5  # passed to TopoShape.tessellate(tolerance)


RenderStyle = Literal["flat_edges", "flat", "edges_only"]
