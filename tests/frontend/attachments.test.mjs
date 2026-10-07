import test from 'node:test';
import assert from 'node:assert/strict';
import { AttachmentInput } from '../../tcad/server/ui/attachments.mjs';

function element() {
  return {
    children: [], listeners: {}, attrs: {}, disabled: false, value: '',
    classList: { toggle() {}, remove() {}, add() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    setAttribute(key, value) { this.attrs[key] = value; },
    querySelectorAll() { return this.children.flatMap(card => card.children?.slice(2) || []); },
  };
}

function harness(read = async file => `data:${file.type};base64,picture`) {
  const nodes = Object.fromEntries(['input', 'composer', 'picker', 'button', 'previews'].map(id => [id, element()]));
  const errors = [], pending = [];
  const controller = new AttachmentInput({ ...nodes, read, document: { createElement: element },
    error: message => errors.push(message), changed: value => pending.push(value) });
  return { ...nodes, controller, errors, pending };
}

const file = (name = '参考图.png', size = 100, type = 'image/png') => ({ name, size, type });
const settle = () => new Promise(resolve => setImmediate(resolve));

test('selected images show preview and can be removed individually', async () => {
  const h = harness();
  await h.controller.add([file(), file('第二张.png')]);
  assert.equal(h.previews.children.length, 2);
  assert.equal(h.previews.hidden, false);
  const [image, label, remove] = h.previews.children[0].children;
  assert.equal(image.alt, '参考图.png');
  assert.equal(label.textContent, '参考图.png');
  assert.match(remove.attrs['aria-label'], /移除附件/);
  remove.listeners.click();
  assert.deepEqual(h.controller.snapshot().map(image => image.name), ['第二张.png']);
  h.controller.consume(h.controller.snapshot());
  assert.equal(h.previews.hidden, true);
});

test('picker remains usable independently of model capability and clears its selection', async () => {
  const h = harness();
  let clicked = false;
  h.picker.click = () => { clicked = true; };
  h.button.listeners.click();
  assert.equal(clicked, true);
  h.picker.files = [file()];
  h.picker.value = 'file';
  h.picker.listeners.change();
  await settle();
  assert.equal(h.picker.value, '');
  assert.equal(h.controller.attachments.length, 1);
});

test('paste handles screenshots while keeping text in a mixed clipboard', async () => {
  const h = harness();
  for (const text of ['', '同时粘贴文字']) {
    let prevented = false;
    h.input.listeners.paste({ clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file() }], getData: () => text },
      preventDefault() { prevented = true; } });
    await settle();
    assert.equal(prevented, text === '');
  }
  assert.equal(h.controller.attachments.length, 2);
});

test('drop accepts files and prevents browser navigation', async () => {
  const h = harness();
  let prevented = false;
  h.composer.listeners.drop({ dataTransfer: { files: [file()] }, preventDefault() { prevented = true; } });
  await settle();
  assert.equal(prevented, true);
  assert.equal(h.controller.attachments.length, 1);
});

test('format, size and count failures preserve already selected images', async () => {
  const h = harness();
  await h.controller.add([file()]);
  for (const files of [[file('big.png', 5 * 1024 * 1024 + 1)], [file('vector.svg', 10, 'image/svg+xml')], Array(4).fill(file())]) {
    await h.controller.add(files);
    assert.equal(h.controller.attachments.length, 1);
    assert.equal(h.controller.pending, false);
    assert.equal(h.button.disabled, false);
  }
  assert.equal(h.errors.length, 3);
});

test('read failure is atomic and in-flight reads or turns cannot change attachments', async () => {
  let release;
  const h = harness(() => new Promise(resolve => { release = resolve; }));
  const loading = h.controller.add([file()]);
  assert.equal(h.button.disabled, true);
  await h.controller.add([file('ignored.png')]);
  release('data:image/png;base64,picture');
  await loading;
  assert.equal(h.controller.attachments.length, 1);
  h.controller.setBusy(true);
  await h.controller.add([file()]);
  h.previews.children[0].children[2].listeners.click();
  assert.equal(h.controller.attachments.length, 1);
  assert.equal(h.picker.disabled, true);
  h.controller.setBusy(false);
  h.controller.read = async () => { throw new Error('无法读取图片'); };
  await h.controller.add([file()]);
  assert.equal(h.controller.attachments.length, 1);
  assert.equal(h.errors.length, 1);
});

test('text and Markdown files are read as UTF-8 and can share a message with images', async () => {
  const h = harness();
  await h.controller.add([new File(['# 设计说明\n宽度 80 mm'], '设计.md', { type: 'text/markdown' }),
    new File(['厚度 8 mm'], '尺寸.txt', { type: 'text/plain' }), file()]);
  assert.equal(h.controller.attachments.length, 3);
  assert.equal(h.controller.attachments[0].content, '# 设计说明\n宽度 80 mm');
  assert.equal(h.controller.attachments[1].content, '厚度 8 mm');
  assert.equal(h.controller.attachments[2].data_url, 'data:image/png;base64,picture');
  assert.equal(h.previews.children[0].children[0].textContent, 'MD');
  assert.equal(h.previews.children[1].children[0].textContent, 'TXT');
});

test('text file limits, invalid UTF-8 and binary content show errors without losing attachments', async () => {
  const h = harness();
  await h.controller.add([file()]);
  for (const file of [new File(['x'.repeat(256 * 1024 + 1)], '大文件.txt'),
    new File([new Uint8Array([255, 254, 253])], '编码.md'), new File(['text\0binary'], '二进制.txt')]) {
    await h.controller.add([file]);
    assert.equal(h.controller.attachments.length, 1);
    assert.equal(h.controller.pending, false);
  }
  assert.equal(h.errors.length, 3);
  assert.match(h.errors[0], /256 KB/);
  assert.match(h.errors[1], /UTF-8/);
  assert.match(h.errors[2], /二进制/);
});

test('combined attachment limit counts text documents and images together', async () => {
  const h = harness();
  await h.controller.add([new File(['内容'], '说明.md'), file(), file(), file()]);
  assert.equal(h.controller.attachments.length, 4);
  await h.controller.add([new File(['另一份'], '说明.txt')]);
  assert.equal(h.controller.attachments.length, 4);
  assert.match(h.errors[0], /最多添加 4 个附件/);
});
