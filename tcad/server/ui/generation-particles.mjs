// Calm, bounded boids that periodically return to the actual drawn wordmark.
export const CYCLE_SECONDS = 23;
const TAU = Math.PI * 2;
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const smooth = value => { const t = clamp(value, 0, 1); return t * t * (3 - 2 * t); };

export function cycleState(seconds) {
  const time = ((seconds % CYCLE_SECONDS) + CYCLE_SECONDS) % CYCLE_SECONDS;
  if (time < 2.5) return { phase: "wordmark", attraction: 1, release: 0 };
  if (time < 6) return { phase: "scatter", attraction: 1 - smooth((time - 2.5) / 1.8), release: smooth((time - 2.5) / 2) };
  if (time < 15) return { phase: "flock", attraction: 0, release: 0 };
  if (time < 20) return { phase: "gather", attraction: smooth((time - 15) / 5), release: 0 };
  return { phase: "wordmark", attraction: 1, release: 0 };
}

export function sampleWordmark(pixels, width, height, step = 4) {
  const targets = [];
  for (let y = 2; y < height; y += step) {
    for (let x = 2; x < width; x += step) {
      const offset = (y * width + x) * 4;
      if (pixels[offset + 3] < 110) continue;
      targets.push({ x: x / width - .5, y: y / height - .5,
        accent: pixels[offset] > pixels[offset + 1] * 1.2 });
    }
  }
  return targets;
}

export class ParticleFlock {
  constructor(targets, { count = 640, random = Math.random } = {}) {
    // Stratified sampling keeps the small red drafting cross represented.
    const normal = targets.filter(target => !target.accent);
    const accents = targets.filter(target => target.accent);
    const accentCount = accents.length ? Math.max(8, Math.round(count * .025)) : 0;
    this.particles = Array.from({ length: targets.length ? count : 0 }, (_, index) => {
      const isAccent = index >= count - accentCount;
      const source = isAccent ? accents : normal.length ? normal : targets;
      const local = isAccent ? index - (count - accentCount) : index;
      const amount = isAccent ? accentCount : count - accentCount;
      const target = source[Math.floor(local / amount * source.length)];
      return { id: index, target, x: 0, y: 0, vx: 0, vy: 0, ax: 0, ay: 0,
        seed: random() * TAU, depth: .7 + random() * .3 };
    });
    this.width = 0;
    this.height = 0;
    this.state = cycleState(0);
  }

  resize(width, height) {
    const previousWidth = this.width, previousHeight = this.height;
    this.width = Math.max(1, width);
    this.height = Math.max(1, height);
    this.wordWidth = Math.min(this.width * .72, this.height * 1.5, 420);
    this.wordHeight = this.wordWidth * 32 / 78;
    for (const particle of this.particles) {
      if (!previousWidth || !previousHeight) {
        particle.x = this.width / 2 + particle.target.x * this.wordWidth;
        particle.y = this.height * .47 + particle.target.y * this.wordHeight;
      } else {
        particle.x *= this.width / previousWidth;
        particle.y *= this.height / previousHeight;
        particle.vx *= this.width / previousWidth;
        particle.vy *= this.height / previousHeight;
      }
    }
  }

