"""CheckContext builder — the structural half of "the generator must not grade
its own paper".

This module is the ONLY place the verification layer obtains its inputs. It
reads artefacts from **disk** and never, under any circumstance, accepts the
loop's in-memory ``IrDocument`` or a digest object the generator handed it.

Why this matters (design §4.6 / §9 "V → S"):
    The generator (loop + worker) builds the model. The Gate decides whether
    the model is "done". If the Gate consumed the generator's live objects it
    would be grading its own output — the exact self-referential trap the
    design forbids. So ``build_check_context`` works purely from paths:
      * the IR snapshot file        (written immutably by the store on commit)
      * ``artifact_dir/digest.json`` (written by the worker's introspect step,
                                      via the artefact store — NOT by the loop)
      * the exported STEP/STL/BREP   (written by the worker's exporter)

If ``digest.json`` is missing we raise a typed error and fabricate nothing.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from tcad.core.types import BuildStamp, CheckContext, GeometryDigest, IrDocument
from tcad.store.artifacts import read_build_stamp
from tcad.worker.protocol import M_IMPORT_ASSET


class DigestNotFoundError(FileNotFoundError):
    """Raised when ``artifact_dir/digest.json`` is absent.

    We never invent measurements — no digest means no Gate can run honestly.
    """


class CheckContextError(Exception):
    """Raised when a required on-disk artefact cannot be loaded."""


def _load_ir(ir_path: str) -> IrDocument:
    if not os.path.isfile(ir_path):
        raise CheckContextError(f"IR snapshot not found on disk: {ir_path}")
    try:
        with open(ir_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return IrDocument.model_validate(data)
    except Exception as exc:  # pydantic / json — surface as a typed build error
        raise CheckContextError(f"failed to parse IR snapshot {ir_path}: {exc}") from exc


def _load_digest(artifact_dir: str) -> GeometryDigest:
    digest_path = os.path.join(artifact_dir, "digest.json")
    if not os.path.isfile(digest_path):
        raise DigestNotFoundError(
            f"digest.json not found at {digest_path}; "
            "the Gate cannot run without an independently produced digest"
        )
    try:
        with open(digest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return GeometryDigest.model_validate(data)
    except Exception as exc:
        raise CheckContextError(f"failed to parse digest.json {digest_path}: {exc}") from exc


# Files the compiler writes to diagnose its own build. They are real geometry,
# but they are not what was asked to be delivered: `roundtrip.step` exists even
# when the exporter never ran, so letting it answer for the STEP deliverable
# grades a diagnostic as an artifact.
_DIAGNOSTIC_STEMS = frozenset({"roundtrip"})


def _discover_exports(artifact_dir: str, model_id: str = "") -> dict[str, str]:
    """Deliverable geometry on disk, keyed by format.

    Three rules the old ``glob``-first-match version got wrong:
      * extensions are matched case-insensitively, because FreeCAD writes
        ``<model_id>.FCStd`` — a ``*.fcstd`` pattern matches nothing on a
        case-sensitive filesystem, so the reopenable document silently went
        unchecked on Linux while passing on macOS.
      * diagnostics are never candidates.
      * when several files share an extension, the one named after this model
        wins, since that is the file the exporter produced for this build.
    """
    exports: dict[str, str] = {}
    try:
        names = sorted(os.listdir(artifact_dir))
    except OSError:
        return exports
    for fmt in ("step", "stl", "brep", "fcstd"):
        candidates = [
            n for n in names
            if os.path.splitext(n)[1].lower() == f".{fmt}"
            and os.path.splitext(n)[0] not in _DIAGNOSTIC_STEMS
        ]
        preferred = [n for n in candidates if n.lower() == f"{model_id}.{fmt}".lower()]
        chosen = (preferred or candidates)[:1]
        if chosen:
            exports[fmt] = os.path.abspath(os.path.join(artifact_dir, chosen[0]))
    return exports


def _load_stamp(artifact_dir: str) -> BuildStamp | None:
    """Which attempt wrote this directory — or None if it predates stamping.

    A corrupt stamp is an error rather than a mystery: the alternative is to
    grade the directory as though nothing was known about its provenance.
    """
    try:
        return read_build_stamp(artifact_dir)
    except Exception as exc:
        raise CheckContextError(
            f"build stamp in {artifact_dir} is unreadable: {type(exc).__name__}: {exc}"
        ) from exc


def build_check_context(
    *,
    model_id: str,
    ir_version: int,
    artifact_dir: str,
    ir_path: str,
    worker: Any | None = None,
) -> CheckContext:
    """Build a disk-only :class:`CheckContext`.

    Parameters are **paths**, never live objects. ``worker`` (optional) is the
    only non-disk handle and is used solely by ``round_trip`` to re-read the
    exported STEP through the worker's independent import path (see
    ``checks_solid.RoundTripCheck``). Everything else comes from files.

    Raises:
        DigestNotFoundError: ``artifact_dir/digest.json`` is missing.
        CheckContextError:   the IR snapshot is missing/unparseable.
    """
    if not os.path.isdir(artifact_dir):
        raise CheckContextError(f"artifact_dir does not exist: {artifact_dir}")

    ir = _load_ir(ir_path)
    digest = _load_digest(artifact_dir)
    exports = _discover_exports(artifact_dir, model_id)

    return CheckContext(
        model_id=model_id,
        ir_version=ir_version,
        ir=ir,
        artifact_dir=os.path.abspath(artifact_dir),
        exports=exports,
        digest=digest,
        build_stamp=_load_stamp(artifact_dir),
        worker=worker,
    )


class WorkerReadbackHandle(BaseModel):
    """Thin adapter so ``round_trip`` can ask the worker to re-measure a STEP
    file from disk without the supervisor needing FreeCAD (which only the
    worker process has).

    The worker exposes ``import_asset(path, fmt)``; we reuse it as a
    disk-read-back: it imports the on-disk STEP and returns a ``shape_summary``
    with ``volume`` / ``faces`` / ``edges``. Those numbers come from a *fresh*
    OCC import of the file, independent of the in-memory compiled shape.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    worker: Any

    def read_step_summary(self, step_path: str) -> dict:
        """Re-measure the on-disk STEP through the worker's import path.

        The Gate is synchronous, so we must use the worker's *blocking* entry
        point. ``WorkerHandle`` exposes both (``request_sync`` / ``await
        request``); if only the async one is reachable we fail loudly with an
        actionable message instead of silently handing back a coroutine (which
        would otherwise surface as a baffling mapping error several lines later).
        """
        fn = getattr(self.worker, "request_sync", None) or getattr(self.worker, "request", None)
        if fn is None:
            raise TypeError(
                f"worker handle {type(self.worker).__name__} exposes neither "
                "request_sync() nor request()"
            )
        raw = fn(M_IMPORT_ASSET, {"path": step_path, "fmt": "step"})
        if inspect.iscoroutine(raw):
            raw.close()  # avoid an un-awaited-coroutine warning
            raise TypeError(
                "worker handle only exposes an async request(); the Gate runs "
                "synchronously — inject a sync-capable handle (WorkerHandle "
                "provides request_sync) into CheckContext.worker"
            )
        summary = raw.get("shape_summary") or raw.get("result", {}) or raw
        return {**summary,
            "volume": float(summary.get("volume", 0.0)),
            "faces": float(summary.get("faces", 0.0)),
            "edges": float(summary.get("edges", 0.0)),
        }


ContextLoader = Callable[[str, int], CheckContext]
"""Type alias for the ``(model_id, ir_version) -> CheckContext`` injector the
loop layer supplies to :class:`tcad.verify.gate.Gate`."""
