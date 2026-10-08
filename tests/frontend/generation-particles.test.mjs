import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { CYCLE_SECONDS, cycleState, sampleWordmark, ParticleFlock, GenerationParticles } from '../../tcad/server/ui/generation-particles.mjs';

const targets = Array.from({ length: 180 }, (_, index) => ({
  x: index < 174 ? (index % 30) / 32 - .46 : .46,
  y: index < 174 ? Math.floor(index / 30) / 8 - .32 : (index - 174) / 30 + .25,
  accent: index >= 174,
}));
function seededRandom() {
  let seed = 19;
  return () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
}
const distanceToLogo = flock => flock.particles.reduce((sum, p) => sum + Math.hypot(
  p.x - flock.width / 2 - p.target.x * flock.wordWidth,
  p.y - flock.height * .47 - p.target.y * flock.wordHeight), 0) / flock.particles.length;

test('each calm cycle disperses, flocks, gathers and holds the wordmark across the loop boundary', () => {
  assert.equal(CYCLE_SECONDS, 23);
  assert.equal(cycleState(0).phase, 'wordmark');
  assert.equal(cycleState(4).phase, 'scatter');
  assert.equal(cycleState(8).phase, 'flock');
  assert.equal(cycleState(14.99).phase, 'flock');
  assert.equal(cycleState(15).phase, 'gather');
  assert.equal(cycleState(17).phase, 'gather');
  assert.equal(cycleState(22).phase, 'wordmark');
  assert.deepEqual(cycleState(CYCLE_SECONDS), cycleState(0));
  for (const boundary of [2.5, 6, 15, 20, CYCLE_SECONDS]) {
    assert.ok(Math.abs(cycleState(boundary - .001).attraction - cycleState(boundary + .001).attraction) < .01);
  }
});

test('wordmark mask samples actual visible strokes and recognizes the terracotta accent', () => {
  const pixels = new Uint8ClampedArray(8 * 8 * 4);
  pixels.set([203, 216, 201, 255], (2 * 8 + 2) * 4);
  pixels.set([213, 130, 105, 255], (4 * 8 + 4) * 4);
  pixels.set([203, 216, 201, 20], (6 * 8 + 6) * 4);
  assert.deepEqual(sampleWordmark(pixels, 8, 8, 2), [
    { x: -.25, y: -.25, accent: false }, { x: 0, y: 0, accent: true },
  ]);
});

test('particles scatter widely but return to their letter positions after a complete cycle', () => {
  const flock = new ParticleFlock(targets, { count: 160, random: seededRandom() });
  flock.resize(600, 420);
  assert.ok(distanceToLogo(flock) < .001);
  for (let step = 1; step <= 450; step++) flock.advance(1 / 30, step / 30);
  assert.ok(distanceToLogo(flock) > 35, 'Flock did not visibly disperse');
  for (let step = 451; step <= CYCLE_SECONDS * 30; step++) flock.advance(1 / 30, step / 30);
  assert.ok(distanceToLogo(flock) < .5, 'Wordmark remained scattered at the loop end');
  assert.ok(flock.particles.filter(p => p.target.accent).length >= 8);
});

test('local alignment and separation affect the flock instead of independent particle noise', () => {
  const make = neighborX => {
    const flock = new ParticleFlock(targets, { count: 2, random: () => .5 });
    flock.resize(500, 400);
    Object.assign(flock.particles[0], { x: 100, y: 100, vx: 0, vy: 0 });
    Object.assign(flock.particles[1], { x: neighborX, y: 100, vx: 30, vy: 0 });
    flock.advance(.03, 9);
    return flock.particles[0];
  };
  assert.ok(make(112).ax > make(350).ax, 'Nearby aligned birds did not influence heading');
  assert.ok(make(101).ax < make(112).ax, 'Close birds did not separate');
});

test('dense flock alignment uses the nearest neighbors regardless of particle storage order', () => {
  const make = reversed => {
    const flock = new ParticleFlock(targets, { count: 31, random: seededRandom() });
    flock.resize(500, 400);
    const subject = flock.particles[0];
    Object.assign(subject, { x: 100, y: 100, vx: 0, vy: 0 });
    flock.particles.slice(1).forEach((p, index) => {
      Object.assign(p, { x: 116 + index * .1, y: 100, vx: index + 1, vy: 0 });
    });
    if (reversed) flock.particles.reverse();
    flock.advance(.03, 9);
    return subject;
  };
  const forward = make(false), reverse = make(true);
  assert.ok(Math.abs(forward.ax - reverse.ax) < 1e-9, 'Storage order biased the flock heading');
  assert.ok(Math.abs(forward.ay - reverse.ay) < 1e-9);
});

test('coincident birds separate instead of remaining locked together', () => {
  const flock = new ParticleFlock(targets, { count: 2, random: seededRandom() });
  flock.resize(500, 400);
  flock.particles.forEach(p => Object.assign(p, { x: 250, y: 200, vx: 0, vy: 0 }));
  for (let step = 0; step < 30; step++) flock.advance(1 / 30, 9);
  const [a, b] = flock.particles;
  assert.ok(Math.hypot(a.x - b.x, a.y - b.y) > 5, 'Coincident birds did not separate');
});

