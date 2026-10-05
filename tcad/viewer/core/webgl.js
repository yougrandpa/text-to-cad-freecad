import { TAU, clamp, dot, sub, cross, length } from "./math.js";
import { OrbitCamera } from "./camera.js";
import { poseMesh, poseAnimation, prepareMesh, prepareScene, motionScopeNote } from "./scene.js";
import { VERTEX, FRAGMENT } from "./shaders.js";

export class MeshViewport {
  constructor(canvas, { axes = null, motionControls = null, onChange = () => {}, onError = () => {}, background = [0, 0, 0, 0] } = {}) {
    this.canvas = canvas;
    this.axes = axes;
    this.motionControls = motionControls;
    this.motion = [];
    this.motionSource = null;
    this.onChange = onChange;
    this.onError = onError;
    this.background = background;
    this.style = "flat_edges";
    this.camera = new OrbitCamera();
    this.available = false;
    this.hasMesh = false;
    this.pointers = new Map();
    this.listeners = [];
    this.frame = null;
    if (motionControls) this.listen(motionControls.input, "input", () => {
      this.stopPlayback();
      this.setMotionValue(Number(motionControls.input.value));
    });
    this.animation = null;
    this.playbackFrame = null;
    if (motionControls?.play) this.listen(motionControls.play, "click", () => {
      if (this.playbackFrame !== null) this.stopPlayback(); else this.play();
    });
    if (motionControls?.reset) this.listen(motionControls.reset, "click", () => {
      this.stopPlayback(); this.setMotionValue(0);
    });
    if (motionControls?.speed) this.listen(motionControls.speed, "change", () => {
      if (this.playbackFrame !== null) { this.stopPlayback(); this.play(); }
    });
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
  setMotionValue(value) {
    if (!this.hasMesh || (!this.animation && !this.motion.length)) return;
    if (this.animation) {
      value = clamp(Math.floor(value), 0, this.animation.frames.length - 1);
      if (this.displayedMotionValue === value) return;
      this.displayedMotionValue = value;
      this._uploadMesh(prepareScene({ mesh: this.motionSource, animation: this.animation }, { frame: value }));
      if (this.motionControls) this.motionControls.output.textContent = `${(this.animation.start + value*this.animation.step).toFixed(2)} s · ${value+1}/${this.animation.frames.length}`;
    } else {
      this._uploadMesh(prepareScene({ mesh: this.motionSource, motion: this.motion }, { angle: value }));
      if (this.motionControls) this.motionControls.output.textContent = `${Math.round(value)}°`;
    }
    if (this.motionControls) this.motionControls.input.value = String(value);
  }
  stopPlayback() {
    if (this.playbackFrame !== null) cancelAnimationFrame(this.playbackFrame);
    this.playbackFrame = null;
    if (this.motionControls?.play) this.motionControls.play.textContent = "播放";
  }
  play() {
    if (!this.motionControls || (!this.animation && !this.motion.length) || this.animation?.frames.length === 1) return;
    this.stopPlayback();
    const maximum = Number(this.motionControls.input.max);
    let initial = Number(this.motionControls.input.value);
    if (initial >= maximum) initial = 0;
    const speed = Number(this.motionControls.speed?.value || 1);
    const rate = this.animation ? 1/this.animation.step : 60;
    const start = performance.now();
    if (this.motionControls.play) this.motionControls.play.textContent = "暂停";
    const tick = now => {
      let value = initial + (now-start)/1000*rate*speed;
      if (value > maximum) {
        if (this.motionControls.loop?.checked) value %= maximum || 1;
        else { this.setMotionValue(maximum); this.stopPlayback(); return; }
      }
      this.setMotionValue(value);
      this.playbackFrame = requestAnimationFrame(tick);
    };
    this.playbackFrame = requestAnimationFrame(tick);
  }
  setMesh(mesh, motion = [], animation = null) {
    poseMesh(mesh, motion, 0); // Validate before replacing the displayed model.
    if (animation) {
      if (!animation.frames?.length || !animation.parts?.length || !(animation.step > 0)) throw new Error("动画数据无效");
      if (animation.frames.length > 600 || !Number.isFinite(animation.start)) throw new Error("动画时间或帧数无效");
      for (const frame of animation.frames) {
        for (const part of animation.parts) {
          const m = frame[part.body_id];
          if (!Array.isArray(m) || m.length !== 16 || !m.every(Number.isFinite)) throw new Error("原生动画姿态无效");
        }
      }
      poseAnimation(mesh, animation, 0);
    }
    this.stopPlayback();
    this.animation = animation;
    this.displayedMotionValue = null;
    const count = this._uploadMesh(prepareScene({ mesh, motion, animation }));
    this.motionSource = mesh;
    this.motion = motion;
    if (this.motionControls) {
      this.motionControls.root.hidden = !animation && motion.length === 0;
      if (this.motionControls.play) this.motionControls.play.disabled = animation?.frames.length === 1;
      this.motionControls.input.max = String(animation ? animation.frames.length-1 : 720);
      if (this.motionControls.label) this.motionControls.label.textContent = animation ? "仿真时间" : "摇杆转角";
      if (this.motionControls.note) this.motionControls.note.textContent = motionScopeNote(animation);
      this.motionControls.input.value = "0";
      this.motionControls.output.textContent = "0°";
    }
    if (animation && this.motionControls) this.setMotionValue(0);
    return count;
  }
  _uploadMesh(mesh) {
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
    this.stopPlayback();
    this.animation = null;
    this.motion = [];
    this.motionSource = null;
    if (this.motionControls) this.motionControls.root.hidden = true;
    this.pointers.clear();
    this.canvas.classList.remove("dragging");
    this.canvas.hidden = true;
    if (this.axes) this.axes.toggleAttribute("hidden", true);
    if (resetCamera) this.camera.reset();
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
  }
  setPreset(view) { this.camera.setPreset(view); this.changed(); }
  setStyle(style) {
    if (!["flat", "flat_edges", "edges_only"].includes(style)) throw new Error("未知渲染样式");
    this.style = style;
    this.changed();
  }
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
    gl.clearColor(...this.background);
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
    if (this.style === "edges_only") gl.colorMask(false, false, false, false);
    gl.drawArrays(gl.TRIANGLES, 0, this.triangleVertices);
    if (this.style === "edges_only") gl.colorMask(true, true, true, true);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    if (this.edgeVertices && this.style !== "flat") {
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
