"""Worker entry point — executed by FreeCADCmd as a positional input-file.

FreeCADCmd runs this via processFiles() -> runFile(). After it returns, the
process would normally exit, so we enter a blocking JSON-Lines RPC loop over
stdin/stdout. The supervisor keeps the pipe open; when it closes stdin (EOF), we
exit cleanly.

Worker start command (verified, review/history/02-架构设计.md §3.2):
    FreeCADCmd --console -P <cwd> <cwd>/tcad/worker/bootstrap.py --worker-id=w0

IMPORT BAN: stdlib + FreeCAD only. We import rpc/protocol which are also
stdlib-only (rpc imports the sibling worker modules, none of which import
pydantic/numpy).

NOTE: FreeCAD prints its own start-up banner to stdout. The supervisor's reader
skips non-JSON lines. Our own writes are strictly one JSON object per line,
flushed immediately.
"""

from __future__ import annotations

import sys

from tcad.worker.protocol import decode_line, encode_line
from tcad.worker.rpc import dispatch


def _parse_worker_id(argv) -> str:
    for a in argv:
        if a.startswith("--worker-id="):
            return a.split("=", 1)[1]
    return "w0"


def main() -> int:
    worker_id = _parse_worker_id(sys.argv)
    # Announce readiness on a clearly non-JSON marker line the supervisor can
    # await (it still skips lines that fail JSON parsing).
    sys.stdout.write(f"###WORKER_READY### worker_id={worker_id}\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = decode_line(line)
        except Exception:
            # Not JSON (e.g. a stray FreeCAD banner line) — skip it.
            continue
        if not isinstance(request, dict):
            continue
        response = dispatch(request)
        sys.stdout.write(encode_line(response))
        sys.stdout.flush()

    # stdin EOF -> clean exit.
    sys.stdout.write("###WORKER_EOF###\n")
    sys.stdout.flush()
    return 0


# FreeCADCmd's runFile() executes this module top-to-bottom with __name__ set
# to "__main__", exactly like `python bootstrap.py`. Call main() unconditionally
# (matching the reference scripts tools/probes/smoke_freecad.py / probe2.py) so the
# RPC loop always starts. main() blocks until stdin EOF, then returns.
sys.exit(main())
