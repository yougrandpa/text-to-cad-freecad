"""The same camera and shading contract used by browser Viewer Core."""

import hashlib
import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "viewer" / "core" / "render-contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_bytes())
_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = [CONTRACT_PATH, *sorted((_ROOT / "render").glob("*.py")),
            _ROOT / "ir" / "animation.py", _ROOT / "ir" / "motion.py",
            *sorted((_ROOT / "viewer").rglob("*.js"))]
RENDER_IDENTITY = hashlib.sha256(b"".join(path.read_bytes() for path in _SOURCES)).hexdigest()
