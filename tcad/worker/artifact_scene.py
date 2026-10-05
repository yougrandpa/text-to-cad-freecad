"""Read a saved build document into a scene; never compile authoring IR."""

import FreeCAD
import Part

from tcad.worker.compiler import _close_doc, _measure, _obj_name


def read_artifact_scene(fcstd_path, bodies, tolerance=0.5, **_extra):
    doc = FreeCAD.openDocument(fcstd_path)
    try:
        vertices, facets, shapes, motion, ranges = [], [], [], [], []
        for body in bodies:
            obj = doc.getObject(_obj_name(body.get("id"), body.get("name")))
            if obj is None or obj.TypeId != "PartDesign::Body" or obj.Shape.isNull():
                raise ValueError(f"saved artifact has no body {body['id']}")
            shape = obj.Shape
            shapes.append(shape)
            points, triangles = shape.tessellate(float(tolerance))
            start = len(vertices)
            vertices.extend([[float(p.x), float(p.y), float(p.z)] for p in points])
            facets.extend([[int(i) + start for i in face] for face in triangles])
            ranges.append({'body_id':body['id'], 'vertex_start':start, 'vertex_count':len(points)})
            if body.get("motion"):
                motion.append({"body_id": body["id"], "vertex_start": start,
                               "vertex_count": len(points), **body["motion"]})
        if not shapes:
            raise ValueError("saved artifact has no solid to display")
        shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
        measure = _measure(shape)
        return {"ok": True, "mesh": {"vertices": vertices, "facets": facets,
                "bbox": measure["bbox"], "volume": measure["volume"],
                "tolerance": float(tolerance)}, "motion": motion, "parts": ranges}
    finally:
        _close_doc(doc)
