import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');
function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', hidden: false, listeners: {},
      addEventListener(type, handler) { this.listeners[type] = handler; },
      replaceChildren(...children) { this.children = children; },
      append(child) { this.children.push(child); } });
    return nodes.get(id);
  };
  const document = { getElementById: node };
  const context = vm.createContext({ document, console });
  vm.runInContext(source + `
    el = (tag, attrs) => attrs;
    globalThis.testing = {state, syncProviderEndpoint, fillModelOptions, bindSettingsDismissal, bindSettingsKeyboard};
  `, context);
  return { ...context.testing, node, document };
}
test('preset endpoints lock and custom endpoints unlock', () => {
  const h = harness();
  h.state.providers = [{id: 'opencode', base_url: 'https://opencode.ai/inference/openai/v1'}, {id: 'custom'}];
  h.node('providerSelect').value = 'opencode';
  h.node('baseUrlInput').value = 'wrong';
  h.syncProviderEndpoint();
  assert.equal(h.node('baseUrlInput').disabled, true);
  assert.equal(h.node('baseUrlInput').value, h.state.providers[0].base_url);
  h.node('providerSelect').value = 'custom';
  h.syncProviderEndpoint();
  assert.equal(h.node('baseUrlInput').disabled, false);
});
test('model menu contains the entire list with an existing input value', () => {
  const h = harness();
  h.node('modelInput').value = 'kimi-k2.6';
  h.fillModelOptions(['kimi-k2.6', 'glm-5.1'], 'live');
  assert.deepEqual(Array.from(h.node('modelOptions').children, c => c.value), ['', 'kimi-k2.6', 'glm-5.1']);
  assert.equal(h.node('modelInput').value, 'kimi-k2.6');
  assert.equal(h.node('modelOptions').disabled, false);
});
test('dragging from inside to backdrop keeps settings open; direct click closes', () => {
  const h = harness();
  h.bindSettingsDismissal();
  const modal = h.node('settingsModal');
  modal.listeners.pointerdown({target: h.node('modelInput')});
  modal.listeners.click({target: modal});
  assert.equal(modal.hidden, false);
  modal.listeners.pointerdown({target: modal});
  modal.listeners.pointercancel();
  modal.listeners.click({target: modal});
  assert.equal(modal.hidden, false);
  modal.listeners.pointerdown({target: modal});
  modal.listeners.click({target: modal});
  assert.equal(modal.hidden, true);
});
test('OpenRouter puts free variants and zero-priced models first without changing values', () => {
  const h = harness();
  h.node('providerSelect').value = 'openrouter';
  const models = ['paid/model', 'vendor/model:free', 'zero/model', 'openrouter/free', 'other/paid'];
  h.fillModelOptions(models, 'live', ['zero/model']);
  const options = h.node('modelOptions').children.slice(1);
  assert.deepEqual(Array.from(options, o => o.value), ['vendor/model:free', 'zero/model', 'openrouter/free', 'paid/model', 'other/paid']);
  assert.ok(options.slice(0, 3).every(o => o.text.startsWith('免费 · ')));
  assert.equal(models[0], 'paid/model');
  h.node('providerSelect').value = 'opencode';
  h.fillModelOptions(models, 'live');
  assert.deepEqual(Array.from(h.node('modelOptions').children.slice(1), o => o.value), models);
});

test('settings keyboard traps focus, skips disabled controls, and Escape closes', () => {
  const h = harness();
  const modal = h.node('settingsModal');
  const first = h.node('closeSettings');
  const last = h.node('saveSettings');
  const disabled = h.node('baseUrlInput');
  disabled.disabled = true;
  for (const node of [first, last, disabled]) node.getClientRects = () => [1];
  let focused;
  first.focus = () => { focused = first; };
  last.focus = () => { focused = last; };
  modal.querySelectorAll = () => [first, last, disabled];
  h.bindSettingsKeyboard();
  h.document.activeElement = last;
  let prevented = false;
  modal.listeners.keydown({ key: 'Tab', preventDefault() { prevented = true; } });
  assert.equal(focused, first);
  assert.equal(prevented, true);
  h.document.activeElement = first;
  modal.listeners.keydown({ key: 'Tab', shiftKey: true, preventDefault() {} });
  assert.equal(focused, last);
  modal.listeners.keydown({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
  assert.equal(modal.hidden, true);
});