  advance(delta, elapsed, pointer = null) {
    const dt = clamp(delta, 0, .05);
    const state = this.state = cycleState(elapsed);
    const neighborhood = Math.min(42, this.width * .13);
    const spacing = Math.max(6, neighborhood * .28);
    const grid = new Map();
    const key = (x, y) => `${x},${y}`;
    for (const particle of this.particles) {
      const cell = key(Math.floor(particle.x / neighborhood), Math.floor(particle.y / neighborhood));
      if (!grid.has(cell)) grid.set(cell, []);
      grid.get(cell).push(particle);
    }
    // Compute forces from one snapshot; integrating inside this loop would let
    // early particles bias the flock's later neighbors.
    for (const particle of this.particles) {
      const tx = this.width / 2 + particle.target.x * this.wordWidth;
      const ty = this.height * .47 + particle.target.y * this.wordHeight;
      const dx = pointer ? particle.x - pointer.x : 0;
      const dy = pointer ? particle.y - pointer.y : 0;
      const distance = Math.hypot(dx, dy);
      const radius = Math.min(72, this.width * .2, this.height * .42);
      const avoidance = pointer ? smooth(1 - distance / radius) : 0;
      // Let hovered birds leave their letter positions, then spring back when
      // the pointer leaves. The smooth falloff avoids a sharp repulsion edge.
      const pull = state.attraction * (1 - avoidance * .97);
      let ax = 0, ay = 0;
      if (pull < .999) {
        const cx = Math.floor(particle.x / neighborhood), cy = Math.floor(particle.y / neighborhood);
        const nearest = [];
        let px = 0, py = 0, vx = 0, vy = 0, sx = 0, sy = 0;
        for (let y = cy - 1; y <= cy + 1; y++) {
          for (let x = cx - 1; x <= cx + 1; x++) {
            for (const other of grid.get(key(x, y)) || []) {
              if (other === particle) continue;
              let dx = particle.x - other.x, dy = particle.y - other.y;
              const distance = dx * dx + dy * dy;
              if (distance > neighborhood * neighborhood) continue;
              // Separation considers every close bird, even in dense groups.
              if (distance < spacing * spacing) {
                if (distance < 1e-8) {
                  const side = particle.id < other.id ? -1 : 1;
                  dx = Math.cos(particle.seed + other.seed) * side;
                  dy = Math.sin(particle.seed + other.seed) * side;
                }
                sx += dx / Math.max(distance, 1);
                sy += dy / Math.max(distance, 1);
              }
              // Bound alignment/cohesion by distance, not grid traversal or
              // array order. Keep at most 24 records instead of sorting a crowd.
              let slot = nearest.length;
              while (slot > 0 && (distance < nearest[slot - 1].distance
                || (distance === nearest[slot - 1].distance && other.id < nearest[slot - 1].other.id))) slot--;
              if (slot < 24) {
                nearest.splice(slot, 0, { other, distance });
                if (nearest.length > 24) nearest.pop();
              }
            }
          }
        }
        if (nearest.length) {
          for (const { other } of nearest) {
            px += other.x; py += other.y; vx += other.vx; vy += other.vy;
          }
          ax += (vx / nearest.length - particle.vx) * .7 + (px / nearest.length - particle.x) * .12 + sx * 30;
          ay += (vy / nearest.length - particle.vy) * .7 + (py / nearest.length - particle.y) * .12 + sy * 30;
        }
        // A continuous curl field guides the flock through broad, slow arcs.
        const nx = (particle.x - this.width / 2) / this.width;
        const ny = (particle.y - this.height * .47) / this.height;
        const curl = Math.sin(nx * 5 - elapsed * .16) + Math.cos(ny * 5 + elapsed * .12);
        ax += Math.cos(curl * 1.8) * 17 - nx * 22;
        ay += Math.sin(curl * 1.8) * 13 - ny * 28;
        ax += Math.cos(particle.seed) * state.release * 18;
        ay += Math.sin(particle.seed) * state.release * 18;
        const margin = 26;
        if (particle.x < margin) ax += (margin - particle.x) * 1.8;
        if (particle.x > this.width - margin) ax -= (particle.x - this.width + margin) * 1.8;
        if (particle.y < margin) ay += (margin - particle.y) * 1.8;
        if (particle.y > this.height - margin) ay -= (particle.y - this.height + margin) * 1.8;
      }
      particle.ax = ax * (1 - pull) + ((tx - particle.x) * 28 - particle.vx * 10.6) * pull;
      particle.ay = ay * (1 - pull) + ((ty - particle.y) * 28 - particle.vy * 10.6) * pull;
      if (avoidance) {
        // A stable heading also separates a bird exactly under the cursor.
        particle.ax += (distance > .01 ? dx / distance : Math.cos(particle.seed)) * avoidance * 220;
        particle.ay += (distance > .01 ? dy / distance : Math.sin(particle.seed)) * avoidance * 220;
      }
    }
    for (const particle of this.particles) {
      particle.vx += particle.ax * dt;
      particle.vy += particle.ay * dt;
      const speed = Math.hypot(particle.vx, particle.vy);
      const limit = 46 + state.attraction * 190;
      if (speed > limit) { particle.vx *= limit / speed; particle.vy *= limit / speed; }
      particle.x = clamp(particle.x + particle.vx * dt, 3, Math.max(3, this.width - 3));
      particle.y = clamp(particle.y + particle.vy * dt, 3, Math.max(3, this.height - 3));
    }
    return state;
  }
}

