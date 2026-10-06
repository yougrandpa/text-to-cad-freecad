/** Hit tests use the currently posed vertices and artifact-local topology IDs. */
import { projectPoint } from "./camera.js";
import { poseMesh, poseAnimation } from "./scene.js";

function triangleDepth(p, a, b, c) {
  const denominator = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1]);
  if (Math.abs(denominator) < 1e-10) return null;
  const u = ((b[1]-c[1])*(p[0]-c[0]) + (c[0]-b[0])*(p[1]-c[1]))/denominator;
  const v = ((c[1]-a[1])*(p[0]-c[0]) + (a[0]-c[0])*(p[1]-c[1]))/denominator;
  const w = 1-u-v;
  return Math.min(u,v,w) >= -1e-8 ? u*a[2] + v*b[2] + w*c[2] : null;
}

export function pickEntity(mesh, mapping, camera, width, height, x, y, kind = "face") {
  if (!mapping || !["body", "face", "edge"].includes(kind) ||
      ![width,height,x,y].every(Number.isFinite) || width <= 0 || height <= 0) return null;
  const projected = mesh.vertices.map(p => projectPoint(p, camera, width, height));
  let depth = -Infinity, hit = null;
  for (const entity of mapping.entities) {
    if (entity.entity_kind !== "face") continue;
    for (let i = entity.triangle_start; i < entity.triangle_start + entity.triangle_count; ++i) {
      const z = triangleDepth([x,y], ...mesh.facets[i].map(index => projected[index]));
      if (z !== null && z > depth) { depth = z; hit = entity; }
    }
  }
  if (kind === "body") return hit ? { body_id: hit.body_id, entity_kind: "body" } : null;
  if (kind === "face") return hit;
  let distance = 7, edge = null;
  const epsilon = Math.max(camera.bounds?.radius || 1, 1)*1e-5;
  for (const entity of mapping.entities) {
    if (entity.entity_kind !== "edge") continue;
    for (const [i,j] of entity.segments) {
      const a = projected[i], b = projected[j], dx = b[0]-a[0], dy = b[1]-a[1];
      const t = Math.max(0, Math.min(1, ((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy || 1)));
      const px = a[0]+t*dx, py = a[1]+t*dy, z = a[2]+t*(b[2]-a[2]);
      const d = Math.hypot(x-px, y-py);
      if (d >= distance) continue;
      // Test occlusion at the closest edge point, not the click's nearby face.
      let front = -Infinity;
      for (const tri of mesh.facets) {
        const d = triangleDepth([px,py], ...tri.map(index => projected[index]));
        if (d !== null) front = Math.max(front,d);
      }
      if (z >= front-epsilon) { distance = d; edge = entity; }
    }
  }
  return edge;
}

export function highlightVertices(mesh, mapping, selected, origin, { motion = [], animation = null, value = 0 } = {}) {
  const points = [];
  if (selected?.overlay) {
    let overlay = { vertices: selected.overlay.vertices };
    if (!overlay.vertices.length) return new Float32Array();
    const part = { body_id: selected.body_id, vertex_start: 0, vertex_count: overlay.vertices.length };
    if (animation) overlay = poseAnimation(overlay, { ...animation, parts: [part] }, value);
    else {
      const spec = motion.find(item => item.body_id === selected.body_id);
      if (spec) overlay = poseMesh(overlay, [{ ...spec, ...part }], value);
    }
    return new Float32Array(overlay.vertices.flatMap(p => p.map((v, j) => v - origin[j])));
  }
  if (!selected || !mapping) return new Float32Array();
  for (const entity of mapping.entities) {
    if (entity.body_id !== selected.body_id || (selected.entity_kind !== "body" &&
        (entity.entity_kind !== selected.entity_kind || entity.local_sub_id !== selected.local_sub_id))) continue;
    if (selected.entity_kind === "body" && entity.entity_kind !== "edge") continue;
    let segments = entity.segments;
    if (entity.entity_kind === "face") {
      const boundary = new Map();
      for (const [a,b,c] of mesh.facets.slice(entity.triangle_start, entity.triangle_start+entity.triangle_count)) {
        for (const [i,j] of [[a,b],[b,c],[c,a]]) {
          const key = i < j ? `${i}:${j}` : `${j}:${i}`;
          const edge = boundary.get(key);
          if (edge) edge.count++; else boundary.set(key,{segment:[i,j],count:1});
        }
      }
      segments = [...boundary.values()].filter(edge => edge.count === 1).map(edge => edge.segment);
    }
    for (const segment of segments) for (const index of segment) {
      points.push(...mesh.vertices[index].map((v,j) => v-origin[j]));
    }
  }
  return new Float32Array(points);
}
