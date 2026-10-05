import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url),'utf8'))
  .replace(/^import .*;$/gm,'').replace(/\nboot\(\);\s*$/,'');
const { validateArtifactScene } = await import(new URL('../../tcad/viewer/core/artifact.js', import.meta.url));
const deferred = () => { let resolve; const promise = new Promise((r)=>{resolve=r;}); return {promise,resolve}; };
const response = (body,status=200) => ({ok:status===200,status,statusText:'Error',json:async()=>body.model_id
  ? {artifact_id:'sha256:'+'f'.repeat(64),status:'verified',...body} : body,blob:async()=>({})});
function harness(fetch) {
  const nodes = new Map(), tabs = ['iso','front','back','left','right','top','bottom'].map((view)=>({dataset:{view},classList:{toggle:()=>{}},setAttribute:()=>{}}));
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id,{hidden:id!=='viewPlaceholder',textContent:'',classList:{toggle:()=>{}},removeAttribute:()=>{},setAttribute:()=>{}});
    return nodes.get(id);
  };
  const drawn=[], revoked=[];
  const viewer = {available:true,hasMesh:false,camera:{},error:'',
    clear(){this.hasMesh=false;},setMesh(mesh){drawn.push(mesh);this.hasMesh=true;return 12;}};
  const context = vm.createContext({document:{getElementById:node,querySelectorAll:()=>tabs},fetch,
    validateArtifactScene,URLSearchParams,AbortController,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL:(url)=>revoked.push(url)},console});
  vm.runInContext(source + '\n globalThis.testing={state,loadView,applyToolImages,displayImage,cancelViewRequest,setViewer:(value)=>{meshViewer=value;}};',context);
  const api=context.testing; api.setViewer(viewer); api.state.modelId='model-A'; api.state.sessionEpoch=1;
  return {...api,node,viewer,drawn,revoked};
}

test('mesh loads without cached state.version and verifies model identity',async()=>{
  const urls=[]; const h=harness(async(url)=>{urls.push(url);return response({model_id:'model-A',version:3,mesh:{id:'v3'}});});
  await h.loadView(false); assert.equal(h.drawn[0].id,'v3'); assert.equal(h.state.version,null);
  assert.match(urls[0],/\/mesh\?force=false$/); assert.equal(h.node('viewPlaceholder').hidden,true);
});

test('out-of-order same-session response cannot replace the newest mesh',async()=>{
  const a=deferred(),b=deferred();let i=0;
  const h=harness(()=>[a,b][i++].promise);
  const first=h.loadView(false),second=h.loadView(false);
  b.resolve(response({model_id:'model-A',version:2,mesh:{id:'new'}}));await second;
  a.resolve(response({model_id:'model-A',version:1,mesh:{id:'old'}}));await first;
  assert.equal(h.drawn.length,1);assert.equal(h.drawn[0].id,'new');
});

test('session epoch also rejects an already-resolved stale JSON body',async()=>{
  const body=deferred(); const h=harness(async()=>({ok:true,status:200,json:()=>body.promise}));
  const pending=h.loadView(false);await Promise.resolve();h.state.sessionEpoch++;h.state.modelId='model-B';
  body.resolve({model_id:'model-A',version:3,mesh:{id:'old'}});await pending;
  assert.equal(h.drawn.length,0);
});

test('mesh error uses genuine PNG fallback with a clear status',async()=>{
  const urls=[];const h=harness(async(url)=>{urls.push(url);return url.includes('/mesh?')?response({detail:'worker unavailable'},502):response({});});
  await h.loadView(true);assert.equal(urls.length,2);assert.match(urls[1],/\/render\?/);
  assert.match(h.node('viewStatus').textContent,/静态预览/);assert.equal(h.node('viewImage').src,'blob:test');
  h.node('viewImage').onload();assert.equal(h.node('viewImage').hidden,false);assert.equal(h.node('fitView').disabled,true);
});

