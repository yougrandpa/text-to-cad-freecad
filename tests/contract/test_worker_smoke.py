"""Contract test: drive the real FreeCADCmd worker over JSON-Lines stdin/stdout.

Spawns ``free-cad/FreeCAD/build/debug/bin/FreeCADCmd --console`` with
``tcad/worker/bootstrap.py`` as the run-file, then sends a real ``compile_ir``
for a 60x40 rectangle padded 10 mm and asserts the golden measurements:

  * 6 faces, ShapeType "Solid", volume 24000.0, isValid True, 1 solid
  * STEP round-trip relative volume error < 1e-6
  * tessellate -> >= 8 vertices / >= 12 facets
  * api_selftest -> ok

Run with:
    cd /Users/slg/workspace/text_to_cad && \
    .venv/bin/python -m pytest tests/contract/test_worker_smoke.py -v

This is a @pytest.mark.contract test (see pyproject.toml markers) — it needs a
real FreeCADCmd build and is intentionally slow.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FREECAD_CMD = os.environ.get(
    "TCAD_FREECAD_CMD",
    os.path.join(REPO_ROOT, "free-cad", "FreeCAD", "build", "debug", "bin", "FreeCADCmd"),
)
BOOTSTRAP = os.path.join(REPO_ROOT, "tcad", "worker", "bootstrap.py")

WORKER_TIMEOUT_S = 120.0
PER_REQUEST_TIMEOUT_S = 90.0


def _rect_pad_ir() -> dict:
    """60 (x) by 40 (y) rectangle, fully constrained, padded 10 mm."""
    return {
        "schema_version": 1,
        "model_id": "rect_pad",
        "version": 0,
        "units": "mm",
        "bodies": [
            {
                "id": "body0",
                "name": "Body",
                "sketches": [
                    {
                        "id": "sk0",
                        "name": "base_sketch",
                        "plane": {"kind": "origin_plane", "plane": "XY"},
                        "map_mode": "FlatFace",
                        "geometry": [
                            {"id": "g0", "kind": "line",
                             "points": [{"x": 0, "y": 0, "z": 0}, {"x": 60, "y": 0, "z": 0}]},
                            {"id": "g1", "kind": "line",
                             "points": [{"x": 60, "y": 0, "z": 0}, {"x": 60, "y": 40, "z": 0}]},
                            {"id": "g2", "kind": "line",
                             "points": [{"x": 60, "y": 40, "z": 0}, {"x": 0, "y": 40, "z": 0}]},
                            {"id": "g3", "kind": "line",
                             "points": [{"x": 0, "y": 40, "z": 0}, {"x": 0, "y": 0, "z": 0}]},
                        ],
                        "constraints": [
                            {"type": "Coincident", "refs": [0, 1, -1, 1]},
                            {"type": "Coincident", "refs": [0, 2, 1, 1]},
                            {"type": "Coincident", "refs": [1, 2, 2, 1]},
                            {"type": "Coincident", "refs": [2, 2, 3, 1]},
                            {"type": "Coincident", "refs": [3, 2, 0, 1]},
                            {"type": "Horizontal", "refs": [0]},
                            {"type": "Horizontal", "refs": [2]},
                            {"type": "Vertical", "refs": [1]},
                            {"type": "Vertical", "refs": [3]},
                            {"type": "DistanceX", "refs": [0, 2], "value": 60.0},
                            {"type": "DistanceY", "refs": [1, 2], "value": 40.0},
                        ],
                    }
                ],
                "features": [
                    {"id": "pad0", "name": "pad", "op": "pad",
                     "profile_sketch": "sk0", "params": {"length": 10.0, "type": "Length"}},
                ],
            }
        ],
        "requirements": {"raw_text": "", "constraints": []},
        "notes": [],
    }


@pytest.mark.contract
@pytest.mark.skipif(
    not os.path.exists(FREECAD_CMD),
    reason="FreeCADCmd build not found (free-cad/FreeCAD/build/debug/bin/FreeCADCmd)",
)
def test_worker_compile_introspect_tessellate():
    if not os.path.exists(BOOTSTRAP):
        pytest.fail(f"worker bootstrap missing: {BOOTSTRAP}")

    out_dir = tempfile.mkdtemp(prefix="tcad_contract_")
    ir = _rect_pad_ir()

    proc = subprocess.Popen(
        [FREECAD_CMD, "--console", "-P", REPO_ROOT, BOOTSTRAP, "--pass", "--worker-id=w0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(os.path.join(out_dir, "worker_stderr.log"), "w"),
        text=True,
        cwd=REPO_ROOT,
        bufsize=1,
    )

    lines_q: "queue.Queue[str]" = queue.Queue()
    reader_error: list = []

    def _reader():
        try:
            for line in proc.stdout:
                lines_q.put(line)
        except Exception as exc:  # noqa: BLE001
            reader_error.append(exc)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    sent_ids = []

    def send(method, params):
        req_id = len(sent_ids) + 1
        sent_ids.append(req_id)
        req = {"id": req_id, "method": method, "params": params}
        proc.stdin.write(json.dumps(req, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        deadline = time.time() + PER_REQUEST_TIMEOUT_S
        while time.time() < deadline:
            try:
                line = lines_q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    pytest.fail(f"worker exited early (method={method})")
                continue
            line = line.strip()
            if not line or not line.startswith("{"):
                continue  # skip banner / ###WORKER_* lines
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == req_id:
                return resp
        pytest.fail(f"no response for method={method} (id={req_id}) within "
                    f"{PER_REQUEST_TIMEOUT_S}s; reader_error={reader_error}")

    try:
        # 1) compile_ir — the golden assertions live here.
        resp = send("compile_ir", {"ir": ir, "out_dir": out_dir})
        assert resp["ok"] is True, f"compile_ir envelope not ok: {resp}"
        res = resp["result"]
        assert res["ok"] is True, f"compile_ir result not ok: {res}"
        assert res["errors"] == [], f"compile errors: {res['errors']}"

        m = res["measurements"]
        assert m["faces"] == 6, f"faces={m['faces']} (expected 6)"
        assert m["shape_type"] == "Solid", f"shape_type={m['shape_type']}"
        assert m["volume"] == pytest.approx(24000.0, rel=1e-9), f"volume={m['volume']}"
        assert m["is_valid"] is True, f"is_valid={m['is_valid']}"
        assert m["solids"] == 1, f"solids={m['solids']}"

        rt = res["round_trip"]
        assert rt["ok"] is True, f"round_trip not ok: {rt}"
        assert rt["rel_err"] < 1e-6, f"STEP round-trip rel_err={rt['rel_err']}"

        # 2) tessellate — mesh density.
        resp = send("tessellate", {"ir": ir, "out_dir": out_dir})
        assert resp["ok"] is True, f"tessellate envelope not ok: {resp}"
        mesh = resp["result"]["mesh"]
        assert mesh["vertex_count"] >= 8, f"vertices={mesh['vertex_count']}"
        assert mesh["facet_count"] >= 12, f"facets={mesh['facet_count']}"

        # 3) api_selftest — every documented API still behaves.
        resp = send("api_selftest", {})
        assert resp["ok"] is True, f"api_selftest envelope not ok: {resp}"
        st = resp["result"]
        assert st["ok"] is True, f"api_selftest failed; missing={st['missing']}"
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=WORKER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