export class GenerationParticles {
  constructor({ root, canvas, logo, viewport, previewElements = [], hasPreview = () => false,
    document = root.ownerDocument, window = document.defaultView, targets = null }) {
    Object.assign(this, { root, canvas, logo, viewport, hasPreview, document, window });
    this.active = false;
    this.frame = null;
    this.hideTimer = null;
    this.elapsed = 0;
    this.lastTime = null;
    this.paintTime = null;
    this.pointer = null;
    this.targets = targets;
    try { this.context = canvas.getContext("2d", { alpha: true }); } catch { this.context = null; }
    this.reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    this.onPreference = () => this.refresh();
    this.onVisibility = () => this.refresh();
    this.reduced.addEventListener("change", this.onPreference);
    document.addEventListener("visibilitychange", this.onVisibility);
    // Observe the underlying viewport so the decoration never blocks model
    // rotation, selection, zoom or the help controls.
    this.onPointerLeave = () => { this.pointer = null; };
    this.onPointerMove = event => {
      if (!this.active || this.reduced.matches || event.pointerType === "touch" || event.buttons) {
        this.pointer = null;
        return;
      }
      const bounds = this.root.getBoundingClientRect();
      const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
      this.pointer = x >= 0 && y >= 0 && x <= bounds.width && y <= bounds.height ? { x, y } : null;
    };
    viewport.addEventListener("pointermove", this.onPointerMove, { passive: true });
    viewport.addEventListener("pointerleave", this.onPointerLeave, { passive: true });
    viewport.addEventListener("pointerdown", this.onPointerLeave, { passive: true });
    this.resizeObserver = new window.ResizeObserver(() => this.refresh());
    this.resizeObserver.observe(root);
    this.previewObserver = new window.MutationObserver(() => this.refresh());
    for (const element of previewElements) this.previewObserver.observe(element, { attributes: true, attributeFilter: ["hidden"] });
    this.onLogo = () => { this.readLogo(); this.refresh(); };
    if (!targets) {
      if (logo.complete && logo.naturalWidth) this.readLogo();
      else logo.addEventListener("load", this.onLogo, { once: true });
    }
  }

  readLogo() {
    if (!this.context || this.targets) return;
    try {
      const mask = this.document.createElement("canvas");
      mask.width = 624; mask.height = 256;
      const context = mask.getContext("2d", { willReadFrequently: true });
      context.drawImage(this.logo, 0, 0, mask.width, mask.height);
      this.targets = sampleWordmark(context.getImageData(0, 0, mask.width, mask.height).data, mask.width, mask.height);
    } catch { this.targets = null; /* SVG fallback remains visible. */ }
  }

  setActive(active) {
    if (this.active === active) return;
    this.active = active;
    this.pointer = null;
    this.window.clearTimeout(this.hideTimer);
    this.hideTimer = null;
    if (active) {
      this.elapsed = 0;
      this.flock = null;
      this.root.hidden = false;
      this.root.classList.add("is-active");
      this.refresh();
    } else {
      this.pause();
      this.root.classList.remove("is-active");
      const hide = () => {
        if (this.active) return;
        this.root.hidden = true;
        delete this.viewport.dataset.generation;
        this.context?.clearRect(0, 0, this.canvas.width, this.canvas.height);
        this.flock = null;
        this.hideTimer = null;
      };
      if (this.reduced.matches || this.document.hidden) hide();
      else this.hideTimer = this.window.setTimeout(hide, 350);
    }
  }