test('resize and stalled frames keep particle positions finite and inside the drawing surface', () => {
  const flock = new ParticleFlock(targets, { count: 160, random: seededRandom() });
  flock.resize(600, 400);
  for (let step = 1; step <= 250; step++) flock.advance(.05, step * .05);
  flock.resize(118, 124);
  for (let step = 1; step <= 200; step++) flock.advance(8, 12 + step / 30);
  for (const particle of flock.particles) {
    assert.ok([particle.x, particle.y, particle.vx, particle.vy].every(Number.isFinite));
    assert.ok(particle.x >= 3 && particle.x <= 115);
    assert.ok(particle.y >= 3 && particle.y <= 121);
  }
});

test('hover opens a local space in the wordmark and leaving restores the letters', () => {
  const flock = new ParticleFlock(targets, { count: 160, random: seededRandom() });
  flock.resize(600, 420);
  const bird = flock.particles[70];
  const pointer = { x: bird.x, y: bird.y };
  const nearby = flock.particles.filter(p => Math.hypot(p.x - pointer.x, p.y - pointer.y) < 25);
  const start = nearby.reduce((sum, p) => sum + Math.hypot(p.x - pointer.x, p.y - pointer.y), 0) / nearby.length;
  for (let step = 0; step < 60; step++) flock.advance(1 / 30, 0, pointer);
  const avoided = nearby.reduce((sum, p) => sum + Math.hypot(p.x - pointer.x, p.y - pointer.y), 0) / nearby.length;
  assert.ok(avoided > start + 10, 'Nearby particles did not move away from the cursor');
  for (let step = 0; step < 90; step++) flock.advance(1 / 30, 0);
  assert.ok(distanceToLogo(flock) < .5, 'Letters did not settle after the pointer left');
});

test('cursor avoidance is local and stays bounded in a compact flock', () => {
  const make = () => {
    const flock = new ParticleFlock(targets, { count: 160, random: seededRandom() });
    flock.resize(180, 96);
    return flock;
  };
  const baseline = make(), distant = make();
  baseline.advance(.03, 0);
  distant.advance(.03, 0, { x: -200, y: -200 });
  assert.deepEqual(distant.particles, baseline.particles, 'Distant cursor moved unrelated birds');
  for (let step = 0; step < 300; step++) distant.advance(.04, step * .04, { x: 90, y: 45 });
  for (const p of distant.particles) {
    assert.ok([p.x, p.y, p.vx, p.vy].every(Number.isFinite));
    assert.ok(p.x >= 3 && p.x <= 177 && p.y >= 3 && p.y <= 93);
  }
});

