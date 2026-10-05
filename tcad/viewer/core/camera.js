import { TAU, clamp, dot, sub, cross, length } from "./math.js";
import CONTRACT from "./render-contract.json" with { type: "json" };
export const PRESETS = Object.freeze(CONTRACT.presets);

export function cameraBasis(yaw, pitch) {
  const c = Math.cos(pitch), s = Math.sin(pitch);
  return {
    right: [-Math.sin(yaw), Math.cos(yaw), 0],
    up: [-Math.cos(yaw) * s, -Math.sin(yaw) * s, c],
    look: [Math.cos(yaw) * c, Math.sin(yaw) * c, s],
  };
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
    this.halfHeight = Math.max(extent / (1 - 2 * CONTRACT.margin), 1e-6);
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