  refresh() {
    if (!this.active) return;
    const mode = this.hasPreview() ? "compact" : "full";
    if (this.root.dataset.mode !== mode) {
      this.pointer = null;
      this.root.dataset.mode = mode;
    }
    this.viewport.dataset.generation = mode;
    const isStatic = !this.context || !this.targets?.length || this.reduced.matches;
    this.root.classList.toggle("is-static", isStatic);
    const visible = this.resize();
    if (isStatic || this.document.hidden || !visible) this.pause();
    else if (this.frame === null) this.frame = this.window.requestAnimationFrame(time => this.tick(time));
  }

  resize() {
    if (!this.active || !this.context || !this.targets?.length || this.reduced.matches) return false;
    const bounds = this.root.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return false;
    const ratio = Math.min(this.window.devicePixelRatio || 1, 2);
    const width = Math.round(bounds.width * ratio), height = Math.round(bounds.height * ratio);
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.pointer = null;
      this.canvas.width = width; this.canvas.height = height;
    }
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (!this.flock) this.flock = new ParticleFlock(this.targets, { count: this.root.dataset.mode === "compact" ? 280 : 640 });
    this.flock.resize(bounds.width, bounds.height);
    this.draw();
    return true;
  }

  tick(time) {
    this.frame = null;
    if (!this.active || this.document.hidden || this.reduced.matches) return;
    if (this.lastTime === null) this.lastTime = time;
    // Paint at 30 fps; simulation time freezes in background tabs.
    if (this.paintTime === null || time - this.paintTime >= 1000 / 30 - 1) {
      const delta = Math.min((time - this.lastTime) / 1000, .05);
      this.elapsed += delta;
      this.lastTime = time;
      this.paintTime = time;
      if (this.flock) { this.flock.advance(delta, this.elapsed, this.pointer); this.draw(); }
    }
    this.frame = this.window.requestAnimationFrame(next => this.tick(next));
  }

  draw() {
    if (!this.flock) return;
    const context = this.context, { width, height, particles, state } = this.flock;
    context.clearRect(0, 0, width, height);
    if (state.attraction < .92) {
      context.beginPath();
      for (const particle of particles) {
        context.moveTo(particle.x, particle.y);
        context.lineTo(particle.x - particle.vx * .12, particle.y - particle.vy * .12);
      }
      context.strokeStyle = "rgba(188, 207, 187, .16)";
      context.lineWidth = .7;
      context.stroke();
    }
    for (const accent of [false, true]) {
      context.beginPath();
      for (const particle of particles) {
        if (particle.target.accent !== accent) continue;
        const radius = (accent ? 1.15 : .95) * particle.depth;
        context.moveTo(particle.x + radius, particle.y);
        context.arc(particle.x, particle.y, radius, 0, TAU);
      }
      context.fillStyle = accent ? "rgba(213, 130, 105, .88)" : "rgba(203, 216, 201, .78)";
      context.fill();
    }
    this.root.dataset.phase = state.phase;
  }

  pause() {
    this.pointer = null;
    if (this.frame !== null) this.window.cancelAnimationFrame(this.frame);
    this.frame = null;
    this.lastTime = null;
    this.paintTime = null;
  }

  dispose() {
    this.pause();
    this.window.clearTimeout(this.hideTimer);
    this.active = false;
    this.root.hidden = true;
    delete this.viewport.dataset.generation;
    this.resizeObserver.disconnect();
    this.previewObserver.disconnect();
    this.reduced.removeEventListener("change", this.onPreference);
    this.document.removeEventListener("visibilitychange", this.onVisibility);
    this.viewport.removeEventListener("pointermove", this.onPointerMove);
    this.viewport.removeEventListener("pointerleave", this.onPointerLeave);
    this.viewport.removeEventListener("pointerdown", this.onPointerLeave);
    this.logo.removeEventListener("load", this.onLogo);
    this.flock = null;
  }
}
