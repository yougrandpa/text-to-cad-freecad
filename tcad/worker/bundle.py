"""One sequential PartDesign build produces all geometry evidence."""

import json
import os

from tcad.worker.compiler import _build, _close_doc
from tcad.worker.exporters import export_built
from tcad.worker.introspect import digest_built
from tcad.worker.artifact_scene import read_artifact_scene
from tcad.worker.assembly import simulate_assembly


def build_artifacts(ir, out_dir, exports=None, **_extra):
    os.makedirs(out_dir, exist_ok=True)
    built = _build(ir, out_dir, _extra.get("components"))
    try:
        if built["errors"]:
            return {"ok": False, "errors": built["errors"]}
        exported = export_built(ir, built, out_dir, sorted(set(exports or []) | {"fcstd"}))
        if not exported["ok"]:
            return exported
        digest = digest_built(ir, built)
        with open(os.path.join(out_dir, "digest.json"), "w", encoding="utf-8") as output:
            json.dump(digest, output, ensure_ascii=False, allow_nan=False)
        if ir.get("assembly"):
            if ir["assembly"].get("rotation"):
                from tcad.ir.pendulum import gravity_frames
                scene = read_artifact_scene(exported["files"]["fcstd"], ir["bodies"])
                from tcad.worker.rotary import measured_rotation
                measured = measured_rotation(ir['assembly'], built['body_results'])
                scene.update(gravity_frames(measured, [b['id'] for b in ir['bodies']]))
            else:
                scene = simulate_assembly(ir=ir, out_dir=out_dir, _built=built,
                                          solve_only=not ir["assembly"]["drivers"])
        else:
            # Reopen the delivered document, not the author's live build state.
            scene = read_artifact_scene(exported["files"]["fcstd"], ir["bodies"])
        if not scene.get("ok"):
            return scene
        return {"ok": True, "scene": scene, "files": exported["files"]}
    finally:
        _close_doc(built["doc"])
