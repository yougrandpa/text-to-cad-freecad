"""Append-only JSONL event log — the single source of truth (design §4.4, L2).

Write-ahead rule: events are appended **before** the IR snapshot is written. A
crash between the two must be recoverable via :meth:`rebuild` / the store's
:meth:`tcad.store.ir_store.IrStore.rebuild_from_events`. Concretely this means:

  * ``append`` assigns a monotonic ``seq`` and ``fsync``s the file handle before
    returning, so the byte is durable even if the process dies immediately after.
  * The store writes the event, *then* does the atomic snapshot swap. If it dies
    in between, the event is on disk but the snapshot is not — and the event
    carries enough to replay the document (see ``ir_store``).

Single-writer discipline: one process writes a given model's log. We hold an
in-process lock so concurrent *threads* in the same writer serialise; cross
process serialization is the caller's responsibility (model_id-level mutex in the
loop / WAL on the SQLite side).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from tcad.core.types import IrEvent

_DEFAULT_DATA_DIR = "data"


class EventLog:
    def __init__(self, data_dir: str | os.PathLike[str] = _DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._seq_cache: dict[str, int] = {}

    # ── paths ────────────────────────────────────────────────────────────────

    def _path(self, model_id: str) -> Path:
        return self.data_dir / "models" / model_id / "events.jsonl"

    def _ensure(self, model_id: str) -> Path:
        p = self._path(model_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch()
        return p

    # ── write ──────────────────────────────────────────────────────────────────

    def append(self, model_id: str, event: IrEvent) -> IrEvent:
        """Persist ``event``, assigning the next monotonic ``seq``. fsync'd."""
        path = self._ensure(model_id)
        with self._lock:
            seq = self._next_seq(model_id, path)
            event.seq = seq
            event.model_id = model_id
            line = event.model_dump_json() + "\n"
            with open(path, "a") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._seq_cache[model_id] = seq
        return event

    def _next_seq(self, model_id: str, path: Path) -> int:
        if model_id in self._seq_cache:
            return self._seq_cache[model_id] + 1
        # cold start: scan for the highest seq on disk
        max_seq = -1
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    s = rec.get("seq")
                    if isinstance(s, int) and s > max_seq:
                        max_seq = s
        return max_seq + 1

    # ── read ───────────────────────────────────────────────────────────────────

    def read_all(self, model_id: str) -> list[IrEvent]:
        """Return every event for ``model_id`` in seq order."""
        path = self._path(model_id)
        if not path.exists():
            return []
        out: list[IrEvent] = []
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(IrEvent.model_validate_json(line))
        out.sort(key=lambda e: e.seq)
        return out

    def tail(self, model_id: str, n: int) -> list[IrEvent]:
        """Return the last ``n`` events (or fewer)."""
        all_ = self.read_all(model_id)
        return all_[-n:] if n > 0 else []

    def rebuild(self, model_id: str):
        """Yield every event for ``model_id`` in order (generator).

        Used by the store to replay the log from the top. Yielding keeps memory
        flat even for long-lived models.
        """
        path = self._path(model_id)
        if not path.exists():
            return
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield IrEvent.model_validate_json(line)
