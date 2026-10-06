import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { ReferenceController } from "../../tcad/server/ui/references.mjs";

class Node {
  constructor() { this.children = []; this.listeners = {}; this.disabled = false; }
  replaceChildren() { this.children = []; }
  append(...children) { this.children.push(...children); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  click() { if (!this.disabled) this.listeners.click?.(); }
  focus() {}
}
function catalog(version = 0, attempt = "a") {
  const identity = { model_id: "plate", ir_version: version, artifact_id: "sha256:" + attempt.repeat(64) };
  return { ...identity, targets: Array.from({ length: 9 }, (_, i) => ({ label: `孔 ${i}`, editable: true,
    ref: { ...identity, schema_version: 1, body_id: "body", entity_kind: "sketch", sketch_id: `hole${i}` } })) };
}
function harness() {
  const box = new Node(), notices = [];
  const controller = new ReferenceController(box, { notice: (n) => notices.push(n), focus: () => {},
    document: { createElement: () => new Node() } });
  return { controller, box, notices };
}

test("actual buttons add, deduplicate, limit, remove and clear safe reference chips", () => {
  const { controller: c, box, notices } = harness();
  c.update(catalog(), "plate", 0);
  for (let i = 0; i < 9; i++) c.button("body", "sketch", `hole${i}`).click();
  assert.equal(c.context().selection_refs.length, 8);
  assert.equal(notices.length, 1);
  c.button("body", "sketch", "hole0").click();
  assert.equal(c.context().selection_refs.length, 8);
  const copy = c.context(); copy.selection_refs[0].sketch_id = "forged";
  assert.equal(c.context().selection_refs[0].sketch_id, "hole0");
  box.children[0].click();
  assert.equal(c.context().selection_refs.length, 7);
  box.children.at(-1).click();
  assert.equal(c.context(), null); assert.equal(box.hidden, true);
});

test("buttons rebuilt while busy are re-enabled together with chips", () => {
  const { controller: c, box } = harness();
  c.update(catalog(), "plate", 0);
  c.button("body", "sketch", "hole0").click();
  c.setBusy(true);
  c.beginTree();
  const newButton = c.button("body", "sketch", "hole1");
  assert.equal(newButton.disabled, true); assert.equal(box.children[0].disabled, true);
  c.setBusy(false);
  assert.equal(newButton.disabled, false); assert.equal(box.children[0].disabled, false);
  newButton.click(); assert.equal(c.context().selection_refs.length, 2);
});

test("new versions, attempts, unavailable publications and switches invalidate references", () => {
  for (const next of [catalog(1), catalog(0, "b"), null, { ...catalog(), model_id: "other" }]) {
    const { controller: c } = harness(); c.update(catalog(), "plate", 0);
    const oldButton = c.button("body", "sketch", "hole0"); oldButton.click();
    c.update(next, "plate", next?.ir_version ?? 0);
    assert.equal(c.context(), null);
    oldButton.click(); assert.equal(c.context(), null);
    c.reset(); assert.equal(c.button("body", "sketch", "hole0"), null);
  }
});

test("app sends pinned context and unlocks a tree rebuilt during its result handler", async () => {
  const source = (await readFile(new URL("../../tcad/server/ui/app.js", import.meta.url), "utf8"))
    .replace(/^import .*;$/gm, "").replace(/\nboot\(\);\s*$/, "");
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({ ReferenceController, AbortController, console,
    document: { getElementById: get, createElement: () => new Node() } });
  vm.runInContext(source + `
    globalThis.testing = {state, referenceUI, send,
      stub(stream, result) {
        ensureModel=async()=>{}; streamChat=stream; handleResult=result;
        pushUser=()=>{}; pushNotice=()=>{}; renderSessions=()=>{}; setStatus=()=>{}; setLive=()=>{}; clearLive=()=>{};
      }};`, context);
  const h = context.testing; h.state.threadId = "thread"; h.state.modelId = "plate";
  const c = h.referenceUI(); c.update(catalog(), "plate", 0); c.button("body", "sketch", "hole0").click();
  let payload, rebuilt;
  h.stub(async (p, handlers) => { payload = p; handlers.result({}); }, async () => {
    assert.equal(h.state.busy, true);
    c.update(catalog(1), "plate", 1); c.beginTree(); rebuilt = c.button("body", "sketch", "hole0");
    assert.equal(rebuilt.disabled, true);
  });
  await h.send("直径改成 8 毫米");
  assert.equal(payload.selection_context.selection_refs[0].ir_version, 0);
  assert.ok(payload.operation_id); assert.equal(payload.kind, "modify");
  assert.equal(c.context(), null); assert.equal(rebuilt.disabled, false);
  rebuilt.click(); assert.equal(c.context().selection_refs[0].ir_version, 1);
});

test("geometry chips validate publication, remain read-only and clear their highlight", () => {
  const {controller:c,box} = harness();
  const targets = catalog();
  const ref = {...targets.targets[0].ref,entity_kind:"face",sketch_id:null,local_sub_id:"Face1"};
  targets.targets.push({ref,label:"面 Face1",editable:false});
  targets.targets.push({ref:{...ref,entity_kind:"body",local_sub_id:null},label:"板",editable:true});
  let highlighted=[];
  c.onChange = refs => { highlighted=refs; };
  c.update(targets,"plate",0);
  const hit = {body_id:"body",entity_kind:"face",local_sub_id:"Face1"};
  c.selectGeometry(hit,"sha256:"+"b".repeat(64)); assert.equal(c.context(),null);
  c.selectGeometry(hit,targets.artifact_id);
  assert.equal(c.inspectionOnly(),true); assert.equal(highlighted[0].local_sub_id,"Face1");
  c.setBusy(true); box.children[0].click(); assert.equal(highlighted.length,1);
  c.setBusy(false); box.children[0].click(); assert.equal(highlighted.length,0);
  assert.equal(c.selectGeometry({body_id:"body",entity_kind:"body"},targets.artifact_id),true);
  assert.equal(c.inspectionOnly(),false);
});
