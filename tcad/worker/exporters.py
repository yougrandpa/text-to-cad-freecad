"""Export artifacts (step/stl/brep/fcstd) from the built document.

Runs inside FreeCADCmd. The document is rebuilt from the IR each call (the IR is
the single source of truth). exportStep/exportStl/exportBrep operate on the
TopoShape; fcstd uses doc.saveAs.
"""

from __future__ import annotations

import os
import tempfile

from tcad.worker.compiler import _build, _close_doc
from tcad.worker.protocol import EXPORT_FORMATS


def export_artifacts(ir: dict | None = None, out_dir: str = "", exports=None,
                     **_extra) -> dict:
    """Write the requested export formats. ``exports`` is a list drawn from
    EXPORT_FORMATS. Returns {"ok", "files": {fmt: path}, "errors": [...]}."""
    if not ir:
        return {"ok": False, "files": {}, "errors": [{"kind": "schema",
                "feature_id": None, "message": "missing ir"}]}

    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="tcad_export_")
    os.makedirs(out_dir, exist_ok=True)

    if exports is None:
        exports = ["step"]
    exports = [e for e in exports if e in EXPORT_FORMATS]

    built = _build(ir, out_dir)
    doc = built["doc"]
    shape = built["result_shape"]

    files: dict = {}
    errors: list = []
    model_id = ir.get("model_id") or "model"

    for fmt in exports:
        try:
            if fmt == "fcstd":
                path = os.path.join(out_dir, f"{model_id}.FCStd")
                doc.saveAs(path)
            else:
                if shape is None or shape.isNull():
                    errors.append({"kind": "compile", "feature_id": None,
                                   "message": f"no solid to export as {fmt}"})
                    continue
                path = os.path.join(out_dir, f"{model_id}.{fmt}")
                if fmt == "step":
                    shape.exportStep(path)
                elif fmt == "stl":
                    shape.exportStl(path)
                elif fmt == "brep":
                    shape.exportBrep(path)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                files[fmt] = path
            else:
                errors.append({"kind": "runtime", "feature_id": None,
                               "message": f"{fmt} export produced no file: {path}"})
        except Exception as exc:  # noqa: BLE001
            errors.append({"kind": "runtime", "feature_id": None,
                           "message": f"{fmt} export failed: {type(exc).__name__}: {exc}"})

    _close_doc(doc)
    return {"ok": len(errors) == 0 and len(files) > 0, "files": files, "errors": errors}
