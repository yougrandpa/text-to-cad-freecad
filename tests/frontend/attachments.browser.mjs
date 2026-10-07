// Browser-only API fixtures: this probe never creates or modifies saved models.
async (page) => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAABAAAAAMCAIAAADkharWAAAAGklEQVR4nGPc4qzDQApgIkk1w6gG4sBwCFYAuKABO+uxqu0AAAAASUVORK5CYII=', 'base64');
  const image = { name: '零件参考图.png', mimeType: 'image/png', buffer: png };
  const dataUrl = `data:image/png;base64,${png.toString('base64')}`;
  const session = { thread_id: 'image-thread', model_id: 'image-model', title: '图片参考设计', folder_id: null,
    archived: false, messages: 1, ir_version: 0, verified: false };
  let supportsVision = false, mode = 'http-error';
  const requests = [], pageErrors = [];
  const onError = error => pageErrors.push(error.message);
  page.on('pageerror', onError);
  const previous = await page.evaluate(() => ({ widths: localStorage.getItem('tcad.paneWidths.v1'), sidebar: localStorage.getItem('tcad.sidebarHidden') }));
  const routeApi = async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/ui/') || path.startsWith('/viewer/')) return route.continue();
    if (path === '/health') return route.fulfill({ json: { worker_alive: true, selection_enabled: false } });
    if (path === '/sessions') return route.fulfill({ json: route.request().method() === 'POST' ? session : { sessions: [session] } });
    if (path === '/session-folders') return route.fulfill({ json: { folders: [] } });
    if (path === '/settings/llm') return route.fulfill({ json: { attachments_supported: true, settings: { provider: 'custom', provider_label: '测试模型', model: '图片输入测试', supports_vision: supportsVision, api_key_set: true } } });
    if (path.endsWith('/messages')) return route.fulfill({ json: { messages: [{ role: 'user', content: '历史参考图片', images: [{ name: image.name, data_url: dataUrl }], files: [{ name: '历史说明.md', content: '# 零件说明\n宽度 80 mm' }] }] } });
    if (path === '/models') return route.fulfill({ status: 409, json: { detail: 'already exists' } });
    if (path.endsWith('/ir')) return route.fulfill({ json: { model_id: 'image-model', version: 0, bodies: [], parameters: {}, requirements: { raw_text: '' } } });
    if (path === '/chat') {
      const request = route.request().postDataJSON();
      requests.push(request);
      if (mode === 'http-error') return route.fulfill({ status: 400, json: { detail: '当前模型不支持图片输入，请切换视觉模型' } });
      const start = `event: start\ndata: ${JSON.stringify({ thread_id: session.thread_id })}\n\n`;
      const terminal = mode === 'provider-error'
        ? `event: error\ndata: ${JSON.stringify({ type: 'ProviderError', message: '此模型不支持 image_url 图片输入' })}\n\n`
        : `event: result\ndata: ${JSON.stringify({ model_id: session.model_id, thread_id: session.thread_id,
          state: 'exhausted', steps: 1, tokens_in: 1, tokens_out: 1, error: null })}\n\n`;
      return route.fulfill({ contentType: 'text/event-stream', body: start + terminal });
    }
    return route.fulfill({ status: 404, json: { detail: 'browser fixture: no geometry' } });
  };
  await page.route('http://127.0.0.1:8000/**', routeApi);
  try {
    await page.evaluate(() => { localStorage.removeItem('tcad.paneWidths.v1'); localStorage.setItem('tcad.sidebarHidden', '0'); });
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto('http://127.0.0.1:8000/ui/?thread=image-thread');
    await page.locator('.msg.user img').waitFor();
    await page.locator('#attachmentPicker').setInputFiles(image);
    await page.locator('#attachmentPreviews img').waitFor();
    await page.locator('#input').fill('按照图片设计这个零件');
    await page.locator('#sendBtn').click();
    await page.locator('#composerErrorDialog[open]').waitFor();
    check(requests.length === 0, 'Unsupported model must fail before POST /chat');
    check((await page.locator('#composerErrorMessage').textContent()).includes('不支持图片输入'), 'Missing model capability error');
    check(await page.locator('#input').inputValue() === '按照图片设计这个零件', 'Error lost draft text');
    check(await page.locator('#attachmentPreviews img').count() === 1, 'Error lost draft image');
    await page.locator('#composerErrorDialog button').click();
    supportsVision = true;
    await page.locator('#sendBtn').click();
    await page.locator('#composerErrorDialog[open]').waitFor();
    check(requests.length === 1 && requests[0].images[0].data_url === dataUrl, 'Image payload did not reach POST /chat');
    check(await page.locator('#attachmentPreviews img').count() === 1, 'HTTP refusal lost image');
    await page.locator('#composerErrorDialog button').click();
    mode = 'provider-error';
    await page.locator('#sendBtn').click();
    await page.locator('#composerErrorDialog[open]').waitFor();
    check((await page.locator('#composerErrorMessage').textContent()).includes('image_url'), 'Provider rejection needs a popup');
    check(await page.locator('.msg.user img').count() === 2, 'Accepted image must render in transcript');
    check(await page.locator('#input').inputValue() === '', 'Accepted text not consumed');
    check(await page.locator('#attachmentPreviews img').count() === 0, 'Accepted image not consumed');
    await page.locator('#composerErrorDialog button').click();
    // Exercise screenshot paste and drag/drop in the real browser.
    await page.locator('#input').evaluate((input, dataUrl) => {
      const bytes = Uint8Array.from(atob(dataUrl.split(',')[1]), char => char.charCodeAt(0));
      const clipboardData = new DataTransfer();
      clipboardData.items.add(new File([bytes], '粘贴图片.png', { type: 'image/png' }));
      input.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
    }, dataUrl);
    await page.waitForFunction(() => document.querySelectorAll('#attachmentPreviews img').length === 1);
    await page.locator('#composer').evaluate((composer, dataUrl) => {
      const bytes = Uint8Array.from(atob(dataUrl.split(',')[1]), char => char.charCodeAt(0));
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File([bytes], '拖入图片.png', { type: 'image/png' }));
      composer.dispatchEvent(new DragEvent('drop', { dataTransfer, bubbles: true, cancelable: true }));
    }, dataUrl);
    await page.waitForFunction(() => document.querySelectorAll('#attachmentPreviews img').length === 2);
    await page.locator('#attachmentPicker').setInputFiles([image, { ...image, name: '第四张.png' }]);
    await page.waitForFunction(() => document.querySelectorAll('#attachmentPreviews img').length === 4);
    const results = [];
    for (const [width, height] of [[1440, 800], [1280, 600], [900, 600], [981, 360]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => {
        const composer = document.querySelector('#composer').getBoundingClientRect();
        const pane = document.querySelector('#chatPane').getBoundingClientRect();
        if (composer.bottom > pane.bottom || composer.top < pane.top) throw new Error('Composer exceeds chat pane');
        for (const control of document.querySelectorAll('#composer textarea, .composer-actions button, #attachmentPreviews')) {
          if (!control.getClientRects().length) continue;
          const rect = control.getBoundingClientRect();
          if (rect.left < composer.left || rect.right > composer.right + 1 || rect.bottom > composer.bottom + 1) throw new Error(`Clipped composer control: ${control.id || control.className}`);
        }
      });
      results.push({ width, height, passed: true });
    }
    await page.setViewportSize({ width: 1440, height: 800 });
    let screenshot = true;
    try { await page.locator('#composer').screenshot({ path: 'output/playwright/image-input-desktop.png', timeout: 5000, animations: 'disabled' }); }
    catch { screenshot = false; }
    mode = 'success';
    await page.locator('#sendBtn').click();
    await page.waitForFunction(() => document.querySelector('#attachmentPreviews').hidden && !document.querySelector('#sendBtn').disabled);
    check(requests.at(-1).text === '' && requests.at(-1).images.length === 4, 'Images-only send failed');
    supportsVision = false;
    const docs = [{ name: '设计说明.md', mimeType: 'text/markdown', buffer: Buffer.from('# 设计说明\n底板 80 × 50 mm') },
      { name: '尺寸.txt', mimeType: 'text/plain', buffer: Buffer.from('厚度 8 mm') }];
    await page.locator('#attachmentPicker').setInputFiles(docs);
    await page.waitForFunction(() => document.querySelectorAll('#attachmentPreviews .text-attachment').length === 2);
    check(await page.locator('#attachmentBtn svg path').getAttribute('d') === 'M12 5v14M5 12h14', 'Attachment entry must show a plus icon');
    await page.locator('#sendBtn').click();
    await page.waitForFunction(() => document.querySelector('#attachmentPreviews').hidden && !document.querySelector('#sendBtn').disabled);
    check(requests.at(-1).files.length === 2 && !requests.at(-1).images, 'Text attachments must work without vision support');
    check(requests.at(-1).files[0].content.includes('80 × 50'), 'Markdown content was not sent');
    check(await page.locator('.message-file summary').count() === 3, 'Text files must appear in transcript and history');
    await page.locator('.message-file summary').last().click();
    check(await page.locator('.message-file[open] pre').textContent() === '厚度 8 mm', 'File preview lost content');
    supportsVision = true;
    await page.locator('#attachmentPicker').setInputFiles([image, docs[0]]);
    await page.waitForFunction(() => document.querySelectorAll('#attachmentPreviews .input-attachment').length === 2);
    await page.locator('#sendBtn').click();
    await page.waitForFunction(() => document.querySelector('#attachmentPreviews').hidden && !document.querySelector('#sendBtn').disabled);
    check(requests.at(-1).images.length === 1 && requests.at(-1).files.length === 1, 'Mixed image and document upload failed');
    await page.locator('#attachmentPicker').setInputFiles({ name: '无效.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg/>') });
    await page.locator('#composerErrorDialog[open]').waitFor();
    check((await page.locator('#composerErrorMessage').textContent()).includes('PNG'), 'Invalid image format needs a popup');
    check(pageErrors.length === 0, `Browser errors: ${pageErrors.join('; ')}`);
    return { layouts: results, checked: ['history', 'file picker', 'paste', 'drop', 'capability popup', 'HTTP popup', 'provider popup', 'images-only send', 'text-only files without vision', 'mixed attachments', 'plus icon', 'invalid format'], requests: requests.length, screenshot };
  } finally {
    await page.unroute('http://127.0.0.1:8000/**', routeApi);
    page.off('pageerror', onError);
    await page.evaluate(previous => {
      for (const [key, value] of [['tcad.paneWidths.v1', previous.widths], ['tcad.sidebarHidden', previous.sidebar]]) {
        if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value);
      }
    }, previous);
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto('http://127.0.0.1:8000/ui/');
  }
}
