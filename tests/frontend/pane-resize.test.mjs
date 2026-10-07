import test from 'node:test';
import assert from 'node:assert/strict';
import { fitPaneWidths, resizeAdjacent } from '../../tcad/server/ui/pane-resize.mjs';

const minimums = [140, 240, 200, 280];
const sum = values => values.reduce((total, value) => total + value, 0);

test('fitting saved widths preserves proportions when all panes have room', () => {
  const widths = fitPaneWidths([200, 400, 300, 600], minimums, 1800);
  assert.deepEqual(widths, [240, 480, 360, 720]);
});

test('window shrink keeps every pane usable and redistributes space from larger panes', () => {
  const widths = fitPaneWidths([200, 300, 220, 800], minimums, 920);
  assert.deepEqual(widths.slice(0, 3), [140, 240, 200]);
  assert.equal(widths[3], 340);
  assert.equal(sum(widths), 920);
});

test('skewed saved preferences stay inside the window for every desktop width', () => {
  for (const total of [900, 1100, 1600, 2300]) {
    for (const preferences of [[99000, 1, 1, 1], [1, 1, 1, 99000], [140, 240, 200, 280]]) {
      const widths = fitPaneWidths(preferences, minimums, total);
      assert.ok(Math.abs(sum(widths) - total) < .00001);
      widths.forEach((width, index) => assert.ok(width >= minimums[index]));
    }
  }
});

test('dragging changes only the adjacent pair and preserves the total width', () => {
  const widths = [166, 350, 248, 600];
  assert.deepEqual(resizeAdjacent(widths, minimums, 1, 30), [166, 380, 218, 600]);
  assert.deepEqual(widths, [166, 350, 248, 600]);
  assert.equal(sum(resizeAdjacent(widths, minimums, 1, 30)), sum(widths));
});

test('large drags and keyboard Home/End stop at the adjacent minimum widths', () => {
  const widths = [166, 350, 248, 600];
  assert.deepEqual(resizeAdjacent(widths, minimums, 0, -Infinity), [140, 376, 248, 600]);
  assert.deepEqual(resizeAdjacent(widths, minimums, 1, Infinity), [166, 398, 200, 600]);
  assert.deepEqual(resizeAdjacent(widths, minimums, 2, 10000), [166, 350, 568, 280]);
});

test('hidden sidebar leaves the three visible panes bounded and reversible', () => {
  const visibleMinimums = minimums.slice(1);
  const widths = fitPaneWidths([350, 248, 600], visibleMinimums, 1300);
  assert.ok(Math.abs(sum(widths) - 1300) < .00001);
  const moved = resizeAdjacent(widths, visibleMinimums, 0, 20);
  const restored = resizeAdjacent(moved, visibleMinimums, 0, -20);
  restored.forEach((width, index) => assert.ok(Math.abs(width - widths[index]) < .00001));
});

test('small desktop windows preserve usable widths for horizontal workbench scrolling', () => {
  assert.deepEqual(fitPaneWidths([166, 350, 248, 600], minimums, 700), minimums);
});
