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


# ── failure normalisation (here so it is testable without FreeCAD) ─────────
#
# This used to live in rpc.py, which imports the FreeCAD-bound handlers and so
# cannot be imported by the ordinary test process. It decides what a person and
# the model actually read when a handler fails — the one string that told a user
# nothing while their phone stand would not build — so it belongs somewhere it
# can be tested directly.


def first_error(result: dict) -> dict:
    """Normalise a handler's failure payload into one {kind, message, feature_id}.

    Handlers use three shapes, because each was written for its own natural return
    value: ``errors: [ {...}, ... ]`` (compile/export collect several), ``error`` as
    a plain string (tessellate), or ``error`` as a dict. Rather than force every
    handler to agree, normalise at the one place that has to produce a single
    envelope error.
    """
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first
        return {"message": str(first)}
    err = result.get("error")
    if isinstance(err, dict):
        return err
    if err:
        return {"message": str(err)}
    return {}


def no_detail_message(result: dict) -> str:
    """What to say when a handler returned ``ok=false`` without saying why.

    Never a bare "handler reported failure": that is the exact string a person
    read while a phone stand would not build, and it names neither a cause nor a
    feature. Handlers are supposed to attach a reason (``compile_ir`` now does,
    even for a document that simply came out empty), so this is the last-resort
    wording — but even here, saying what the payload *does* contain gives the next
    reader somewhere to look instead of a dead end.
    """
    keys = sorted(k for k in result if k not in ("ok", "elapsed_s"))
    return (
        "handler reported failure with no error detail "
        f"(result keys: {', '.join(keys) if keys else 'none'})"
    )
