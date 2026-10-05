"""Immutable document bytes, addressed independently of path or attempt."""

import hashlib
import re
import tempfile
from pathlib import Path

from tcad.core.ids import contained_path


class DocumentStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "document_objects"

    def path(self, identity):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise ValueError("invalid document identity")
        return contained_path(self.root, identity[7:9], identity[7:])

    def put(self, raw):
        identity = "sha256:" + hashlib.sha256(raw).hexdigest()
        path = self.path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.read(identity)
            return identity
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(raw)
            candidate = Path(temporary.name)
        try:
            candidate.replace(path)
        finally:
            candidate.unlink(missing_ok=True)
        return identity

    def read(self, identity):
        raw = self.path(identity).read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError("document object failed its integrity check")
        return raw