function harness({ reduced = false, contextAvailable = true } = {}) {
  const frames = new Map(), timers = new Map(), classes = new Set(), listeners = new Map();
  let sequence = 0, preview = false, width = 400, height = 240;
  const ctx = Object.fromEntries(['clearRect', 'setTransform', 'beginPath', 'moveTo', 'lineTo', 'stroke', 'arc', 'fill'].map(key => [key, () => {}]));
  const root = { hidden: true, dataset: {}, classList: {
    add(key) { classes.add(key); }, remove(key) { classes.delete(key); },
    toggle(key, value) { if (value) classes.add(key); else classes.delete(key); },
  }, getBoundingClientRect: () => ({ left: 20, top: 10, width, height }) };
  const canvas = { width: 0, height: 0, getContext: () => contextAvailable ? ctx : null };
  const media = { matches: reduced, addEventListener: (_, fn) => { media.change = fn; }, removeEventListener() {} };
  const observers = [];
  class Observer {
    constructor(fn) { this.callback = fn; observers.push(this); }
    observe() {} disconnect() { this.disconnected = true; }
  }
  const window = { devicePixelRatio: 3, matchMedia: () => media, ResizeObserver: Observer, MutationObserver: Observer,
    requestAnimationFrame(fn) { const id = ++sequence; frames.set(id, fn); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
    setTimeout(fn) { const id = ++sequence; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const document = { hidden: false, defaultView: window,
    addEventListener(type, fn) { listeners.set(type, fn); }, removeEventListener(type) { listeners.delete(type); } };
  const pointerListeners = new Map();
  const viewport = { dataset: {},
    addEventListener(type, fn) { pointerListeners.set(type, fn); },
    removeEventListener(type) { pointerListeners.delete(type); },
  }, logo = { removeEventListener() {} };
  const controller = new GenerationParticles({ root, canvas, logo, viewport, hasPreview: () => preview,
    targets, document, window });
  const paint = time => { const [id, fn] = frames.entries().next().value; frames.delete(id); fn(time); };
  return { controller, root, canvas, viewport, frames, timers, classes, media, document, observers, paint,
    flushTimers() { for (const [id, fn] of timers) { timers.delete(id); fn(); } },
    visibility(hidden) { document.hidden = hidden; listeners.get('visibilitychange')(); },
    resize(w, h) { width = w; height = h; observers[0].callback(); },
    preview(value) { preview = value; observers[1].callback(); },
    pointer(type, event = {}) { pointerListeners.get(type)?.({ pointerType: 'mouse', buttons: 0, ...event }); },
    pointerListeners,
  };
}

test('generation owns one bounded animation loop and stops it before the exit fade', () => {
  const h = harness();
  h.controller.setActive(true);
  h.controller.setActive(true);
  assert.equal(h.root.hidden, false);
  assert.equal(h.frames.size, 1);
  assert.equal(h.canvas.width, 800, 'Pixel ratio must be capped at two');
  h.paint(100); h.paint(134);
  assert.ok(h.controller.elapsed > 0);
  h.controller.setActive(false);
  assert.equal(h.frames.size, 0);
  assert.equal(h.timers.size, 1);
  h.flushTimers();
  assert.equal(h.root.hidden, true);
  assert.equal(h.viewport.dataset.generation, undefined);
});

test('every new turn restores viewport presentation after a completed exit', () => {
  for (const compact of [false, true]) {
    const h = harness();
    h.preview(compact);
    h.controller.setActive(true);
    h.controller.setActive(false);
    h.flushTimers();
    assert.equal(h.viewport.dataset.generation, undefined);
    h.controller.setActive(true);
    assert.equal(h.viewport.dataset.generation, compact ? 'compact' : 'full');
    assert.equal(h.root.hidden, false);
    assert.equal(h.frames.size, 1);
  }
});

test('viewport hover maps to the decorative canvas without intercepting model gestures', () => {
  const h = harness();
  h.pointer('pointermove', { clientX: 120, clientY: 80 });
  assert.equal(h.controller.pointer, null, 'Idle view must not retain the pointer');
  h.controller.setActive(true);
  h.pointer('pointermove', { clientX: 120, clientY: 80 });
  assert.deepEqual(h.controller.pointer, { x: 100, y: 70 });
  h.pointer('pointerleave');
  assert.equal(h.controller.pointer, null);
  h.pointer('pointermove', { clientX: 120, clientY: 80, buttons: 1 });
  assert.equal(h.controller.pointer, null, 'Dragging the model must not repel particles');
  h.pointer('pointermove', { clientX: 10, clientY: 5 });
  assert.equal(h.controller.pointer, null, 'Outside the canvas must not repel particles');
  h.pointer('pointermove', { clientX: 120, clientY: 80 });
  h.preview(true);
  assert.equal(h.controller.pointer, null, 'Moving to compact layout must discard old coordinates');
  h.pointer('pointermove', { clientX: 120, clientY: 80 });
  h.pointer('pointerdown');
  assert.equal(h.controller.pointer, null);
  h.pointer('pointermove', { clientX: 120, clientY: 80 });
  h.visibility(true);
  assert.equal(h.controller.pointer, null);
  h.controller.dispose();
  assert.equal(h.pointerListeners.size, 0, 'Dispose must remove all pointer listeners');
});

test('stale exit callbacks cannot hide a new turn and geometry switches to compact presentation', () => {
  const h = harness();
  h.controller.setActive(true);
  h.controller.setActive(false);
  const stale = [...h.timers.values()][0];
  h.controller.setActive(true);
  stale();
  assert.equal(h.root.hidden, false);
  h.preview(true);
  assert.equal(h.root.dataset.mode, 'compact');
  assert.equal(h.viewport.dataset.generation, 'compact');
  assert.equal(h.frames.size, 1);
});

test('hidden tabs and zero-width panes pause until visibility or size returns', () => {
  const h = harness();
  h.controller.setActive(true);
  h.paint(0); h.paint(34);
  const before = h.controller.elapsed;
  h.visibility(true);
  assert.equal(h.frames.size, 0);
  h.visibility(false);
  h.paint(100_000);
  assert.equal(h.controller.elapsed, before, 'Background time must not jump the animation');
  h.resize(0, 0);
  assert.equal(h.frames.size, 0);
  h.resize(320, 240);
  assert.equal(h.frames.size, 1);
});

test('reduced motion and missing Canvas 2D use a static logo without a running animation', () => {
  for (const options of [{ reduced: true }, { contextAvailable: false }]) {
    const h = harness(options);
    h.controller.setActive(true);
    assert.equal(h.classes.has('is-static'), true);
    assert.equal(h.frames.size, 0);
    h.preview(true);
    assert.equal(h.root.dataset.mode, 'compact');
    h.controller.dispose();
    assert.equal(h.root.hidden, true);
    assert.equal(h.observers.every(observer => observer.disconnected), true);
  }
});

test('particle formation uses the same four round paths and drafting accent as the existing logo', async () => {
  const brand = await readFile(new URL('../../tcad/server/ui/brand.svg', import.meta.url), 'utf8');
  const wordmark = await readFile(new URL('../../tcad/server/ui/wordmark.svg', import.meta.url), 'utf8');
  const group = brand.split('A drawn wordmark:')[1];
  for (const [, path] of group.matchAll(/<path d="([^"]+)"/g)) assert.ok(wordmark.includes(`d="${path}"`));
});
