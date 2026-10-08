// Browser-only turn fixtures; no real provider, conversation or CAD writes.
async (page) => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const session = { thread_id: 'flock-test', model_id: 'flock-model', title: '粒子动画验证', folder_id: null,
    archived: false, messages: 0, ir_version: 0, verified: false };
  const nextSession = { ...session, thread_id: 'flock-next', model_id: 'flock-next-model', title: '新的验证会话' };
  const ir = { model_id: session.model_id, version: 0, bodies: [], parameters: {}, requirements: { raw_text: '' } };
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const preferences = await page.evaluate(() => ({ widths: localStorage.getItem('tcad.paneWidths.v1'), sidebar: localStorage.getItem('tcad.sidebarHidden') }));
  let mesh = false, finish = null, started = 0, createdSession = false, cancelledTurn = false;
  const routeApi = async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/ui/') || path.startsWith('/viewer/')) return route.continue();
    if (path === '/health') return route.fulfill({ json: { worker_alive: true, selection_enabled: false } });
    if (path === '/sessions') {
      if (route.request().method() === 'POST') {
        createdSession = true;
        return route.fulfill({ json: nextSession });
      }
      return route.fulfill({ json: { sessions: createdSession ? [nextSession, session] : [session] } });
    }
    if (path === '/session-folders') return route.fulfill({ json: { folders: [] } });
    if (path === '/settings/llm') return route.fulfill({ json: { attachments_supported: true, settings: { provider_label: '测试模型', model: '粒子动画', api_key_set: true, supports_vision: false } } });
    if (path.endsWith('/messages')) return route.fulfill({ json: { messages: [] } });
    if (path === '/models') return route.fulfill({ status: 409, json: { detail: 'already exists' } });
    if (path.endsWith('/ir') || path.endsWith('/ir.json')) return route.fulfill({ json: {
      ...ir, model_id: path.includes(nextSession.model_id) ? nextSession.model_id : session.model_id,
    } });
    if (path.endsWith('/mesh') && mesh && path.includes(session.model_id)) return route.fulfill({ json: {
      model_id: session.model_id, version: 0, status: 'verified', artifact_id: 'sha256:' + 'a'.repeat(64),
      mesh: { vertices: [[0,0,0],[2,0,0],[2,2,0],[0,2,0],[0,0,2],[2,0,2],[2,2,2],[0,2,2]],
        facets: [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]] },
    } });
    if (path === '/chat') {
      started++;
      await new Promise(resolve => { finish = resolve; });
      finish = null;
      const body = `event: start\ndata: ${JSON.stringify({ thread_id: session.thread_id })}\n\n`
        + `event: result\ndata: ${JSON.stringify({ thread_id: session.thread_id, model_id: session.model_id,
          state: 'aborted', steps: 1, tokens_in: 0, tokens_out: 0, error: null })}\n\n`;
      try {
        return await route.fulfill({ contentType: 'text/event-stream', body });
      } catch (error) {
        // Navigation deliberately aborts the pending second turn's fetch.
        if (!cancelledTurn) throw error;
      }
      return;
    }
    if (path === '/chat/interrupt') {
      finish?.();
      return route.fulfill({ json: { found: true, cancelled: true } });
    }
    return route.fulfill({ status: 404, json: { detail: 'no geometry in browser fixture' } });
  };
  await page.route('http://127.0.0.1:8000/**', routeApi);
  const phase = name => page.waitForFunction(name => document.querySelector('#generationParticles').dataset.phase === name, name, { timeout: 70000 });
  const pixels = () => page.locator('#generationCanvas').evaluate(canvas => {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let visible = 0, accent = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] > 15) visible++;
      if (data[i + 3] > 15 && data[i] > data[i + 1] * 1.2) accent++;
    }
    return { visible, accent, width: canvas.width, height: canvas.height };
  });
  const hoverTest = async () => {
    // Find a dense patch of the actual SVG particles, then verify it opens
    // around the real mouse while the cycle is still holding its wordmark.
    const patch = await page.locator('#generationCanvas').evaluate(canvas => {
      const ctx = canvas.getContext('2d'), { width, height } = canvas;
      const data = ctx.getImageData(0, 0, width, height).data;
      const ratio = Math.min(devicePixelRatio, 2), radius = 14 * ratio;
      let best = { count: 0, x: 0, y: 0 };
      for (let y = radius; y < height - radius; y += 10 * ratio) {
        for (let x = radius; x < width - radius; x += 10 * ratio) {
          let count = 0;
          for (let py = Math.floor(y - radius); py < y + radius; py++) {
            for (let px = Math.floor(x - radius); px < x + radius; px++) {
              if ((px - x) ** 2 + (py - y) ** 2 < radius ** 2 && data[(py * width + px) * 4 + 3] > 15) count++;
            }
          }
          if (count > best.count) best = { x: x / ratio, y: y / ratio, count };
        }
      }
      return best;
    });
    check(patch.count > 20, 'Cannot find a visible particle patch to hover');
    const bounds = await page.locator('#generationParticles').boundingBox();
    await page.mouse.move(bounds.x + patch.x, bounds.y + patch.y);
    await page.waitForFunction(patch => {
      const canvas = document.querySelector('#generationCanvas');
      const ratio = Math.min(devicePixelRatio, 2), radius = 14 * ratio;
      const x = patch.x * ratio, y = patch.y * ratio;
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let py = Math.floor(y - radius); py < y + radius; py++) {
        for (let px = Math.floor(x - radius); px < x + radius; px++) {
          if ((px - x) ** 2 + (py - y) ** 2 < radius ** 2 && data[(py * canvas.width + px) * 4 + 3] > 15) count++;
        }
      }
      return count < patch.count * .65;
    }, patch, { timeout: 2000 });
    check(await page.locator('#generationParticles').getAttribute('data-phase') === 'wordmark', 'Hover check must not rely on the scatter phase');
    const mode = await page.locator('#generationParticles').getAttribute('data-mode');
    await page.locator('#viewport').screenshot({ path: `output/playwright/particle-hover-${mode}.png`, timeout: 10000 });
    await page.mouse.move(0, 0);
  };
  try {
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.evaluate(() => { localStorage.removeItem('tcad.paneWidths.v1'); localStorage.setItem('tcad.sidebarHidden', '0'); });
    await page.goto('http://127.0.0.1:8000/ui/?thread=flock-test');
    await page.locator('#sendBtn').waitFor();
    await page.waitForFunction(() => document.querySelector('#threadLabel').textContent === 'flock-test');
    check(await page.locator('#generationParticles').isHidden(), 'Idle view must not animate');
    await page.locator('#viewPlaceholder').waitFor({ state: 'visible' });
    const idleLogo = await page.locator('#viewPlaceholder').evaluate(node => {
      const style = getComputedStyle(node, '::before');
      return { image: style.backgroundImage, animation: style.animationName, width: parseFloat(style.width), height: parseFloat(style.height) };
    });
    check(idleLogo.image.includes('wordmark.svg') && idleLogo.animation === 'none' && idleLogo.width > 0 && idleLogo.height > 0,
      'Empty session must show the static tcad logo');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-idle.png', timeout: 10000 });
    await page.locator('#input').fill('创建一个安装底板');
    const began = await page.evaluate(() => performance.now());
    await page.locator('#sendBtn').click();
    await page.waitForFunction(() => document.querySelector('#generationParticles').dataset.mode === 'full'
      && document.querySelector('#generationParticles').dataset.phase === 'wordmark');
    const initial = await pixels();
    check(initial.visible > 1000 && initial.accent > 20, `Wordmark pixels missing: ${JSON.stringify(initial)}`);
    check(await page.locator('#generationParticles').evaluate(root => getComputedStyle(root).pointerEvents === 'none'), 'Animation must not consume pointer input');
    check(await page.locator('#viewPlaceholder').evaluate(node => getComputedStyle(node).visibility === 'hidden'), 'Idle logo must yield to particles');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-wordmark.png', timeout: 10000 });
    await phase('flock');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-flock.png', timeout: 10000 });
    const flockPixels = await pixels();
    check(flockPixels.visible > 1000, 'Flocking canvas became empty');
    await page.waitForFunction(began => performance.now() - began >= 45000, began, { timeout: 50000 });
    check(await page.locator('#generationParticles').getAttribute('data-phase') === 'flock', 'Birds must still be flocking at 45 seconds');
    const edgePixels = await page.locator('#generationCanvas').evaluate(canvas => {
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      const margin = 8 * Math.min(devicePixelRatio, 2);
      let visible = 0, edge = 0;
      for (let y = 0; y < canvas.height; y++) {
        for (let x = 0; x < canvas.width; x++) {
          if (data[(y * canvas.width + x) * 4 + 3] <= 15) continue;
          visible++;
          if (x < margin || x >= canvas.width - margin || y < margin || y >= canvas.height - margin) edge++;
        }
      }
      return { visible, edge };
    });
    check(edgePixels.visible > 1000 && edgePixels.edge / edgePixels.visible < .01, 'Late flock piled up at the canvas edge');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-late-flock.png', timeout: 10000 });
    await phase('gather');
    await phase('wordmark');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-return.png', timeout: 10000 });
    await hoverTest();
    const layouts = [];
    for (const [width, height] of [[981,360], [900,600], [1920,900], [1440,800]]) {
      await page.setViewportSize({ width, height });
      await page.waitForFunction(() => {
        const root = document.querySelector('#generationParticles');
        const canvas = document.querySelector('#generationCanvas');
        return Math.abs(canvas.width / Math.min(devicePixelRatio, 2) - root.getBoundingClientRect().width) < 1;
      });
      const bounds = await page.locator('#generationParticles').evaluate(root => {
        const parent = root.parentElement.getBoundingClientRect(), rect = root.getBoundingClientRect();
        return { fits: rect.left >= parent.left && rect.right <= parent.right + 1 && rect.top >= parent.top && rect.bottom <= parent.bottom + 1 };
      });
      check(bounds.fits, `Animation exceeds model pane at ${width}×${height}`);
      layouts.push({ width, height, passed: true });
    }
    mesh = true;
    await page.locator('#refreshView').click();
    await page.waitForFunction(() => document.querySelector('#generationParticles').dataset.mode === 'compact');
    check(await page.locator('#viewCanvas').isVisible(), 'Existing mesh must remain visible during animation');
    const wordmark = await page.locator('#generationParticles').boundingBox();
    const model = await page.locator('#viewport').boundingBox();
    check(wordmark.width < model.width * .5, 'Compact flock should preserve the model center');
    check(wordmark.width <= 180 && wordmark.height <= 96, 'Compact animation must use the smaller bounds');
    check(Math.abs(wordmark.y - model.y - 12) < 2 && Math.abs(model.x + model.width - wordmark.x - wordmark.width - 6) < 2,
      'Compact animation should sit close to the upper-right corner');
    await page.locator('#viewport').screenshot({ path: 'output/playwright/particle-compact.png', timeout: 10000 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.waitForFunction(() => document.querySelector('#generationParticles').classList.contains('is-static'));
    check(await page.locator('#generationWordmark').isVisible(), 'Reduced motion needs a static wordmark');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.waitForFunction(() => !document.querySelector('#generationParticles').classList.contains('is-static'));
    await page.locator('#stopBtn').click();
    await page.waitForFunction(() => document.querySelector('#generationParticles').hidden && !document.querySelector('#sendBtn').disabled);
    check(await page.locator('#viewCanvas').isVisible(), 'Stopping must preserve the mesh');
    // Start a second turn, then leave it. Its old cleanup must not restart a flock.
    await page.locator('#input').fill('继续修改');
    await page.locator('#sendBtn').click();
    await page.waitForFunction(() => !document.querySelector('#generationParticles').hidden);
    check(await page.locator('#viewport').getAttribute('data-generation') === 'compact', 'Second turn must restore the viewport generation mode');
    await hoverTest();
    cancelledTurn = true;
    await page.locator('#newSessionBtn').click();
    await page.waitForFunction(() => document.querySelector('#threadLabel').textContent === 'flock-next');
    finish?.();
    await page.waitForFunction(() => document.querySelector('#generationParticles').hidden);
    check(await page.locator('#viewPlaceholder').isVisible(), 'New session must restore the static logo');
    check(!await page.locator('#sendBtn').isDisabled(), 'New session must accept input after cancelling the old turn');
    check(errors.length === 0, `Browser errors: ${errors.join('; ')}`);
    return { layouts, initial, flockPixels, edgePixels, turns: started, checked: ['one-minute cycle', 'soft edge avoidance', 'real SVG wordmark', 'mouse avoidance', 'smaller upper-right layout', 'geometry remains interactive', 'reduced motion', 'stop cleanup', 'session switch cleanup'] };
  } finally {
    finish?.();
    await page.unroute('http://127.0.0.1:8000/**', routeApi);
    page.off('pageerror', onError);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.evaluate(preferences => {
      for (const [key, value] of [['tcad.paneWidths.v1', preferences.widths], ['tcad.sidebarHidden', preferences.sidebar]]) {
        if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value);
      }
    }, preferences);
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto('http://127.0.0.1:8000/ui/');
  }
}
