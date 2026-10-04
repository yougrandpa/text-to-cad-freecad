/* Small, offline WebGL CAD viewport. No framework, CDN, or build step.
 * Coordinates follow the headless renderer: millimetres, Z up; front is -Y.
 * Pure camera/mesh helpers are exported so their geometry is tested in Node.
 */

const TAU = Math.PI * 2;
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a, b) => a.map((v, i) => v - b[i]);
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const length = (v) => Math.hypot(...v);

export const PRESETS = Object.freeze({
  iso: [Math.PI / 4, Math.atan(1 / Math.sqrt(2))],
  front: [-Math.PI / 2, 0], back: [Math.PI / 2, 0],
  left: [Math.PI, 0], right: [0, 0],
  top: [-Math.PI / 2, Math.PI / 2], bottom: [-Math.PI / 2, -Math.PI / 2],
});

export function cameraBasis(yaw, pitch) {
  const c = Math.cos(pitch), s = Math.sin(pitch);
  return {
    right: [-Math.sin(yaw), Math.cos(yaw), 0],
    up: [-Math.cos(yaw) * s, -Math.sin(yaw) * s, c],
    look: [Math.cos(yaw) * c, Math.sin(yaw) * c, s],
  };
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

export class OrbitCamera {
  constructor() { this.reset(); }
  reset() {
    this.target = [0, 0, 0];
    this.halfHeight = 1;
    this.bounds = null;
    this.setPreset("iso");
  }
  get basis() { return cameraBasis(this.yaw, this.pitch); }
  setPreset(view) {
    if (!Object.hasOwn(PRESETS, view)) throw new Error(`未知视角：${view}`);
    [this.yaw, this.pitch] = PRESETS[view];
    this.view = view;
  }
  setBounds(bounds, aspect = 1) {
    const first = this.bounds === null;
    this.bounds = bounds;
    if (first) this.fit(aspect);
  }
  fit(aspect = 1) {
    if (!this.bounds) return;
    aspect = Math.max(aspect, 1e-6);
    this.target = [...this.bounds.center];
    const { right, up } = this.basis;
    let extent = this.bounds.radius * 0.01;
    for (const point of this.bounds.corners) {
      const p = sub(point, this.target);
      extent = Math.max(extent, Math.abs(dot(p, up)), Math.abs(dot(p, right)) / aspect);
    }
    this.halfHeight = Math.max(extent * 1.22, 1e-6);
  }
  orbit(dx, dy) {
    this.yaw = ((this.yaw - dx * 0.008) % TAU + TAU) % TAU;
    this.pitch = clamp(this.pitch + dy * 0.008, -Math.PI / 2, Math.PI / 2);
    this.view = "free";
  }
  pan(dx, dy, height) {
    const { right, up } = this.basis;
    const scale = 2 * this.halfHeight / Math.max(height, 1);
    this.target = this.target.map((v, j) => v - right[j] * dx * scale + up[j] * dy * scale);
  }
  zoom(factor) {
    if (!(factor > 0) || !Number.isFinite(factor)) return;
    const radius = this.bounds?.radius || 1;
    this.halfHeight = clamp(this.halfHeight * factor, radius / 1000, radius * 1000);
  }
}

export function projectPoint(point, camera, width, height) {
  const p = sub(point, camera.target), { right, up, look } = camera.basis;
  const scale = height / (2 * camera.halfHeight);
  return [width / 2 + dot(p, right) * scale, height / 2 - dot(p, up) * scale, dot(p, look)];
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
        if (dot(existing.normal, n) < Math.cos(Math.PI / 7.2)) existing.crease = true;
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

const VERTEX = `
precision highp float;
attribute vec3 aPosition;
attribute vec3 aNormal;
uniform vec3 uTarget, uRight, uUp, uLook;
uniform float uHalfHeight, uAspect, uDepth;
varying vec3 vNormal;
void main() {
  vec3 p = aPosition - uTarget;
  gl_Position = vec4(dot(p,uRight)/(uHalfHeight*uAspect), dot(p,uUp)/uHalfHeight, -dot(p,uLook)/uDepth, 1.0);
  vNormal = aNormal;
}`;
const FRAGMENT = `
precision highp float;
varying vec3 vNormal;
uniform vec3 uLook, uUp, uRight;
uniform bool uLines;
void main() {
  if (uLines) { gl_FragColor = vec4(0.13, 0.25, 0.34, 1.0); return; }
  vec3 n = normalize(vNormal) * (gl_FrontFacing ? 1.0 : -1.0);
  vec3 light = normalize(uLook + 0.6*uUp - 0.4*uRight);
  float shade = 0.48 + 0.44*max(dot(n, light),0.0) + 0.08*max(dot(n,-uRight),0.0);
  gl_FragColor = vec4(vec3(0.53,0.74,0.88)*shade, 1.0);
}`;

export class MeshViewport {
  constructor(canvas, { axes = null, onChange = () => {}, onError = () => {} } = {}) {
    this.canvas = canvas;
    this.axes = axes;
    this.onChange = onChange;
    this.onError = onError;
    this.camera = new OrbitCamera();
    this.available = false;
    this.hasMesh = false;
    this.pointers = new Map();
    this.listeners = [];
    this.frame = null;
    try {
      this.gl = canvas.getContext("webgl", { antialias: true, alpha: true }) || canvas.getContext("experimental-webgl");
      if (!this.gl) throw new Error("浏览器未启用 WebGL");
      this.initGL();
      this.available = true;
    } catch (err) { this.error = err.message; }
    this.listen(canvas, "webglcontextlost", (e) => {
      e.preventDefault();
      this.available = false;
      this.error = "WebGL 上下文已丢失，刷新页面可恢复交互";
      this.clear();
      this.onError(this.error);
    });
    this.bindControls();
    this.resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => this.schedule()) : null;
    this.resizeObserver?.observe(canvas.parentElement);
    this.listen(globalThis, "resize", () => this.schedule());
  }
  listen(target, event, listener, options) {
    target.addEventListener(event, listener, options);
    this.listeners.push(() => target.removeEventListener(event, listener, options));
  }
  initGL() {
    const gl = this.gl, shaders = [];
    const program = gl.createProgram();
    try {
      for (const [kind, source] of [[gl.VERTEX_SHADER, VERTEX], [gl.FRAGMENT_SHADER, FRAGMENT]]) {
        const shader = gl.createShader(kind);
        shaders.push(shader);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "着色器编译失败");
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "着色器链接失败");
      this.program = program;
      this.attributes = Object.fromEntries(["aPosition", "aNormal"].map((name) => [name, gl.getAttribLocation(program, name)]));
      this.uniforms = Object.fromEntries(["uTarget", "uRight", "uUp", "uLook", "uHalfHeight", "uAspect", "uDepth", "uLines"].map((name) => [name, gl.getUniformLocation(program, name)]));
      this.triangleBuffer = gl.createBuffer();
      this.edgeBuffer = gl.createBuffer();
      if (!this.triangleBuffer || !this.edgeBuffer) throw new Error("无法分配网格缓冲区");
    } catch (error) { gl.deleteProgram(program); throw error; }
    finally { for (const shader of shaders) gl.deleteShader(shader); }
  }
  get aspect() { return Math.max(this.canvas.clientWidth, 1) / Math.max(this.canvas.clientHeight, 1); }
  setMesh(mesh) {
    if (!this.available) throw new Error(this.error || "WebGL 不可用");
    const data = prepareMesh(mesh), gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.triangleBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data.triangles, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data.edges, gl.STATIC_DRAW);
    if (gl.getError() !== gl.NO_ERROR) throw new Error("网格上传到显卡失败");
    this.triangleVertices = data.triangles.length / 6;
    this.edgeVertices = data.edges.length / 3;
    this.triangleCount = data.triangleCount;
    this.origin = data.bounds.center;
    this.canvas.hidden = false;
    this.camera.setBounds(data.bounds, this.aspect);
    this.hasMesh = true;
    // SVGElement.hidden is not an HTMLElement reflected property in every
    // browser. Toggle the real attribute so the hidden axes can become visible.
    if (this.axes) this.axes.toggleAttribute("hidden", false);
    this.changed();
    return data.triangleCount;
  }
  clear({ resetCamera = false } = {}) {
    this.hasMesh = false;
    this.pointers.clear();
    this.canvas.classList.remove("dragging");
    this.canvas.hidden = true;
    if (this.axes) this.axes.toggleAttribute("hidden", true);
    if (resetCamera) this.camera.reset();
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
  }
  setPreset(view) { this.camera.setPreset(view); this.changed(); }
  fit() { this.camera.fit(this.aspect); this.changed(); }
  changed() { this.schedule(); this.onChange({ view: this.camera.view }); }
  schedule() {
    if (!this.available || !this.hasMesh || this.frame !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      try { this.render(); }
      catch (err) {
        this.available = false;
        this.error = err.message;
        this.clear();
        this.onError(this.error);
      }
    });
  }
  render() {
    const gl = this.gl, canvas = this.canvas, camera = this.camera;
    const ratio = Math.min(globalThis.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    gl.viewport(0, 0, width, height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.useProgram(this.program);
    const u = this.uniforms, { right, up, look } = camera.basis;
    gl.uniform3fv(u.uTarget, sub(camera.target, this.origin));
    gl.uniform3fv(u.uRight, right); gl.uniform3fv(u.uUp, up); gl.uniform3fv(u.uLook, look);
    gl.uniform1f(u.uHalfHeight, camera.halfHeight); gl.uniform1f(u.uAspect, width / height);
    // Depth follows model bounds even after panning far away. Clipping planes
    // never cut the actual solid just because the user rotated or changed scale.
    gl.uniform1f(u.uDepth, Math.max(camera.bounds.radius * 2 + Math.abs(dot(sub(this.origin, camera.target), look)), 1e-6));
    const { aPosition, aNormal } = this.attributes;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.triangleBuffer);
    gl.enableVertexAttribArray(aPosition); gl.enableVertexAttribArray(aNormal);
    gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 24, 0);
    gl.vertexAttribPointer(aNormal, 3, gl.FLOAT, false, 24, 12);
    gl.uniform1i(u.uLines, 0);
    gl.enable(gl.POLYGON_OFFSET_FILL); gl.polygonOffset(1, 1);
    gl.drawArrays(gl.TRIANGLES, 0, this.triangleVertices);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    if (this.edgeVertices) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeBuffer);
      gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 12, 0);
      gl.disableVertexAttribArray(aNormal); gl.vertexAttrib3f(aNormal, 0, 0, 1);
      gl.uniform1i(u.uLines, 1);
      gl.drawArrays(gl.LINES, 0, this.edgeVertices);
    }
    this.drawAxes(right, up, look);
  }
  drawAxes(right, up, look) {
    if (!this.axes) return;
    const groups = [...this.axes.querySelectorAll("[data-axis]")];
    groups.sort((a, b) => look[Number(a.dataset.axis)] - look[Number(b.dataset.axis)]);
    for (const group of groups) {
      const axis = Number(group.dataset.axis);
      const x = 48 + right[axis] * 29, y = 48 - up[axis] * 29;
      const line = group.querySelector("line"), text = group.querySelector("text");
      line.setAttribute("x2", x); line.setAttribute("y2", y);
      text.setAttribute("x", 48 + right[axis] * 38);
      text.setAttribute("y", 48 - up[axis] * 38);
      group.setAttribute("opacity", look[axis] < -0.01 ? "0.5" : "1");
      this.axes.append(group);
    }
  }
  bindControls() {
    const canvas = this.canvas;
    this.listen(canvas, "contextmenu", (e) => { if (this.hasMesh) e.preventDefault(); });
    this.listen(canvas, "pointerdown", (e) => {
      if (!this.hasMesh || e.button > 2) return;
      e.preventDefault(); canvas.focus({ preventScroll: true });
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, pan: e.button !== 0 || e.shiftKey });
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.add("dragging");
    });
    this.listen(canvas, "pointermove", (e) => {
      const previous = this.pointers.get(e.pointerId);
      if (!previous || !this.hasMesh) return;
      const before = [...this.pointers.values()];
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, pan: previous.pan });
      const after = [...this.pointers.values()];
      if (before.length === 2) {
        const distance = (p) => Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
        const oldDistance = distance(before), newDistance = distance(after);
        if (oldDistance > 0 && newDistance > 0) this.camera.zoom(oldDistance / newDistance);
        this.camera.pan((e.clientX - previous.x) / 2, (e.clientY - previous.y) / 2, canvas.clientHeight);
      } else if (before.length === 1) {
        if (previous.pan || e.shiftKey) this.camera.pan(e.clientX - previous.x, e.clientY - previous.y, canvas.clientHeight);
        else this.camera.orbit(e.clientX - previous.x, e.clientY - previous.y);
      }
      this.changed();
    });
    const release = (e) => {
      this.pointers.delete(e.pointerId);
      if (!this.pointers.size) canvas.classList.remove("dragging");
    };
    for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) this.listen(canvas, event, release);
    this.listen(canvas, "wheel", (e) => {
      if (!this.hasMesh) return;
      e.preventDefault();
      const pixels = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? canvas.clientHeight : 1);
      this.camera.zoom(Math.exp(clamp(pixels * 0.0015, -1.5, 1.5)));
      this.changed();
    }, { passive: false });
    this.listen(canvas, "dblclick", () => { if (this.hasMesh) this.fit(); });
    this.listen(canvas, "keydown", (e) => {
      if (!this.hasMesh || e.ctrlKey || e.metaKey || e.altKey) return;
      const arrows = { ArrowLeft: [-18, 0], ArrowRight: [18, 0], ArrowUp: [0, -18], ArrowDown: [0, 18] };
      const views = { "0": "iso", "1": "front", "2": "back", "3": "right", "4": "left", "5": "top", "6": "bottom" };
      if (Object.hasOwn(arrows, e.key)) {
        const [dx, dy] = arrows[e.key];
        if (e.shiftKey) this.camera.pan(dx, dy, canvas.clientHeight);
        else this.camera.orbit(dx, dy);
      } else if (["+", "=", "-", "_"].includes(e.key)) this.camera.zoom(["-", "_"].includes(e.key) ? 1.15 : 1 / 1.15);
      else if (e.key === "Home" || e.key.toLowerCase() === "f") this.camera.fit(this.aspect);
      else if (Object.hasOwn(views, e.key)) this.camera.setPreset(views[e.key]);
      else return;
      e.preventDefault(); this.changed();
    });
  }
  dispose() {
    this.clear(); this.resizeObserver?.disconnect();
    for (const remove of this.listeners) remove();
    if (this.gl) {
      this.gl.deleteBuffer(this.triangleBuffer); this.gl.deleteBuffer(this.edgeBuffer);
      this.gl.deleteProgram(this.program);
    }
  }
}
