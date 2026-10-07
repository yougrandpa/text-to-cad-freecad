"""Complete artifact-local topology indexing; no source-feature guesses."""

import hashlib
import json


def mesh_digest(vertices, facets):
    data = json.dumps([vertices, facets], allow_nan=False, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


class PickMesh:
    def __init__(self):
        self.vertices, self.facets, self.entities, self.parts = [], [], [], []
        # Per-body tessellation cost. Not serialized (PickPart forbids extra
        # fields); the adaptive preview uses it to name the parts that spend
        # the vertex/facet budget, and to report the actual counts on failure.
        self.stats = []

    def add(self, body_id, shape, tolerance):
        start = len(self.vertices)
        triangle_total = 0
        for index, face in enumerate(shape.Faces, 1):
            points, triangles = face.tessellate(tolerance)
            offset, first = len(self.vertices), len(self.facets)
            self.vertices.extend([[float(p.x), float(p.y), float(p.z)] for p in points])
            self.facets.extend([[int(i) + offset for i in tri] for tri in triangles])
            triangle_total += len(triangles)
            self.entities.append({"body_id": body_id, "entity_kind": "face", "local_sub_id": f"Face{index}",
                                  "triangle_start": first, "triangle_count": len(triangles), "segments": []})
        for index, edge in enumerate(shape.Edges, 1):
            points = edge.discretize(Deflection=tolerance)
            offset = len(self.vertices)
            self.vertices.extend([[float(p.x), float(p.y), float(p.z)] for p in points])
            self.entities.append({"body_id": body_id, "entity_kind": "edge", "local_sub_id": f"Edge{index}",
                                  "triangle_start": 0, "triangle_count": 0,
                                  "segments": [[offset + i, offset + i + 1] for i in range(len(points) - 1)]})
        part = {"body_id": body_id, "vertex_start": start, "vertex_count": len(self.vertices) - start}
        self.parts.append(part)
        self.stats.append({"body_id": body_id, "vertices": part["vertex_count"],
                           "facets": triangle_total})
        return part

    def mapping(self):
        return {"schema_version": 1, "generator": "freecad-face-mesh-v1",
                "mesh_digest": mesh_digest(self.vertices, self.facets),
                "parts": self.parts, "entities": self.entities}
