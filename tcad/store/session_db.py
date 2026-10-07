"""SQLite session/index store (design §4.4, L3).

Holds threads / turns / steps / messages / token_usage / approvals. This is
*rebuildable, disposable* metadata — the IR itself lives in the event log + the
versioned snapshots, never here. So we can be relaxed about it: WAL mode +
single writer, short synchronous transactions, no held-across-await business.

Single-writer discipline: one ``sqlite3.Connection`` per ``SessionDB`` instance,
created with ``check_same_thread=True``. The loop owns one writer; workers never
touch this. WAL lets a reader (e.g. a dashboard) coexist without blocking the
writer.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tcad.core.types import Thread, Turn

_DEFAULT_SQLITE = "data/tcad.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_folders (
    folder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_threads (
    thread_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    model_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    context_state TEXT NOT NULL DEFAULT 'full'
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id           TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    state             TEXT NOT NULL,
    steps             INTEGER NOT NULL DEFAULT 0,
    tokens_in         INTEGER NOT NULL DEFAULT 0,
    tokens_out        INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT NOT NULL,
    base_ir_version   INTEGER NOT NULL,
    error             TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    step_id     TEXT PRIMARY KEY,
    turn_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    tool_name   TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    turn_id     TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    tokens      INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_thread   ON turns(thread_id);
CREATE INDEX IF NOT EXISTS idx_steps_turn     ON steps(turn_id);
CREATE INDEX IF NOT EXISTS idx_msgs_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_token_turn     ON token_usage(turn_id);
CREATE INDEX IF NOT EXISTS idx_appr_thread    ON approvals(thread_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SessionDB:
    def __init__(
        self,
        sqlite_path: str | os.PathLike[str] = _DEFAULT_SQLITE,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A sqlite3 connection is thread-bound by default. The harness core is
        # single-threaded, so the default keeps that guarantee. The HTTP front
        # end is not: uvicorn runs sync endpoints on a worker thread pool, so it
        # opens the connection with ``check_same_thread=False`` and relies on
        # ``_lock`` below to serialise access. WAL means a reader never blocks
        # the writer, but the *connection object* still needs one-at-a-time use.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=check_same_thread)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Upgrade existing histories in place; old conversations stay unfiled.
        # Serialise the inspection too: simultaneous first HTTP reads can open
        # two connections against an old database.
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(threads)")}
            if "folder_id" not in columns:
                self._conn.execute("ALTER TABLE threads ADD COLUMN folder_id TEXT")
            if "archived" not in columns:
                self._conn.execute("ALTER TABLE threads ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    # ── context ───────────────────────────────────────────────────────────────

    @contextmanager
    def _tx(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── threads ─────────────────────────────────────────────────────────────────

    def create_thread(self, model_id: str, thread_id: str | None = None,
                      context_state: str = "full", folder_id: str | None = None) -> Thread:
        tid = thread_id or _uid("thr")
        with self._tx() as cur:
            if cur.execute("SELECT 1 FROM deleted_threads WHERE thread_id=?", (tid,)).fetchone():
                raise ValueError("conversation has been deleted")
            if folder_id is not None and not cur.execute(
                "SELECT 1 FROM session_folders WHERE folder_id=?", (folder_id,)
            ).fetchone():
                raise KeyError(folder_id)
            cur.execute(
                "INSERT INTO threads(thread_id, model_id, created_at, context_state, folder_id) "
                "VALUES(?,?,?,?,?)",
                (tid, model_id, _now(), context_state, folder_id),
            )
        return Thread(thread_id=tid, model_id=model_id, context_state=context_state)  # type: ignore[arg-type]

    def get_thread(self, thread_id: str) -> Thread | None:
        row = self._fetchone(
            "SELECT * FROM threads WHERE thread_id=?", (thread_id,))
        if row is None:
            return None
        return Thread(
            thread_id=row["thread_id"], model_id=row["model_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            context_state=row["context_state"])  # type: ignore[arg-type]

    def is_deleted(self, thread_id: str) -> bool:
        return self._fetchone("SELECT 1 FROM deleted_threads WHERE thread_id=?", (thread_id,)) is not None

    def list_folders(self) -> list[dict]:
        return [dict(row) for row in self._fetchall(
            "SELECT * FROM session_folders ORDER BY created_at, rowid")]

    def save_folder(self, name: str, folder_id: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("文件夹名称须为 1–60 个字符")
        fid = folder_id or _uid("fld")
        with self._tx() as cur:
            try:
                if folder_id is None:
                    cur.execute("INSERT INTO session_folders VALUES(?,?,?)", (fid, name, _now()))
                else:
                    cur.execute("UPDATE session_folders SET name=? WHERE folder_id=?", (name, fid))
                    if not cur.rowcount:
                        raise KeyError(fid)
            except sqlite3.IntegrityError as exc:
                raise ValueError("已存在同名文件夹") from exc
            return dict(cur.execute("SELECT * FROM session_folders WHERE folder_id=?", (fid,)).fetchone())

    def delete_folder(self, folder_id: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM session_folders WHERE folder_id=?", (folder_id,))
            if not cur.rowcount:
                raise KeyError(folder_id)
            cur.execute("UPDATE threads SET folder_id=NULL WHERE folder_id=?", (folder_id,))

    def update_thread(self, thread_id: str, changes: dict) -> None:
        if not changes or set(changes) - {"folder_id", "archived"}:
            raise ValueError("请选择文件夹或归档状态")
        with self._tx() as cur:
            if not cur.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)).fetchone():
                raise KeyError(thread_id)
            folder_id = changes.get("folder_id")
            if folder_id is not None and not cur.execute(
                "SELECT 1 FROM session_folders WHERE folder_id=?", (folder_id,)
            ).fetchone():
                raise KeyError(folder_id)
            fields = ", ".join(f"{key}=?" for key in changes)
            cur.execute(f"UPDATE threads SET {fields} WHERE thread_id=?", (*changes.values(), thread_id))

    def delete_thread(self, thread_id: str) -> None:
        """Delete conversation records atomically; versioned CAD files remain."""
        with self._tx() as cur:
            if not cur.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)).fetchone():
                raise KeyError(thread_id)
            for table in ("steps", "token_usage"):
                cur.execute(f"DELETE FROM {table} WHERE turn_id IN "
                            "(SELECT turn_id FROM turns WHERE thread_id=?)", (thread_id,))
            for table in ("approvals", "messages", "turns", "threads"):
                cur.execute(f"DELETE FROM {table} WHERE thread_id=?", (thread_id,))
            cur.execute("INSERT INTO deleted_threads VALUES(?)", (thread_id,))

    # ── turns ───────────────────────────────────────────────────────────────────

    def start_turn(self, thread_id: str, kind: str, base_ir_version: int,
                   turn_id: str | None = None) -> Turn:
        tid = turn_id or _uid("trn")
        with self._tx() as cur:
            # The model identity lives on the thread (single source of truth);
            # the Turn carries a copy so every collaborator that only receives a
            # Turn still knows which model it is operating on.
            row = cur.execute(
                "SELECT model_id FROM threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no such thread: {thread_id!r}")
            model_id = row["model_id"]
            cur.execute(
                "INSERT INTO turns(turn_id, thread_id, kind, state, started_at, "
                "base_ir_version) VALUES(?,?,?,?,?,?)",
                (tid, thread_id, kind, "running", _now(), base_ir_version),
            )
        return Turn(turn_id=tid, thread_id=thread_id, model_id=model_id,
                    kind=kind,  # type: ignore[arg-type]
                    state="running", started_at=datetime.now(timezone.utc),  # type: ignore[arg-type]
                    base_ir_version=base_ir_version)

    def finish_turn(self, turn_id: str, state: str | None = None,
                    error: str | None = None) -> None:
        with self._tx() as cur:
            if error is not None:
                cur.execute("UPDATE turns SET error=? WHERE turn_id=?",
                            (error, turn_id))
            if state is not None:
                cur.execute("UPDATE turns SET state=? WHERE turn_id=?",
                            (state, turn_id))

    def update_turn_tokens(self, turn_id: str, tokens_in: int = 0,
                           tokens_out: int = 0) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE turns SET tokens_in=tokens_in+?, tokens_out=tokens_out+? "
                "WHERE turn_id=?", (tokens_in, tokens_out, turn_id))

    # ── steps ───────────────────────────────────────────────────────────────────

    def record_step(self, turn_id: str, tool_name: str | None = None,
                    step_id: str | None = None, status: str = "ok") -> str:
        sid = step_id or _uid("stp")
        # Read and write inside one lock: computing idx outside it would let two
        # concurrent steps claim the same index.
        with self._tx() as cur:
            idx = cur.execute(
                "SELECT COALESCE(MAX(idx),-1)+1 AS n FROM steps WHERE turn_id=?",
                (turn_id,)).fetchone()["n"]
            cur.execute(
                "INSERT INTO steps(step_id, turn_id, idx, started_at, status, tool_name) "
                "VALUES(?,?,?,?,?,?)",
                (sid, turn_id, idx, _now(), status, tool_name))
            cur.execute("UPDATE turns SET steps=steps+1 WHERE turn_id=?", (turn_id,))
        return sid

    # ── messages ────────────────────────────────────────────────────────────────

    def add_message(self, thread_id: str, role: str, content: str,
                    turn_id: str | None = None) -> str:
        mid = _uid("msg")
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO messages(message_id, thread_id, turn_id, role, content, "
                "created_at) VALUES(?,?,?,?,?,?)",
                (mid, thread_id, turn_id, role, content, _now()))
        return mid

    # ── token usage ─────────────────────────────────────────────────────────────

    def add_token_usage(self, turn_id: str, kind: str, tokens: int) -> None:
        """``kind`` is typically ``"in"`` or ``"out"``."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO token_usage(turn_id, kind, tokens, created_at) "
                "VALUES(?,?,?,?)", (turn_id, kind, tokens, _now()))

    def sum_token_usage(self, turn_id: str) -> dict[str, int]:
        """Return ``{"in": int, "out": int, "total": int}`` for the turn."""
        rows = self._fetchall(
            "SELECT kind, SUM(tokens) AS t FROM token_usage WHERE turn_id=? "
            "GROUP BY kind", (turn_id,))
        out: dict[str, int] = {"in": 0, "out": 0, "total": 0}
        for r in rows:
            k = r["kind"]
            v = int(r["t"] or 0)
            if k in ("in", "out"):
                out[k] = v
            out["total"] += v
        return out

    # ── approvals ───────────────────────────────────────────────────────────────

    def request_approval(self, thread_id: str, turn_id: str, tool_name: str,
                         approval_id: str | None = None) -> str:
        aid = approval_id or _uid("apr")
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO approvals(approval_id, thread_id, turn_id, tool_name, "
                "status, created_at) VALUES(?,?,?,?,?,?)",
                (aid, thread_id, turn_id, tool_name, "pending", _now()))
        return aid

    def resolve_approval(self, approval_id: str, granted: bool, note: str | None = None) -> None:
        status = "granted" if granted else "denied"
        with self._tx() as cur:
            cur.execute(
                "UPDATE approvals SET status=?, resolved_at=?, note=? WHERE approval_id=?",
                (status, _now(), note, approval_id))

    def get_pending_approvals(self, thread_id: str | None = None) -> list[dict]:
        if thread_id is None:
            rows = self._fetchall(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at")
        else:
            rows = self._fetchall(
                "SELECT * FROM approvals WHERE status='pending' AND thread_id=? "
                "ORDER BY created_at", (thread_id,))
        return [dict(r) for r in rows]

    # ── conversation history ────────────────────────────────────────────────────

    def list_threads(self, model_id: str | None = None, limit: int | None = 100,
                     *, archived: bool | None = None) -> list[dict]:
        """Threads, most recently active first — what a UI needs to offer "resume".

        Ordering is by last activity — the newest message, or the thread's own
        creation time when it has none yet. That second case is what makes "new
        session" land at the top of the list rather than below every conversation
        that has ever been used, which is where an empty thread would otherwise
        sort.

        Sorting on the ISO-8601 text is exact rather than approximate: ``_now()``
        writes UTC with six-digit microseconds and a fixed ``+00:00`` offset, so
        every value is the same width and lexicographic order equals time order.

        ``title`` is the first user message: a thread has no name column, and
        asking someone to name a conversation before it has any content is
        backwards. ``last_message`` is the preview. Both come back whole;
        truncation is a display decision.
        """
        filters, values = [], []
        if model_id:
            filters.append("t.model_id=?")
            values.append(model_id)
        if archived is not None:
            filters.append("t.archived=?")
            values.append(int(archived))
        where = "WHERE " + " AND ".join(filters) if filters else ""
        params = (*values, int(limit) if limit is not None else -1)
        rows = self._fetchall(
            "SELECT t.thread_id, t.model_id, t.created_at, t.context_state, t.folder_id, t.archived, "
            "  (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.thread_id) AS messages, "
            "  (SELECT m.content FROM messages m WHERE m.thread_id=t.thread_id "
            "     ORDER BY m.rowid DESC LIMIT 1) AS last_message, "
            "  (SELECT m.content FROM messages m WHERE m.thread_id=t.thread_id "
            "     AND m.role='user' ORDER BY m.rowid ASC LIMIT 1) AS title, "
            "  COALESCE((SELECT MAX(m.created_at) FROM messages m "
            "              WHERE m.thread_id=t.thread_id), t.created_at) AS last_at "
            f"FROM threads t {where} "
            "ORDER BY last_at DESC, t.rowid DESC LIMIT ?",
            params,
        )
        return [{**dict(r), "archived": bool(r["archived"])} for r in rows]

    def list_messages(self, thread_id: str, limit: int = 500) -> list[dict]:
        """Messages in arrival order.

        Ordered by ``rowid`` rather than ``created_at``: the timestamp has
        one-second resolution, so a user message and the assistant's reply to it
        routinely share a value and would come back in arbitrary order.
        """
        rows = self._fetchall(
            "SELECT message_id, thread_id, turn_id, role, content, created_at "
            "FROM messages WHERE thread_id=? ORDER BY rowid ASC LIMIT ?",
            (thread_id, int(limit)))
        return [dict(r) for r in rows]