test('empty geometry does not show an old model or a misleading fallback',async()=>{
  let calls=0;const h=harness(async()=>{calls++;return response({detail:'no solid'},422);});
  h.viewer.hasMesh=true;await h.loadView(false);assert.equal(calls,1);assert.equal(h.viewer.hasMesh,false);
  assert.match(h.node('viewPlaceholder').textContent,/还没有几何/);
});

test('tool-generated PNG does not force the interactive camera to its view',async()=>{
  const pending=deferred();const h=harness(()=>pending.promise);h.state.view='free';
  h.applyToolImages({images:[{url:'/old-front.png',view:'front'}]});
  assert.equal(h.state.view,'free');assert.equal(h.node('viewImage').src,undefined);
  pending.resolve(response({model_id:'model-A',version:3,mesh:{id:'new'}}));await new Promise((r)=>setImmediate(r));
  assert.equal(h.drawn.length,1);
});

test('no WebGL bypasses mesh, and stale PNG image callbacks are ignored',async()=>{
  const urls=[];const h=harness(async(url)=>{urls.push(url);return response({});});h.viewer.available=false;
  await h.loadView(false);assert.equal(urls.length,1);assert.match(urls[0],/\/render\?/);
  const onload=h.node('viewImage').onload;h.state.sessionEpoch++;onload();
  assert.equal(h.node('viewImage').hidden,true);
});

test('explicit version request rejects a wrong-version response',async()=>{
  const urls=[];const h=harness(async(url)=>{urls.push(url);return url.includes('/mesh?')
    ?response({model_id:'model-A',version:7,mesh:{id:'wrong'}}):response({});});
  await h.loadView(false,{version:8});assert.equal(h.drawn.length,0);
  assert.match(urls[0],/version=8/);assert.match(h.node('viewStatus').textContent,/不匹配/);
});

test('busy mesh admission never bypasses limits through PNG fallback',async()=>{
  let calls=0;const h=harness(async()=>{calls++;return response({detail:'preview capacity reached'},429);});
  h.viewer.hasMesh=true;
  await h.loadView(true);
  assert.equal(calls,1);assert.equal(h.viewer.hasMesh,true);
  assert.match(h.node('viewStatus').textContent,/仍显示此前几何/);
});

test('viewer displays the saved build verification status',async()=>{
  const h=harness(async()=>response({model_id:'model-A',version:3,mesh:{id:'saved'},status:'verified'}));
  await h.loadView(false);
  assert.match(h.node('viewStatus').textContent,/构建 v3 · 几何已验证/);
  assert.doesNotMatch(h.node('viewStatus').textContent,/IR v/);
});

test('PNG fallback stays pinned to the mesh artifact when GPU loading fails',async()=>{
  const id='sha256:'+'a'.repeat(64),urls=[];
  const h=harness(async(url)=>{
    urls.push(url);
    if(url.includes('/mesh?')) return response({model_id:'model-A',version:3,artifact_id:id,mesh:{id:'saved'}});
    return {...response({}),headers:{get:(name)=>({'X-Artifact-ID':id,'X-Artifact-Version':'3','X-Artifact-Status':'verified'}[name]??null)}};
  });
  h.viewer.setMesh=()=>{throw new Error('GPU allocation failed');};
  await h.loadView(false);
  assert.match(urls[1],/artifact_id=sha256%3A/);
  assert.equal(h.node('viewImage').src,'blob:test');
  assert.match(h.node('viewStatus').textContent,/静态预览 · 构建 v3 · 几何已验证/);
});

test('PNG fallback refuses a different artifact identity',async()=>{
  const h=harness(async(url)=>url.includes('/mesh?')
    ?response({model_id:'model-A',version:3,mesh:{id:'saved'}})
    :{...response({}),headers:{get:()=> 'sha256:'+'b'.repeat(64)}});
  h.viewer.setMesh=()=>{throw new Error('GPU allocation failed');};
  await h.loadView(false);
  assert.equal(h.node('viewImage').src,undefined);
  assert.match(h.node('viewPlaceholder').textContent,/身份不匹配/);
});
