"""Durable at-most-once chat execution, independent of cancellation IDs."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from tcad.selection.types import SelectionError


def fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class OperationStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "chat_operations.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                request_id TEXT NOT NULL, outcome TEXT)""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def read(self, operation_id: str, request_hash: str):
        with self.connection() as db:
            row = db.execute("SELECT fingerprint, request_id, outcome FROM operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise SelectionError("operation_conflict", "This operation ID belongs to a different request.")
        if row[2] is None:
            raise SelectionError("operation_in_progress", "Operation is running or interrupted by server exit; inspect before starting a new operation.")
        return {"request_id": row[1], **json.loads(row[2])}

    def begin(self, operation_id: str, request_hash: str, request_id: str):
        try:
            with self.connection() as db:
                db.execute("INSERT INTO operations VALUES (?, ?, ?, NULL)",
                           (operation_id, request_hash, request_id))
        except sqlite3.IntegrityError as exc:
            # Another process may have claimed the ID after read(). Never rerun.
            raise SelectionError("operation_in_progress", "Operation already claimed; retry its ID to read the outcome.") from exc

    def finish(self, operation_id: str, event: str, data: dict):
        with self.connection() as db:
            db.execute("UPDATE operations SET outcome=? WHERE operation_id=? AND outcome IS NULL",
                       (json.dumps({"event": event, "data": data}, ensure_ascii=False), operation_id))
