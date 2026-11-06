import { expect, test } from '@playwright/test';

const now = '2026-09-01T00:00:00Z';
const user = { id: 'img-user', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true, can_use_agent_sessions: true };
const workspace = { id: 'img-workspace', display_name: '图片工作区', desired_state: 'RUNNING', updated_at: now };
const conversation = { id: 'img-conversation', display_title: '图片会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'idle', created_at: now, updated_at: now };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6HwAAAABJRU5ErkJggg==', 'base64');
const attachments = ['一', '二', '三'].map((name, index) => ({
  filename: `图片${name}.png`, mime_type: 'image/png', byte_size: 70,
  path: `/runtime/workspace/project/uploads/img-conversation-${index}--图片${name}.png`,
}));
const events = {
  events: [{ id: 'img-user-1', event_type: 'MESSAGE', payload: { source: 'user', parent_id: '__root__', content: '三张图片', timestamp: now, attachments } }],
  next_cursor: 'img-user-1', history_cursor: null, result: { status: 'COMPLETED' },
};

async function open(page: import('@playwright/test').Page) {
  let authenticated = false;
  await page.routeWebSocket('**/agent-workspaces/**/stream', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const fulfill = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return authenticated ? fulfill(user) : fulfill({ error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login') && request.method() === 'POST') { authenticated = true; return fulfill(user); }
    if (path.endsWith('/agent-workspaces/default')) return fulfill(workspace);
    if (path.endsWith('/runtime')) return fulfill({ state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') && request.method() === 'GET') return fulfill({ items: [conversation], next_cursor: null });
    if (path.endsWith('/workspace/file')) return route.fulfill({ status: 200, contentType: 'image/png', body: png });
    if (path.endsWith('/hydration')) return fulfill({ events, context: { model_name: 'm', window_tokens: 1000, used_tokens: 1, usage_current: true }, readiness: { ready: true, execution_status: 'idle' } });
    if (path.endsWith('/events')) return fulfill(events);
    if (path.endsWith('/input-readiness')) return fulfill({ ready: true, execution_status: 'idle' });
    if (path.endsWith('/pending-confirmation')) return fulfill({ pending: false });
    if (path.endsWith('/context')) return fulfill({ model_name: 'm', window_tokens: 1000, used_tokens: 1, usage_current: true });
    if (path.endsWith('/work-directories')) return fulfill({ root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return fulfill({ root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', work_directory: null, files: [], repositories: [], runtime: {}, ide: { workspace_path: '/runtime/workspace/project', gateway: { supported: false, status: '不可用', note: '' } } });
    if (path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return fulfill([]);
    if (path.includes('/conversations/') && request.method() === 'GET') return fulfill(conversation);
    return fulfill({ error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill(user.username);
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByRole('button', { name: '账户与设置' })).toContainText(user.username);
  await page.goto(`/agent/conversations/${conversation.id}`);
}

test('images of one conversation can be stepped with buttons and arrow keys, and zoomed without a mouse', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: /图片一\.png/ }).first().click();
  const dialog = page.getByRole('dialog', { name: '文件预览' });
  await expect(dialog.getByRole('heading', { name: '图片一.png' })).toBeVisible();
  await expect(dialog.getByText('1 / 3')).toBeVisible();

  await dialog.getByRole('button', { name: '下一张图片' }).click();
  await expect(dialog.getByRole('heading', { name: '图片二.png' })).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(dialog.getByRole('heading', { name: '图片三.png' })).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(dialog.getByRole('heading', { name: '图片一.png' })).toBeVisible();
  await page.keyboard.press('ArrowLeft');
  await expect(dialog.getByRole('heading', { name: '图片三.png' })).toBeVisible();

  const image = dialog.locator('.agent-image-preview-viewport img');
  const scale = async () => (await image.evaluate(node => new DOMMatrix(getComputedStyle(node).transform).a));
  const offsetX = async () => (await image.evaluate(node => new DOMMatrix(getComputedStyle(node).transform).e));
  expect(await scale()).toBeCloseTo(1, 2);
  await page.keyboard.press('+');
  await expect.poll(scale).toBeGreaterThan(1.2);
  const zoomed = await scale();
  // Zoomed: arrow keys pan instead of switching images.
  const before = await offsetX();
  await page.keyboard.press('ArrowLeft');
  await expect(dialog.getByRole('heading', { name: '图片三.png' })).toBeVisible();
  await expect.poll(offsetX).toBeGreaterThan(before);
  // Trackpad pinch is delivered as ctrl+wheel.
  await image.hover();
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, -120);
  await page.keyboard.up('Control');
  await expect.poll(scale).toBeGreaterThan(zoomed);
  await page.keyboard.press('0');
  await expect.poll(scale).toBeCloseTo(1, 2);
  // Back at 100% the arrow keys switch images again.
  await page.keyboard.press('ArrowLeft');
  await expect(dialog.getByRole('heading', { name: '图片二.png' })).toBeVisible();
  // On-screen controls work as well.
  await dialog.getByRole('button', { name: '放大' }).click();
  await expect.poll(scale).toBeGreaterThan(1.2);
  await dialog.getByRole('button', { name: '还原大小' }).click();
  await expect.poll(scale).toBeCloseTo(1, 2);
});

test('pinch (ctrl+wheel) and browser zoom shortcuts are consumed by the preview instead of zooming the page', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: /图片一\.png/ }).first().click();
  const dialog = page.getByRole('dialog', { name: '文件预览' });
  await expect(dialog.getByRole('heading', { name: '图片一.png' })).toBeVisible();
  const image = dialog.locator('.agent-image-preview-viewport img');
  const scale = async () => (await image.evaluate(node => new DOMMatrix(getComputedStyle(node).transform).a));

  // A passive observer on window runs after the preview's handler: if the preview did not call
  // preventDefault, the browser would zoom the whole page together with the image.
  await page.evaluate(() => {
    (window as unknown as { __wheel: boolean[] }).__wheel = [];
    window.addEventListener('wheel', event => { if (event.ctrlKey || event.metaKey) (window as unknown as { __wheel: boolean[] }).__wheel.push(event.defaultPrevented); }, { passive: true });
  });
  await image.hover();
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, -120);
  await page.keyboard.up('Control');
  await expect.poll(scale).toBeGreaterThan(1.05);
  expect(await page.evaluate(() => (window as unknown as { __wheel: boolean[] }).__wheel)).toEqual([true]);

  // Pinching over the header / toolbar (outside the image) must not zoom the page either.
  await dialog.getByRole('heading', { name: '图片一.png' }).hover();
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, -120);
  await page.keyboard.up('Control');
  expect(await page.evaluate(() => (window as unknown as { __wheel: boolean[] }).__wheel)).toEqual([true, true]);

  // Cmd/Ctrl + = / - / 0 zoom the image, not the page.
  const before = await scale();
  const defaults: boolean[] = [];
  await page.exposeFunction('recordKey', (prevented: boolean) => defaults.push(prevented));
  await page.evaluate(() => window.addEventListener('keydown', event => { if ((event.metaKey || event.ctrlKey) && ['=', '+', '-', '0'].includes(event.key)) (window as unknown as { recordKey: (v: boolean) => void }).recordKey(event.defaultPrevented); }));
  await page.keyboard.press('Control+=');
  await expect.poll(scale).toBeGreaterThan(before);
  await page.keyboard.press('Control+-');
  await page.keyboard.press('Control+0');
  await expect.poll(scale).toBeCloseTo(1, 2);
  expect(defaults).toEqual([true, true, true]);
});
