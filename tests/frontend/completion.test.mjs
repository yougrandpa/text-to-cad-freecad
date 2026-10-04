import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
const source = (await readFile(new URL('../../tcad/server/ui/app.js',import.meta.url),'utf8'))
  .replace(/^import .*;$/m,'').replace(/\nboot\(\);\s*$/,'');
function harness() {
  const notices=[],verdicts=[],statuses=[],nodes=new Map();
  const node=(id)=>{if(!nodes.has(id)) nodes.set(id,{textContent:''});return nodes.get(id);};
  const context=vm.createContext({document:{getElementById:node},console});
  vm.runInContext(source+`
    globalThis.testing={state,handleResult,pushHookLine,
      stub:function(notice,verdict,status){
        pushNotice=notice;pushVerdict=verdict;setStatus=status;
        pushGateCard=()=>{};append=()=>{};el=()=>({});
        refreshInspector=async()=>{};loadArtifacts=()=>{};loadView=()=>{};
        loadSessions=async()=>{};setSessionHeader=()=>{};
      }};`,context);
  context.testing.stub((...args)=>notices.push(args),(...args)=>verdicts.push(args),(...args)=>statuses.push(args));
  return {...context.testing,notices,verdicts,statuses};
}
test('legacy geometric success cannot claim all-green functional completion',async()=>{
  const h=harness();await h.handleResult({thread_id:'t',state:'succeeded',gate_report:{passed:true},steps:1});
  assert.match(h.verdicts[0][1],/功能待验收/);
  assert.ok(!JSON.stringify(h.verdicts).includes('Gate 全绿'));
  assert.equal(h.statuses[0][1],'构建通过');
});
test('draft exposes unresolved objectives without a completion badge',async()=>{
  const h=harness();await h.handleResult({thread_id:'t',state:'draft',steps:4,completion_review:{
    verified:false,summary:'已完成箱体草稿',checklist:[{source_text:'削铅笔',check_ids:[]}],
    remaining_work:['刀片及实际切削性能未验证'],note:'实际功能需验收'}});
  assert.match(h.verdicts[0][1],/草稿.*待验收/);
  assert.ok(h.notices.some(([,text])=>text.includes('实际切削性能未验证')));
  assert.equal(h.statuses[0][1],'草稿 · 待验收');
});
test('measured success labels its recorded-constraint scope',async()=>{
  const h=harness();await h.handleResult({thread_id:'t',state:'succeeded',steps:2,completion_review:{
    verified:true,summary:'尺寸通过',checklist:[],remaining_work:[],note:'功能仍需验收'}});
  assert.match(h.verdicts[0][1],/已记录.*约束验收通过/);
  assert.equal(h.statuses[0][1],'约束验收通过');
});
test('default no-hook allow events are quiet',()=>{
  const h=harness();h.pushHookLine({decision:'allow',hook:'<no-hooks>',event:'pre_step'});
  assert.equal(h.notices.length,0);
});
