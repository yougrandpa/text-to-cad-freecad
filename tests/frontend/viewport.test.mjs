import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Load the shipped ES module directly, without adding npm/package.json or a
// second compiled implementation. This also works on Node versions that treat
// .js files as CommonJS unless an enclosing package opts into ESM.
const source = await readFile(new URL('../../tcad/server/ui/viewport.js', import.meta.url), 'utf8');
const { PRESETS, cameraBasis, OrbitCamera, projectPoint, prepareMesh, meshBounds, MeshViewport } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const cube = {
  vertices: [[0,0,0],[2,0,0],[2,2,0],[0,2,0],[0,0,2],[2,0,2],[2,2,2],[0,2,2]],
  facets: [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]],
};
const close = (actual, expected, eps = 1e-8) => assert.ok(Math.abs(actual - expected) < eps, `${actual} != ${expected}`);
const dot = (a,b) => a.reduce((sum,v,i) => sum + v*b[i],0);

test('all seven presets are orthonormal and right-handed, including poles', () => {
  assert.equal(Object.keys(PRESETS).length, 7);
  for (const [yaw,pitch] of Object.values(PRESETS)) {
    const { right:r, up:u, look:l } = cameraBasis(yaw,pitch);
    for (const a of [r,u,l]) close(dot(a,a),1);
    close(dot(r,u),0); close(dot(u,l),0); close(dot(l,r),0);
    const cross = [r[1]*u[2]-r[2]*u[1],r[2]*u[0]-r[0]*u[2],r[0]*u[1]-r[1]*u[0]];
    cross.forEach((v,i) => close(v,l[i]));
  }
});

test('front/top/right match the headless renderer and do not mirror a part', () => {
  const camera = new OrbitCamera();
  camera.setPreset('front');
  assert.ok(projectPoint([1,0,0],camera,400,400)[0] > 200);
  assert.ok(projectPoint([0,0,1],camera,400,400)[1] < 200);
  close(projectPoint([0,-1,0],camera,400,400)[2],1);
  camera.setPreset('top');
  assert.ok(projectPoint([1,0,0],camera,400,400)[0] > 200);
  assert.ok(projectPoint([0,1,0],camera,400,400)[1] < 200);
  camera.setPreset('right');
  assert.ok(projectPoint([0,1,0],camera,400,400)[0] > 200);
  assert.ok(projectPoint([0,0,1],camera,400,400)[1] < 200);
});

test('fit contains every corner for all presets and portrait/landscape panes', () => {
  const bounds = meshBounds([[-120,5,-2],[470,12,43]]);
  for (const view of Object.keys(PRESETS)) for (const [width,height] of [[900,280],[240,700],[500,500]]) {
    const camera = new OrbitCamera(); camera.setPreset(view); camera.setBounds(bounds,width/height);
    for (const p of bounds.corners) {
      const [x,y] = projectPoint(p,camera,width,height);
      assert.ok(x > 0 && x < width && y > 0 && y < height, `${view}: ${x},${y}`);
    }
  }
});

test('orbit/pan/zoom persist across version changes and reset for a new session', () => {
  const camera = new OrbitCamera(); camera.setBounds(meshBounds(cube.vertices),1.5);
  camera.orbit(78,-21); camera.pan(23,-46,500); camera.zoom(0.55);
  const before = { target:[...camera.target], yaw:camera.yaw, pitch:camera.pitch, span:camera.halfHeight };
  camera.setBounds(meshBounds([[100,100,100],[500,500,500]]),1.5);
  assert.deepEqual(camera.target,before.target); close(camera.yaw,before.yaw); close(camera.pitch,before.pitch); close(camera.halfHeight,before.span);
  camera.fit(1.5); assert.deepEqual(camera.target,[300,300,300]);
  camera.reset(); assert.equal(camera.view,'iso'); assert.equal(camera.bounds,null);
  camera.setBounds(meshBounds(cube.vertices),1.5); assert.deepEqual(camera.target,[1,1,1]);
});

test('zoom and pole rotation cannot produce NaN or cross the model', () => {
  const camera = new OrbitCamera(); camera.setBounds(meshBounds(cube.vertices));
  camera.zoom(1e-100); assert.ok(camera.halfHeight > 0);
  camera.zoom(Infinity); assert.ok(Number.isFinite(camera.halfHeight));
  camera.orbit(100000,-100000); assert.ok(camera.pitch >= -Math.PI/2);
  const p = projectPoint([0,0,0],camera,400,400); assert.ok(p.every(Number.isFinite));
});

test('cube retains real feature edges without coplanar triangle diagonals', () => {
  const result = prepareMesh(cube);
  assert.equal(result.triangleCount,12);
  assert.equal(result.triangles.length,12*3*6);
  assert.equal(result.edges.length,12*2*3);
  assert.deepEqual(result.bounds.center,[1,1,1]);
});

test('large origin is removed before Float32 upload; corrupt geometry is rejected', () => {
  const shifted = { ...cube, vertices:cube.vertices.map((p) => p.map((v) => v+1e9)) };
  const result = prepareMesh(shifted);
  close(result.triangles[0],-1);
  for (const mesh of [{vertices:[],facets:[]},{vertices:[[NaN,0,0]],facets:[[0,0,0]]},
    {...cube, facets:[[0,1,99]]}, {...cube, facets:[[-1,1,2]]}, {...cube, facets:[[0,0,0]]}]) {
    assert.throws(() => prepareMesh(mesh));
  }
});

test('actual pointer, wheel, keyboard handlers support orbit, pan, zoom and fit', () => {
  const handlers = new Map();
  const originalAdd = globalThis.addEventListener, originalRemove = globalThis.removeEventListener;
  globalThis.addEventListener = () => {}; globalThis.removeEventListener = () => {};
  try {
    const canvas = { getContext:()=>null, addEventListener:(name,cb)=>handlers.set(name,cb), removeEventListener:()=>{},
      classList:{add:()=>{},remove:()=>{}}, focus:()=>{}, setPointerCapture:()=>{}, clientWidth:600,clientHeight:400 };
    const viewer = new MeshViewport(canvas);
    assert.equal(viewer.available,false); // graceful no-WebGL construction
    viewer.hasMesh = true; viewer.camera.setBounds(meshBounds(cube.vertices),1.5);
    const event = (extra={}) => ({preventDefault:()=>{},pointerId:1,button:0,clientX:100,clientY:100,...extra});
    const yaw = viewer.camera.yaw;
    handlers.get('pointerdown')(event()); handlers.get('pointermove')(event({clientX:130}));
    assert.notEqual(viewer.camera.yaw,yaw); handlers.get('pointerup')(event());
    const target = [...viewer.camera.target];
    handlers.get('pointerdown')(event({button:2})); handlers.get('pointermove')(event({clientY:140}));
    assert.notDeepEqual(viewer.camera.target,target); handlers.get('pointerup')(event());
    const span = viewer.camera.halfHeight;
    handlers.get('wheel')(event({deltaY:-100,deltaMode:0})); assert.ok(viewer.camera.halfHeight<span);
    handlers.get('keydown')(event({key:'5'})); assert.equal(viewer.camera.view,'top');
    handlers.get('keydown')(event({key:'f'})); assert.deepEqual(viewer.camera.target,[1,1,1]);
    viewer.clear({resetCamera:true}); assert.equal(viewer.camera.bounds,null); assert.equal(viewer.pointers.size,0);
  } finally { globalThis.addEventListener=originalAdd; globalThis.removeEventListener=originalRemove; }
});
