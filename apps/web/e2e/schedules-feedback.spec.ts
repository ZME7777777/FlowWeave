import { expect, test } from '@playwright/test';

const authenticatedUser = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'flowweave',
  role: 'SUPER_ADMIN',
  is_super_admin: true,
};

test('new schedule explains how to create a usable template when none exist', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' || path === '/api/v1/flows' || path === '/api/v1/flow-run-schedule-templates') return respond([]);
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时调度', exact: true }).click();

  const createButton = page.getByRole('button', { name: '新建定时任务', exact: true });
  await expect(createButton).toBeEnabled();
  await createButton.click();
  const notice = page.getByRole('alertdialog');
  await expect(notice).toContainText('当前没有可用的连续运行母版');
  await expect(notice).toContainText('显示“草稿已就绪”后即可新建定时任务');
  await notice.getByRole('button', { name: '我知道了', exact: true }).click();
  await expect(notice).toBeHidden();
});

test('new schedule filters FlowRuns before choosing a continuous record', async ({ page }) => {
  const templates = [
    { id: 'record-1', name: '早盘巡检', flow_definition_id: 'flow-1', flow_name: '行情巡检', parent_flow_run_id: 'run-1', parent_flow_run_name: '沪深主流程', parent_flow_run_no: 8, state: 'DRAFT', run_no: 2 },
    { id: 'record-2', name: '收盘巡检', flow_definition_id: 'flow-2', flow_name: '日报生成', parent_flow_run_id: 'run-2', parent_flow_run_name: '港股流程', parent_flow_run_no: 9, state: 'DRAFT', run_no: 3 },
  ];
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' || path === '/api/v1/flows') return respond([]);
    if (path === '/api/v1/flow-run-schedule-templates') return respond(templates);
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  await page.getByRole('button', { name: '新建定时任务', exact: true }).click();
  await expect(page.getByLabel('选择连续运行记录')).toBeDisabled();
  await page.getByLabel('选择 FlowRun', { exact: true }).click();
  await page.getByLabel('筛选选择 FlowRun').fill('港股');
  await expect(page.getByRole('option', { name: /港股流程/ })).toBeVisible();
  await expect(page.getByRole('option', { name: /沪深主流程/ })).toBeHidden();
  await page.getByRole('option', { name: /港股流程/ }).click();
  await page.getByLabel('选择连续运行记录', { exact: true }).click();
  await expect(page.getByRole('option', { name: /收盘巡检/ })).toBeVisible();
  await expect(page.getByRole('option', { name: /早盘巡检/ })).toHaveCount(0);
});

test('schedule deletion uses a destructive icon and confirms cascading record cleanup', async ({ page }) => {
  const schedule = {
    id: 'schedule-1', flow_definition_id: 'flow-1', environment_version_id: 'env-version-1',
    name: '每小时检查', source_flow_run_id: 'automatic-master-1', run_mode: 'AUTOMATIC', start_node_key: 'first', interval_minutes: 1, cron_expression: '0 * * * *',
    source_flow_run: { id: 'automatic-master-1', name: '连续运行母版', run_no: 2, state: 'DRAFT' }, status: 'PAUSED', next_run_at: null, row_version: 1, config_version: 1, last_run_at: null, has_execution: true,
    created_at: '2026-09-07T09:00:00Z', updated_at: '2026-09-07T09:00:00Z',
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' && request.method() === 'GET') return respond([schedule]);
    if (path === '/api/v1/flow-run-schedule-templates' || path === '/api/v1/flows' || path.endsWith('/occurrences')) return respond([]);
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  const remove = page.getByRole('button', { name: '删除定时任务', exact: true });
  await expect(remove.locator('svg').first()).toBeVisible();
  await expect(remove.locator('svg.lucide-x')).toHaveCount(0);
  await remove.click();
  await expect(page.getByRole('alertdialog')).toContainText('全部连续运行记录、节点 Attempt、运行会话和关联运行资源');
});

test('schedule rows expand across their full width and immediate trigger reports progress', async ({ page }) => {
  let triggerAttempts = 0;
  const schedule = {
    id: 'schedule-1', flow_definition_id: 'flow-1', environment_version_id: 'env-version-1',
    name: '每小时检查', source_flow_run_id: 'automatic-master-1', run_mode: 'AUTOMATIC',
    start_node_key: 'first', interval_minutes: 1, cron_expression: '0 * * * *',
    source_flow_run: { id: 'automatic-master-1', name: '连续运行母版', run_no: 2, state: 'DRAFT' },
    status: 'ACTIVE', next_run_at: '2026-09-07T10:00:00Z', row_version: 1,
    config_version: 1, last_run_at: null, has_execution: false,
    created_at: '2026-09-07T09:00:00Z', updated_at: '2026-09-07T09:00:00Z',
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return respond(authenticatedUser);
    if (path === '/api/v1/flow-run-schedules' && request.method() === 'GET') return respond([schedule]);
    if (path === '/api/v1/flows') return respond([{ id: 'flow-1', name: '行情巡检流程' }]);
    if (path === '/api/v1/flow-run-schedule-templates') return respond([]);
    if (path === `/api/v1/flow-run-schedules/${schedule.id}/trigger`) {
      triggerAttempts += 1;
      if (triggerAttempts === 2) {
        return respond({ error: { code: 'SCHEDULE_TRIGGER_FAILED', message: '定时任务启动失败' } }, 500);
      }
      await new Promise(resolve => setTimeout(resolve, 120));
      return respond(schedule, 202);
    }
    return respond([]);
  });

  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') pageErrors.push(message.text()); });
  await page.goto('/');
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  const workspace = page.locator('.schedule-workspace');
  const recovery = page.getByRole('alert');
  await Promise.race([
    workspace.waitFor({ state: 'visible', timeout: 5_000 }),
    recovery.waitFor({ state: 'visible', timeout: 5_000 }),
  ]).catch(() => undefined);
  if (await recovery.isVisible()) throw new Error(`Schedule page render failed: ${pageErrors.join(' | ')}`);
  await expect(workspace).toBeVisible();
  const [listBox, workspaceBox] = await Promise.all([
    page.locator('.schedule-list-panel').boundingBox(),
    workspace.boundingBox(),
  ]);
  expect(listBox?.width).toBeGreaterThan(200);
  expect(workspaceBox?.width).toBeGreaterThan((page.viewportSize()?.width ?? 0) * .6);
  await expect(page.locator('.schedule-task-list button.active')).toContainText('每小时检查');
  await expect(page.getByRole('heading', { name: '每小时检查', exact: true })).toBeVisible();
  await expect(page.getByText('执行记录', { exact: true })).toBeVisible();
  const trigger = page.getByRole('button', { name: '立即运行', exact: true });
  await trigger.click();
  await expect(page.getByRole('button', { name: '启动中…', exact: true })).toBeDisabled();
  await expect(page.getByRole('status')).toHaveText('已创建连续运行记录，后台正在启动。');
  await trigger.click();
  await expect(page.getByRole('alert')).toHaveText('定时任务启动失败');
});


