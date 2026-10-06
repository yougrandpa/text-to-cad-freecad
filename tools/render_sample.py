"""Render the REAL meshed geometry to PNGs — closes the visual-feedback loop.

Builds the reference part in a live FreeCADCmd worker, pulls the triangle mesh
across the process boundary, rasterises it in the supervisor, and writes the four
standard views to ``output/renders/``.

Run from anywhere:
    .venv/bin/python tools/render_sample.py

Requires a built FreeCADCmd (free-cad/FreeCAD/build/debug/bin/FreeCADCmd).
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcad.core.types import BBox, Mesh
from tcad.core.worker_client import WorkerHandle
from tcad.render.png import backend_in_use, write_png
from tcad.render.raster import render_views
from tests.contract.test_e2e_pipeline import FREECAD_CMD, make_ir

out = REPO_ROOT / "output" / "renders"
out.mkdir(parents=True, exist_ok=True)

handle = WorkerHandle(FREECAD_CMD, REPO_ROOT, worker_id="rnd",
                      startup_timeout_s=180, request_timeout_s=180)
handle.start()
ir = json.loads(make_ir(model_id="bracket").model_dump_json())
res = handle.request_sync("tessellate", {"ir": ir, "out_dir": str(out)})
handle.close()

m = res["mesh"]
bb = m.get("bbox") or {}
mesh = Mesh(
    vertices=[tuple(v) for v in m["vertices"]],
    facets=[tuple(f) for f in m["facets"]],
    bbox=BBox(**{k: float(bb.get(k, 0.0)) for k in
                 ("x", "y", "z", "x_min", "y_min", "z_min")}),
    volume=float(m.get("volume", 0.0)),
    tolerance=float(m.get("tolerance", 0.5)),
)
print(f"mesh: {len(mesh.vertices)} verts / {len(mesh.facets)} facets | bbox="
      f"({mesh.bbox.x}x{mesh.bbox.y}x{mesh.bbox.z}) | png backend: {backend_in_use()}")

imgs = render_views(mesh, views=["iso", "front", "top", "right"],
                    width=640, height=480, style="flat_edges", supersample=2)
for view, arr in imgs.items():
    path = out / f"{view}.png"
    w, h = write_png(arr, str(path))
    ink = float((arr < 250).mean())
    print(f"  {view:6s} -> {path}  {w}x{h}  std={arr.std():6.2f}  ink={ink:6.1%}")
