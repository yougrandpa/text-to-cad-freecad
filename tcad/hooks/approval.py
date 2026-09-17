"""Approval records and store (L layer).

An approval is the user's explicit go-ahead for a *specific* privileged call
(tool name + args hash) for a bounded time. The store is deliberately
decoupled from the session SQLite DB via the :class:`ApprovalStore` Protocol —
another teammate owns that schema, and a Protocol keeps us from importing it.

The default implementation persists to a JSON file with atomic writes (no
SQLite, no third-party deps). TTL expiry and args-hash matching are enforced in
``lookup_valid`` so the privileged gate can trust the result, while
``expire_stale`` physically prunes dead records.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from tcad.core.types import HookSpec  # noqa: F401  (kept for symmetry of imports)


class ApprovalRecord(BaseModel):
    """One approval request/grant for a privileged tool call."""

    id: str
    tool_name: str
    args_hash: Optional[str] = None
    requested_at: datetime
    expires_at: datetime
    granted: bool = False
    resolved_at: Optional[datetime] = None


@runtime_checkable
class ApprovalStore(Protocol):
    """What the rest of the system may rely on."""

    def request(
        self, tool_name: str, args_hash: Optional[str] = None, ttl_s: Optional[float] = None
    ) -> ApprovalRecord: ...

    def resolve(self, approval_id: str, granted: bool) -> Optional[ApprovalRecord]: ...

    def lookup_valid(self, tool_name: str, args_hash: Optional[str] = None) -> Optional[ApprovalRecord]: ...

    def expire_stale(self) -> int: ...


class JsonFileApprovalStore:
    """Default :class:`ApprovalStore` backed by a JSON file.

    Not thread-safe across processes by design (the harness serialises writes
    per model); writes are atomic via a temp file + ``os.replace``.
    """

    def __init__(self, path: Optional[str] = None, ttl_s: float = 900.0) -> None:
        self._path = path or os.path.join(tempfile.gettempdir(), "tcad_approvals.json")
        self._ttl_s = float(ttl_s)

    # ── public API ──────────────────────────────────────────────────────────

    def request(
        self, tool_name: str, args_hash: Optional[str] = None, ttl_s: Optional[float] = None
    ) -> ApprovalRecord:
        ttl = self._ttl_s if ttl_s is None else float(ttl_s)
        now = datetime.now(timezone.utc)
        rec = ApprovalRecord(
            id=uuid.uuid4().hex,
            tool_name=tool_name,
            args_hash=args_hash,
            requested_at=now,
            expires_at=now + timedelta(seconds=ttl),
            granted=False,
            resolved_at=None,
        )
        self._mutate(lambda recs: recs.append(rec))
        return rec

    def resolve(self, approval_id: str, granted: bool) -> Optional[ApprovalRecord]:
        found: Optional[ApprovalRecord] = None

        def upd(recs):
            nonlocal found
            for r in recs:
                if r.id == approval_id:
                    r.granted = bool(granted)
                    r.resolved_at = datetime.now(timezone.utc)
                    found = r
                    return True
            return False

        self._mutate(upd)
        return found

    def lookup_valid(
        self, tool_name: str, args_hash: Optional[str] = None
    ) -> Optional[ApprovalRecord]:
        now = datetime.now(timezone.utc)
        for r in self._load():
            if r.tool_name != tool_name:
                continue
            if args_hash is not None and r.args_hash != args_hash:
                continue
            if not r.granted:
                continue
            exp = r.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue
            return r
        return None

    def expire_stale(self) -> int:
        """Remove approvals whose TTL has lapsed and that were never granted.

        Returns the number of records removed.
        """
        now = datetime.now(timezone.utc)

        def prune(recs):
            kept = []
            removed = 0
            for r in recs:
                exp = r.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now:  # any lapsed approval is stale, granted or not
                    removed += 1
                    continue
                kept.append(r)
            return kept, removed

        return self._mutate_return(prune)

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> list[ApprovalRecord]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [ApprovalRecord.model_validate(d) for d in data]
        except Exception:
            return []

    def _save(self, recs: list[ApprovalRecord]) -> None:
        data = [r.model_dump(mode="json") for r in recs]
        d = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _mutate(self, fn) -> None:
        recs = self._load()
        fn(recs)
        self._save(recs)

    def _mutate_return(self, fn) -> int:
        recs = self._load()
        result = fn(recs)
        new_recs, retval = result if isinstance(result, tuple) else (result, 0)
        self._save(new_recs)
        return retval
