import { expect, test, type Route } from '@playwright/test';

async function respond(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('conversation management keeps the selected workspace and pagination in fixed panes', async ({ page }) => {
  const now = new Date().toISOString();
  const workspace = { id: 'workspace-default', display_name: '默认工作区', desired_state: 'RUNNING', updated_at: now };
  const items = Array.from({ length: 21 }, (_, index) => ({
    id: `conversation-${index + 1}`,
    display_title: `会话 ${index + 1}`,
    pinned: false,
    unread: false,
    created_at: now,
    updated_at: now,
    last_message_at: index === 20 ? new Date(Date.now() - 31 * 24 * 60 * 60 * 1000).toISOString() : now,
  }));

  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/me')) return respond(route, {
      id: 'conversation-manager', username: 'flowweave', role: 'USER', is_super_admin: false,
      can_use_agent_sessions: true,
    });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, workspace);
    if (path.endsWith('/agent-workspaces/workspace-default/work-directories')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: [] });
    if (path.endsWith('/agent-workspaces/workspace-default/conversation-management')) return respond(route, { items, truncated: false });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台' }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();

  const layout = page.locator('.conversation-management-layout');
  await expect(layout).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Agent 工作区' })).toContainText('根工作区');
  await expect(page.getByRole('button', { name: '根工作区', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '最后活动时间', exact: true })).toContainText('全部时间');
  await expect(page.locator('.conversation-management-workspace-list small')).toHaveCount(0);
  await expect(page.locator('.conversation-management-table tbody td small')).toHaveCount(0);
  await expect(page.locator('.conversation-management-table tbody tr')).toHaveCount(20);
  await expect(page.getByRole('navigation', { name: '列表分页' })).toBeVisible();
  await expect(page.getByText('共 21 项，第 1 / 2 页')).toBeVisible();

  const [pageBox, pagerBox, tableBox] = await Promise.all([
    page.locator('.conversation-management-page').boundingBox(),
    page.getByRole('navigation', { name: '列表分页' }).boundingBox(),
    page.locator('.conversation-management-table-wrap').boundingBox(),
  ]);
  expect(pageBox).not.toBeNull();
  expect(pagerBox).not.toBeNull();
  expect(tableBox).not.toBeNull();
  expect((pagerBox?.y ?? 0) + (pagerBox?.height ?? 0)).toBeLessThanOrEqual((pageBox?.y ?? 0) + (pageBox?.height ?? 0));
  expect((tableBox?.y ?? 0) + (tableBox?.height ?? 0)).toBeLessThanOrEqual(pagerBox?.y ?? 0);
  await expect(page.locator('.conversation-management-page')).toHaveCSS('overflow-y', 'hidden');
  await expect(page.locator('.conversation-management-table-wrap')).toHaveCSS('overflow-y', 'auto');

  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.getByText('共 21 项，第 2 / 2 页')).toBeVisible();
  await expect(page.locator('.conversation-management-table tbody tr')).toHaveCount(1);
  await expect(page.getByText('会话 21', { exact: true })).toBeVisible();
});

async function deletionSetup(page: import('@playwright/test').Page, count: number, options: { failBatch?: number; holdBatches?: number[]; skippedIds?: string[] } = {}) {
  const now = new Date().toISOString();
  let items = Array.from({ length: count }, (_, index) => ({ id: `batch-conversation-${index + 1}`, display_title: `批量会话 ${index + 1}`, pinned: false, unread: false, created_at: now, updated_at: now, last_message_at: now }));
  const requests: Array<{ ids: string[]; directory: string | null; key: string | undefined }> = [];
  const releases = new Map<number, () => void>();
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/me')) return respond(route, { id: 'batch-manager', username: 'batch-manager', role: 'USER', is_super_admin: false, can_use_agent_sessions: true });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, { id: 'workspace-default', display_name: '默认工作区', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/work-directories')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: [{ id: 'directory-project', display_name: '测试项目' }] });
    if (path.endsWith('/conversation-management/delete')) {
      const ids = request.postDataJSON().binding_ids as string[];
      requests.push({ ids, directory: url.searchParams.get('work_directory_id'), key: request.headers()['idempotency-key'] });
      const batch = requests.length;
      if (options.holdBatches?.includes(batch)) await new Promise<void>(resolve => releases.set(batch, resolve));
      if (ids.length > 100) return respond(route, { error: { code: 'INVALID_COMMAND', message: '每批最多100个' } }, 422);
      if (batch === options.failBatch) return respond(route, { error: { code: 'EXECUTOR_UNAVAILABLE', message: '暂时无法连接执行服务' } }, 503);
      const skipped = ids.filter(id => options.skippedIds?.includes(id)).map(id => ({ id, code: 'AGENT_CONVERSATION_RUNNING' }));
      const deleted_ids = ids.filter(id => !options.skippedIds?.includes(id));
      items = items.filter(item => !deleted_ids.includes(item.id));
      return respond(route, { deleted_ids, skipped });
    }
    if (path.endsWith('/conversation-management')) return respond(route, { items: url.searchParams.get('work_directory_id') === 'directory-project' ? items : [], truncated: count === 500 });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台' }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await page.getByRole('button', { name: '测试项目', exact: true }).click();
  await expect(page.getByText(`已选 0 / ${count} 个会话`, { exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: '选择当前筛选结果中的全部会话' }).check();
  await page.getByRole('button', { name: '删除已选会话', exact: true }).click();
  return { requests, release: (batch: number) => releases.get(batch)!() };
}

