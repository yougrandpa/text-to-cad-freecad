"""Read a saved build document into a scene; never compile authoring IR.

The scene must never be the reason a valid CAD build is lost. When the
viewport budget cannot be met even at the coarsest preview tolerance, the
result says so (`preview.status == "unavailable"`) and returns no mesh, while
the document, its exports and its parametric history stay exactly where the
build put them.
"""

import FreeCAD
import Part

from tcad.worker.compiler import _close_doc, _measure, _obj_name
from tcad.worker.preview import adaptive_pick_mesh, summarize


def read_artifact_scene(fcstd_path, bodies, tolerance=0.5, **_extra):
    doc = FreeCAD.openDocument(fcstd_path)
    try:
        indexed_inputs, motion, shapes = [], [], []
        for body in bodies:
            obj = doc.getObject(_obj_name(body.get("id"), body.get("name")))
            if obj is None or obj.TypeId != "PartDesign::Body" or obj.Shape.isNull():
                raise ValueError(f"saved artifact has no body {body['id']}")
            shape = obj.Shape
            shapes.append(shape)
            indexed_inputs.append((body["id"], shape))
            if body.get("motion"):
                motion.append({"body_id": body["id"], **body["motion"]})
        if not shapes:
            raise ValueError("saved artifact has no solid to display")
        shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
        measure = _measure(shape)

        result = adaptive_pick_mesh(indexed_inputs, float(tolerance))
        preview = summarize(result)
        scene = {"ok": True, "preview": preview, "motion": [], "parts": [],
                 "pick_mapping": None, "mesh": None}
        if preview["status"] == "unavailable":
            return scene
        indexed, chosen = result["pick_mesh"], result["tolerance"]
        by_id = {part["body_id"]: part for part in indexed.parts}
        for entry in motion:
            part = by_id[entry["body_id"]]
            entry["vertex_start"] = part["vertex_start"]
            entry["vertex_count"] = part["vertex_count"]
        scene.update({
            "mesh": {"vertices": indexed.vertices, "facets": indexed.facets,
                     "bbox": measure["bbox"], "volume": measure["volume"],
                     "tolerance": chosen},
            "motion": motion,
            "parts": indexed.parts,
            "pick_mapping": indexed.mapping(),
        })
        return scene
    finally:
        _close_doc(doc)
