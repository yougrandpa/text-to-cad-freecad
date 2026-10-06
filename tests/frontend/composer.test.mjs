import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');

function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', style: {}, disabled: false, scrollHeight: 100, listeners: {}, attrs: {},
      classList: { toggle() {} },
      setAttribute(key, value) { this.attrs[key] = value; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      focus() { this.focused = true; },
      setSelectionRange(start, end) { this.selection = [start, end]; },
    });
    return nodes.get(id);
  };
  const sample = node('sample');
  sample.dataset = { text: '一个 80x50 的底板，厚度 8mm' };
  const sent = [];
  const context = vm.createContext({
    document: { getElementById: node, querySelectorAll: () => [sample] },
    localStorage: { setItem() {} }, console,
  });
  vm.runInContext(source + '\nglobalThis.testing = {state, bindComposer, toggleSidebar};', context);
  context.send = text => sent.push(text);
  const composer = node('composer');
  composer.requestSubmit = () => composer.listeners.submit({ preventDefault() {} });
  context.testing.bindComposer();
  return { ...context.testing, node, sample, sent };
}

test('sample selection fills and focuses an editable draft without sending', () => {
  const h = harness();
  h.sample.listeners.click();
  assert.equal(h.node('input').value, h.sample.dataset.text);
  assert.equal(h.node('input').focused, true);
  assert.equal(h.node('input').style.height, '100px');
  assert.equal(h.sent.length, 0);
});

test('Enter confirms IME text and Shift+Enter keeps a newline without sending', () => {
  const h = harness();
  h.node('input').value = '底板';
  for (const event of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }]) {
    h.node('input').listeners.keydown({ key: 'Enter', preventDefault() { assert.fail('should keep native input'); }, ...event });
  }
  assert.equal(h.sent.length, 0);
  h.node('input').listeners.keydown({ key: 'Enter', preventDefault() {} });
  assert.deepEqual(h.sent, ['底板']);
  assert.equal(h.node('input').value, '');
});

test('busy and disabled composers preserve drafts and do not submit', () => {
  const h = harness();
  const input = h.node('input');
  input.value = '未发送的设计';
  h.state.busy = true;
  h.node('composer').requestSubmit();
  h.sample.listeners.click();
  assert.equal(input.value, '未发送的设计');
  h.state.busy = false;
  input.disabled = true;
  h.node('composer').requestSubmit();
  h.sample.listeners.click();
  assert.equal(input.value, '未发送的设计');
  assert.equal(h.sent.length, 0);
});

test('empty submission is a no-op and autosizing is bounded', () => {
  const h = harness();
  h.node('input').value = '  ';
  h.node('composer').requestSubmit();
  assert.equal(h.node('input').value, '  ');
  assert.equal(h.sent.length, 0);
  h.node('input').scrollHeight = 500;
  h.node('input').listeners.input();
  assert.equal(h.node('input').style.height, '180px');
});

test('sidebar visibility is announced to assistive technology', () => {
  const h = harness();
  h.toggleSidebar(true);
  assert.equal(h.node('sidebarToggle').attrs['aria-expanded'], 'false');
  h.toggleSidebar(false);
  assert.equal(h.node('sidebarToggle').attrs['aria-expanded'], 'true');
});
