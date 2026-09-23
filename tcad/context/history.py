"""Session history -> budgeted :class:`Message` list (task book §5-D).

The session database has always stored the conversation; until now nothing read
it back into a model request, so "the history is in SQLite" and "the model was
told the history" were two different facts. This module is the reader that makes
them the same fact.

What is *not* here
------------------
No summarisation. When the history does not fit, the caller composes this with
:class:`tcad.context.assembler.ContextAssembler`, which owns the three-level
degradation and the injected summariser. Splitting the two keeps this module a
pure, synchronous, trivially testable projection.

The current message is excluded
--------------------------------
The HTTP front end persists the incoming user message *before* the turn starts
(``app.py`` ``remember_user()``). If that message were also replayed from
history the model would see it twice — once in the replayed transcript and once
as the turn's own user message the engine appends — which reads as the user
having said the same thing twice. Callers pass the current text so the trailing
copy can be dropped.
"""

from __future__ import annotations

from typing import Any, Iterable

from tcad.context.compactor import Message

#: Roles a chat model accepts in a replayed transcript. Anything else (a stray
#: ``tool`` row, a future role) is skipped rather than forwarded: an unknown role
#: makes the provider reject the whole request, which would look like the model
#: being broken rather than the transcript being wrong.
_REPLAYABLE_ROLES = frozenset({"user", "assistant"})


def messages_from_rows(
    rows: Iterable[dict[str, Any]] | Iterable[Any],
    *,
    current_text: str | None = None,
) -> list[Message]:
    """Turn ``SessionDB.list_messages`` rows into replayable messages.

    ``current_text`` drops one trailing user message equal to it (see the module
    docstring). Only the *last* row is considered, so a genuine earlier repeat of
    the same sentence survived from an earlier turn is kept.
    """
    records = [_row_to_dict(r) for r in rows]
    records = [r for r in records if r is not None]

    if current_text is not None and records:
        last = records[-1]
        if last.get("role") == "user" and (last.get("content") or "") == current_text:
            records = records[:-1]

    out: list[Message] = []
    for rec in records:
        role = str(rec.get("role") or "")
        content = rec.get("content") or ""
        if role not in _REPLAYABLE_ROLES or not content.strip():
            continue
        out.append(
            Message(
                role=role,
                content=content,
                kind="history",
                tokens_estimate=max(1, len(content) // 4),
            )
        )
    return out


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, dict):
        return row
    # sqlite3.Row supports mapping access; anything else is not a row we know.
    try:
        return {k: row[k] for k in row.keys()}  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def load_thread_history(
    session_db: Any,
    thread_id: str,
    *,
    current_text: str | None = None,
    limit: int = 500,
) -> list[Message]:
    """Read a thread's transcript and project it to replayable messages.

    Never raises: a conversation store that is missing, closed or erroring means
    "no history available", and a turn should still run with the context it can
    get. A missing transcript is a degradation, not a build failure.
    """
    if session_db is None or not thread_id:
        return []
    try:
        rows = session_db.list_messages(thread_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return messages_from_rows(rows, current_text=current_text)
