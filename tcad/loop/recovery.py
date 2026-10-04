"""Per-turn repeated-failure detection, separate from total work budgets.

Diagnostic reads do not make a rejected edit into progress. Conversely, an IR
change clears the failure history so the same operation can legitimately be
retried against the changed design. This guard grants no permissions and never
replays a write itself.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json


class RepeatedFailures:
    def __init__(self, limit: int | None = 3, *, max_entries: int = 128):
        self.limit = limit
        self.max_entries = max_entries
        self.clear()

    def clear(self) -> None:
        self.version = None
        self._counts: OrderedDict[str, int] = OrderedDict()

    def record(self, name: str, args: dict, error, version: int | None) -> int:
        """Return repeated count; 0 for a disabled guard or non-error outcome."""
        if self.limit is None or error is None:
            return 0
        if self.version != version:
            self._counts.clear()
            self.version = version
        # A commit message does not change the attempted CAD operation. Letting
        # fresh narration reset this guard would make it trivially ineffective.
        meaningful_args = {} if name == "ir_commit" else args
        fingerprint = hashlib.sha256(json.dumps(
            [name, meaningful_args, str(error.kind), error.message, error.feature_id],
            sort_keys=True, ensure_ascii=False, default=str,
        ).encode()).hexdigest()
        count = self._counts.pop(fingerprint, 0) + 1
        self._counts[fingerprint] = count
        while len(self._counts) > self.max_entries:
            self._counts.popitem(last=False)
        return count
