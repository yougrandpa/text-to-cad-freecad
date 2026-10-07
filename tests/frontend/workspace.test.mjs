import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = (await readFile(new URL('../../tcad/server/ui/app.js', import.meta.url), 'utf8'))
  .replace(/^import .*;$/gm, '').replace(/\nboot\(\);\s*$/, '');

function harness(saved = '0', unavailable = false) {
  const nodes = new Map(), storage = new Map([['tcad.sidebarHidden', saved]]);
  const node = id => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { attrs: {}, classes, setAttribute(key, value) { this.attrs[key] = value; },
        classList: { toggle(name, value) { if (value) classes.add(name); else classes.delete(name); } } });
    }
    return nodes.get(id);
  };
  const context = vm.createContext({
    console, document: { getElementById: node },
    localStorage: {
      getItem(key) { if (unavailable) throw new Error('Unavailable'); return storage.get(key); },
      setItem(key, value) { if (unavailable) throw new Error('Unavailable'); storage.set(key, value); },
    },
  });
  vm.runInContext(source + '\nglobalThis.testing = {state, bindSidebar, toggleSidebar};', context);
  context.testing.bindSidebar();
  return { ...context.testing, node, storage };
}

test('desktop sidebar restores its saved visibility without drawer semantics', () => {
  const h = harness('1');
  assert.equal(h.state.sidebarHidden, true);
  assert.equal(h.node('layout').classes.has('no-sidebar'), true);
  assert.equal(h.node('sidebarToggle').attrs['aria-expanded'], 'false');
  assert.equal(h.node('sessionPane').attrs.role, undefined);
});

test('desktop sidebar toggle persists both hidden and visible states', () => {
  const h = harness();
  h.toggleSidebar();
  assert.equal(h.storage.get('tcad.sidebarHidden'), '1');
  assert.equal(h.node('layout').classes.has('no-sidebar'), true);
  h.toggleSidebar();
  assert.equal(h.storage.get('tcad.sidebarHidden'), '0');
  assert.equal(h.node('layout').classes.has('no-sidebar'), false);
  assert.equal(h.node('sidebarToggle').attrs['aria-expanded'], 'true');
});

test('disabled browser storage does not prevent desktop sidebar interaction', () => {
  const h = harness('1', true);
  assert.equal(h.state.sidebarHidden, false);
  h.toggleSidebar();
  assert.equal(h.state.sidebarHidden, true);
});
