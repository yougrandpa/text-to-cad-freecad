"""RPC dispatch for the worker (runs inside FreeCADCmd).

Maps a method name to its handler, builds a uniform ok/error envelope using the
K_* keys from tcad.worker.protocol, times each call with a wall clock, and
captures any top-level exception so a single bad call never kills the worker.
"""

from __future__ import annotations

import time
import traceback

from tcad.worker.protocol import (
    K_ERROR, K_FEATURE_ID, K_ID, K_KIND, K_MESSAGE, K_METHOD, K_OK, K_PARAMS,
    K_RESULT, K_TRACEBACK, E_COMPILE, E_RUNTIME, WORKER_METHODS, clamp_traceback,
    first_error, no_detail_message,
)

from tcad.worker.compiler import compile_ir
from tcad.worker.introspect import introspect_document
from tcad.worker.mesh import tessellate
from tcad.worker.exporters import export_artifacts
from tcad.worker.selftest import api_selftest


_HANDLERS = {
    "ping": lambda **kw: {"ok": True, "pong": True},
    "api_selftest": api_selftest,
    "compile_ir": compile_ir,
    "introspect_document": introspect_document,
    "tessellate": tessellate,
    "export_artifacts": export_artifacts,
    "import_asset": None,  # assigned below
}


def _import_asset(path=None, fmt=None, out_dir=None, **_extra) -> dict:
    """Minimal asset import (STEP/IGES/BREP) via Part.Shape.read.

    Returns a shape summary. Kept simple: the worker's job is to compile IR, not
    to be a general importer — full import tooling lives in the supervisor layer.
    """
    import os
    import tempfile

    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"asset not found: {path!r}", "shape_summary": None}
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="tcad_import_")
    try:
        sh = __import__("Part").Shape()
        sh.read(path)
        summary = {
            "solids": len(sh.Solids), "faces": len(sh.Faces),
            "edges": len(sh.Edges), "vertexes": len(sh.Vertexes),
            "volume": float(sh.Volume), "is_valid": bool(sh.isValid()),
            "shape_type": str(sh.ShapeType),
        }
        return {"ok": True, "shape_summary": summary}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"import failed: {type(exc).__name__}: {exc}",
                "shape_summary": None}


_HANDLERS["import_asset"] = _import_asset


def dispatch(request: dict) -> dict:
    """Turn one decoded request dict into one response dict. Never raises."""
    req_id = request.get(K_ID)
    method = request.get(K_METHOD)
    params = request.get(K_PARAMS) or {}

    if method not in WORKER_METHODS:
        return _error(req_id, method, E_RUNTIME, f"unknown method: {method!r}",
                      feature_id=None, tb="")

    handler = _HANDLERS.get(method)
    if handler is None:
        return _error(req_id, method, E_RUNTIME, f"method not implemented: {method!r}",
                      feature_id=None, tb="")

    start = time.perf_counter()
    try:
        result = handler(**params)
        # Handlers that build documents may return {"ok": ...} nested; normalise.
        elapsed = time.perf_counter() - start
        if isinstance(result, dict):
            result["elapsed_s"] = round(elapsed, 4)
        else:
            result = {"elapsed_s": round(elapsed, 4), "value": result}

        # A handler that REPORTED failure is a failed call.
        #
        # Every handler returns {"ok": bool, ...} where ok=false means "I ran but
        # the work failed" — a conflicting sketch, no solid to export, no mesh to
        # tessellate. Treating "did not raise" as success wrapped those failures in
        # an ok=true envelope, so the supervisor saw a successful compile for a
        # document that had produced no geometry at all. The downstream Gate could
        # then only say "cannot attest", and the model never learned that its
        # sketch had conflicting constraints — the single most useful thing it
        # could have been told. Nested failure must not be flattened into success.
        if isinstance(result, dict) and result.get("ok") is False:
            detail = first_error(result)
            return _error(
                req_id, method,
                detail.get("kind") or E_COMPILE,
                detail.get("message") or no_detail_message(result),
                feature_id=detail.get("feature_id"),
                tb=detail.get("traceback") or "",
                elapsed=elapsed,
            )

        return {
            K_ID: req_id, K_OK: True, K_RESULT: result, K_ERROR: None,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        tb = clamp_traceback(traceback.format_exc())
        return _error(req_id, method, E_RUNTIME,
                      f"{type(exc).__name__}: {exc}", feature_id=None, tb=tb,
                      elapsed=elapsed)


def _error(req_id, method, kind, message, feature_id=None, tb="", elapsed=None) -> dict:
    err = {K_KIND: kind, K_MESSAGE: message, K_FEATURE_ID: feature_id, K_TRACEBACK: tb}
    resp = {K_ID: req_id, K_OK: False, K_RESULT: None, K_ERROR: err}
    if elapsed is not None:
        resp["elapsed_s"] = round(elapsed, 4)
    return resp
