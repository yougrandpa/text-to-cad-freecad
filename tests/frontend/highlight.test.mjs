import test from "node:test";
import assert from "node:assert/strict";
import { TreeHighlighter } from "../../tcad/server/ui/highlight.mjs";
import { highlightVertices } from "../../tcad/viewer/core/picking.js";

const sketch = { body_id: "cup", entity_kind: "sketch", sketch_id: "handle" };
const overlay = (nodeId = "handle") => ({ artifact_id: "build", body_id: "cup",
  entity_kind: "sketch", node_id: nodeId, vertices: [[2,0,0],[3,0,0]] });
const response = data => ({ ok: true, json: async () => data });
function harness(fetch) {
  const viewer = { hasMesh: true, artifactId: "build", selectedEntity: { entity_kind: "body" }, schedule() {} };
  const notices = [];
  const controller = new TreeHighlighter(viewer, { fetch, notice: message => notices.push(message) });
  return { viewer, controller, notices };
}

test("browser fetch keeps its global receiver", async () => {
  const { viewer, controller } = harness(async function () {
    assert.equal(this, globalThis);
    return response(overlay());
  });
  await controller.select(sketch, "build", "model");
  assert.equal(viewer.selectedEntity.entity_kind, "sketch");
});

test("sketch selection uses its own outline, caches it, and never falls back to the body", async () => {
  const urls = [];
  const { viewer, controller } = harness(async url => { urls.push(url); return response(overlay()); });
  await controller.select(sketch, "build", "model");
  assert.equal(viewer.selectedEntity.entity_kind, "sketch");
  assert.deepEqual([...highlightVertices({}, null, viewer.selectedEntity, [1,0,0])], [1,0,0,2,0,0]);
  await controller.select(sketch, "build", "model");
  assert.equal(urls.length, 1);
  assert.match(urls[0], /body_id=cup&kind=sketch&node_id=handle/);
  await controller.select({ body_id: "cup", entity_kind: "body" }, "build", "model");
  assert.equal(viewer.selectedEntity.entity_kind, "body");
  await controller.select(null, "build", "model");
  assert.equal(viewer.selectedEntity, null);
});

test("late feature responses cannot restore a cleared or different selection", async () => {
  const pending = [];
  const { viewer, controller } = harness(() => new Promise(resolve => pending.push(resolve)));
  const first = controller.select(sketch, "build", "model");
  const second = controller.select({ ...sketch, sketch_id: "other" }, "build", "model");
  pending[1](response(overlay("other"))); await second;
  pending[0](response(overlay())); await first;
  assert.equal(viewer.selectedEntity.sketch_id, "other");
  const third = controller.select({ ...sketch, sketch_id: "third" }, "build", "model");
  await controller.select(null, "build", "model");
  pending[2](response(overlay("third"))); await third;
  assert.equal(viewer.selectedEntity, null);
  const fourth = controller.select({ ...sketch, sketch_id: "fourth" }, "build", "model");
  viewer.artifactId = "new-build";
  pending[3](response(overlay("fourth"))); await fourth;
  assert.equal(viewer.selectedEntity, null);
});

test("failed, mismatched, and empty overlays never highlight the whole part", async () => {
  for (const result of [{ ok: false }, response({ ...overlay(), artifact_id: "stale" }),
    response({ ...overlay(), vertices: [[NaN,0,0],[1,0,0]] })]) {
    const { viewer, controller, notices } = harness(async () => result);
    await controller.select(sketch, "build", "model");
    assert.equal(viewer.selectedEntity, null);
    assert.equal(notices.length, 1);
  }
  const { viewer, controller } = harness(async () => response({ ...overlay(), vertices: [] }));
  await controller.select(sketch, "build", "model");
  assert.equal(highlightVertices({}, null, viewer.selectedEntity, [0,0,0]).length, 0);
});

test("feature outlines follow prescribed and native body poses without drift", () => {
  const selected = { ...sketch, overlay: overlay() };
  const motion = [{ body_id: "cup", pivot: { x:0,y:0,z:0 }, axis: { x:0,y:0,z:1 }, ratio: 1 }];
  const rotated = highlightVertices({}, null, selected, [0,0,0], { motion, value: 90 });
  assert.ok(Math.abs(rotated[0]) < 1e-6); assert.equal(rotated[1], 2);
  const animation = { frames: [{ cup: [1,0,0,5,0,1,0,0,0,0,1,0,0,0,0,1] }] };
  assert.deepEqual([...highlightVertices({}, null, selected, [0,0,0], { animation })], [7,0,0,8,0,0]);
  assert.deepEqual(selected.overlay.vertices, [[2,0,0],[3,0,0]]);
});