test('115 selected conversations are deleted sequentially in bounded batches and running skips are combined', async ({ page }) => {
  const state = await deletionSetup(page, 115, { holdBatches: [1, 2], skippedIds: ['batch-conversation-1', 'batch-conversation-115'] });
  const dialog = page.getByRole('dialog', { name: '确认删除会话' });
  let summary = '';
  page.on('dialog', async alert => { summary = alert.message(); await alert.accept(); });
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect.poll(() => state.requests.length).toBe(1);
  expect(state.requests[0].ids).toHaveLength(100);
  await expect(dialog.getByRole('status')).toHaveText('已处理 0 / 115 个会话…');
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '根工作区', exact: true })).toBeDisabled();
  state.release(1);
  await expect.poll(() => state.requests.length).toBe(2);
  await expect(dialog.getByRole('status')).toHaveText('已处理 100 / 115 个会话…');
  expect(state.requests[1].ids).toHaveLength(15);
  expect(state.requests.every(request => request.directory === 'directory-project')).toBe(true);
  state.release(2);
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => summary).toContain('已删除 113 个会话；2 个会话');
  await expect(page.getByText('已选 0 / 2 个会话', { exact: true })).toBeVisible();
  const allIds = state.requests.flatMap(request => request.ids);
  expect(new Set(allIds).size).toBe(115);
  expect(new Set(state.requests.map(request => request.key)).size).toBe(2);
  expect(state.requests.every(request => Boolean(request.key))).toBe(true);
});

test('a failed middle batch stops deletion and retry submits only the remaining selection', async ({ page }) => {
  const state = await deletionSetup(page, 500, { failBatch: 2 });
  const dialog = page.getByRole('dialog', { name: '确认删除会话' });
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('已删除 100 个，跳过 0 个');
  await expect(dialog.getByRole('alert')).toContainText('未完成的会话仍保留选中');
  await expect(dialog.getByRole('button', { name: '确认永久删除', exact: true })).toBeEnabled();
  expect(state.requests).toHaveLength(2);
  await expect(page.getByText('已选 400 / 400 个会话', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.requests.map(request => request.ids.length)).toEqual([100, 100, 100, 100, 100, 100]);
  const completedBeforeFailure = new Set(state.requests[0].ids);
  expect(state.requests.slice(2).flatMap(request => request.ids).some(id => completedBeforeFailure.has(id))).toBe(false);
  expect(new Set(state.requests.slice(2).flatMap(request => request.ids)).size).toBe(400);
  await expect(page.getByText('没有符合当前条件的会话', { exact: true })).toBeVisible();
});

test('deletion from the final page returns to a valid page after the result shrinks', async ({ page }) => {
  const state = await deletionSetup(page, 21);
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('checkbox', { name: '选择当前筛选结果中的全部会话' }).uncheck();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await page.getByRole('checkbox', { name: '选择会话 批量会话 21', exact: true }).check();
  await page.getByRole('button', { name: '删除已选会话', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.conversation-management-table tbody tr')).toHaveCount(20);
  expect(state.requests.map(request => request.ids)).toEqual([['batch-conversation-21']]);
});
