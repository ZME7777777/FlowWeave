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
    if (path.endsWith('/agent-workspaces/workspace-default/conversation-management/workspaces')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: [] });
    if (path.endsWith('/agent-workspaces/workspace-default/conversation-management')) return respond(route, { items, truncated: false });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台' }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();

  const layout = page.locator('.conversation-management-layout');
  await expect(layout).toBeVisible();
  await expect(page.getByRole('complementary', { name: '会话工作区' })).toContainText('根工作区');
  await expect(page.getByRole('button', { name: '根工作区', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '最后活动时间', exact: true })).toContainText('全部时间');
  await expect(page.locator('.conversation-management-workspace-list small')).toHaveCount(1);
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

test('management groups Agent schedules and scenarios by their business names', async ({ page }) => {
  const now = new Date().toISOString();
  const requestedDirectories: Array<string | null> = [];
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith('/auth/me')) return respond(route, { id: 'group-manager', username: 'manager', role: 'USER', can_use_agent_sessions: true });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, { id: 'group-workspace', display_name: '我的 Agent', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/conversation-management/workspaces')) return respond(route, {
      root: { display_name: '我的 Agent', workspace_kind: 'AGENT' },
      items: [
        { id: 'schedule-directory', display_name: '香港美股全量极值任务巡检', workspace_kind: 'AGENT_SCHEDULE', state: 'ACTIVE' },
        { id: 'case-directory', display_name: '年度审计场景', workspace_kind: 'CASE', state: 'ACTIVE' },
      ],
    });
    if (path.endsWith('/conversation-management')) {
      const directory = url.searchParams.get('work_directory_id');
      requestedDirectories.push(directory);
      return respond(route, { items: directory ? [{ id: `${directory}-conversation`, display_title: `${directory} 会话`, pinned: false, unread: false, created_at: now, updated_at: now }] : [], truncated: false });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await expect(page.getByRole('button', { name: '我的 Agent', exact: true })).toContainText('Agent 会话');
  await expect(page.getByRole('button', { name: '香港美股全量极值任务巡检', exact: true })).toContainText('Agent 定时');
  await expect(page.getByRole('button', { name: '年度审计场景', exact: true })).toContainText('场景空间');
  await page.getByRole('button', { name: '香港美股全量极值任务巡检', exact: true }).click();
  await expect(page.getByText('schedule-directory 会话', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '年度审计场景', exact: true }).click();
  await expect(page.getByText('case-directory 会话', { exact: true })).toBeVisible();
  expect(requestedDirectories).toContain('schedule-directory');
  expect(requestedDirectories).toContain('case-directory');
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
    if (path.endsWith('/conversation-management/workspaces')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: [{ id: 'directory-project', display_name: '测试项目', workspace_kind: 'AGENT' }] });
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

