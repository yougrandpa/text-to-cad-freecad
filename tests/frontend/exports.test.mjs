import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFile } from "node:fs/promises";

const source = (await readFile(new URL("../../tcad/server/ui/app.js", import.meta.url), "utf8"))
  .replace(/^import .*;$/gm, "").replace(/\nboot\(\);\s*$/, "");
const artifactId = "sha256:" + "a".repeat(64);
const exported = format => ({ artifact_id: artifactId, filename: `part.${format}`,
  url: `/derived/exports/hash/part.${format}` });

function element(tag = "div") {
  return { tag, children: [], attrs: {}, listeners: {}, isConnected: true, disabled: false,
    textContent: "", setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children.forEach(node => { node.isConnected = false; }); this.children = nodes; },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    click() { this.clicked = true; return this.listeners.click?.(); }, remove() { this.isConnected = false; },
  };
}
function harness() {
  const bar = element(), downloads = [], requests = [], notices = [];
  let listing = { artifact_id: artifactId, status: "verified", version: 2, files: ["part.FCStd", "assembly.FCStd"] };
  let reply = async () => ({ ok: true, json: async () => exported("stl") });
  const context = vm.createContext({ console,
    document: { getElementById: () => bar, createElement: element, body: { append: node => downloads.push(node) } },
    fetch: async (path, options) => { requests.push({ path, ...options }); return reply(); },
  });
  vm.runInContext(source + `
    globalThis.testing = { state, loadArtifacts, sessionToken,
      stub(list, notice) { api = list; pushNotice = notice; } };`, context);
  const h = context.testing;
  Object.assign(h.state, { modelId: "part", threadId: "thread", version: 2, sessionEpoch: 1 });
  h.stub(async () => typeof listing === "function" ? listing() : listing, (...args) => notices.push(args));
  return { ...h, bar, downloads, requests, notices,
    listing: value => { listing = value; }, reply: fn => { reply = fn; } };
}
const buttons = h => h.bar.children.filter(node => node.tag === "button");

test("loading a built model exposes unique formats without exporting anything", async () => {
  const h = harness(); await h.loadArtifacts();
  assert.deepEqual(buttons(h).map(node => node.textContent), ["FreeCAD", "STEP", "STL", "BREP"]);
  assert.equal(h.requests.length, 0);
  assert.equal(h.downloads.length, 0);
});

test("click exports pinned geometry once, then downloads after the response", async () => {
  const h = harness(); await h.loadArtifacts();
  let release; h.reply(() => new Promise(resolve => { release = resolve; }));
  const button = buttons(h)[2];
  const pending = button.click();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "导出中…");
  await button.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.downloads.length, 0);
  assert.equal(h.requests[0].path, "/models/part/exports");
  assert.equal(h.requests[0].method, "POST");
  assert.deepEqual(JSON.parse(h.requests[0].body), { artifact_id: artifactId, fmt: "stl" });
  release({ ok: true, json: async () => exported("stl") });
  await pending;
  assert.equal(h.downloads[0].attrs.download, "part.stl");
  assert.equal(h.downloads[0].clicked, true);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "STL");
  assert.equal(h.downloads[0].attrs.href, "/derived/exports/hash/part.stl");
});

test("an export failure allows retry and never triggers a download", async () => {
  const h = harness(); await h.loadArtifacts();
  h.reply(async () => ({ ok: false, json: async () => ({ detail: "转换失败" }) }));
  const button = buttons(h)[1]; await button.click();
  assert.equal(h.downloads.length, 0);
  assert.equal(button.disabled, false);
  assert.match(h.notices[0][1], /STEP 导出失败.*转换失败.*重试/);
  h.reply(async () => ({ ok: true, json: async () => exported("step") }));
  await button.click(); assert.equal(h.downloads[0].attrs.download, "part.step");
});

test("switching sessions or replacing the bar suppresses late downloads", async () => {
  for (const switchSession of [true, false]) {
    const h = harness(); await h.loadArtifacts();
    let release; h.reply(() => new Promise(resolve => { release = resolve; }));
    const pending = buttons(h)[0].click();
    if (switchSession) h.state.sessionEpoch += 1;
    else h.bar.replaceChildren();
    release({ ok: true, json: async () => exported("stl") });
    await pending; assert.equal(h.downloads.length, 0);
  }
});

test("unbuilt models expose no download buttons; mismatched exports cannot download", async () => {
  const h = harness(); h.listing({ files: [], version: 3 }); await h.loadArtifacts();
  assert.equal(buttons(h).length, 0);
  assert.match(h.bar.children[0].textContent, /构建验证通过/);
  h.listing({ artifact_id: artifactId, status: "verified", version: 2 }); await h.loadArtifacts();
  h.reply(async () => ({ ok: true, json: async () => ({ ...exported("FCStd"), artifact_id: "different-build" }) }));
  await buttons(h)[0].click(); assert.equal(h.downloads.length, 0);
  assert.match(h.notices[0][1], /构建不匹配/);
});


test("an older artifacts response cannot replace buttons for a newer build", async () => {
  const h = harness();
  let release; h.listing(() => new Promise(resolve => { release = resolve; }));
  const old = h.loadArtifacts();
  h.listing({ artifact_id: artifactId, status: "verified", version: 2 });
  await h.loadArtifacts();
  release({ artifact_id: "sha256:" + "f".repeat(64), status: "verified", version: 1 });
  await old;
  await buttons(h)[2].click();
  assert.equal(JSON.parse(h.requests[0].body).artifact_id, artifactId);
  assert.equal(buttons(h).length, 4);
});
