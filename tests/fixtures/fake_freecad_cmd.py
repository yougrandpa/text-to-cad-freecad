#!/usr/bin/env python3
"""A stand-in for FreeCADCmd, used to test the supervisor-side transport without
needing a FreeCAD build.

It deliberately mimics the parts of the real worker that have historically broken
JSONL transports:
  * prints a noisy banner on **stdout** before protocol traffic (FreeCAD does this)
  * prints unrelated chatter on **stderr**
  * answers with one compact JSON object per line
  * supports a ``die`` method that exits mid-flight (crash detection)
  * supports a ``slow`` method (timeout handling)
  * rejects unknown methods and returns a structured error envelope

Usage: <python> fake_freecad_cmd.py --worker-id=test
"""

from __future__ import annotations

import json
import sys
import time

BANNER = [
    "FreeCAD 26.3.0, Libs: 26.3.0devR48708 (Git)",
    "(C) 2001-2026 FreeCAD contributors",
    "FreeCAD is free and open-source software licensed under the terms of LGPL2+ license.",
]


def main() -> int:
    worker_id = "fake"
    for arg in sys.argv[1:]:
        if arg.startswith("--worker-id="):
            worker_id = arg.split("=", 1)[1]

    for line in BANNER:
        print(line)
    sys.stdout.flush()
    print(f"fake worker {worker_id} starting", file=sys.stderr)
    sys.stderr.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "ping":
            resp = {"id": req_id, "ok": True, "result": {"pong": True, "worker_id": worker_id}}
        elif method == "api_selftest":
            resp = {"id": req_id, "ok": True, "result": {"ok": True, "checks": [], "missing": []}}
        elif method == "die":
            print("fake worker dying on request", file=sys.stderr)
            sys.stderr.flush()
            return 7
        elif method == "slow":
            time.sleep(float(params.get("seconds", 5)))
            resp = {"id": req_id, "ok": True, "result": {"slept": True}}
        elif method == "fail":
            resp = {
                "id": req_id,
                "ok": False,
                "error": {
                    "kind": params.get("kind", "compile"),
                    "message": params.get("message", "boom"),
                    "feature_id": params.get("feature_id"),
                    "traceback": "Traceback (most recent call last):\n  ...\n",
                },
            }
        elif method == "nonsense_frame":
            # Emit a non-JSON line then a valid response — the reader must survive.
            print("this line is not JSON at all")
            sys.stdout.flush()
            resp = {"id": req_id, "ok": True, "result": {"survived": True}}
        else:
            resp = {
                "id": req_id,
                "ok": False,
                "error": {"kind": "not_found", "message": f"unknown method {method!r}"},
            }

        print(json.dumps(resp, separators=(",", ":")))
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
