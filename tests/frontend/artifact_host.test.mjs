import test from "node:test";
import assert from "node:assert/strict";
import { loadArtifactScene } from "../../tcad/viewer/core/artifact.js";
import { prepareScene } from "../../tcad/viewer/core/scene.js";
import { ArtifactViewport } from "../../tcad/viewer/hosts/web.js";

const id = "sha256:" + "f".repeat(64);
const scene = { model_id: "part", version: 3, artifact_id: id, status: "verified",
  mesh: { vertices: [[1,0,0], [2,0,0], [1,1,0]], facets: [[0,1,2]] } };

test("portable artifact loader pins model and artifact without consulting source", async () => {
  const urls = [];
  const loaded = await loadArtifactScene({ modelId: "part", artifactId: id, baseUrl: "/api",
    fetcher: async url => { urls.push(url); return { ok: true, json: async () => scene }; } });
  assert.equal(loaded, scene);
  assert.match(urls[0], /^\/api\/artifact-sets\/sha256%3A/);
  assert.match(urls[0], /model_id=part$/);
  await assert.rejects(loadArtifactScene({ modelId: "another", artifactId: id,
    fetcher: async () => ({ ok: true, json: async () => scene }) }), /不匹配/);
});

test("portable host ignores late scene responses and disposes pending loads", async () => {
  const pending = [];
  const host = Object.create(ArtifactViewport.prototype);
  const drawn = [];
  Object.assign(host, { sequence: 0, request: null, artifact: null,
    viewport: { setMesh: mesh => drawn.push(mesh), dispose: () => {} } });
  const selection = { modelId: "part", artifactId: id,
    fetcher: () => new Promise(resolve => pending.push(resolve)) };
  const first = host.load(selection), latest = host.load(selection);
  pending[1]({ ok: true, json: async () => scene });
  await latest;
  pending[0]({ ok: true, json: async () => ({ ...scene, mesh: { stale: true } }) });
  assert.equal(await first, null);
  assert.equal(drawn.length, 1);
  const stopped = host.load(selection);
  host.dispose();
  pending[2]({ ok: true, json: async () => scene });
  assert.equal(await stopped, null);
  assert.equal(drawn.length, 1);
  await assert.rejects(host.load(selection), /disposed/);
  assert.throws(() => host.capture(), /active artifact/);
});

test("shared scene preparation derives bounds for a saved pose without drift", () => {
  const motion = [{ vertex_start: 0, vertex_count: 3, pivot: {x:0,y:0,z:0}, axis: {x:0,y:0,z:1}, ratio: 1 }];
  const posed = prepareScene({ mesh: scene.mesh, motion }, { angle: 90 });
  assert.ok(Math.abs(posed.bbox.x_min + 1) < 1e-8);
  assert.ok(Math.abs(posed.bbox.y_min - 1) < 1e-8);
  assert.deepEqual(prepareScene({ mesh: scene.mesh, motion }).vertices, scene.mesh.vertices);
  assert.throws(() => prepareScene(scene, { frame: 1 }), /没有原生动画/);
});
