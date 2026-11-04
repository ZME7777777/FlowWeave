import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-11-05T00:00:00Z';
const parentId = 'child-product-parent';
const otherParentId = 'child-product-other';
const children = Array.from({ length: 8 }, (_, index) => ({
  id: `native-child-${index + 1}`, parent_binding_id: parentId,
  display_title: index === 2 ? '很长的子会话标题用来检查标签和悬浮卡不会因为内容被撑开'.repeat(3) : `独立任务 ${index + 1}`,
  lifecycle: 'ACTIVE', unread: index !== 0, unread_origin: index === 1 ? 'SYSTEM' : 'MANUAL', created_at: now,
}));
async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}
async function openParent(page: Page, host: 'workspace' | 'node', delayFirst = false) {
  let authenticated = false;
  const writes: string[] = [];
  const requests: string[] = [];
  let release: (() => void) | undefined;
  let peak = 0;
  let reads = 0;
  let shouldDelay = delayFirst;
  const unread = new Set(children.filter(child => child.unread).map(child => child.id));
  const root = host === 'node' ? '/flow-runs/child-run/nodes/child-node/attempts/child-attempt/agent-sessions' : '/agent/conversations';
  const workspace = { id: host === 'node' ? 'child-run' : 'children-workspace', display_name: '子会话工作区', desired_state: 'RUNNING', updated_at: now };
  const parents = [parentId, otherParentId].map((id, index) => ({ id, display_title: index ? '另一个父会话' : '父会话', title_state: 'MANUAL', lifecycle: 'ACTIVE', streaming_callback_ready: true, write_available: true, execution_status: 'finished', created_at: now, updated_at: now, child_summary: id === parentId ? { total: 8, unread: unread.size, running: 1, attention: 1, condensing: 0, unknown: 0, pending: 0 } : undefined }));
  const activity = () => children.map((child, index) => ({ id: child.id, parent_binding_id: parentId, execution_status: index === 0 ? 'running' : index === 1 ? 'error' : 'finished', possibly_stuck: false, failed: index === 1, condensing: false, sync_pending: false, unread: unread.has(child.id), unread_origin: child.unread_origin }));
  const hydration = (id: string) => ({
    events: { events: [{ id: `${id}-answer`, event_type: 'MESSAGE', payload: { source: 'agent', content: `${id} 的正式独立历史`, final_reply: true } }], next_cursor: `${id}-answer`, history_cursor: null },
    context: {}, readiness: { ready: true, execution_status: 'finished' },
  });
  await page.routeWebSocket('**/stream*', () => undefined);
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    requests.push(url.pathname + url.search);
    if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`);
    const user = { id: 'child-owner', username: 'child-user', role: 'SUPER_ADMIN', is_super_admin: true };
    if (path.endsWith('/auth/me')) return authenticated ? json(route, user) : json(route, { error: { code: 'AUTHENTICATION_REQUIRED', message: '请先登录' } }, 401);
    if (path.endsWith('/auth/login')) { authenticated = true; return json(route, user); }
    if (path.includes('/agent-session-children/')) {
      const suffix = path.split('/agent-session-children/')[1].split('/');
      const childId = suffix[1];
      if (suffix.length === 1) return json(route, suffix[0] === parentId ? children.map(child => ({ ...child, unread: unread.has(child.id) })) : []);
      if (suffix[2] === 'unread') { unread.delete(childId); return json(route, { id: childId, unread: false, unread_origin: null }); }
      if (suffix[2] === 'hydration') {
        reads += 1;
        peak = Math.max(peak, reads);
        if (shouldDelay && childId === children[0].id) { shouldDelay = false; await new Promise<void>(resolve => { release = resolve; }); }
        await json(route, hydration(childId));
        reads -= 1;
        return;
      }
      if (suffix[2] === 'events') return json(route, { events: [], next_cursor: `${childId}-answer` });
      return json(route, { ...children.find(child => child.id === childId), write_available: true, streaming_callback_ready: true });
    }
    if (path.endsWith('/default') || path.endsWith('/agent-sessions/host')) return json(route, workspace);
    if (path.endsWith('/runtime')) return json(route, { state: 'ACTIVE', write_available: true, updated_at: now });
    if (path.endsWith('/conversations') || path.endsWith('/agent-sessions')) return json(route, { items: parents, next_cursor: null });
    if (path.endsWith('/conversation-activity') || path.endsWith('/agent-sessions/activity')) return json(route, { conversations: [parents[0]], running_binding_ids: [], child_activity_parent_binding_ids: [parentId], child_activity: activity() });
    if (path.endsWith('/hydration')) return json(route, { events: { events: [], next_cursor: null }, context: {}, readiness: { ready: true, execution_status: 'finished' } });
    if (path.endsWith('/events')) return json(route, { events: [], next_cursor: null });
    if (path.endsWith('/input-readiness')) return json(route, { ready: true, execution_status: 'finished' });
    if (path.endsWith('/context')) return json(route, {});
    if (path.endsWith('/pending-confirmation')) return json(route, { pending: false });
    if (path.endsWith('/work-directories')) return json(route, { root: { kind: 'ROOT', display_name: '根工作区', working_directory: '/runtime/workspace/project' }, items: [] });
    if (path.endsWith('/workspace')) return json(route, { root: '/runtime/workspace/project', scope: { kind: 'ROOT', display_name: '根工作区' }, working_directory: '/runtime/workspace/project', files: [], repositories: [], runtime: {}, ide: { gateway: { supported: false } } });
    if (path.endsWith('/pinned-conversations') || path.endsWith('/model-providers') || path.endsWith('/capabilities') || path.endsWith('/capability-collections')) return json(route, []);
    if (path.includes('/conversations/') || path.includes('/agent-sessions/')) return json(route, parents.find(parent => path.endsWith(parent.id)) ?? parents[0]);
    return json(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByLabel('账号').fill('child-user');
  await page.getByLabel('密码').fill('test-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.goto(`${root}/${parentId}`);
  await expect(page.getByRole('button', { name: '查看子会话', exact: true })).toBeVisible();
  return { writes, requests, root, unread, release: () => release?.(), hasPending: () => Boolean(release), peak: () => peak };
}
async function chooseChild(page: Page, childId: string) {
  const showCard = page.getByRole('button', { name: '显示悬浮信息卡' });
  if (await showCard.isVisible()) await showCard.click();
  await page.getByRole('button', { name: '查看子会话', exact: true }).click();
  const picker = page.getByRole('dialog', { name: '子会话列表' });
  await picker.locator(`[data-child-binding-id="${childId}"]`).click();
}
for (const host of ['workspace', 'node'] as const) {
  test(`child card opens independent titled side tabs and closes only views (${host})`, async ({ page }) => {
    const state = await openParent(page, host);
    const parent = page.locator(`[data-conversation-binding-id="${parentId}"]`);
    await expect(parent.getByRole('img', { name: '会话正在运行', exact: true })).toHaveCount(0);
    await expect(parent).toContainText('子会话 · 1 个运行中');
    await chooseChild(page, children[0].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[0].display_title}` })).toContainText(`${children[0].id} 的正式独立历史`);
    await expect(page).toHaveURL(`${state.root}/${parentId}`);
    await expect(page.getByRole('button', { name: '全屏查看工作区工具' })).toBeDisabled();
    await chooseChild(page, children[1].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toContainText(`${children[1].id} 的正式独立历史`);
    await expect.poll(() => state.unread.has(children[1].id)).toBe(false);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` }).getByRole('button', { name: '从此处分叉会话' })).toHaveCount(0);
    const heights = await page.locator('.agent-child-conversation-panel.active').evaluate(element => ({ pane: element.clientHeight, body: element.parentElement!.clientHeight }));
    expect(heights.pane).toBeGreaterThan(heights.body - 4);
    await page.screenshot({ path: test.info().outputPath('child-tabs.png') });
    await page.locator(`[data-conversation-binding-id="${otherParentId}"] .agent-workspace-conversation-select`).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toHaveCount(0);
    await page.locator(`[data-conversation-binding-id="${parentId}"] .agent-workspace-conversation-select`).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toBeVisible();
    expect(state.unread.has(children[2].id)).toBe(true);
    const tabs = page.getByRole('navigation', { name: '工作区页签' });
    await expect(tabs.getByRole('button', { name: children[0].display_title, exact: true })).toBeVisible();
    await tabs.getByRole('button', { name: children[0].display_title, exact: true }).dblclick();
    await expect(page.locator('.agent-workspace-drawer.fullscreen')).toHaveCount(0);
    await tabs.getByRole('button', { name: `关闭${children[0].display_title}页签` }).click();
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toBeVisible();
    await tabs.getByRole('button', { name: `关闭${children[1].display_title}页签` }).click();
    expect(state.writes.filter(write => /DELETE|\/control/.test(write))).toEqual([]);
    await expect(page.getByRole('button', { name: '查看子会话', exact: true })).toBeVisible();
    await chooseChild(page, children[0].id);
    await page.reload();
    await expect(page.getByRole('button', { name: '查看子会话', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: `关闭${children[0].display_title}页签` })).toHaveCount(0);
  });
  test(`child reads serialize across fast tab selection and reject late display (${host})`, async ({ page }) => {
    const state = await openParent(page, host, true);
    await chooseChild(page, children[0].id);
    await expect.poll(state.hasPending).toBe(true);
    await chooseChild(page, children[1].id);
    await expect(page.getByRole('region', { name: `子会话 ${children[1].display_title}` })).toContainText('正在加载会话');
    state.release();
    const pane = page.getByRole('region', { name: `子会话 ${children[1].display_title}` });
    await expect(pane).toContainText(`${children[1].id} 的正式独立历史`);
    await expect(pane).not.toContainText(`${children[0].id} 的正式独立历史`);
    expect(state.peak()).toBe(1);
  });
}

test('child picker pages separately and the floating card never scrolls', async ({ page }) => {
  await openParent(page, 'workspace');
  await page.getByRole('button', { name: '查看子会话', exact: true }).click();
  const picker = page.getByRole('dialog', { name: '子会话列表' });
  await expect(picker.locator('[data-child-binding-id]')).toHaveCount(6);
  await picker.getByRole('button', { name: '下一页子会话' }).click();
  await expect(picker.locator('[data-child-binding-id]')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(picker).toHaveCount(0);
  const dimensions = await page.locator('.agent-workspace-summary').evaluate(element => ({
    width: element.clientWidth, contentWidth: element.scrollWidth,
    height: element.clientHeight, contentHeight: element.scrollHeight,
  }));
  expect(dimensions.contentWidth).toBeLessThanOrEqual(dimensions.width);
  expect(dimensions.contentHeight).toBeLessThanOrEqual(dimensions.height);
  await page.screenshot({ path: test.info().outputPath('child-card.png') });
});
