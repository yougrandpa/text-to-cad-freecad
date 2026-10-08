// Open /ui/ with playwright-cli, then run-code --filename this file.
// All API calls use fixtures; no real conversations or provider requests.
async (page) => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const previous = { thread_id: 'welcome-old', model_id: 'welcome-old-model', title: '之前的设计',
    folder_id: null, archived: false, messages: 1, ir_version: 0, verified: false };
  let sessions = [previous], created = 0, chatRequests = 0;
  const errors = [];
  const onError = error => errors.push(error.message);
  const routeApi = async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/ui/') || path.startsWith('/viewer/')) return route.continue();
    if (path === '/health') return route.fulfill({ json: { worker_alive: true, selection_enabled: false } });
    if (path === '/settings/llm') return route.fulfill({ json: { attachments_supported: true,
      settings: { provider_label: '测试模型', model: 'welcome-fixture', api_key_set: true } } });
    if (path === '/session-folders') return route.fulfill({ json: { folders: [] } });
    if (path === '/sessions') {
      if (route.request().method() === 'POST') {
        const fresh = { ...previous, thread_id: `welcome-new-${++created}`, model_id: `welcome-model-${created}`,
          title: '新会话', messages: 0 };
        sessions.unshift(fresh);
        return route.fulfill({ json: fresh });
      }
      return route.fulfill({ json: { sessions } });
    }
    if (path.endsWith('/messages')) return route.fulfill({ json: {
      messages: path.includes(previous.thread_id) ? [{ role: 'user', content: '之前的设计要求' }] : [],
    } });
    if (path.endsWith('/ir')) return route.fulfill({ json: {
      model_id: path.split('/')[2], version: 0, bodies: [], parameters: {}, requirements: { raw_text: '' },
    } });
    if (path === '/chat') chatRequests++;
    return route.fulfill({ status: 404, json: { detail: 'no geometry in browser fixture' } });
  };
  page.on('pageerror', onError);
  await page.route('http://127.0.0.1:8000/**', routeApi);
  try {
    await page.goto('http://127.0.0.1:8000/ui/?thread=welcome-old');
    await page.waitForFunction(() => document.querySelector('#stream').textContent.includes('之前的设计要求'));
    check(await page.locator('#welcome').isHidden(), 'Existing messages must hide the welcome panel');
    await page.locator('#newSessionBtn').click();
    await page.waitForFunction(() => document.querySelector('#threadLabel').textContent === 'welcome-new-1');
    await page.locator('#input').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.activeElement === document.querySelector('#input'));
    check(await page.locator('#welcome').isVisible(), 'New session must immediately show the welcome panel');
    check(await page.locator('#welcome .sample').count() === 3, 'All three editable suggestions must be present');
    check(await page.locator('#stream > :not(#welcome)').count() === 0, 'New session must have an empty transcript');

    for (const sample of await page.locator('#welcome .sample').all()) {
      const text = await sample.getAttribute('data-text');
      await sample.click();
      check(await page.locator('#input').inputValue() === text, 'Suggestion must fill an editable draft');
      check(await page.locator('#welcome').isVisible(), 'Picking a suggestion must keep the welcome panel');
    }
    check(chatRequests === 0, 'Picking suggestions must not send provider requests');
    await page.locator('#chatPane').screenshot({ path: 'output/playwright/new-session-welcome.png' });

    await page.reload();
    await page.waitForFunction(() => document.querySelector('#threadLabel').textContent === 'welcome-new-1');
    check(await page.locator('#welcome').isVisible(), 'Reload must show the same empty welcome panel');
    await page.locator('#input').focus();
    await page.keyboard.press('Control+k');
    await page.waitForFunction(() => document.querySelector('#threadLabel').textContent === 'welcome-new-2');
    await page.waitForFunction(() => document.activeElement === document.querySelector('#input'));
    check(await page.locator('#welcome').isVisible(), 'Keyboard-created session must immediately show suggestions');
    check(errors.length === 0, `Browser errors: ${errors.join('; ')}`);
    return { created, suggestions: 3, chatRequests, checked: ['button creation', 'editable samples', 'reload parity', 'keyboard creation'] };
  } finally {
    page.off('pageerror', onError);
    await page.unroute('http://127.0.0.1:8000/**', routeApi);
  }
}
