import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFile } from "node:fs/promises";
import { buildModelTree, filterModelTree, StructureController } from "../../tcad/server/ui/structure.mjs";

const ir = { model_id: "plate", version: 3, bodies: [
  { id: "base", name: "安装底板", sketches: [
    { id: "outline", name: "底板轮廓", plane: { plane: "XY" }, geometry: [1, 2, 3, 4], constraints: [1] },
    { id: "hole", name: "孔位草图", plane: { kind: "face", feature_id: "pad", sub: "Face6" } },
    { id: "unused", name: "备用草图" },
  ], features: [
    { id: "pad", name: "底板拉伸", op: "pad", profile_sketch: "outline", params: { length: 8 } },
    { id: "pocket", name: "安装孔", op: "pocket", profile_sketch: "hole", refs: ["pad"], suppress: true },
    { id: "shared", name: "复用轮廓", op: "pad", profile_sketch: "outline" },
  ] },
  { id: "instance", name: "引用垫圈", part_ref: { model_id: "washer", body_id: "washer1" } },
] };
const walk = nodes => nodes.flatMap(node => [node, ...walk(node.children)]);

test("history keeps body ownership and feature order while nesting each sketch exactly once", () => {
  const tree = buildModelTree(ir);
  assert.deepEqual(tree.map(n => n.id), ["base", "instance"]);
  assert.deepEqual(tree[0].children.map(n => n.id), ["unused", "pad", "pocket", "shared"]);
  assert.deepEqual(tree[0].children.slice(1).map(n => n.order), [1, 2, 3]);
  assert.equal(tree[0].children[1].children[0].id, "outline");
  assert.equal(walk(tree).filter(n => n.id === "outline").length, 1);
  assert.equal(tree[0].children[2].suppressed, true);
  assert.equal(tree[1].type, "引用零件");
});

test("loft history nests every ordered section and details preserve section names", () => {
  const model = { model_id: "loft", version: 1, bodies: [{ id: "shell", name: "外壳",
    sketches: ["start", "middle", "end"].map(id => ({ id, name: `截面 ${id}` })),
    features: [{ id: "skin", name: "连续外壳", op: "additive_loft", profile_sketch: "start", sections: ["middle", "end"] }] }] };
  const nodes = buildModelTree(model);
  assert.equal(nodes[0].children[0].type, "放样");
  assert.deepEqual(nodes[0].children[0].children.map(n => n.id), ["start", "middle", "end"]);
  assert.equal(walk(nodes).filter(n => n.kind === "sketch").length, 3);
  const { c, detail } = controller();
  c.update(model); c.select(c.walk().find(n => n.id === "skin"));
  assert.match(textOf(detail), /放样截面 截面 middle → 截面 end/);
});

test("filter matches Chinese labels, raw IDs and operation types and retains their ancestors", () => {
  const tree = buildModelTree(ir);
  assert.deepEqual(filterModelTree(tree, "孔位")[0].children.map(n => n.id), ["pocket"]);
  assert.equal(filterModelTree(tree, "孔位")[0].children[0].children[0].id, "hole");
  assert.deepEqual(filterModelTree(tree, "PAD")[0].children.map(n => n.id), ["pad", "shared"]);
  assert.equal(filterModelTree(tree, "安装底板")[0].children.length, 4);
  assert.deepEqual(filterModelTree(tree, "no-match"), []);
  assert.equal(tree[0].children.length, 4);
});

