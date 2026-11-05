import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const fixturePath = process.env.FLOWWEAVE_CHILD_BROWSER_FIXTURE;
test.skip(!fixturePath, 'Requires an isolated platform, PostgreSQL and fixed native Runtime fixture');

test('native child browser followup attachment and result quote stay under the original parent', async ({ page }) => {
  test.setTimeout(180_000);
  const fixture = JSON.parse(readFileSync(fixturePath!, 'utf8')) as {
    gateway: string; route: string; parentId: string; childId: string; childTitle: string;
  };
  const writes: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = { ...request.headers(), cookie: 'flowweave_session=fixture-cookie' };
    for (const name of ['host', 'origin', 'referer', 'content-length']) delete headers[name];
    if (!['GET', 'HEAD'].includes(request.method())) writes.push(request.method() + ' ' + url.pathname);
    const response = await page.request.fetch(fixture.gateway + url.pathname + url.search, {
      method: request.method(), headers, data: request.postDataBuffer() ?? undefined, timeout: 90_000,
    });
    if (response.status() >= 400) console.error(`Isolated API ${response.status()} ${url.pathname}`);
    await route.fulfill({ response });
  });
  await page.routeWebSocket('**/api/v1/**/stream*', socket => {
    const original = new URL(socket.url());
    const upstream = new WebSocket(fixture.gateway.replace(/^http/, 'ws') + original.pathname + original.search);
    const queued: Array<string | Buffer> = [];
    upstream.addEventListener('open', () => queued.splice(0).forEach(value => upstream.send(value)));
    upstream.addEventListener('message', event => socket.send(String(event.data)));
    upstream.addEventListener('error', () => socket.close());
    socket.onMessage(value => { if (upstream.readyState === WebSocket.OPEN) upstream.send(value); else queued.push(value); });
    socket.onClose(() => upstream.close());
  });
  await page.goto(fixture.route);
  await expect(page.getByRole('button', { name: '查看子会话', exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: '查看子会话', exact: true }).click();
  await page.getByRole('dialog', { name: '子会话列表' }).locator(`[data-child-binding-id="${fixture.childId}"]`).click();
  const pane = page.getByRole('region', { name: `子会话 ${fixture.childTitle}`, exact: true });
  await expect(pane).toContainText('user follow-up', { timeout: 90_000 });
  await expect(pane.locator('.conversation-message.user')).toHaveCount(3);
  const input = pane.getByRole('textbox', { name: '发送子会话消息' });
  await expect(input).toBeEditable();
  await pane.getByLabel('上传子会话附件', { exact: true }).setInputFiles({ name: 'native-ui.txt', mimeType: 'text/plain', buffer: Buffer.from('isolated browser attachment') });
  await pane.getByRole('button', { name: 'native-ui.txt', exact: true }).click();
  const preview = page.getByRole('dialog', { name: '文件预览' });
  await expect(preview).toContainText('isolated browser attachment');
  await preview.getByRole('button', { name: '关闭文件预览' }).click();
  await input.fill('Reply with BROWSER_READY only.');
  await input.press('Enter');
  await expect(pane.locator('.conversation-message.assistant').last()).toContainText('BROWSER_READY', { timeout: 90_000 });
  await expect(pane.locator('.conversation-message.user')).toHaveCount(4);
  const parentInput = page.getByRole('textbox', { name: '发送 Agent 消息' });
  await parentInput.fill('parent draft retained');
  await pane.getByRole('button', { name: '引用到父会话' }).last().click();
  await expect(parentInput).toHaveValue('parent draft retained');
  await expect(page.locator('.agent-conversation-reference')).toContainText('BROWSER_READY');
  await page.screenshot({ path: test.info().outputPath('native-child-pane.png') });
  await page.getByRole('button', { name: `关闭${fixture.childTitle}页签` }).click();
  expect(writes.filter(value => /DELETE|\/control$/.test(value))).toEqual([]);
  expect(writes.filter(value => value.endsWith(`/${fixture.parentId}/messages`))).toEqual([]);
  await expect(page).toHaveURL(fixture.route);
  await page.screenshot({ path: test.info().outputPath('native-child-browser.png') });
});