test('compact management emphasizes records and keeps archive filters and row actions independent', async ({ page }) => {
  const now = '2026-10-28T02:00:00Z';
  const old = '2026-07-01T02:00:00Z';
  const active = Array.from({ length: 25 }, (_, index) => ({
    id: `compact-${index}`, display_title: `项目复盘 ${index + 1}：核对阶段目标与下一步工作安排`,
    pinned: false, unread: false, archived_at: null, created_at: old, updated_at: now, last_message_at: now,
  }));
  const archived = [{ ...active[0], id: 'archived-1', display_title: '已归档的项目复盘', archived_at: now, last_message_at: old },
    { ...active[1], id: 'pinned-1', display_title: '置顶复盘', archived_at: now, pinned: true, last_message_at: old },
    { ...active[2], id: 'unread-1', display_title: '未读复盘', archived_at: now, unread: true, last_message_at: old }];
  let restoreCount = 0;
  let reads = 0;
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (path.endsWith('/auth/me')) return respond(route, { id: 'compact-manager', username: 'manager', role: 'USER', can_use_agent_sessions: true });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, { id: 'compact-workspace', display_name: '工作区', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/conversation-management/workspaces')) return respond(route, { root: { id: 'root', display_name: '根工作区' }, items: Array.from({ length: 30 }, (_, index) => ({ id: `directory-${index}`, display_name: `项目工作区 ${index + 1}`, workspace_kind: 'AGENT' })) });
    if (path.endsWith('/restore') && request.method() === 'POST') { restoreCount += 1; return respond(route, { ...archived[0], archived_at: null }); }
    if (path.endsWith('/conversation-management')) { reads += 1; return respond(route, { items: url.searchParams.has('archived') ? url.searchParams.get('archived') === 'true' ? archived : active : [...active, ...archived], truncated: false }); }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台' }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  const rows = page.locator('.conversation-management-table tbody tr');
  await expect(rows).toHaveCount(20);
  await rows.first().click({ position: { x: 2, y: 2 } });
  await expect(rows.first().getByRole('checkbox')).toBeChecked();
  await expect(rows.first()).toHaveClass('selected');
  await rows.first().getByRole('checkbox').uncheck();
  await rows.first().getByRole('button', { name: active[0].display_title, exact: true }).click();
  await expect(page.locator('.agent-workbench-page')).toBeVisible();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await expect(rows.first().getByRole('checkbox')).not.toBeChecked();
  await rows.first().getByRole('checkbox').press('Space');
  await expect(rows.first().getByRole('checkbox')).toBeChecked();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.getByText('已选 1 / 26 个会话', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '上一页', exact: true }).click();
  for (const width of [1920, 1440, 1024]) {
    await page.setViewportSize({ width, height: 600 });
    await expect.poll(() => page.locator('.conversation-management-page').evaluate(element => element.getBoundingClientRect().bottom)).toBeLessThanOrEqual(601);
    const bounds = await page.locator('.conversation-management-page').evaluate(element => {
      const table = element.querySelector('.conversation-management-table-wrap')!;
      const first = element.querySelector('tbody tr')!;
      const heading = element.querySelector('.conversation-management-header')!;
      const workspace = element.querySelector('.conversation-management-workspace-list')!;
      return { start: element.getBoundingClientRect().top, table: table.getBoundingClientRect().top,
        row: first.getBoundingClientRect().height, heading: heading.getBoundingClientRect().height,
        scroll: table.scrollHeight, client: table.clientHeight, workspaceScroll: workspace.scrollHeight,
        workspaceClient: workspace.clientHeight, page: document.documentElement.scrollHeight, viewport: innerHeight };
    });
    expect(bounds.heading).toBeLessThanOrEqual(48);
    expect(bounds.table - bounds.start).toBeLessThanOrEqual(width === 1024 ? 140 : 100);
    expect(bounds.row).toBeLessThanOrEqual(46);
    expect(bounds.scroll).toBeGreaterThan(bounds.client);
    expect(bounds.workspaceScroll).toBeGreaterThan(bounds.workspaceClient);
    expect(bounds.page).toBeLessThanOrEqual(bounds.viewport);
    await expect(page.getByRole('button', { name: '下一页', exact: true })).toBeInViewport();
    const scroller = page.locator('.conversation-management-table-wrap');
    await scroller.hover(); await page.mouse.wheel(0, 400);
    await expect.poll(() => scroller.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    await expect(page.getByRole('columnheader', { name: '会话名称', exact: true })).toBeInViewport();
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(rows).toHaveCount(6);
    await expect.poll(() => scroller.evaluate(node => node.scrollTop)).toBe(0);
    await page.getByRole('button', { name: '上一页', exact: true }).click();
    await expect(rows).toHaveCount(20);
    await page.screenshot({ path: test.info().outputPath(`conversation-management-${width}.png`) });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '会话状态', exact: true }).click();
  await page.getByRole('option', { name: '已归档', exact: true }).click();
  await expect(rows).toHaveCount(1);
  await expect(page.getByText('已选 0 / 1 个会话', { exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: '包含置顶' }).check();
  await page.getByRole('checkbox', { name: '包含未读' }).check();
  await expect(rows).toHaveCount(3);
  await page.getByRole('button', { name: '最后活动时间', exact: true }).click();
  await page.getByRole('option', { name: '30 天前' }).click();
  await expect(rows).toHaveCount(3);
  await page.getByRole('button', { name: '最后活动时间', exact: true }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', { name: '最后活动时间' })).toHaveCount(0);
  await page.getByRole('button', { name: '最后活动时间', exact: true }).click();
  await page.getByRole('heading', { name: '会话管理', exact: true }).click();
  await expect(page.getByRole('listbox', { name: '最后活动时间' })).toHaveCount(0);
  await page.getByRole('textbox', { name: '搜索会话', exact: true }).fill('已归档的');
  await expect(rows).toHaveCount(1);
  await page.getByRole('button', { name: '恢复会话 已归档的项目复盘', exact: true }).click();
  await expect.poll(() => restoreCount).toBe(1);
  await expect(rows.first().getByRole('checkbox')).not.toBeChecked();
  await page.getByRole('button', { name: '重置', exact: true }).click();
  await expect(page.getByRole('button', { name: '会话状态', exact: true })).toContainText('全部');
  await expect(rows).toHaveCount(20);
  const before = reads;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect.poll(() => reads).toBeGreaterThan(before);
});

test('archived directory restores require confirmation and restoring one child refreshes its parent', async ({ page }) => {
  const now = '2026-10-29T02:00:00Z';
  let directoryArchived = true;
  let directoryRestores = 0;
  let conversationRestores = 0;
  let failRestore = false;
  const archivedIds = new Set(['child-1', 'child-2']);
  const items = ['child-1', 'child-2'].map((id, index) => ({ id, display_title: `归档会话 ${index + 1}`, pinned: false, unread: false, archived_at: now, created_at: now, updated_at: now, last_message_at: now }));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (path.endsWith('/auth/me')) return respond(route, { id: 'archive-manager', username: 'manager', role: 'USER', can_use_agent_sessions: true });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, { id: 'archive-workspace', display_name: '工作区', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/conversation-management/workspaces')) return respond(route, { root: { display_name: '根工作区' }, items: [{ id: 'archived-directory', display_name: '归档项目', workspace_kind: 'AGENT', state: directoryArchived ? 'ARCHIVED' : 'ACTIVE', current_version: { id: 'archive-version', working_directory: '/runtime/workspace/project/archive-project' } }] });
    if (path.endsWith('/work-directories/archived-directory/restore')) {
      directoryRestores += 1;
      if (failRestore) return respond(route, { error: { code: 'RESTORE_FAILED', message: '暂时无法恢复工作区' } }, 503);
      directoryArchived = false; archivedIds.clear();
      return respond(route, { id: 'archived-directory', display_name: '归档项目', workspace_kind: 'AGENT', state: 'ACTIVE' });
    }
    if (path.endsWith('/conversations/child-1/restore')) {
      conversationRestores += 1; directoryArchived = false; archivedIds.delete('child-1');
      return respond(route, { ...items[0], archived_at: null });
    }
    if (path.endsWith('/conversation-management')) return respond(route, { items: url.searchParams.get('work_directory_id') === 'archived-directory' ? items.filter(item => archivedIds.has(item.id) === (url.searchParams.get('archived') === 'true')).map(item => ({ ...item, archived_at: archivedIds.has(item.id) ? now : null })) : [], truncated: false });
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await page.getByRole('button', { name: '会话状态', exact: true }).click();
  await page.getByRole('option', { name: '已归档', exact: true }).click();
  await page.getByRole('button', { name: '归档项目（已归档）', exact: true }).click();
  const rows = page.locator('.conversation-management-table tbody tr');
  await expect(rows).toHaveCount(2);
  await expect(rows.locator('.conversation-management-status').getByText('已归档', { exact: true })).toHaveCount(2);
  expect(await page.locator('.conversation-management-page select').count()).toBe(0);
  await page.getByRole('button', { name: '恢复工作区 归档项目', exact: true }).click();
  let dialog = page.getByRole('alertdialog', { name: '恢复工作区“归档项目”？' });
  await expect(dialog).toContainText('所有已归档会话');
  expect(directoryRestores).toBe(0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(directoryRestores).toBe(0);
  await page.getByRole('button', { name: '恢复会话 归档会话 1', exact: true }).click();
  await expect.poll(() => conversationRestores).toBe(1);
  await expect(page.getByRole('button', { name: '归档项目', exact: true })).toBeVisible();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('归档会话 2');
  // A later batch archive restores the directory action; cancellation and
  // request failure must never hide the archived container or its records.
  directoryArchived = true; archivedIds.add('child-1');
  await page.reload();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await page.getByRole('button', { name: '会话状态', exact: true }).click();
  await page.getByRole('option', { name: '已归档', exact: true }).click();
  await page.getByRole('button', { name: '归档项目（已归档）', exact: true }).click();
  failRestore = true;
  await page.getByRole('button', { name: '恢复工作区 归档项目', exact: true }).click();
  dialog = page.getByRole('alertdialog', { name: '恢复工作区“归档项目”？' });
  await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法恢复工作区');
  await expect(page.getByRole('button', { name: '归档项目（已归档）', exact: true })).toBeVisible();
  failRestore = false;
  await page.getByRole('button', { name: '恢复工作区 归档项目', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认恢复', exact: true }).click();
  await expect.poll(() => directoryRestores).toBe(2);
  await expect(rows).toHaveCount(0);
  const filter = page.getByRole('button', { name: '会话状态', exact: true });
  await filter.press('ArrowDown');
  await page.keyboard.press('Home'); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(filter).toContainText('未归档');
  await expect(rows).toHaveCount(2);
  await filter.click(); await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', { name: '会话状态', exact: true })).toHaveCount(0);
  await expect(filter).toBeFocused();
  await page.screenshot({ path: test.info().outputPath('restored-directory.png') });
});

test('archive filter defaults to all, filters both states, and resets across workspaces', async ({ page }) => {
  const now = new Date().toISOString();
  const queries: Array<string | null> = [];
  const items = [
    { id: 'active', display_title: '正常会话', archived_at: null },
    { id: 'archived', display_title: '归档会话', archived_at: now },
  ].map(item => ({ ...item, pinned: false, unread: false, created_at: now, updated_at: now, last_message_at: now }));
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()); const path = url.pathname;
    if (path.endsWith('/auth/me')) return respond(route, { id: 'filter-manager', username: 'manager', role: 'USER', can_use_agent_sessions: true });
    if (path.endsWith('/agent-workspaces/default')) return respond(route, { id: 'filter-workspace', display_name: '工作区', desired_state: 'RUNNING', updated_at: now });
    if (path.endsWith('/conversation-management/workspaces')) return respond(route, { root: { display_name: '根工作区' }, items: [
      { id: 'active-directory', display_name: '正常项目', workspace_kind: 'AGENT', state: 'ACTIVE' },
      { id: 'archived-directory', display_name: '归档项目', workspace_kind: 'AGENT', state: 'ARCHIVED' },
    ] });
    if (path.endsWith('/conversation-management')) {
      const archived = url.searchParams.get('archived'); queries.push(archived);
      const scoped = url.searchParams.get('work_directory_id') === 'archived-directory' ? items.slice(1) : items;
      return respond(route, { items: scoped.filter(item => archived === null || Boolean(item.archived_at) === (archived === 'true')), truncated: false });
    }
    if (path.endsWith('/node-directories') || path.endsWith('/node-assets')) return respond(route, []);
    return respond(route, { error: { code: 'RESOURCE_NOT_FOUND', message: 'not found' } }, 404);
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'AI 工作台', exact: true }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  const filter = page.getByRole('button', { name: '会话状态', exact: true });
  const rows = page.locator('.conversation-management-table tbody tr');
  await expect(filter).toContainText('全部');
  await expect(rows).toHaveCount(2);
  expect(queries.at(-1)).toBeNull();
  await page.getByRole('button', { name: '正常项目', exact: true }).click();
  await expect(filter).toContainText('全部');
  await expect(rows).toHaveCount(2);
  await filter.click();
  await page.getByRole('option', { name: '未归档', exact: true }).click();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('正常会话');
  expect(queries.at(-1)).toBe('false');
  await filter.click();
  await page.getByRole('option', { name: '已归档', exact: true }).click();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText('归档会话');
  expect(queries.at(-1)).toBe('true');
  await page.getByRole('button', { name: '重置', exact: true }).click();
  await expect(filter).toContainText('全部');
  await expect(rows).toHaveCount(2);
  await page.getByRole('button', { name: '归档项目（已归档）', exact: true }).click();
  await expect(filter).toContainText('全部');
  await expect(rows).toHaveCount(1);
  expect(queries.at(-1)).toBeNull();
  await filter.click();
  await page.getByRole('option', { name: '已归档', exact: true }).click();
  await page.getByRole('button', { name: '根工作区', exact: true }).click();
  await expect(filter).toContainText('已归档');
  await expect(rows).toHaveCount(1);
  await filter.press('ArrowDown');
  await page.keyboard.press('Home'); await page.keyboard.press('Enter');
  await expect(filter).toContainText('全部');
  await expect(rows).toHaveCount(2);
});
