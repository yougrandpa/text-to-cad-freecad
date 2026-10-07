import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');

function element(tag = 'div') {
  return {
    tag, children: [], attrs: {}, listeners: {}, hidden: false, value: '', open: false, isConnected: true,
    classList: { toggle() {} },
    get textContent() { return this.text || this.children.map(child => child.textContent || '').join(''); },
    set textContent(value) { this.text = value; this.children = []; },
    setAttribute(key, value) { this.attrs[key] = value; if (key === 'hidden') this.hidden = true; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    querySelectorAll() { return []; },
    focus() { this.focused = true; }, select() {},
    showModal() { this.open = true; },
    close() { this.open = false; this.onclose?.(); },
  };
}

function harness() {
  const nodes = new Map(), requests = [], switches = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const context = vm.createContext({ console, document: { getElementById: node, createElement: element,
    createElementNS: (_, tag) => element(tag) } });
  vm.runInContext(source + `
    globalThis.testing = { state, visibleSessions, renderSessions, loadSessions, archiveSession,
      folderDialog, moveSessionDialog, deleteSessionDialog, newSession, historyMenu, closeHistoryMenu,
      stub(apiFn, switchFn) { api = apiFn; switchSession = switchFn; pushNotice = () => {}; },
    };`, context);
  const h = context.testing;
  h.state.sessions = [
    { thread_id: 'one', model_id: 'part-one', folder_id: null, archived: false, title: '底板', messages: 2 },
    { thread_id: 'two', model_id: 'part-two', folder_id: 'folder', archived: false, title: '机构', messages: 4 },
    { thread_id: 'old', model_id: 'part-old', folder_id: 'folder', archived: true, title: '旧机构', messages: 6 },
  ];
  h.state.folders = [{ folder_id: 'folder', name: '机械设计' }];
  let respond = async path => path.startsWith('/sessions?') ? { sessions: h.state.sessions }
    : path === '/session-folders' ? { folders: h.state.folders } : { updated: true };
  h.stub(async (path, options = {}) => {
    requests.push({ path, ...options, body: options.body ? JSON.parse(options.body) : null });
    return respond(path, options);
  }, async id => { switches.push(id); h.state.threadId = id; });
  return { ...h, node, requests, switches, respond(fn) { respond = fn; } };
}

function descendants(node) {
  return node.children.flatMap(child => typeof child === 'object' ? [child, ...descendants(child)] : []);
}

test('folder and archive filters compose while all conversations stay available', () => {
  const h = harness();
  assert.deepEqual(Array.from(h.visibleSessions(), s => s.thread_id), ['one', 'two']);
  h.state.historyFolder = 'folder';
  assert.deepEqual(Array.from(h.visibleSessions(), s => s.thread_id), ['two']);
  h.state.historyView = 'archived';
  assert.deepEqual(Array.from(h.visibleSessions(), s => s.thread_id), ['old']);
  h.state.historyFolder = 'unfiled';
  assert.equal(h.visibleSessions().length, 0);
});

test('rendered action menus are outside session buttons and disable running session mutations', () => {
  const h = harness();
  h.state.threadId = 'one'; h.state.busy = true;
  h.renderSessions();
  const all = descendants(h.node('sessionList'));
  for (const button of all.filter(node => node.tag === 'button')) {
    assert.equal(descendants(button).some(node => node.tag === 'button' || node.tag === 'summary'), false);
  }
  const disabled = all.filter(node => node.tag === 'button' && 'disabled' in node.attrs);
  assert.deepEqual(disabled.map(node => node.textContent), ['移动到文件夹…', '归档对话', '删除对话…']);
});

test('action menus keep one panel open and Escape closes it while returning focus to its button', () => {
  const h = harness();
  const first = h.historyMenu('第一个对话', []), second = h.historyMenu('第二个对话', []);
  const [firstButton, firstPanel] = first.children, [secondButton, secondPanel] = second.children;
  assert.equal(firstPanel.hidden, true);
  firstButton.listeners.click();
  assert.equal(firstPanel.hidden, false);
  assert.equal(firstButton.attrs['aria-expanded'], 'true');
  secondButton.listeners.click();
  assert.equal(firstPanel.hidden, true);
  assert.equal(firstButton.attrs['aria-expanded'], 'false');
  assert.equal(secondPanel.hidden, false);
  let prevented = false;
  second.listeners.keydown({ key: 'Escape', preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(secondPanel.hidden, true);
  assert.equal(secondButton.focused, true);
});

test('create and move confirmations expose their text and fixed-size SVG icons', () => {
  const h = harness();
  for (const [open, text] of [[() => h.folderDialog(), '创建'], [() => h.moveSessionDialog(h.state.sessions[0]), '移动']]) {
    open();
    const confirm = h.node('historyConfirm');
    assert.equal(confirm.textContent, text);
    assert.equal(confirm.disabled, false);
    assert.equal(confirm.children[0].tag, 'svg');
    assert.equal(confirm.children[0].attrs.width, '16');
    assert.equal(confirm.children[0].attrs['aria-hidden'], 'true');
    assert.ok(confirm.children[0].children[0].attrs.d);
    h.node('historyCancel').onclick();
  }
});

test('history refresh reads archived sessions and folders and recovers a removed filter', async () => {
  const h = harness(); h.state.historyFolder = 'gone';
  await h.loadSessions();
  assert.deepEqual(h.requests.map(r => r.path), ['/sessions?include_archived=true', '/session-folders']);
  assert.equal(h.state.historyFolder, 'all');
});

test('an older list response cannot resurrect a conversation after a newer refresh', async () => {
  const h = harness();
  let release;
  const oldSessions = h.state.sessions;
  let reads = 0;
  h.respond(async path => {
    if (path === '/session-folders') return { folders: h.state.folders };
    if (++reads === 1) return new Promise(resolve => { release = resolve; });
    return { sessions: [] };
  });
  const older = h.loadSessions();
  await h.loadSessions();
  release({ sessions: oldSessions });
  await older;
  assert.equal(h.state.sessions.length, 0);
});

test('archiving the selected final conversation clears selection and restore retains its folder', async () => {
  const h = harness(); h.state.threadId = 'two'; h.state.historyFolder = 'folder';
  await h.archiveSession(h.state.sessions[1]);
  assert.deepEqual(h.requests[0].body, { archived: true });
  assert.deepEqual(h.switches, [null]);
  h.state.historyView = 'archived';
  await h.archiveSession(h.state.sessions[1]);
  assert.equal(h.state.sessions[1].archived, false);
  assert.equal(h.state.sessions[1].folder_id, 'folder');
});

test('failed archive preserves history and reports the error in the sidebar', async () => {
  const h = harness(); h.respond(async () => { throw new Error('请先停止生成'); });
  await h.archiveSession(h.state.sessions[0]);
  assert.equal(h.state.sessions[0].archived, false);
  assert.equal(h.state.historyError, '请先停止生成');
  assert.equal(h.switches.length, 0);
});

test('delete requires explicit submit; cancel never mutates and deleting the selected session switches away', async () => {
  const h = harness(); h.state.threadId = 'one';
  h.deleteSessionDialog(h.state.sessions[0]);
  assert.equal(h.node('historyDialog').open, true);
  assert.match(h.node('historyDialogHint').textContent, /无法恢复.*CAD 模型文件会保留/);
  h.node('historyCancel').onclick();
  assert.equal(h.requests.length, 0);
  h.deleteSessionDialog(h.state.sessions[0]);
  await h.node('historyForm').onsubmit({ preventDefault() {} });
  assert.equal(h.requests[0].method, 'DELETE');
  assert.equal(h.requests[0].path, '/sessions/one');
  assert.equal(h.state.sessions.some(s => s.thread_id === 'one'), false);
  assert.deepEqual(h.switches, ['two']);
  assert.equal(h.node('historyDialog').open, false);
});

test('failed folder submission remains editable, trims its name and does not create on cancel', async () => {
  const h = harness(); h.respond(async () => { throw new Error('已存在同名文件夹'); });
  h.folderDialog(); h.node('historyName').value = '  机构  ';
  await h.node('historyForm').onsubmit({ preventDefault() {} });
  assert.deepEqual(h.requests[0].body, { name: '机构' });
  assert.equal(h.node('historyDialog').open, true);
  assert.equal(h.node('historyConfirm').disabled, false);
  assert.equal(h.node('historyDialogError').textContent, '已存在同名文件夹');
  h.node('historyCancel').onclick();
  assert.equal(h.requests.length, 1);
});

test('moving to unfiled uses null and changes the sidebar filter', async () => {
  const h = harness(); h.moveSessionDialog(h.state.sessions[1]);
  h.node('historyFolder').value = '';
  await h.node('historyForm').onsubmit({ preventDefault() {} });
  assert.deepEqual(h.requests[0].body, { folder_id: null });
  assert.equal(h.state.historyFolder, 'unfiled');
});

test('new conversation inherits selected folder and exits the archive view', async () => {
  const h = harness(); h.state.historyFolder = 'folder'; h.state.historyView = 'archived';
  h.respond(async path => path === '/sessions' ? { thread_id: 'fresh' }
    : path.startsWith('/sessions?') ? { sessions: h.state.sessions } : { folders: h.state.folders });
  await h.newSession({ announce: false });
  assert.deepEqual(h.requests[0].body, { folder_id: 'folder' });
  assert.equal(h.state.historyView, 'active');
  assert.deepEqual(h.switches, ['fresh']);
});
