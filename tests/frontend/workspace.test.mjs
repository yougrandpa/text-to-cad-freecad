import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { WorkspaceController } from '../../tcad/server/ui/workspace.mjs';

function element(id) {
  return {
    id, dataset: {}, attrs: {}, listeners: {}, tabIndex: 0, inert: false,
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; },
    addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); },
    dispatch(type, event = {}) { for (const handler of this.listeners[type] || []) handler(event); },
    focus() { this.focused = true; },
    classList: { toggle() {} },
  };
}

function harness(compact = true) {
  const names = ['chat', 'model', 'inspect'];
  const tabs = Object.fromEntries(names.map(name => [name, element(`tab-${name}`)]));
  const panels = Object.fromEntries(names.map(name => [name, element(`pane-${name}`)]));
  const layout = element('layout');
  const media = element('media');
  media.matches = compact;
  media.resize = matches => { media.matches = matches; media.dispatch('change', { matches }); };
  const modes = [], selections = [];
  const controller = new WorkspaceController({
    layout, tabs, panels, media,
    onModeChange: value => modes.push(value),
    onSelect: (name, value) => selections.push([name, value]),
  });
  return { controller, layout, tabs, panels, media, modes, selections };
}

test('compact tabs select one work area and expose its matching accessible panel', () => {
  const h = harness();
  h.tabs.inspect.dispatch('click');
  assert.equal(h.layout.dataset.workspace, 'inspect');
  assert.deepEqual(h.selections, [['inspect', true]]);
  for (const [name, tab] of Object.entries(h.tabs)) {
    assert.equal(tab.attrs['aria-selected'], String(name === 'inspect'));
    assert.equal(tab.tabIndex, name === 'inspect' ? 0 : -1);
    assert.equal(h.panels[name].attrs.role, 'tabpanel');
    assert.equal(h.panels[name].attrs['aria-labelledby'], tab.id);
  }
});

test('loading a model opens the model area without overriding a manual tab choice', () => {
  const h = harness();
  h.controller.showModel();
  assert.equal(h.layout.dataset.workspace, 'model');
  h.tabs.chat.dispatch('click');
  h.controller.showModel();
  assert.equal(h.layout.dataset.workspace, 'chat');
  assert.equal(h.selections.length, 2);
});

test('resizing to desktop keeps the selected area and removes tab-only panel semantics', () => {
  const h = harness();
  h.tabs.inspect.dispatch('click');
  h.media.resize(false);
  assert.equal(h.layout.dataset.workspace, 'inspect');
  for (const panel of Object.values(h.panels)) {
    assert.equal(panel.attrs.role, undefined);
    assert.equal(panel.attrs['aria-labelledby'], undefined);
  }
  h.media.resize(true);
  assert.equal(h.panels.inspect.attrs.role, 'tabpanel');
  assert.equal(h.tabs.inspect.attrs['aria-selected'], 'true');
  assert.deepEqual(h.modes, [true, false, true]);
});

test('keyboard tab navigation wraps, supports Home/End, and moves focus with selection', () => {
  const h = harness();
  for (const [from, key, to] of [
    ['chat', 'ArrowLeft', 'inspect'], ['inspect', 'ArrowRight', 'chat'],
    ['chat', 'End', 'inspect'], ['inspect', 'Home', 'chat'],
  ]) {
    let prevented = false;
    h.tabs[from].dispatch('keydown', { key, preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(h.layout.dataset.workspace, to);
    assert.equal(h.tabs[to].focused, true);
  }
  h.tabs.chat.dispatch('keydown', { key: 'Tab', preventDefault() { assert.fail('keep native Tab'); } });
  assert.equal(h.layout.dataset.workspace, 'chat');
});

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');

function appHarness(compact = true, storedHidden = '0') {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const media = element('media');
  media.matches = compact;
  media.resize = matches => { media.matches = matches; media.dispatch('change', { matches }); };
  const storage = new Map([['tcad.sidebarHidden', storedHidden]]);
  const doc = element('document');
  doc.getElementById = node;
  const buttons = ['newSessionBtn', 'closeSessions', 'sessionButton'].map(node);
  for (const button of buttons) {
    button.getClientRects = () => [{}];
    button.focus = () => { doc.activeElement = button; };
  }
  node('sessionPane').querySelectorAll = () => buttons;
  node('settingsModal').hidden = true;
  const context = vm.createContext({
    document: doc, window: { matchMedia: () => media }, WorkspaceController, console,
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  });
  vm.runInContext(source + '\nglobalThis.testing = {state, bindWorkspace, toggleSidebar};', context);
  context.testing.bindWorkspace();
  return { ...context.testing, node, media, storage, doc, buttons };
}

test('compact session drawer blocks background panes and preserves the desktop sidebar preference', () => {
  const h = appHarness();
  assert.equal(h.state.sidebarHidden, true);
  assert.equal(h.node('sessionPane').attrs.role, 'dialog');
  h.toggleSidebar();
  assert.equal(h.node('modelPane').inert, true);
  assert.equal(h.node('sessionPane').attrs['aria-modal'], 'true');
  assert.equal(h.doc.activeElement, h.node('newSessionBtn'));
  h.node('sessionBackdrop').dispatch('click');
  assert.equal(h.node('modelPane').inert, false);
  assert.equal(h.node('sidebarToggle').focused, true);
  assert.equal(h.storage.get('tcad.sidebarHidden'), '0');
  h.media.resize(false);
  assert.equal(h.state.sidebarHidden, false);
  assert.equal(h.node('sessionPane').attrs.role, undefined);
  assert.equal(h.node('sessionPane').attrs['aria-modal'], undefined);
});

test('desktop sidebar preference survives a compact window and background panes regain focusability', () => {
  const h = appHarness(false, '1');
  assert.equal(h.state.sidebarHidden, true);
  h.toggleSidebar();
  assert.equal(h.storage.get('tcad.sidebarHidden'), '0');
  h.media.resize(true);
  h.toggleSidebar();
  assert.equal(h.node('chatPane').inert, true);
  h.media.resize(false);
  assert.equal(h.state.sidebarHidden, false);
  assert.equal(h.node('chatPane').inert, false);
  assert.equal(h.node('sidebarToggle').attrs['aria-haspopup'], undefined);
});

test('drawer keyboard focus stays inside and Escape is consumed before turn interruption', () => {
  const h = appHarness();
  h.toggleSidebar();
  for (const [current, shiftKey, next] of [
    [h.buttons[0], true, h.buttons.at(-1)], [h.buttons.at(-1), false, h.buttons[0]],
  ]) {
    h.doc.activeElement = current;
    let prevented = false;
    h.node('sessionPane').dispatch('keydown', { key: 'Tab', shiftKey, preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(h.doc.activeElement, next);
  }
  const event = { key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  h.state.busy = true;
  h.doc.dispatch('keydown', event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.state.sidebarHidden, true);
  assert.equal(h.state.busy, true);
});

test('workspace selection closes an open drawer so its panel becomes interactive', () => {
  const h = appHarness();
  h.toggleSidebar();
  h.node('workspaceInspect').dispatch('click');
  assert.equal(h.node('layout').dataset.workspace, 'inspect');
  assert.equal(h.state.sidebarHidden, true);
  assert.equal(h.node('inspectPane').inert, false);
});
