"""Zero-dependency wire protocol shared by supervisor and worker.

⚠️ IMPORT BAN: this module (and everything under ``tcad/worker/``) is executed
by **FreeCAD's own Python interpreter**, which has neither pydantic nor numpy
installed. Only the standard library may be imported here.

The supervisor wraps these plain values in pydantic models (see
``tcad.core.types``); the worker speaks raw dicts / JSON lines.
"""

from __future__ import annotations

# ── method names ──────────────────────────────────────────────────────────
M_PING = "ping"
M_API_SELFTEST = "api_selftest"
M_COMPILE_IR = "compile_ir"
M_INTROSPECT = "introspect_document"
M_EXPORT = "export_artifacts"
M_TESSELLATE = "tessellate"
M_IMPORT_ASSET = "import_asset"

WORKER_METHODS: tuple[str, ...] = (
    M_PING,
    M_API_SELFTEST,
    M_COMPILE_IR,
    M_INTROSPECT,
    M_EXPORT,
    M_TESSELLATE,
    M_IMPORT_ASSET,
)

# ── error kinds (must stay in sync with tcad.core.types.ToolErrorKind) ────
E_SCHEMA = "schema"
E_SEMANTIC = "semantic"
E_COMPILE = "compile"
E_SOLVER = "solver"
E_RUNTIME = "runtime"
E_DENIED = "denied"
E_TIMEOUT = "timeout"
E_NOT_FOUND = "not_found"

ERROR_KINDS: tuple[str, ...] = (
    E_SCHEMA, E_SEMANTIC, E_COMPILE, E_SOLVER, E_RUNTIME, E_DENIED, E_TIMEOUT, E_NOT_FOUND,
)

# ── envelope keys ─────────────────────────────────────────────────────────
K_ID = "id"
K_METHOD = "method"
K_PARAMS = "params"
K_OK = "ok"
K_RESULT = "result"
K_ERROR = "error"
K_KIND = "kind"
K_MESSAGE = "message"
K_FEATURE_ID = "feature_id"
K_TRACEBACK = "traceback"

# ── export formats ────────────────────────────────────────────────────────
EXPORT_FORMATS: tuple[str, ...] = ("step", "stl", "brep", "fcstd")

# ── defaults ──────────────────────────────────────────────────────────────
DEFAULT_TESSELLATE_TOLERANCE = 0.5
"""TopoShape.tessellate(tolerance) REQUIRES an argument — verified at runtime.
The .pyi declaring a zero-arg signature is wrong (design doc 附录 B-1)."""

DEFAULT_REQUEST_TIMEOUT_S = 60.0
MAX_TRACEBACK_CHARS = 4000


def encode_line(payload: dict) -> str:
    """Serialise one JSONL frame. stdlib only."""
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_line(line: str) -> dict:
    import json

    return json.loads(line)


def clamp_traceback(tb: str) -> str:
    if len(tb) <= MAX_TRACEBACK_CHARS:
        return tb
    return "...(truncated)...\n" + tb[-MAX_TRACEBACK_CHARS:]