test('schedule history is paginated and only shows node states for an expanded record', async ({ page }) => {
  const schedule = {
    id: 'schedule-1', flow_definition_id: 'flow-1', environment_version_id: 'env-version-1', name: '每小时检查',
    source_flow_run_id: 'automatic-master-1', run_mode: 'AUTOMATIC', start_node_key: 'first', interval_minutes: 1, cron_expression: '0 * * * *',
    source_flow_run: { id: 'automatic-master-1', name: '连续运行母版', run_no: 2, state: 'DRAFT' }, status: 'ACTIVE',
    next_run_at: '2026-09-07T10:00:00Z', row_version: 1, config_version: 1, last_run_at: '2026-09-07T09:00:00Z', has_execution: true,
    created_at: '2026-09-07T09:00:00Z', updated_at: '2026-09-07T09:00:00Z',
  };
  const occurrence = (index: number) => ({
    id: `occurrence-${index}`, scheduled_for: `2026-09-${String(20 - index).padStart(2, '0')}T09:00:00Z`, trigger_kind: index % 2 ? 'SCHEDULED' : 'MANUAL', config_version: 1, state: 'STARTED', error_detail: null,
    flow_run: { id: `run-${index}`, name: `连续运行 ${index}`, run_no: index, parent_flow_run_id: 'parent-run', node_runs: [{ id: `node-${index}`, name: 'collect', flow_node_snapshot_key: 'collect', state: 'ACCEPTED', attempts: [{ id: `attempt-${index}` }] }] },
  });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname.endsWith('/auth/me')) return respond(authenticatedUser);
    if (url.pathname === '/api/v1/flow-run-schedules' && request.method() === 'GET') return respond([schedule]);
    if (url.pathname === '/api/v1/flow-run-schedule-templates' || url.pathname === '/api/v1/flows') return respond([]);
    if (url.pathname.endsWith('/occurrences')) {
      const currentPage = Number(url.searchParams.get('page') ?? '1');
      return respond({ items: currentPage === 1 ? Array.from({ length: 10 }, (_, index) => occurrence(index + 1)) : [occurrence(11)], total: 11, page: currentPage, page_size: 10 });
    }
    return respond([]);
  });

  await page.goto('/');
  await page.getByRole('button', { name: '定时调度', exact: true }).click();
  const record = page.locator('.schedule-record').first();
  await expect(page.getByText('collect', { exact: true })).toHaveCount(0);
  await record.locator('.schedule-record-summary').click();
  await expect(record.getByText('collect', { exact: true })).toBeVisible();
  await record.locator('.schedule-record-summary').click();
  await expect(record.getByText('collect', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('.schedule-record-run')).toContainText('连续运行 11');
  await expect(page.getByText('第 2 / 2 页 · 共 11 条', { exact: true })).toBeVisible();
});
