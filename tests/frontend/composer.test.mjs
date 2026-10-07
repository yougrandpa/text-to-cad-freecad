import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { AttachmentInput } from '../../tcad/server/ui/attachments.mjs';
import { readFile } from 'node:fs/promises';

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');

function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', style: {}, disabled: false, scrollHeight: 100, listeners: {}, attrs: {},
      classList: { toggle() {} },
      replaceChildren() {}, querySelectorAll() { return []; },
      setAttribute(key, value) { this.attrs[key] = value; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      focus() { this.focused = true; },
      showModal() { this.open = true; },
      setSelectionRange(start, end) { this.selection = [start, end]; },
    });
    return nodes.get(id);
  };
  const sample = node('sample');
  sample.dataset = { text: '一个 80x50 的底板，厚度 8mm' };
  const sent = [];
  const context = vm.createContext({
    document: { getElementById: node, querySelectorAll: () => [sample] },
    localStorage: { setItem() {} }, console, AttachmentInput,
  });
  vm.runInContext(source + '\nglobalThis.testing = {state, bindComposer, toggleSidebar, checkAttachmentInput, images: () => attachmentInput, stubApi(fn) { api = fn; }};', context);
  context.send = (text, images, accepted) => { sent.push(text); accepted(); };
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


test('unsupported image model opens a popup without changing draft or selected images', async () => {
  const h = harness();
  h.node('input').value = '按照图片设计';
  h.images().attachments = [{ name: '图.png', data_url: 'data:image/png;base64,picture' }];
  h.stubApi(async () => ({ settings: { supports_vision: false }, attachments_supported: true }));
  assert.equal(await h.checkAttachmentInput([{}]), false);
  assert.equal(h.node('composerErrorDialog').open, true);
  assert.match(h.node('composerErrorMessage').textContent, /不支持图片输入/);
  assert.equal(h.node('input').value, '按照图片设计');
  assert.equal(h.images().attachments.length, 1);
  assert.equal(h.node('attachmentBtn').disabled, false);
  assert.equal(h.node('sendBtn').disabled, false);
  assert.equal(h.sent.length, 0);
});

test('capability lookup locks send and ignores replies after switching sessions', async () => {
  const h = harness();
  let resolve;
  h.stubApi(() => new Promise(done => { resolve = done; }));
  const lookup = h.checkAttachmentInput();
  assert.equal(h.node('sendBtn').disabled, true);
  assert.equal(h.node('attachmentBtn').disabled, true);
  h.state.sessionEpoch += 1;
  resolve({ settings: { supports_vision: false }, attachments_supported: true });
  assert.equal(await lookup, false);
  assert.equal(h.node('composerErrorDialog').open, undefined);
  assert.equal(h.node('sendBtn').disabled, false);
});

test('supported image model allows send while lookup errors produce a popup', async () => {
  const h = harness();
  h.stubApi(async () => ({ settings: { supports_vision: true }, attachments_supported: true }));
  assert.equal(await h.checkAttachmentInput([{}]), true);
  h.stubApi(async () => { throw new Error('offline'); });
  assert.equal(await h.checkAttachmentInput([{}]), false);
  assert.equal(h.node('composerErrorDialog').open, true);
  assert.match(h.node('composerErrorMessage').textContent, /offline/);
  assert.equal(h.node('sendBtn').disabled, false);
});


test('text attachments skip vision checks and an older backend cannot silently drop them', async () => {
  const h = harness();
  h.stubApi(async () => ({ settings: { supports_vision: false }, attachments_supported: true }));
  assert.equal(await h.checkAttachmentInput([]), true);
  h.stubApi(async () => ({ settings: { supports_vision: false } }));
  assert.equal(await h.checkAttachmentInput([]), false);
  assert.match(h.node('composerErrorMessage').textContent, /重启服务/);
});
