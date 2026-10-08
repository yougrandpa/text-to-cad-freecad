// Open /ui/ with playwright-cli, then run-code --filename this file.
// Only local layout preferences change; saved conversations remain untouched.
async (page) => {
  const key = 'tcad.paneWidths.v1';
  const previous = await page.evaluate(key => ({ widths: localStorage.getItem(key), sidebar: localStorage.getItem('tcad.sidebarHidden') }), key);
  const sizes = () => page.evaluate(() => ['sessionPane', 'chatPane', 'inspectPane', 'modelPane']
    .map(id => document.getElementById(id).getBoundingClientRect().width));
  const equal = (a, b) => a.length === b.length && a.every((value, index) => Math.abs(value - b[index]) < 1);
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const checkHighlights = async expected => {
    const actual = await page.evaluate(() => {
      const accent = getComputedStyle(document.querySelector('#newSessionBtn')).color;
      const rails = [...document.querySelectorAll('.pane-resizer')];
      return ['::before', '::after'].map(part => rails.flatMap((rail, index) =>
        !rail.hidden && getComputedStyle(rail, part).backgroundColor === accent ? [index] : []));
    });
    check(actual.every(indices => JSON.stringify(indices) === JSON.stringify(expected)),
      `Wrong rail highlights: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  };
  const drag = async (index, delta, cancel = false) => {
    const rail = page.locator('.pane-resizer').nth(index);
    const rect = await rail.boundingBox();
    await page.mouse.move(rect.x + rect.width / 2, rect.y + 80);
    await checkHighlights([index]);
    await page.mouse.down();
    await checkHighlights(await page.locator('.pane-resizer').evaluateAll(rails =>
      rails.flatMap((rail, index) => rail.hidden ? [] : [index])));
    await page.mouse.move(rect.x + rect.width / 2 + delta, rect.y + 80, { steps: 8 });
    if (cancel) await rail.press('Escape');
    await page.mouse.up();
    check(await rail.evaluate(node => document.activeElement !== node && getComputedStyle(node).outlineStyle === 'none'),
      'Pointer release left a focus outline on the rail');
    if (!cancel && Math.abs(delta) <= 30) await checkHighlights([index]);
    await page.mouse.move(10, 10);
    await checkHighlights([]);
  };
  const checkLayout = async () => page.evaluate(() => {
    const layout = document.querySelector("#layout");
    const right = layout.getBoundingClientRect().left + layout.scrollWidth - layout.scrollLeft;
    if (document.documentElement.scrollWidth > innerWidth) throw new Error('Page overflows the window');
    for (const pane of document.querySelectorAll('.layout > .col')) {
      if (!pane.getClientRects().length) continue;
      const bounds = pane.getBoundingClientRect();
      if (bounds.left < 0 || bounds.right > right + 1 || bounds.bottom > innerHeight + 1) {
        throw new Error(`Pane exceeds window: ${pane.id}, window ${innerWidth}×${innerHeight}, bounds ${JSON.stringify(bounds.toJSON())}`);
      }
      for (const control of pane.querySelectorAll('.col-head button, .col-head select, .history-toolbar button, .composer button, .motion-controls button, .motion-controls select')) {
        if (!control.getClientRects().length) continue;
        const rect = control.getBoundingClientRect();
        if (rect.left < bounds.left || rect.right > bounds.right + 1) throw new Error(`Clipped control: ${control.id}`);
      }
    }
  });
  const results = [];
  try {
    await page.evaluate(key => { localStorage.removeItem(key); localStorage.setItem('tcad.sidebarHidden', '0'); }, key);
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.reload();
    await page.locator('.pane-resizer').first().waitFor();
    await page.mouse.move(10, 10);
    await checkHighlights([]);
    const defaults = await sizes();
    // The composer starts in keyboard focus mode. A plain mouse click on the
    // separator must not leave that inherited dashed focus outline behind.
    await page.locator('#input').focus();
    await page.keyboard.press('ArrowLeft');
    await drag(1, 0);
    for (let index = 0; index < 3; index++) {
      const before = await sizes();
      await drag(index, 30);
      const after = await sizes();
      check(Math.abs(after[index] - before[index] - 30) < 1, `Rail ${index} did not widen left pane`);
      check(Math.abs(after[index + 1] - before[index + 1] + 30) < 1, `Rail ${index} did not narrow right pane`);
      before.forEach((width, other) => {
        if (other !== index && other !== index + 1) check(Math.abs(width - after[other]) < 1, 'Unrelated pane moved');
      });
      await checkLayout();
    }
    const saved = await sizes();
    await page.reload();
    await page.locator('.pane-resizer').first().waitFor();
    check(equal(saved, await sizes()), 'Widths were not restored after reload');
    await drag(2, -80, true);
    check(equal(saved, await sizes()), 'Escape did not cancel the drag');
    check(await page.locator('#layout').getAttribute('class').then(value => !value.includes('is-resizing')), 'Drag cursor stuck');
    const middle = page.locator('.pane-resizer').nth(1);
    await middle.press('Home');
    check(await middle.evaluate(node => node.matches(':focus-visible') && getComputedStyle(node).outlineStyle === 'dashed'),
      'Keyboard resizing must retain its focus indication');
    await checkHighlights([]);
    check(Math.abs((await sizes())[1] - 240) < 1, 'Keyboard minimum not respected');
    await middle.press('End');
    await checkHighlights([]);
    check(Math.abs((await sizes())[2] - 200) < 1, 'Adjacent minimum not respected');
    await page.locator('.pane-resizer').first().dblclick();
    check(equal(defaults, await sizes()), 'Double click did not restore default widths');
    check(await page.evaluate(key => localStorage.getItem(key) === null, key), 'Reset did not clear saved widths');
    await page.locator('#sidebarToggle').click();
    check(await page.locator('.pane-resizer:visible').count() === 2, 'Hidden sidebar retained its rail');
    await drag(1, 25);
    await checkLayout();
    await page.locator('#sidebarToggle').click();
    check(await page.locator('.pane-resizer:visible').count() === 3, 'Sidebar rail did not return');
    for (const [width, height] of [[880, 700], [900, 600], [981, 360], [1280, 600], [1920, 900], [1440, 600], [1440, 800]]) {
      await page.setViewportSize({ width, height });
      await page.waitForFunction(() => {
        const pane = document.querySelector('#modelPane').getBoundingClientRect();
        const layout = document.querySelector("#layout");
        return pane.right <= layout.getBoundingClientRect().left + layout.scrollWidth && pane.bottom <= innerHeight;
      });
      await checkLayout();
      results.push({ width, height, passed: true });
    }
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.locator('.pane-resizer').first().waitFor();
    await page.locator('.pane-resizer').first().dblclick();
    await drag(2, 200);
    await drag(1, 158);
    await drag(0, 74);
    await checkLayout();
    await page.screenshot({ path: 'output/playwright/resizable-panes-desktop.png' });
    const canvas = page.locator('#viewport canvas:visible');
    if (await canvas.count()) {
      await page.waitForFunction(() => {
        const canvas = document.querySelector('#viewport canvas');
        return Math.abs(canvas.width - Math.round(canvas.clientWidth * devicePixelRatio)) < 2;
      });
    }
    return { dragAndReload: true, keyboardAndCancel: true, sidebar: true, viewportResize: true, sizes: results };
  } finally {
    await page.mouse.up();
    await page.evaluate(({ key, previous }) => {
      for (const [name, value] of [[key, previous.widths], ['tcad.sidebarHidden', previous.sidebar]]) {
        if (value === null) localStorage.removeItem(name); else localStorage.setItem(name, value);
      }
    }, { key, previous });
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.reload();
  }
}
