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

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from tcad.core.types import HookSpec  # noqa: F401  (kept for symmetry of imports)


#: How much of the digest is kept. 16 hex chars = 64 bits is plenty to bind one
#: call: the space of "different argument sets" a model can produce in one turn is
#: nowhere near 2**32, so a collision is not a practical concern, and a short id
#: stays readable in a log line.
FINGERPRINT_CHARS = 16


def args_fingerprint(args: Any) -> str:
    """Stable fingerprint of a tool call's arguments.

    This is the binding between an approval and the *exact* call it was granted
    for. It must be computed the same way everywhere — the engine when it creates
    the request, and the privileged gate when it checks one — otherwise the gate
    compares two different strings and denies a call the user just approved (or,
    worse, an approval granted for one payload authorises another).

    ``sort_keys`` makes it independent of dict ordering; ``default=str`` keeps it
    total for values pydantic may hand through (Path, datetime). The result is
    truncated (see ``FINGERPRINT_CHARS``) and prefixed so a log line says what it
    is.
    """
    payload = json.dumps(args if args is not None else {}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
    return f"sha256:{digest}"


class ApprovalRecord(BaseModel):
    """One approval request/grant for a privileged tool call.

    Bound to more than a tool name, because "approve this" must mean *this*:

    * ``tool_name``     — which tool;
    * ``args_hash``     — the exact arguments (see :func:`args_fingerprint`);
    * ``thread_id`` / ``turn_id`` — which conversation and which turn asked, so a
      grant cannot silently carry over into another session's call;
    * ``expires_at``    — a bounded window.

    ``args_summary`` is the same arguments rendered for a human. Without it the
    only thing a person approving a ``raw_python`` call can see is a tool name and
    an opaque hash — which is not an approval, it is a signature on a blank page.
    """

    id: str
    tool_name: str
    args_hash: Optional[str] = None
    args_summary: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    requested_at: datetime
    expires_at: datetime
    granted: bool = False
    resolved_at: Optional[datetime] = None


@runtime_checkable
class ApprovalStore(Protocol):
    """What the rest of the system may rely on."""

    def request(
        self,
        tool_name: str,
        args_hash: Optional[str] = None,
        ttl_s: Optional[float] = None,
        *,
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        args_summary: Optional[str] = None,
    ) -> ApprovalRecord: ...

    def resolve(self, approval_id: str, granted: bool) -> Optional[ApprovalRecord]: ...

    def lookup_valid(
        self,
        tool_name: str,
        args_hash: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[ApprovalRecord]: ...

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
        self,
        tool_name: str,
        args_hash: Optional[str] = None,
        ttl_s: Optional[float] = None,
        *,
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        args_summary: Optional[str] = None,
    ) -> ApprovalRecord:
        ttl = self._ttl_s if ttl_s is None else float(ttl_s)
        now = datetime.now(timezone.utc)
        rec = ApprovalRecord(
            id=uuid.uuid4().hex,
            tool_name=tool_name,
            args_hash=args_hash,
            args_summary=args_summary,
            thread_id=thread_id,
            turn_id=turn_id,
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
        self,
        tool_name: str,
        args_hash: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[ApprovalRecord]:
        """A granted, unexpired approval matching tool (+ args, + session if asked).

        An unscoped record (``thread_id is None``, written before session binding
        existed) still matches any session — deliberately, so a pre-existing
        approvals file keeps working. A *scoped* record only matches its own
        session. Either way the argument digest, when the caller supplies one, is
        mandatory: a record with no ``args_hash`` never satisfies a hash-checked
        lookup.
        """
        now = datetime.now(timezone.utc)
        for r in self._load():
            if r.tool_name != tool_name:
                continue
            if args_hash is not None and r.args_hash != args_hash:
                continue
            if thread_id is not None and r.thread_id is not None and r.thread_id != thread_id:
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
