// Browser regression probe: open /ui/ in playwright-cli, then run this file
// with `run-code --filename tests/frontend/history-layout.browser.mjs`.
// API fixtures stay in the browser; no saved conversations are changed.
async (page) => {
  const folders = Array.from({ length: 12 }, (_, i) => ({ folder_id: `layout-folder-${i}`, name: `机械设计文件夹 ${i + 1}` }));
  const sessions = Array.from({ length: 40 }, (_, i) => ({
    thread_id: `layout-thread-${i}`, model_id: 'layout-model', folder_id: null,
    archived: false, title: `创建一个带装配动画的人机支架，第 ${i + 1} 个设计`,
    last_at: new Date().toISOString(), messages: 1, ir_version: 0, verified: false,
  }));
  const sessionsRoute = route => route.fulfill({ json: { sessions } });
  const foldersRoute = route => route.fulfill({ json: { folders } });
  await page.route('**/sessions?include_archived=true', sessionsRoute);
  await page.route('**/session-folders', foldersRoute);
  const results = [];
  try {
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('.session-row').length === 40
      && document.querySelectorAll('.history-folder-row').length === 14);
    for (const [width, height] of [[1440, 700], [1280, 400], [981, 360]]) {
      await page.setViewportSize({ width, height });
      if (await page.locator('#sidebarToggle').getAttribute('aria-expanded') === 'false') {
        await page.locator('#sidebarToggle').click();
      }
      const geometry = await page.evaluate(() => {
        const toolbar = document.querySelector('.history-toolbar');
        const list = document.querySelector('#sessionList');
        const bounds = toolbar.getBoundingClientRect();
        const check = (condition, message) => { if (!condition) throw new Error(message); };
        const surface = getComputedStyle(toolbar);
        const fade = getComputedStyle(toolbar, '::after');
        const boundaryGap = list.getBoundingClientRect().top - bounds.bottom;
        check(surface.backgroundColor !== 'rgba(0, 0, 0, 0)' && parseFloat(surface.borderTopWidth) > 0,
          'Toolbar needs a complete visible surface');
        check(boundaryGap >= 10, 'Missing fixed separation between toolbar and list');
        check(fade.position === 'absolute' && fade.pointerEvents === 'none'
          && fade.backgroundImage.includes('linear-gradient') && parseFloat(fade.height) - boundaryGap >= 12,
          'Scroll transition must reach into list without blocking controls');
        check(list.clientHeight > 0 && list.scrollHeight > list.clientHeight,
          `History must scroll: viewport ${innerWidth}×${innerHeight}, list ${list.clientHeight}/${list.scrollHeight}`);
        for (const button of toolbar.querySelectorAll('button')) {
          const rect = button.getBoundingClientRect();
          check(rect.left >= bounds.left && rect.right <= bounds.right, 'Toolbar button exceeds sidebar');
          check(list.getBoundingClientRect().top - rect.bottom >= 12, 'Missing space below toolbar buttons');
        }
        for (const offset of [0, 35, 260, 740, list.scrollHeight]) {
          list.scrollTop = offset;
          const current = toolbar.getBoundingClientRect();
          check(current.top === bounds.top && current.height === bounds.height, 'Scrolling moved toolbar');
          for (let y = bounds.top + 2; y < bounds.bottom; y += 3) {
            const hit = document.elementFromPoint(bounds.left + bounds.width / 2, y);
            check(!hit?.closest('#sessionList'), 'List content bleeds into toolbar');
          }
        }
        for (const group of [list, document.querySelector('.history-folders')]) {
          let bottom = -Infinity;
          for (const child of group.children) {
            const rect = child.getBoundingClientRect();
            check(rect.top >= bottom - .5, 'History rows overlap');
            bottom = rect.bottom;
          }
        }
        list.scrollTop = 0;
        return { top: bounds.top, height: bounds.height };
      });
      const trigger = page.locator('.session-row .history-menu-toggle').first();
      await trigger.click();
      await page.evaluate(before => {
        const toolbar = document.querySelector('.history-toolbar').getBoundingClientRect();
        const panel = document.querySelector('.session-row .history-menu-actions:not([hidden])');
        const row = panel.closest('.session-row').getBoundingClientRect();
        const rect = panel.getBoundingClientRect();
        if (toolbar.top !== before.top || toolbar.height !== before.height) {
          throw new Error(`Menu expansion moved toolbar: ${innerWidth}×${innerHeight}, before ${JSON.stringify(before)}, after ${toolbar.top}/${toolbar.height}`);
        }
        if (rect.left < row.left || rect.right > row.right || rect.bottom > row.bottom) throw new Error('Menu exceeds conversation row');
      }, geometry);
      await trigger.press('Escape');
      results.push({ width, height, passed: true });
    }
    await page.emulateMedia({ forcedColors: 'active' });
    const forcedColors = await page.evaluate(() => {
      const toolbar = document.querySelector('.history-toolbar');
      return getComputedStyle(toolbar, '::after').display === 'none'
        && parseFloat(getComputedStyle(toolbar).borderTopWidth) > 0;
    });
    if (!forcedColors) throw new Error('High contrast mode must preserve a clear toolbar boundary');
    return results;
  } finally {
    await page.emulateMedia({ forcedColors: 'none' });
    await page.unroute('**/sessions?include_archived=true', sessionsRoute);
    await page.unroute('**/session-folders', foldersRoute);
    await page.setViewportSize({ width: 1440, height: 700 });
    await page.reload();
  }
}
