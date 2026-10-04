import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../../tcad/server/ui/viewport.js', import.meta.url), 'utf8');
const { poseMesh } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const motion = (start, ratio, pivot = {x:0,y:0,z:0}) => ({body_id: `body-${start}`, vertex_start:start,
  vertex_count:1, pivot, axis:{x:0,y:0,z:2}, ratio});
const close = (point, expected) => point.forEach((v,i) => assert.ok(Math.abs(v-expected[i])<1e-9));

test('crank and external driven gear rotate with signed ratio while housing stays fixed', () => {
  const original = {vertices:[[5,5,5],[1,0,0],[11,0,0]],facets:[[0,1,2]]};
  const parts = [motion(1,1),motion(2,-0.5,{x:10,y:0,z:0})];
  const posed = poseMesh(original,parts,180);
  close(posed.vertices[0],[5,5,5]); close(posed.vertices[1],[-1,0,0]); close(posed.vertices[2],[10,-1,0]);
  close(original.vertices[1],[1,0,0]);
  // Repeated slider movement always starts at the reference pose; no accumulated drift.
  close(poseMesh(original,parts,360).vertices[1],[1,0,0]);
});

test('rotation works about offset Y axis and preserves length', () => {
  const mesh={vertices:[[31,-12,38]],facets:[]};
  const part={...motion(0,1,{x:28,y:0,z:38}),axis:{x:0,y:1,z:0}};
  const p=poseMesh(mesh,[part],90).vertices[0];
  close(p,[28,-12,35]); assert.ok(Math.abs(Math.hypot(p[0]-28,p[1],p[2]-38)-Math.hypot(3,-12))<1e-9);
});

test('invalid, overlapping, or out-of-bounds ranges are rejected', () => {
  const mesh={vertices:[[1,0,0]],facets:[]};
  for (const parts of [[{...motion(0,1),axis:{x:0,y:0,z:0}}],[motion(1,1)],
      [motion(0,1),motion(0,2)],[motion(0,Infinity)]]) assert.throws(()=>poseMesh(mesh,parts,0));
  assert.throws(()=>poseMesh(mesh,[motion(0,1)],NaN));
});
