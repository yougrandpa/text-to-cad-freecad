"""Extract selection outlines from the frozen BRep, without recomputing IR."""

import FreeCAD
import Part

from tcad.worker.compiler import _close_doc, _obj_name


def _world_shape(obj):
    shape = obj.Shape.copy()
    # Shape already includes the object's placement; add only parent placement.
    parent = obj.getGlobalPlacement().multiply(obj.Placement.inverse())
    shape.Placement = parent.multiply(shape.Placement)
    return shape


def read_feature_highlight(fcstd_path, body, kind, node_id, tolerance=0.5, **_extra):
    doc = FreeCAD.openDocument(fcstd_path)
    try:
        definitions = body.get("sketches" if kind == "sketch" else "features", [])
        definition = next(item for item in definitions if item["id"] == node_id)
        if definition.get("suppress"):
            return {"ok": True, "vertices": []}
        obj = doc.getObject(_obj_name(node_id, definition.get("name")))
        if obj is None or not hasattr(obj, "Shape"):
            raise ValueError("selected node has no saved geometry")
        shape = _world_shape(obj)
        if kind == "feature" and shape.Solids:
            previous = None
            for item in definitions:
                if item["id"] == node_id:
                    break
                source = doc.getObject(_obj_name(item["id"], item.get("name")))
                if not item.get("suppress") and source is not None and source.Shape.Solids:
                    previous = _world_shape(source)
            # PartDesign feature Shapes are cumulative. Subtract the preceding
            # *surfaces*, rather than its volume, to retain cut walls as well as
            # added surfaces while removing unchanged or merely trimmed faces.
            shape = Part.makeCompound(shape.Faces)
            if previous is not None:
                shape = shape.cut(Part.makeCompound(previous.Faces))
            tip = doc.getObject(_obj_name(body["id"], body.get("name")))
            if tip is None:
                raise ValueError("selected body has no saved geometry")
            visible = shape.common(Part.makeCompound(_world_shape(tip).Faces))
            shape = Part.makeCompound(visible.Faces) if visible.Faces else Part.Shape()
        vertices = []
        for edge in shape.Edges:
            points = edge.discretize(Deflection=float(tolerance))
            for a, b in zip(points, points[1:]):
                vertices.extend([[float(p.x), float(p.y), float(p.z)] for p in (a, b)])
                if len(vertices) > 100_000:
                    raise ValueError("selected geometry exceeds highlight vertex limit")
        return {"ok": True, "vertices": vertices}
    finally:
        _close_doc(doc)
