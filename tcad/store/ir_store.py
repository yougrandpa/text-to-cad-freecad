"""Versioned IR store (design §4.4, L1 + L2).

Layering
--------
* **L1 — immutable snapshots**: ``data/models/<model_id>/v<int>.json``. Every
  commit writes a *new* file via temp-file + ``os.replace`` (atomic, never
  overwrites). Old versions persist, which is exactly what makes rollback an
  O(1) file read instead of a replay.
* **L2 — append-only event log**: ``events.jsonl`` (see :mod:`event_log`). The
  store treats it as the source of truth for *reconstruction*.

Ordered write (the core guarantee)
-----------------------------------
``apply_patch`` does, in this exact order:

  1. load current IR (latest snapshot),
  2. compute the new IR via :func:`tcad.ir.patch.apply_patch` (pure, may raise
     ``ToolError`` — nothing is written yet if it does),
  3. **append the ``patch_applied`` event and fsync it** (write-ahead),
  4. atomically write ``v{new_version}.json``.

If the process dies between 3 and 4, the event is durable but the snapshot is
not. :meth:`rebuild_from_events` replays the log from the ``create`` event and
reconstructs the exact intended document — so no committed patch is ever lost,
and at most one in-flight (not-yet-snapshotted) patch is recoverable too.

The ``create`` event stores the *initial* IR so replay has a base. Subsequent
``patch_applied`` events store the IrPatch that produced them.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from tcad.core.ids import contained_path, ensure_safe_id
from tcad.core.types import IrEvent, ToolError, ToolErrorKind
from tcad.ir.patch import PatchOutcome, apply_patch
from tcad.ir.schema import IrDocument, IrPatch
from tcad.store.event_log import EventLog

_DEFAULT_DATA_DIR = "data"
_CREATE_KIND = "create"


class IrStore:
    def __init__(self, data_dir: str | os.PathLike[str] = _DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self._log = EventLog(self.data_dir)
        #: One lock per model, held across the whole read-modify-write.
        #:
        #: The event log has an append lock of its own, but that only orders the
        #: *writes*. Without this, two concurrent ``apply_patch`` calls both read
        #: vN, both pass the ``base_version`` check (a comparison against the
        #: document each of them loaded), and both snapshot v(N+1) — one patch
        #: silently lost while both callers were told they succeeded. Optimistic
        #: concurrency only means something if the check and the write are one
        #: critical section.
        #:
        #: Scope, stated honestly: this serialises writers **within one process**,
        #: which is what the shipped stack has (one ``IrStore`` inside one
        #: ``StoreAdapter``, one local server). It is not a cross-process file
        #: lock; a second process writing the same ``data_dir`` would need one.
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, model_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(model_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[model_id] = lock
            return lock

    # ── paths ──────────────────────────────────────────────────────────────────

    def _model_dir(self, model_id: str) -> Path:
        """The model's directory — validated, because ``model_id`` is a path part.

        Validated here and not only at the API: this is the last point before the
        name becomes a path, and a caller that reaches the store directly (a
        script, a test, a future front end) must not be able to escape
        ``data_dir`` either.
        """
        ensure_safe_id(model_id, kind="model_id")
        return contained_path(self.data_dir, "models", model_id)

    def _snapshot_path(self, model_id: str, version: int) -> Path:
        return self._model_dir(model_id) / f"v{version}.json"

    # ── create / seed ───────────────────────────────────────────────────────────

    def create(self, model_id: str, ir: IrDocument) -> IrDocument:
        """Seed a model with its v0 document.

        Persists ``v0.json`` and a ``patch_applied`` event carrying the initial
        IR so the log alone can reconstruct it. Returns the stored document
        (``model_id``/``version`` normalised).
        """
        ir = ir.model_copy(deep=True)
        ir.model_id = model_id
        ir.version = 0
        # Same critical section as apply_patch: a create that races a patch must
        # not be able to interleave a v0 snapshot between the patch's read and
        # its write.
        with self._lock_for(model_id):
            self._write_snapshot(model_id, ir)
            event = IrEvent(
                model_id=model_id,
                kind="patch_applied",
                ir_version_before=None,
                ir_version_after=0,
                actor="model",
                payload={"action": _CREATE_KIND, "ir": ir.model_dump()},
            )
            self._log.append(model_id, event)
        return ir

    # ── load ─────────────────────────────────────────────────────────────────────

    def load(self, model_id: str, version: int | None = None) -> IrDocument:
        """Load a snapshot. ``version=None`` -> latest. Missing -> FileNotFoundError."""
        versions = self.list_versions(model_id)
        if not versions:
            raise FileNotFoundError(f"no IR stored for model '{model_id}'")
        target = max(versions) if version is None else version
        path = self._snapshot_path(model_id, target)
        if not path.exists():
            raise FileNotFoundError(f"no snapshot v{target} for model '{model_id}'")
        return IrDocument.model_validate_json(path.read_text())

    def list_versions(self, model_id: str) -> list[int]:
        d = self._model_dir(model_id)
        if not d.is_dir():
            return []
        out = []
        for f in d.glob("v*.json"):
            stem = f.stem[1:]  # strip leading 'v'
            if stem.isdigit():
                out.append(int(stem))
        return sorted(out)

    def exists(self, model_id: str) -> bool:
        """True when this model has at least one stored snapshot.

        Cheaper and safer than ``load()`` in a try/except when all you need is
        presence — no JSON parsing, and no chance of catching a ``FileNotFoundError``
        raised by something else inside the parse.
        """
        return bool(self.list_versions(model_id))

    def latest_version(self, model_id: str) -> int | None:
        """Highest stored version, or ``None`` when the model does not exist.

        Deliberately returns ``None`` rather than ``0``:

        ``0`` is a **valid version** — a freshly created model *is* at v0 — so a
        sentinel of ``0`` cannot be told apart from a real one. That ambiguity
        already cost a 500 in the render endpoint once, where a missing model was
        read as "a model at v0" and then loaded. Use this when the question is
        "does it exist and at what version".
        """
        versions = self.list_versions(model_id)
        return max(versions) if versions else None

    # ── apply_patch (ordered: event first, then snapshot) ───────────────────────

    def apply_patch(self, model_id: str, patch: IrPatch) -> tuple[IrDocument, IrEvent]:
        """Apply ``patch`` and durably store it. Returns ``(new_ir, event)``.

        The load → apply → append → snapshot sequence is one critical section per
        model. Without it the ``base_version`` check is a TOCTOU race: two writers
        can both read vN, both pass, and both write v(N+1).
        """
        with self._lock_for(model_id):
            current = self.load(model_id)  # raises FileNotFoundError if not created
            outcome: PatchOutcome = apply_patch(current, patch)

            event = IrEvent(
                model_id=model_id,
                kind="patch_applied",
                ir_version_before=current.version,
                ir_version_after=outcome.version,
                actor="model",
                payload={
                    "action": "apply",
                    "patch": patch.model_dump(),
                    "summary": outcome.summary,
                    "changes": outcome.changes,
                    "created_ids": outcome.created_ids,
                    "renamed_ids": outcome.renamed_ids,
                },
            )
            # 1) write-ahead: event durable before the snapshot
            self._log.append(model_id, event)
            # 2) atomic snapshot
            self._write_snapshot(model_id, outcome.ir)
            return outcome.ir, event

    # ── rollback / snapshot bookkeeping ──────────────────────────────────────────

    def rollback(self, model_id: str, version: int) -> IrDocument:
        """Return a previously committed version. O(1) file read, not a replay."""
        return self.load(model_id, version)

    def snapshot_before_commit(self, model_id: str) -> int:
        """Record the current latest version as the reversible safe-point.

        Because every commit writes an immutable ``v{n}.json`` (never
        overwritten), the value returned here is a stable point you can
        :meth:`rollback` to. Returns the latest existing version (or -1 if the
        model has no commits yet).
        """
        versions = self.list_versions(model_id)
        return max(versions) if versions else -1

    # ── rebuild from events (crash recovery) ──────────────────────────────────────

    def rebuild_from_events(self, model_id: str, upto: int | None = None) -> IrDocument:
        """Reconstruct the IR by replaying the event log.

        Starts from the ``create`` event's stored IR, then re-applies each
        ``patch_applied`` event (with ``ir_version_after <= upto``) in order,
        using the *same* :func:`apply_patch` that produced them originally — so
        a crash mid-commit (event written, snapshot not) still yields the exact
        intended document.
        """
        base: IrDocument | None = None
        base_version: int = -1
        for ev in self._log.rebuild(model_id):
            action = ev.payload.get("action")
            if action == _CREATE_KIND:
                base = IrDocument.model_validate(ev.payload["ir"])
                base_version = ev.ir_version_after if ev.ir_version_after is not None else 0
                if upto is not None and base_version > upto:
                    break
                continue
            if action != "apply":
                continue
            if upto is not None and ev.ir_version_after is not None and ev.ir_version_after > upto:
                continue
            patch = IrPatch.model_validate(ev.payload["patch"])
            outcome = apply_patch(base, patch)
            base = outcome.ir
            base_version = outcome.version

        if base is None:
            raise FileNotFoundError(
                f"cannot rebuild '{model_id}': no create event in the log")
        return base

    # ── low-level ─────────────────────────────────────────────────────────────────

    def _write_snapshot(self, model_id: str, ir: IrDocument) -> None:
        d = self._model_dir(model_id)
        d.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_path(model_id, ir.version)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            fh.write(ir.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
