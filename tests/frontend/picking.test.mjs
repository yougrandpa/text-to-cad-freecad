import test from "node:test";
import assert from "node:assert/strict";
import { OrbitCamera, projectPoint } from "../../tcad/viewer/core/camera.js";
import { meshBounds, poseMesh, poseAnimation } from "../../tcad/viewer/core/scene.js";
import { pickEntity, highlightVertices } from "../../tcad/viewer/core/picking.js";

const mesh = { vertices: [[-3,-1,0],[-1,-1,0],[-2,1,0],[1,-1,0],[3,-1,0],[2,1,0]], facets: [[0,1,2],[3,4,5]] };
const mapping = { entities: [
  {body_id:"a",entity_kind:"face",local_sub_id:"Face1",triangle_start:0,triangle_count:1,segments:[]},
  {body_id:"a",entity_kind:"edge",local_sub_id:"Edge1",triangle_start:0,triangle_count:0,segments:[[0,1]]},
  {body_id:"b",entity_kind:"face",local_sub_id:"Face1",triangle_start:1,triangle_count:1,segments:[]},
  {body_id:"b",entity_kind:"edge",local_sub_id:"Edge1",triangle_start:0,triangle_count:0,segments:[[3,4]]},
] };
function pick(m, point, kind="face") {
  const camera = new OrbitCamera(); camera.setPreset("top"); camera.setBounds(meshBounds(m.vertices),1);
  const [x,y] = projectPoint(point,camera,400,400);
  return pickEntity(m,mapping,camera,400,400,x,y,kind);
}

test("same local Face1 and Edge1 remain scoped to their body; misses and legacy scenes do not pick", () => {
  for (const [x,body] of [[-2,"a"],[2,"b"]]) {
    assert.equal(pick(mesh,[x,0,0]).body_id,body);
    assert.equal(pick(mesh,[x,-1,0],"edge").body_id,body);
    assert.deepEqual(pick(mesh,[x,0,0],"body"),{body_id:body,entity_kind:"body"});
  }
  assert.equal(pick(mesh,[0,0,0]),null);
  assert.equal(pickEntity(mesh,null,new OrbitCamera(),400,400,200,200),null);
});

test("picking and highlighter follow prescribed and repeated native instance poses", () => {
  const motion = [{body_id:"b",vertex_start:3,vertex_count:3,pivot:{x:0,y:0,z:0},axis:{x:0,y:0,z:1},ratio:1}];
  const posed = poseMesh(mesh,motion,90);
  assert.equal(pick(posed,[0,2,0]).body_id,"b");
  const transform = x => [1,0,0,x,0,1,0,0,0,0,1,0,0,0,0,1];
  const animation = {parts:[{body_id:"a",vertex_start:0,vertex_count:3},{body_id:"b",vertex_start:3,vertex_count:3}],
    frames:[{a:transform(0),b:transform(0)},{a:transform(0),b:transform(4)}]};
  const native = poseAnimation(mesh,animation,1);
  assert.equal(pick(native,[6,0,0]).body_id,"b");
  assert.equal(pick(native,[2,0,0]),null);
  const highlights = highlightVertices(native,mapping,mapping.entities[2],[0,0,0]);
  assert.ok([...highlights].filter((_,i) => i%3 === 0).every(x => x >= 5));
  assert.deepEqual(mesh.vertices[3],[1,-1,0]);
});

test("front face wins and hidden edges cannot be picked through the solid", () => {
  const stacked = {vertices:[[-1,-1,0],[1,-1,0],[0,1,0],[-1,-1,1],[1,-1,1],[0,1,1]],facets:[[0,1,2],[3,4,5]]};
  assert.equal(pick(stacked,[0,0,1]).body_id,"b");
  assert.equal(pick(stacked,[0,-1,1],"edge").body_id,"b");
});

test("triangle subdivision preserves the frozen face identity", () => {
  const refined = {vertices:[...mesh.vertices,[-2,-1,0]],facets:[[0,6,2],[6,1,2],[3,4,5]]};
  const index = {entities:mapping.entities.map(e => e.entity_kind === "face" ? {...e,
    triangle_start:e.body_id === "a" ? 0 : 2,triangle_count:e.body_id === "a" ? 2 : 1} : e)};
  const camera = new OrbitCamera(); camera.setPreset("top"); camera.setBounds(meshBounds(refined.vertices),1);
  const [x,y] = projectPoint([-2,0,0],camera,400,400);
  const hit = pickEntity(refined,index,camera,400,400,x,y);
  assert.equal(hit.body_id,"a"); assert.equal(hit.local_sub_id,"Face1");
});