class Node {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.attributes = {}; this.value = ""; this.textContent = "";
    this.classList = { toggle: (name, on) => {
      const names = new Set(this.className.split(" ")); on ? names.add(name) : names.delete(name);
      this.className = [...names].join(" ");
    } };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(key, value) { this.attributes[key] = value; }
  scrollIntoView(options) { this.scrolled = options; }
  querySelectorAll(selector) {
    const name = selector.slice(1);
    return this.children.flatMap(child => [...(child.className.split(" ").includes(name) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  click() { this.listeners.click?.({ preventDefault() {}, stopPropagation() {} }); }
}
const textOf = node => [node.textContent, ...node.children.map(textOf)].join(" ");
function controller() {
  const elements = Object.fromEntries(["tree", "detail", "search", "meta", "count", "expand", "collapse"].map(key => [key, new Node()]));
  const selections = [];
  const c = new StructureController(elements, { document: { createElement: tag => new Node(tag) },
    onSelect: (node, artifact) => selections.push([node?.id, artifact]) });
  return { c, ...elements, selections };
}

test("rendered selection shows parameters, dependencies and safe names without altering IR", () => {
  const { c, tree, detail, selections } = controller();
  const original = JSON.stringify(ir);
  c.update(ir, { artifactId: "saved", status: "verified" });
  const selected = tree.querySelectorAll(".structure-select").find(button => button.dataset.key.includes('"pad"'));
  selected.click();
  assert.deepEqual(selected.scrolled, { block: "nearest", inline: "nearest" });
  assert.match(textOf(detail), /长度 \(mm\) 8/);
  assert.equal(selections.at(-1)[0], "pad");
  assert.equal(selections.at(-1)[1], "saved");
  c.select(c.walk().find(node => node.id === "pocket"));
  assert.match(textOf(detail), /依赖特征 底板拉伸/);
  assert.match(textOf(detail), /状态 已抑制/);
  c.select(c.walk().find(node => node.id === "hole"));
  assert.match(textOf(detail), /附着平面 底板拉伸 · Face6/);
  assert.equal(JSON.stringify(ir), original);
});

test("search, collapse, geometry selection and model changes retain only valid state", () => {
  const { c, tree, search, meta, expand, collapse } = controller();
  c.update(ir, { artifactId: "saved", sourceVersion: 4, status: "verified" });
  assert.match(meta.textContent, /画布构建 v3.*编辑 v4 待构建/);
  c.setSourceVersion(2); assert.doesNotMatch(meta.textContent, /待构建/);
  collapse.click(); assert.equal(c.openState.get(c.nodes[0].key), false);
  expand.click(); assert.equal(c.openState.get(c.nodes[0].children[1].key), true);
  search.value = "找不到"; search.listeners.input(); assert.match(textOf(tree), /没有匹配/);
  assert.equal(collapse.disabled, true);
  search.value = ""; search.listeners.input();
  c.selectBody("base", "other"); assert.equal(c.selected, null);
  c.selectBody("base", "saved"); assert.equal(c.selected, c.nodes[0].key);
  c.selectReference({ artifact_id: "saved", body_id: "base", entity_kind: "feature", feature_id: "pad" });
  assert.equal(c.selected, c.walk().find(node => node.id === "pad").key);
  c.selectReference({ artifact_id: "saved", body_id: "base", entity_kind: "face", local_sub_id: "Face1" });
  assert.equal(c.selected, c.nodes[0].key);
  c.update({ ...ir, model_id: "new" }, { artifactId: "new-build" });
  assert.equal(c.selected, null); assert.equal(search.value, ""); assert.equal(c.openState.size, 0);
  c.update(null); assert.equal(search.disabled, true);
});

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function appHarness() {
  const drawn = [], messages = [], pending = [];
  const panel = { artifactId: null, update: (ir, options) => drawn.push([ir, options]),
    message: value => messages.push(value), render() {}, setSourceVersion() {} };
  const context = vm.createContext({ console, document: {} });
  vm.runInContext(source + `
    globalThis.testing = { state, syncDisplayedStructure, displayStructureArtifact, clearDisplayedStructure,
      stub(panel, fetcher, ir) { structure=panel; api=fetcher; sourceIr=ir; }};`, context);
  const h = context.testing;
  h.state.modelId = "plate";
  h.stub(panel, url => { const item = deferred(); pending.push({ ...item, url }); return item.promise; }, { ...ir, version: 4 });
  return { ...h, pending, drawn, messages, panel };
}

test("structure reads the frozen artifact source rather than a pending edited version", async () => {
  const h = appHarness();
  h.displayStructureArtifact("plate", "sha256:saved", 3, "verified");
  assert.match(h.pending[0].url, /artifact-sets\/sha256%3Asaved\/files\/ir.json/);
  h.pending[0].resolve(ir); await new Promise(r => setImmediate(r));
  assert.equal(h.drawn.at(-1)[0].version, 3);
  assert.equal(h.drawn.at(-1)[1].sourceVersion, 4);
});

test("late builds and switched sessions cannot overwrite the structure for the visible geometry", async () => {
  const h = appHarness();
  h.displayStructureArtifact("plate", "first", 3, "verified");
  h.displayStructureArtifact("plate", "second", 4, "verified");
  h.pending[1].resolve({ ...ir, version: 4 }); await new Promise(r => setImmediate(r));
  h.pending[0].resolve(ir); await new Promise(r => setImmediate(r));
  assert.equal(h.drawn.length, 1); assert.equal(h.drawn[0][1].artifactId, "second");
  h.displayStructureArtifact("plate", "third", 5, "verified");
  h.state.sessionEpoch++; h.pending[2].resolve({ ...ir, version: 5 });
  await new Promise(r => setImmediate(r)); assert.equal(h.drawn.length, 1);
});

test("wrong-model and wrong-version frozen IR are reported without showing misleading structure", async () => {
  for (const bad of [{ ...ir, model_id: "other" }, { ...ir, version: 9 }]) {
    const h = appHarness(); h.displayStructureArtifact("plate", "saved", 3, "verified");
    h.pending[0].resolve(bad); await new Promise(r => setImmediate(r));
    assert.equal(h.drawn.length, 0); assert.match(h.messages.at(-1), /结构与画布构建不匹配/);
  }
});
