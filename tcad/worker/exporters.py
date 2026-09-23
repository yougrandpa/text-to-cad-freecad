"""Export artifacts (step/stl/brep/fcstd) from the built document.

Runs inside FreeCADCmd. The document is rebuilt from the IR each call (the IR is
the single source of truth). exportStep/exportStl/exportBrep operate on the
TopoShape; fcstd uses doc.saveAs.
"""

from __future__ import annotations

import os
import re
import tempfile

from tcad.worker.compiler import _build, _close_doc
from tcad.worker.protocol import EXPORT_FORMATS

# Mirrors ``tcad/core/ids.py``: the worker runs under an import ban (stdlib +
# FreeCAD only, see compiler.py), so the rule is duplicated here rather than
# imported. ``tests/unit/test_ids.py`` asserts the two agree, so they cannot
# drift into "the API validated one thing and the writer wrote another".
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_component(value: object, *, kind: str) -> str:
    """The name must be a single path component, or the export is refused.

    This is the last line before bytes hit the disk: even if a caller skipped the
    supervisor-side check, ``name = "../../x"`` must not be able to write outside
    ``out_dir``.
    """
    if not isinstance(value, str) or not _SAFE_COMPONENT_RE.match(value):
        raise ValueError(
            f"unsafe {kind}: {value!r}; expected letters/digits/'.'/'_'/'-', "
            "1-64 chars, no path separators"
        )
    return value


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
    # Never export a shape the build itself failed to produce: a stale or
    # partial Tip must not be delivered as a valid artefact.
    if built["errors"]:
        _close_doc(built["doc"])
        return {"ok": False, "files": {}, "errors": built["errors"]}
    doc = built["doc"]
    shape = built["result_shape"]

    files: dict = {}
    errors: list = []
    try:
        model_id = _safe_component(ir.get("model_id") or "model", kind="model_id")
    except ValueError as exc:
        _close_doc(built["doc"])
        return {"ok": False, "files": {},
                "errors": [{"kind": "schema", "feature_id": None, "message": str(exc)}]}

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
