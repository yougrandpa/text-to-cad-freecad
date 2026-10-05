import { TAU, clamp, dot, sub, cross, length } from "./math.js";
import CONTRACT from "./render-contract.json" with { type: "json" };

export function prepareScene(scene, { angle = 0, frame = 0 } = {}) {
  const motion = scene.motion || [], animation = scene.animation;
  if (animation && motion.length) throw new Error("原生动画与声明运动不能混用");
  let mesh;
  if (animation) {
    if (angle) throw new Error("原生动画使用 frame，不能使用 angle");
    mesh = poseAnimation(scene.mesh, animation, frame);
  } else {
    if (frame) throw new Error("该场景没有原生动画帧");
    mesh = poseMesh(scene.mesh, motion, angle);
  }
  const bounds = meshBounds(mesh.vertices);
  return { ...mesh, bbox: { x: bounds.max[0] - bounds.min[0], y: bounds.max[1] - bounds.min[1],
    z: bounds.max[2] - bounds.min[2], x_min: bounds.min[0], y_min: bounds.min[1], z_min: bounds.min[2] } };
}
/** Derive actual bounds rather than trusting a possibly absent bbox field. */
export function meshBounds(vertices) {
  if (!Array.isArray(vertices) || !vertices.length) throw new Error("网格没有顶点");
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const p of vertices) {
    if (!Array.isArray(p) || p.length !== 3 || !p.every(Number.isFinite)) {
      throw new Error("网格包含无效顶点");
    }
    for (let j = 0; j < 3; j++) {
      min[j] = Math.min(min[j], p[j]);
      max[j] = Math.max(max[j], p[j]);
    }
  }
  const center = min.map((v, j) => v / 2 + max[j] / 2);
  const radius = Math.max(length(sub(max, min)) / 2, 1e-6);
  if (!Number.isFinite(radius)) throw new Error("网格坐标超出范围");
  const corners = [];
  for (let mask = 0; mask < 8; mask++) corners.push(min.map((v, j) => mask & (1 << j) ? max[j] : v));
  return { min, max, center, radius, corners };
}

/** Prescribed rigid kinematics; always transform the original zero-angle mesh. */
export function poseMesh(mesh, motion, angle) {
  if (!Number.isFinite(angle)) throw new Error("摇杆角度必须是有限数值");
  const vertices = mesh.vertices.map(p => [...p]);
  const ranges = [];
  for (const part of motion) {
    const start = part.vertex_start, end = start + part.vertex_count;
    const pivot = [part.pivot?.x, part.pivot?.y, part.pivot?.z];
    const axis = [part.axis?.x, part.axis?.y, part.axis?.z];
    const norm = length(axis);
    if (!Number.isInteger(start) || !Number.isInteger(part.vertex_count) || start < 0 ||
        part.vertex_count <= 0 || end > vertices.length || !pivot.every(Number.isFinite) ||
        !axis.every(Number.isFinite) || !(norm > 1e-12) || !Number.isFinite(part.ratio) ||
        ranges.some(([lo, hi]) => start < hi && end > lo)) throw new Error("零件运动参数无效");
    ranges.push([start, end]);
    const u = axis.map(v => v / norm), theta = angle * Math.PI / 180 * part.ratio;
    if (!Number.isFinite(theta)) throw new Error("零件转角超出范围");
    const c = Math.cos(theta), s = Math.sin(theta);
    for (let i = start; i < end; i++) {
      const p = sub(mesh.vertices[i], pivot), w = cross(u, p), d = dot(u, p);
      vertices[i] = p.map((v, j) => pivot[j] + v*c + w[j]*s + u[j]*d*(1-c));
    }
  }
  return { ...mesh, vertices };
}

/** Apply native solver placements to the unchanged reference mesh. */
export function poseAnimation(mesh, animation, frameIndex) {
  if (!Number.isInteger(frameIndex) || frameIndex < 0 || frameIndex >= animation.frames.length) throw new Error("动画帧无效");
  const poses = animation.frames[frameIndex], vertices = mesh.vertices.map(p => [...p]), ranges = [];
  for (const part of animation.parts) {
    const start = part.vertex_start, end = start + part.vertex_count, m = poses[part.body_id];
    if (!Number.isInteger(start) || !Number.isInteger(part.vertex_count) || start < 0 || part.vertex_count <= 0 || end > vertices.length ||
        ranges.some(([lo,hi]) => start < hi && end > lo) || !Array.isArray(m) || m.length !== 16 || !m.every(Number.isFinite)) throw new Error("原生动画姿态无效");
    ranges.push([start,end]);
    for (let i = start; i < end; i++) {
      const [x,y,z] = mesh.vertices[i];
      vertices[i] = [m[0]*x+m[1]*y+m[2]*z+m[3], m[4]*x+m[5]*y+m[6]*z+m[7], m[8]*x+m[9]*y+m[10]*z+m[11]];
    }
  }
  return {...mesh, vertices};
}

/** Flat shaded triangles and feature edges (no coplanar triangulation seams).
 * Vertices are recentered before Float32 upload, retaining CAD precision even
 * when a small part lives far from the world origin. Degenerate faces are
 * skipped; corrupt indices/nonfinite coordinates reject the response outright.
 */
export function prepareMesh(mesh) {
  const bounds = meshBounds(mesh?.vertices);
  const { vertices, facets } = mesh;
  if (!Array.isArray(facets) || !facets.length) throw new Error("网格没有三角面");
  const triangles = [], edgeMap = new Map();
  // FreeCAD can duplicate a vertex across adjacent faces. Join exact positions
  // for edge visibility only; never weld or otherwise alter the solid itself.
  const keys = vertices.map((p) => p.join(","));
  for (const f of facets) {
    if (!Array.isArray(f) || f.length !== 3 || !f.every((i) => Number.isInteger(i) && i >= 0 && i < vertices.length)) {
      throw new Error("网格包含无效三角面索引");
    }
    const points = f.map((i) => vertices[i]);
    const normal = cross(sub(points[1], points[0]), sub(points[2], points[0]));
    const magnitude = length(normal);
    if (!magnitude) continue;
    const n = normal.map((v) => v / magnitude);
    for (const p of points) triangles.push(...sub(p, bounds.center), ...n);
    for (let j = 0; j < 3; j++) {
      const a = f[j], b = f[(j + 1) % 3];
      const key = keys[a] < keys[b] ? `${keys[a]}|${keys[b]}` : `${keys[b]}|${keys[a]}`;
      const existing = edgeMap.get(key);
      if (existing) {
        existing.count++;
        if (dot(existing.normal, n) < CONTRACT.crease_cos) existing.crease = true;
      } else edgeMap.set(key, { a, b, normal: n, count: 1, crease: false });
    }
  }
  if (!triangles.length) throw new Error("网格没有有效三角面");
  const edges = [];
  for (const e of edgeMap.values()) {
    if (e.count === 1 || e.crease) edges.push(...sub(vertices[e.a], bounds.center), ...sub(vertices[e.b], bounds.center));
  }
  const positions = new Float32Array(triangles), lines = new Float32Array(edges);
  if (!positions.every(Number.isFinite) || !lines.every(Number.isFinite)) throw new Error("网格坐标超出显示范围");
  return { bounds, triangles: positions, edges: lines, triangleCount: triangles.length / 18 };
}
